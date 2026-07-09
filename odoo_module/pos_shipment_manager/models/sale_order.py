# -*- coding: utf-8 -*-
import urllib.parse
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    state = fields.Selection([
        ('draft', "Orden"),
        ('sent', "Orden enviada"),
        ('sale', "Orden de venta"),
        ('done', "Bloqueado"),
        ('cancel', "Cancelado"),
    ], string="Estado", readonly=True, copy=False, index=True, tracking=3, default='draft')

    shipment_mode = fields.Selection([
        ('none', 'Sin Envío'),
        ('paid', 'Pago al Instante'),
        ('cod', 'Contra Entrega'),
    ], string="Modo de Envío", default='none')
    x_customer_phone = fields.Char(string="Teléfono / WhatsApp", tracking=True)
    x_customer_name = fields.Char(string="Nombre del Cliente", tracking=True)

    @api.onchange('partner_id')
    def _onchange_partner_id_custom(self):
        """Cuando se selecciona un partner del many2one, sincronizar los campos visibles."""
        if self.partner_id:
            self.x_customer_name = self.partner_id.name
            self.x_customer_phone = self.partner_id.phone or self.partner_id.mobile or ''

    @api.onchange('x_customer_phone')
    def _onchange_x_customer_phone(self):
        """Teléfono = llave primaria. Al escribir un número, buscar partner existente."""
        phone = (self.x_customer_phone or '').strip()
        if not phone or len(phone) < 7:
            return
        # Normalizar: quitar espacios y guiones para comparación
        digits = ''.join(filter(str.isdigit, phone))
        if len(digits) < 7:
            return
        partner = self.env['res.partner'].search([
            '|', ('phone', 'ilike', digits[-10:]), ('mobile', 'ilike', digits[-10:])
        ], limit=1)
        if partner:
            self.partner_id = partner
            self.x_customer_name = partner.name

    def _resolve_partner_from_phone(self, phone, name=None):
        """Busca o crea un partner por teléfono. Teléfono es la llave primaria."""
        if not phone:
            return False
        digits = ''.join(filter(str.isdigit, phone))
        if len(digits) < 7:
            return False
        partner = self.env['res.partner'].search([
            '|', ('phone', 'ilike', digits[-10:]), ('mobile', 'ilike', digits[-10:])
        ], limit=1)
        if not partner and name:
            partner = self.env['res.partner'].create({
                'name': name,
                'phone': phone,
                'mobile': phone,
            })
        return partner

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('partner_id'):
                partner = self.env['res.partner'].browse(vals['partner_id'])
                if partner.exists():
                    if not vals.get('x_customer_name'):
                        vals['x_customer_name'] = partner.name
                    if not vals.get('x_customer_phone'):
                        vals['x_customer_phone'] = partner.phone or partner.mobile or ''
            elif vals.get('x_customer_phone'):
                phone = vals['x_customer_phone']
                name = vals.get('x_customer_name') or 'Cliente de Envío'
                partner = self._resolve_partner_from_phone(phone, name)
                if partner:
                    vals['partner_id'] = partner.id
                    vals['partner_invoice_id'] = partner.id
                    vals['partner_shipping_id'] = partner.id
                    vals['x_customer_name'] = partner.name
        return super(SaleOrder, self).create(vals_list)

    shipping_fee = fields.Monetary(string="Cargo por Envío", tracking=True)
    distance_km = fields.Float(string='Distancia (KM)', digits=(16, 2), tracking=True)
    manual_location_link = fields.Char(string="Link Ubicación", tracking=True)
    
    @api.onchange('distance_km')
    def _onchange_distance_km(self):
        if self.distance_km > 0:
            d = self.distance_km
            raw_fee = 0.0
            
            if d <= 5:
                raw_fee = 150.0
            elif d <= 10:
                # De 5 a 10km: Base 150 + 30 por cada km extra
                raw_fee = 150.0 + ((d - 5) * 30.0)
            elif d <= 20:
                # De 10 a 20km: Base 300 + 15 por cada km extra
                raw_fee = 300.0 + ((d - 10) * 15.0)
            else:
                # Más de 20km: Base 500 + 15 por cada km extra
                raw_fee = 500.0 + ((d - 20) * 15.0)
            
            # Redondear a la decena más cercana (unidades de 10)
            self.shipping_fee = round(raw_fee / 10.0) * 10.0

    messenger_id = fields.Many2one('res.partner', string="Mensajero", domain="[('is_messenger', '=', True)]", tracking=True)
    
    shipment_id = fields.Many2one('pos.shipment', compute='_compute_shipment_id', string='Envío')
    messenger_whatsapp = fields.Char(related='messenger_id.messenger_whatsapp', string='WhatsApp del Mensajero')
    
    messenger_portal_url = fields.Char(compute='_compute_portal_urls_from_so', string='Link Mensajero')
    customer_portal_url = fields.Char(compute='_compute_portal_urls_from_so', string='Link Cliente')

    def _compute_portal_urls_from_so(self):
        for order in self:
            order.messenger_portal_url = order.shipment_id.messenger_portal_url
            order.customer_portal_url = order.shipment_id.customer_portal_url

    payment_status_label = fields.Char(compute='_compute_payment_status_label', string='Estado de Pago')

    @api.depends('order_line.price_total', 'shipping_fee')
    def _compute_amounts(self):
        super()._compute_amounts()
        for order in self:
            # En Odoo 18, ya no modificamos amount_total directamente si usamos líneas
            pass

    def _get_or_create_delivery_product(self):
        product_id = self.env['ir.config_parameter'].sudo().get_param('pos_shipment.product_id')
        product = self.env['product.product'].browse(int(product_id)) if product_id else False
        if not product or not product.exists():
            product = self.env['product.product'].search([('name', '=ilike', 'Envío')], limit=1)
        if not product:
            product = self.env['product.product'].search([('name', 'ilike', 'Envío')], limit=1)
        if not product:
            product = self.env['product.product'].create({
                'name': 'Envío',
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
            })
            self.env['ir.config_parameter'].sudo().set_param('pos_shipment.product_id', str(product.id))
        return product

    @api.onchange('shipping_fee', 'shipment_mode')
    def _onchange_shipping_fee(self):
        if self.shipment_mode == 'none' or self.shipping_fee <= 0:
            shipping_lines = self.order_line.filtered(lambda l: l.is_delivery_line)
            if shipping_lines:
                self.order_line = self.order_line - shipping_lines
            return

        shipping_line = self.order_line.filtered(lambda l: l.is_delivery_line)
        product = self._get_or_create_delivery_product()

        if shipping_line:
            shipping_line[0].price_unit = self.shipping_fee
        else:
            # En onchange usamos new para que se vea en la UI antes de guardar
            self.order_line += self.env['sale.order.line'].new({
                'order_id': self.id,
                'product_id': product.id,
                'name': _('Cargo por Envío'),
                'product_uom_qty': 1.0,
                'price_unit': self.shipping_fee,
                'is_delivery_line': True,
                'sequence': 999,
            })

    def write(self, vals):
        # 1. Si se establece un partner_id explícito, sincronizar campos visibles
        if vals.get('partner_id'):
            partner = self.env['res.partner'].browse(vals['partner_id'])
            if partner.exists():
                vals['x_customer_name'] = partner.name
                vals['x_customer_phone'] = partner.phone or partner.mobile or ''

        # 2. Si se cambia el teléfono (sin partner_id en vals), resolver partner
        elif 'x_customer_phone' in vals and 'partner_id' not in vals:
            for order in self:
                phone = vals.get('x_customer_phone') or ''
                name = vals.get('x_customer_name', order.x_customer_name) or 'Cliente de Envío'
                partner = order._resolve_partner_from_phone(phone, name)
                if partner:
                    super(SaleOrder, order).write(dict(
                        vals,
                        partner_id=partner.id,
                        partner_invoice_id=partner.id,
                        partner_shipping_id=partner.id,
                        x_customer_name=partner.name,
                    ))
            vals = {k: v for k, v in vals.items() if k not in [
                'x_customer_name', 'x_customer_phone', 'partner_id',
                'partner_invoice_id', 'partner_shipping_id'
            ]}

        # 3. Si se limpia el partner_id, limpiar los campos
        elif 'partner_id' in vals and not vals.get('partner_id'):
            vals['x_customer_name'] = ''
            vals['x_customer_phone'] = ''

        # Asegurar sincronización al guardar si cambió el monto o modo
        res = super(SaleOrder, self).write(vals) if vals else True
        if any(f in vals for f in ['shipping_fee', 'shipment_mode', 'messenger_id', 'manual_location_link']):
            for order in self:
                order.with_context(tracking_disable=True)._sync_shipping_line()
                # Sincronizar también con el envío si existe
                if order.shipment_id:
                    order.shipment_id.sudo().write({
                        'messenger_id': order.messenger_id.id if 'messenger_id' in vals else order.shipment_id.messenger_id.id,
                        'shipping_charge': order.shipping_fee if 'shipping_fee' in vals else order.shipment_id.shipping_charge,
                        'shipping_cost': order.shipping_fee if 'shipping_fee' in vals else order.shipment_id.shipping_cost,
                        'manual_location_link': order.manual_location_link if 'manual_location_link' in vals else order.shipment_id.manual_location_link,
                        'shipment_mode': order.shipment_mode if 'shipment_mode' in vals else order.shipment_id.shipment_mode,
                    })
        return res


    def _sync_shipping_line(self):
        """Sincronización persistente de la línea de envío."""
        self.ensure_one()
        shipping_line = self.order_line.filtered(lambda l: l.is_delivery_line)
        
        if self.shipment_mode == 'none' or self.shipping_fee <= 0:
            if shipping_line:
                shipping_line.unlink()
            return

        product = self._get_or_create_delivery_product()

        if shipping_line:
            shipping_line.write({'price_unit': self.shipping_fee})
        else:
            self.env['sale.order.line'].create({
                'order_id': self.id,
                'product_id': product.id if product else False,
                'name': _('Cargo por Envío'),
                'product_uom_qty': 1.0,
                'price_unit': self.shipping_fee,
                'is_delivery_line': True,
                'sequence': 999,
            })

    def _compute_payment_status_label(self):
        for order in self:
            pos_orders = self.env['pos.order'].search([('sale_order_id', '=', order.id)])
            if not pos_orders:
                order.payment_status_label = 'Pendiente de Facturar'
                continue
            
            # Si todas las órdenes de POS están pagadas
            if all(po.state in ['paid', 'done', 'invoiced'] and po.amount_total > 0 for po in pos_orders):
                order.payment_status_label = 'Facturado y Pagado'
            else:
                order.payment_status_label = 'Facturado y Pendiente de Pago'

    def _compute_shipment_id(self):
        for order in self:
            # Buscar pedidos de POS vinculados a esta cotización
            pos_orders = self.env['pos.order'].search([('sale_order_id', '=', order.id)])
            shipments = pos_orders.mapped('shipment_id')
            # También buscar envíos creados directamente desde la SO
            so_shipments = self.env['pos.shipment'].search([('sale_order_id', '=', order.id)])
            order.shipment_id = (shipments | so_shipments)[:1]

    def action_view_shipment(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'pos.shipment',
            'res_id': self.shipment_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_share_whatsapp_messenger_from_so(self):
        self.ensure_one()
        if not self.messenger_id:
            raise UserError(_("Seleccione un mensajero primero."))
            
        shipment = self.shipment_id
        if not shipment:
            # Crear el envío en estado borrador vinculado a esta SO
            shipment = self.env['pos.shipment'].create({
                'sale_order_id': self.id,
                'messenger_id': self.messenger_id.id,
                'shipping_charge': self.shipping_fee,
                'shipping_cost': self.shipping_fee,
                'state': 'draft',
                'company_id': self.company_id.id,
            })
            # Limpiar caché para futuras lecturas
            self.invalidate_recordset(['shipment_id'])
        # Asegurar que el envío refleje el estado actual de la SO
        shipment.write({
            'messenger_id': self.messenger_id.id,
            'shipping_charge': self.shipping_fee,
            'shipping_cost': self.shipping_fee,
            'shipment_mode': self.shipment_mode,
        })
        return shipment.action_open_share_wizard()

    def action_share_whatsapp_customer_from_so(self):
        self.ensure_one()
        shipment = self.shipment_id
        if not shipment:
            # Crear el envío en estado borrador para tener el token del cliente
            shipment = self.env['pos.shipment'].create({
                'sale_order_id': self.id,
                'messenger_id': self.messenger_id.id if self.messenger_id else False,
                'shipping_charge': self.shipping_fee,
                'shipping_cost': self.shipping_fee,
                'state': 'draft',
                'company_id': self.company_id.id,
            })
            self.invalidate_recordset(['shipment_id'])
        # Asegurar sincronización
        shipment.write({
            'shipping_charge': self.shipping_fee,
            'shipping_cost': self.shipping_fee,
            'shipment_mode': self.shipment_mode,
        })
        return shipment.action_open_share_wizard()

class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    is_delivery_line = fields.Boolean(string='Es línea de envío', default=False)
