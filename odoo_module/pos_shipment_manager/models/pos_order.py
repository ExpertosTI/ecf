# -*- coding: utf-8 -*-
from datetime import timedelta
from odoo import api, fields, models, _


class PosOrder(models.Model):
    _inherit = 'pos.order'

    shipment_id = fields.Many2one(
        'pos.shipment', string='Envío Relacionado', readonly=True, copy=False
    )
    sale_order_id = fields.Many2one(
        'sale.order', string="Cotización de Origen",
        compute="_compute_sale_order_id", store=True
    )
    shipment_mode = fields.Selection([
        ('none', 'Sin Envío'),
        ('paid', 'Pago al Instante'),
        ('cod', 'Contra Entrega'),
    ], string="Modo de Envío", default='none')
    messenger_id = fields.Many2one('res.users', string='Mensajero Seleccionado')
    manual_location_link = fields.Char(string='Link de Ubicación (Manual)')
    nav_google_url = fields.Char(related='shipment_id.nav_google_url', string='Google Maps (Envío)')
    nav_waze_url = fields.Char(related='shipment_id.nav_waze_url', string='Waze (Envío)')

    @api.depends('lines.sale_order_origin_id')
    def _compute_sale_order_id(self):
        for order in self:
            # En Odoo 18 el vínculo está en las líneas (pos.order.line -> sale_order_origin_id)
            if hasattr(order.lines, 'sale_order_origin_id'):
                order.sale_order_id = order.lines.mapped('sale_order_origin_id')[:1]
            else:
                order.sale_order_id = False

    @api.model
    def _order_fields(self, ui_order):
        fields = super()._order_fields(ui_order)
        if 'shipment_mode' in ui_order:
            fields['shipment_mode'] = ui_order['shipment_mode']
        if 'messenger_id' in ui_order:
            fields['messenger_id'] = ui_order['messenger_id']
        if 'manual_location_link' in ui_order:
            fields['manual_location_link'] = ui_order['manual_location_link']
        return fields

    @api.model
    def get_details(self, filters=None):
        if filters and filters.get('is_shipment'):
            # Si el filtro de envíos está activo, inyectamos el dominio
            self = self.search([('shipment_id', '!=', False)])
            
        res = super(PosOrder, self).get_details(filters=filters)
        
        # Dashboard Shipment Metrics
        shipments = self.env['pos.shipment'].search([
            ('state', 'in', ['street', 'draft']),
            ('create_date', '>=', fields.Datetime.now() - timedelta(days=7)),
            ('company_id', 'in', self.env.companies.ids)
        ])
        
        pending_count = len(shipments.filtered(lambda s: s.state == 'draft'))
        street_count = len(shipments.filtered(lambda s: s.state == 'street'))
        
        # Cash in transit (Contra Entrega only)
        cash_transit = sum(shipments.filtered(lambda s: s.state == 'street').mapped('shipping_charge'))
        
        currency = self.env.company.currency_id
        def fmt(amt):
            return f"{currency.name} {amt:,.2f}"

        res.update({
            'shipment_pending_count': pending_count,
            'shipment_street_count': street_count,
            'shipment_cash_transit': fmt(cash_transit),
        })
        return res

    @api.model
    def get_detailed_sales(self, filters=None, search_term=None, category_ids=None):
        if filters and filters.get('is_shipment'):
             # Forzar que solo traiga órdenes con envío
             orders = self.search([('shipment_id', '!=', False)])
        
        res = super(PosOrder, self).get_detailed_sales(filters=filters, search_term=search_term, category_ids=category_ids)
        
        # Añadir información de envío a cada producto en el resultado
        if res and res.get('products'):
            for p in res['products']:
                order = self.browse(p.get('order_id'))
                if order and order.shipment_id:
                    p['messenger'] = order.shipment_id.messenger_id.name
                    p['delivery_status'] = order.shipment_id.state
                    p['shipment_mode'] = order.sale_order_id.shipment_mode
        return res

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        shipments_to_create = []
        
        for order in orders:
            # Sincronizar desde cotización si aplica
            if order.sale_order_id and order.sale_order_id.shipment_mode != 'none' and order.shipment_mode == 'none':
                order.shipment_mode = order.sale_order_id.shipment_mode

            # Crear el envío si el modo lo amerita (venga o no de cotización)
            if order.shipment_mode in ['paid', 'cod']:
                messenger = order.messenger_id.id or (order.sale_order_id.messenger_id.id if order.sale_order_id else False)
                
                # Calcular tarifa de envío: desde cotización o leyendo las líneas del POS
                if order.sale_order_id:
                    shipping_fee = order.sale_order_id.shipping_fee
                else:
                    shipping_lines = order.lines.filtered(lambda l: 'envio' in (l.product_id.name or '').lower() or 'envío' in (l.product_id.name or '').lower())
                    shipping_fee = sum(shipping_lines.mapped('price_subtotal_incl')) if shipping_lines else 0.0
                    
                location_link = order.manual_location_link or (order.sale_order_id.manual_location_link if order.sale_order_id else False)
                
                shipments_to_create.append({
                    'order_id': order.id,
                    'messenger_id': messenger,
                    'date_invoiced': order.create_date or fields.Datetime.now(),
                    'state': 'street',  # Pasa directo a "En la Calle"
                    'shipping_charge': shipping_fee,
                    'company_id': order.company_id.id,
                    'manual_location_link': location_link,
                    'shipment_mode': order.shipment_mode,
                })
        
        if shipments_to_create:
            shipments = self.env['pos.shipment'].sudo().create(shipments_to_create)
            # Vincular órdenes con sus envíos (en lote si es posible)
            for shipment in shipments:
                shipment.order_id.sudo().shipment_id = shipment.id
                shipment.order_id._apply_shipment_costs()
        
        return orders

    def _apply_shipment_costs(self):
        """Aplica la lógica de costos tipo mobile_service de forma eficiente."""
        for order in self:
            if not order.shipment_id:
                continue
            
            # Buscamos todas las líneas de envío de una vez
            shipping_lines = order.lines.filtered(lambda l: 'envio' in (l.product_id.name or '').lower())
            for line in shipping_lines:
                # El usuario pide que NO se vea como ganancia.
                # El costo de la línea debe ser igual al precio cobrado.
                line.sudo().write({'line_cost': line.price_unit})

    def action_view_shipment(self):
        self.ensure_one()
        return {
            'name': _('Envío Relacionado'),
            'type': 'ir.actions.act_window',
            'res_model': 'pos.shipment',
            'res_id': self.shipment_id.id,
            'view_mode': 'form',
            'target': 'current',
        }


class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def _loader_params_res_users(self):
        params = super()._loader_params_res_users()
        params['search_params']['fields'] += ['is_messenger']
        return params

    def _pos_data_process(self, loaded_data):
        super()._pos_data_process(loaded_data)
        # Forzar la carga de mensajeros en una clave dedicada para máxima fiabilidad
        loaded_data['pos_messengers'] = self.env['res.users'].search_read(
            [('is_messenger', '=', True)], 
            ['id', 'name', 'is_messenger']
        )

class PosOrderLine(models.Model):
    _inherit = 'pos.order.line'

    line_cost = fields.Float(
        string='Costo Línea',
        help="Costo interno (ej. lo que se le paga al mensajero o costo de pieza)."
    )
