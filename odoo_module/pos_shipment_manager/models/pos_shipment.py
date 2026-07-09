# -*- coding: utf-8 -*-
import uuid
import logging
import urllib.parse
import math
import calendar
from datetime import timedelta, datetime
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
import re
import unicodedata

def slugify(value):
    """Convierte un string en un slug amigable para URLs."""
    if not value: return "cliente"
    value = str(value)
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value).strip().lower()
    return re.sub(r'[-\s]+', '-', value)


class PosShipment(models.Model):
    _name = 'pos.shipment'
    _description = 'Envío de Pedido POS'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char(
        string='Referencia', required=True, copy=False, readonly=True,
        index=True, default=lambda self: _('Nuevo')
    )
    order_id = fields.Many2one(
        'pos.order', string='Pedido POS', required=False, readonly=True,
        ondelete='cascade', index=True
    )
    sale_order_id = fields.Many2one(
        'sale.order', string='Cotización Origen', readonly=True
    )
    partner_id = fields.Many2one(
        'res.partner', string='Cliente', compute='_compute_partner_id', store=True
    )

    @api.depends('order_id.partner_id', 'sale_order_id.partner_id')
    def _compute_partner_id(self):
        for s in self:
            s.partner_id = s.order_id.partner_id or s.sale_order_id.partner_id
    messenger_id = fields.Many2one(
        'res.partner', string='Mensajero', tracking=True,
        domain="[('is_messenger', '=', True)]"
    )
    state = fields.Selection([
        ('draft', 'Borrador'),
        ('street', 'En la Calle'),
        ('delivered', 'Entregado'),
        ('settled', 'Pagado y Cerrado'),
        ('cancelled', 'Cancelado'),
    ], string='Estado', default='draft', tracking=True, index=True)
    is_cod = fields.Boolean(string='Es Contra Entrega', compute='_compute_dashboard_fields')
    total_order = fields.Float(string='Total de la Orden', compute='_compute_dashboard_fields')

    @api.depends('shipment_mode', 'sale_order_id.shipment_mode', 'order_id.amount_total', 'sale_order_id.amount_total')
    def _compute_dashboard_fields(self):
        for s in self:
            s.is_cod = (s.shipment_mode or (s.sale_order_id.shipment_mode if s.sale_order_id else 'none')) == 'cod'
            s.total_order = (s.sale_order_id.amount_total if s.sale_order_id else s.order_id.amount_total) or 0.0

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('Nuevo')) == _('Nuevo'):
                vals['name'] = self.env['ir.sequence'].next_by_code('pos.shipment') or _('Nuevo')
        records = super().create(vals_list)
        records._notify_dashboard()
        return records

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in ['state', 'messenger_id', 'is_settled']):
            self._notify_dashboard()
        return res

    def _notify_dashboard(self):
        """Notificar al dashboard en tiempo real vía Bus."""
        self.env['bus.bus']._sendone('pos_shipment_update', 'pos_shipment_update', {})

    session_id = fields.Many2one('pos.session', string='Sesión POS', related='order_id.session_id', store=True)
    is_settled = fields.Boolean(string='Caja Cuadrada', default=False, tracking=True)
    settled_at = fields.Datetime(string='Validado por Cajero', readonly=True)
    
    # ── Elite Fields ──
    is_liquidated = fields.Boolean(string='Liquidado', default=False, tracking=True)

    is_messenger_paid = fields.Boolean(string='Pago a Mensajero', default=False, tracking=True)
    messenger_paid_at = fields.Datetime(string='Fecha Pago Mensajero', readonly=True)

    access_token = fields.Char(string='Token Mensajero', default=lambda self: str(uuid.uuid4()), copy=False)
    customer_token = fields.Char(string='Token Cliente', default=lambda self: str(uuid.uuid4()), copy=False)
    secure_token = fields.Char(
        string='Token de Seguridad', readonly=True, copy=False,
        default=lambda self: str(uuid.uuid4())
    )
    shipment_mode = fields.Selection([
        ('none', 'Sin Envío'),
        ('paid', 'Pago al Instante'),
        ('cod', 'Contra Entrega'),
    ], string="Modo de Envío", default='none')

    # --- Calificación del Cliente (Tipo Uber) ---
    customer_rating = fields.Integer(string='Calificación Mensajero', default=0, tracking=True)
    customer_rating_note = fields.Text(string='Comentario Mensajero')
    vendor_rating = fields.Integer(string='Calificación Tienda', default=0, tracking=True)
    vendor_rating_note = fields.Text(string='Comentario Tienda')

    # --- Métricas de Tiempo ---
    date_invoiced = fields.Datetime(
        string='Fecha Facturación', help="Cuando se creó el pedido en el POS"
    )
    date_processed = fields.Datetime(
        string='Fecha Procesado', help="Cuando se asignó al mensajero", tracking=True
    )
    date_delivered = fields.Datetime(
        string='Fecha Entrega', readonly=True, tracking=True
    )
    delivery_time = fields.Integer(string='Minutos Entrega', help="Tiempo desde asignación hasta entrega")
    
    # --- Cálculo de KM y Precios ---
    distance_km = fields.Float(string='Distancia (KM)', digits=(16, 2))
    shipping_cost = fields.Monetary(
        string='Costo Mensajero', currency_field='currency_id',
        compute='_compute_shipping_amounts', store=True, readonly=False,
        help="Lo que se le paga al mensajero."
    )
    shipping_charge = fields.Monetary(
        string='Cargo Cliente', currency_field='currency_id',
        compute='_compute_shipping_amounts', store=True, readonly=False,
        help="Lo que se le cobra al cliente."
    )

    is_paid = fields.Boolean(string='Pagado', compute='_compute_payment_status', help="Indica si el pedido ya fue pagado en POS o Factura")

    @api.depends('order_id.state', 'order_id.amount_paid', 'sale_order_id.invoice_ids.payment_state', 'order_id.amount_total')
    def _compute_payment_status(self):
        for s in self:
            paid = False
            # 1. Caso POS: Verificamos estado y si el total está cubierto
            if s.order_id:
                # En Odoo 18, un pedido pagado suele estar en 'paid', 'done' o 'invoiced'
                paid = s.order_id.state in ['paid', 'done', 'invoiced']
                # Refuerzo: Si el estado no es claro, comparamos montos
                if not paid and s.order_id.amount_total > 0:
                    paid = s.order_id.amount_paid >= s.order_id.amount_total

            # 2. Caso Sale Order / Factura: Verificamos facturas
            elif s.sale_order_id:
                invoices = s.sale_order_id.invoice_ids.filtered(lambda i: i.state == 'posted')
                if invoices:
                    paid = all(i.payment_state in ['paid', 'in_payment'] for i in invoices)
                else:
                    paid = s.sale_order_id.state == 'sale' and s.sale_order_id.invoice_status == 'invoiced'
            
            s.is_paid = paid

    map_url = fields.Char(string='Mapa Destino', compute='_compute_map_url')
    manual_location_link = fields.Char(string='Link Ubicación Manual', help="Pegar link de WhatsApp o Google Maps compartido")
    customer_portal_url = fields.Char(string='Link Cliente', compute='_compute_portal_urls')
    messenger_portal_url = fields.Char(string='Link Mensajero', compute='_compute_portal_urls')

    # --- Lógica Dinámica para el Cliente (Renquitec Elite) ---
    @api.depends('create_date', 'date_processed', 'state')
    def _compute_dynamic_status_info(self):
        now = fields.Datetime.now()
        for s in self:
            # Minutos desde la creación (para fase inicial)
            creation_start = s.date_invoiced or s.create_date
            elapsed_creation = int((now - creation_start).total_seconds() / 60) if creation_start else 0
            
            # Minutos desde la asignación (para fase de ruta)
            elapsed_route = int((now - s.date_processed).total_seconds() / 60) if s.date_processed else 0
            
            s.elapsed_minutes = elapsed_route if s.state == 'street' else elapsed_creation
            
            if s.state == 'draft':
                s.dynamic_status_title = _("PEDIDO RECIBIDO")
                s.dynamic_status_message = _("El equipo está preparando tu pedido. En breve será asignado a un mensajero.")
            elif s.state == 'street':
                if elapsed_route < 12:
                    s.dynamic_status_title = _("MENSAJERO ASIGNADO")
                    s.dynamic_status_message = _("¡Buenas noticias! Tu pedido ha sido entregado al mensajero y está organizando su ruta de salida.")
                else:
                    s.dynamic_status_title = _("PEDIDO EN RUTA")
                    s.dynamic_status_message = _("El mensajero ya está en camino con tu pedido. Tiene otras entregas programadas, por lo que te pedimos un poco de paciencia. ¡Ya casi llega!")
            elif s.state in ['delivered', 'settled']:
                s.dynamic_status_title = _("¡PEDIDO ENTREGADO!")
                s.dynamic_status_message = _("Tu pedido ha llegado a su destino. ¡Esperamos que lo disfrutes! Gracias por elegirnos.")
            else:
                s.dynamic_status_title = _("PROCESANDO PEDIDO")
                s.dynamic_status_message = _("Estamos validando los detalles de tu orden para enviarla lo antes posible.")

            # Cálculo de ETA Dinámico
            if s.state == 'draft':
                s.dynamic_eta = _("PREPARANDO")
            elif s.state == 'street':
                total_est = s.estimated_delivery_time
                remaining = max(total_est - elapsed_route, 5) # Mínimo 5 min si ya pasó el tiempo
                s.dynamic_eta = _("LLEGA EN %s MIN") % remaining
            elif s.state in ['delivered', 'settled']:
                s.dynamic_eta = _("ENTREGADO")
            else:
                s.dynamic_eta = _("PENDIENTE")

    elapsed_minutes = fields.Integer(compute='_compute_dynamic_status_info')
    dynamic_status_title = fields.Char(compute='_compute_dynamic_status_info')
    dynamic_status_message = fields.Char(compute='_compute_dynamic_status_info')
    dynamic_eta = fields.Char(compute='_compute_dynamic_status_info')

    # URLs de navegación para el mensajero (Almacenados para portal)
    nav_google_url = fields.Char(compute='_compute_nav_urls', store=True)
    nav_waze_url = fields.Char(compute='_compute_nav_urls', store=True)

    @api.depends('manual_location_link', 'partner_id.partner_latitude', 'partner_id.partner_longitude', 'partner_id.street')
    def _compute_nav_urls(self):
        for s in self:
            lat = s.partner_id.partner_latitude
            lon = s.partner_id.partner_longitude
            manual = s.manual_location_link
            addr = s.partner_id.contact_address or s.partner_id.street or ""
            
            # Google Maps: Manual -> GPS -> Address Search
            g_url = manual
            if not g_url:
                if lat and lon:
                    g_url = f"https://www.google.com/maps/dir/?api=1&destination={lat},{lon}"
                elif addr:
                    q = urllib.parse.quote(addr)
                    g_url = f"https://www.google.com/maps/search/?api=1&query={q}"
            s.nav_google_url = g_url or "#"

            # Waze: Manual -> GPS -> Address Search
            w_url = manual
            if not w_url:
                if lat and lon:
                    w_url = f"https://waze.com/ul?ll={lat},{lon}&navigate=yes"
                elif addr:
                    q = urllib.parse.quote(addr)
                    w_url = f"https://waze.com/ul?q={q}&navigate=yes"
            s.nav_waze_url = w_url or "#"

    def action_submit_rating(self, customer_rating=0, customer_rating_note=None, vendor_rating=0, vendor_rating_note=None):
        """Procesar calificación del cliente desde el portal."""
        self.ensure_one()
        self.write({
            'customer_rating': int(customer_rating or 0),
            'customer_rating_note': customer_rating_note,
            'vendor_rating': int(vendor_rating or 0),
            'vendor_rating_note': vendor_rating_note,
        })
        self.message_post(body=f"⭐ Calificación Recibida: Mensajero ({customer_rating}) - Tienda ({vendor_rating})")
        return True


    @api.depends('access_token', 'customer_token', 'partner_id.name')
    def _compute_portal_urls(self):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for s in self:
            partner_name = s.partner_id.name or 'cliente'
            slug = slugify(partner_name)
            s.customer_portal_url = f"{base_url}/confirmar-pedido/{s.customer_token or ''}/{slug}"
            s.messenger_portal_url = f"{base_url}/reportar-entrega/{s.access_token or ''}/{slug}"

    @api.depends('partner_id.partner_latitude', 'partner_id.partner_longitude')
    def _compute_map_url(self):
        for s in self:
            if s.partner_id.partner_latitude and s.partner_id.partner_longitude:
                s.map_url = f"https://www.google.com/maps/dir/?api=1&destination={s.partner_id.partner_latitude},{s.partner_id.partner_longitude}"
            else:
                s.map_url = False
    
    @api.depends('partner_id.partner_latitude', 'order_id.lines', 'sale_order_id.order_line', 'shipment_mode')
    def _compute_shipping_amounts(self):
        for record in self:
            charge = record.shipping_charge or 0.0
            
            # Buscar productos de envío de forma agresiva (Acentos, Mayúsculas y Términos Comunes)
            lines = record.order_id.lines if record.order_id else record.sale_order_id.order_line
            delivery_line = False
            if lines:
                if record.sale_order_id:
                    delivery_line = lines.filtered(lambda l: l.is_delivery_line)[:1]
                else:
                    delivery_line = lines.filtered(lambda l: 
                        any(word in (l.product_id.name or '').lower() for word in ['envio', 'envío', 'delivery', 'cargo', 'flete', 'mensajeria', 'mensajería'])
                    )[:1]

            if delivery_line:
                # Extraer monto final (con impuestos si es POS, total si es Sale)
                val = delivery_line.price_subtotal_incl if record.order_id else delivery_line.price_total
                if val > 0:
                    record.shipping_charge = val
                    record.shipping_cost = val
                    continue
                elif charge > 0:
                    record.shipping_charge = charge
                    record.shipping_cost = charge
                    continue

            d = record.distance_km
            if not d and record.partner_id.partner_latitude:
                d = record._calculate_gps_distance()
            
            raw_charge = 0.0
            if d <= 5: raw_charge = 150.0
            elif d <= 10: raw_charge = 150.0 + ((d - 5) * 30.0)
            elif d <= 20: raw_charge = 300.0 + ((d - 10) * 15.0)
            elif d > 20: raw_charge = 500.0 + ((d - 20) * 15.0)
            
            val = round(raw_charge / 10.0) * 10.0
            record.shipping_charge = charge or val
            record.shipping_cost = charge or val

    def _calculate_gps_distance(self):
        """Calcula distancia Haversine desde la compañía al cliente."""
        self.ensure_one()
        source = self.company_id.partner_id
        dest = self.partner_id
        if not source.partner_latitude or not dest.partner_latitude: return 0.0
        R = 6371.0
        lat1, lon1 = math.radians(source.partner_latitude), math.radians(source.partner_longitude)
        lat2, lon2 = math.radians(dest.partner_latitude), math.radians(dest.partner_longitude)
        dlon, dlat = lon2 - lon1, lat2 - lat1
        a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c
    
    delivered_at_confirmed = fields.Datetime(string='Confirmado el')
    customer_confirmed = fields.Boolean(string='Validado por Cliente', default=False)
    customer_note = fields.Text(string='Nota del Cliente')
    messenger_note = fields.Text(string='Nota del Mensajero')
    payment_method_confirmed = fields.Selection([
        ('cash', 'Efectivo'),
        ('transfer', 'Transferencia'),
    ], string='Cobro Confirmado')
    customer_signature_name = fields.Char(string='Firmado por (Cliente)')

    @property
    def estimated_delivery_time(self):
        """Calcula un tiempo estimado dinámico basado en la distancia."""
        base = 65
        dist_factor = self.distance_km * 8
        total = min(max(base + dist_factor, 65), 120)
        return int(total)

    company_id = fields.Many2one(
        'res.company', string='Compañía', required=True,
        default=lambda self: self.env.company
    )
    currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id'
    )

    def action_assign_messenger(self, messenger_id):
        self.ensure_one()
        self.write({
            'messenger_id': messenger_id,
            'state': 'street',
            'date_processed': fields.Datetime.now()
        })

    def action_confirm_delivery(self, payment_method, note=None):
        """Método llamado desde el portal público con Validación de Seguridad (Renquitec Security)."""
        self.ensure_one()
        if self.state in ['delivered', 'settled']: return True
        
        # GATE DE SEGURIDAD (Renquitec Hard-Lock): 
        # Solo permitir si es Contra Entrega o ya está Pagado
        mode = self.shipment_mode or (self.sale_order_id.shipment_mode if self.sale_order_id else 'none')
        is_cod = mode == 'cod'
        if not is_cod and not self.is_paid:
            _logger.warning(f"[PSM Security] Bloqueo de confirmación: Pedido {self.name} NO pagado y NO es COD.")
            raise UserError(_("No puedes confirmar la entrega: El pedido debe ser pagado en la tienda primero."))

        self.write({
            'state': 'delivered',
            'date_delivered': fields.Datetime.now(),
            'payment_method_confirmed': payment_method,
            'messenger_note': note,
        })
        return True

    def action_settle_cash(self):
        """Validar dinero recibido (Cajero) — un solo envío."""
        self.ensure_one()
        if self.state != 'delivered': raise UserError(_("Solo se pueden liquidar envíos entregados."))
        self.write({
            'is_settled': True,
            'is_liquidated': True,
            'settled_at': fields.Datetime.now(),
            'state': 'settled'
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Éxito"),
                'message': _("Envío liquidado correctamente"),
                'type': 'success',
                'sticky': False,
                'next': {'shipment_id': self.id, 'model': self._name, 'print_thermal': True}
            }
        }

    @api.model
    def action_settle_cash_bulk(self, ids=None, pay_messenger=False):
        """Liquidar múltiples envíos de una sola vez y registrar salida de efectivo si aplica."""
        if not ids: 
            return {'settled': [], 'errors': [_("No hay IDs seleccionados")]}
        
        records = self.browse(ids)
        settled, errors, now = [], [], fields.Datetime.now()
        
        # Buscar sesión activa de forma más robusta (Priorizar la del usuario actual)
        session = self.env['pos.session'].search([
            ('state', '=', 'opened'),
            ('user_id', '=', self.env.user.id)
        ], limit=1)
        
        if not session and pay_messenger:
            # Si no hay sesión propia, buscar cualquier sesión abierta en esta compañía
            session = self.env['pos.session'].search([
                ('state', '=', 'opened'),
                ('company_id', '=', self.env.company.id)
            ], limit=1)

        for shipment in records:
            try:
                if shipment.state == 'settled' or shipment.is_settled: 
                    continue
                    
                vals = {
                    'is_settled': True, 
                    'is_liquidated': True, 
                    'settled_at': now, 
                    'state': 'settled'
                }
                
                if pay_messenger:
                    vals.update({'is_messenger_paid': True, 'messenger_paid_at': now})
                    
                    # Automatizar Salida de Efectivo (Smart Reconciliation)
                    if session and shipment.shipping_cost > 0:
                        # Buscar el método de pago en efectivo
                        cash_pm = session.config_id.payment_method_ids.filtered(lambda p: p.is_cash_count)[:1]
                        if not cash_pm:
                            cash_pm = session.config_id.payment_method_ids.filtered(lambda p: p.type == 'cash')[:1]
                        
                        if cash_pm and cash_pm.journal_id:
                            self.env['account.bank.statement.line'].create({
                                'journal_id': cash_pm.journal_id.id,
                                'pos_session_id': session.id,
                                'amount': -shipment.shipping_cost,
                                'payment_ref': _("PSM: Salida Pago Mensajero %s (%s)") % (shipment.name, shipment.messenger_id.name or 'N/A'),
                            })
                            _logger.info(f"[PSM] Salida de caja registrada para {shipment.name} en sesión {session.name}")
                        else:
                            _logger.warning(f"[PSM] No se pudo registrar salida para {shipment.name}: Diario de efectivo no encontrado.")

                shipment.write(vals)
                settled.append(shipment.name)
            except Exception as e:
                _logger.error(f"[PSM] Error liquidando envío {shipment.name}: {str(e)}")
                errors.append(f"{shipment.name} (Error: {str(e)})")
        return {'settled': settled, 'errors': errors}

    def _prepare_single_shipment(self, s):
        """Helper para formatear un envío para el dashboard y el POS (SST)."""
        is_cod = (s.shipment_mode or (s.sale_order_id.shipment_mode if s.sale_order_id else 'none')) == 'cod'
        total_order = (s.sale_order_id.amount_total if s.sale_order_id else s.order_id.amount_total) or 0.0
        
        # Resumen de productos para el POS
        product_summary = []
        lines = s.sale_order_id.order_line if s.sale_order_id else (s.order_id.lines if s.order_id else [])
        for l in lines:
            p_name = l.product_id.name or "Producto"
            qty = getattr(l, 'product_uom_qty', 0) if s.sale_order_id else getattr(l, 'qty', 0)
            if qty > 0 and 'envio' not in p_name.lower():
                product_summary.append({'name': p_name, 'qty': qty, 'price': l.price_total if s.sale_order_id else l.price_subtotal_incl})

        order_ref = s.order_id.pos_reference or s.sale_order_id.name or ''
        return {
            'id': s.id,
            'name': f"{s.name} [{order_ref}]" if order_ref else s.name,
            'partner_name': s.partner_id.name or 'Consumidor Final',
            'partner_phone': s.partner_id.phone or s.partner_id.mobile or '',
            'messenger_name': s.messenger_id.name or 'Sin asignar',
            'seller_name': (s.sale_order_id.user_id.name or s.order_id.user_id.name) if (s.sale_order_id or s.order_id) else 'Sistema',
            'time_ago': self._get_time_ago(s.create_date),
            'date_formatted': s.create_date.strftime('%d/%m %H:%M') if s.create_date else '',
            'total_order': total_order,
            'is_cod': is_cod,
            'charge': s.shipping_charge,
            'cost': s.shipping_cost,
            'amount': total_order if is_cod else 0.0,
            'state': s.state,
            'state_label': 'ENTREGADO' if s.state == 'delivered' else 'EN LA CALLE' if s.state == 'street' else 'BORRADOR',
            'customer_portal_url': s.customer_portal_url,
            'messenger_portal_url': s.messenger_portal_url,
            'products': product_summary,
            'pos_order_id': s.order_id.id,
            'sale_order_id': s.sale_order_id.id,
        }

    @api.model
    def get_dashboard_data(self, date_filter='today', search_query=None, month=None, year=None):
        """Panel de Control Elite: Gestión Real-Time + Analíticas Históricas."""
        today = fields.Date.today()
        
        # 1. ANALÍTICAS (Filtradas por fecha)
        start_date = fields.Datetime.to_datetime(today)
        end_date = fields.Datetime.now()

        if month and year:
            start_date = datetime(int(year), int(month), 1)
            last_day = calendar.monthrange(int(year), int(month))[1]
            end_date = datetime(int(year), int(month), last_day, 23, 59, 59)
        elif date_filter == 'yesterday':
            start_date = fields.Datetime.to_datetime(today - timedelta(days=1))
            end_date = fields.Datetime.to_datetime(today).replace(hour=0, minute=0, second=0) - timedelta(seconds=1)
        elif date_filter == 'week':
            start_date = fields.Datetime.to_datetime(today - timedelta(days=7))
        elif date_filter == 'all':
            start_date = fields.Datetime.to_datetime(today - timedelta(days=365))

        stat_domain = [('create_date', '>=', start_date), ('create_date', '<=', end_date)]
        if search_query:
            stat_domain += ['|', '|', '|', '|', '|',
                            ('name', 'ilike', search_query), 
                            ('partner_id.name', 'ilike', search_query),
                            ('messenger_id.name', 'ilike', search_query),
                            ('partner_id.phone', 'ilike', search_query),
                            ('order_id.user_id.name', 'ilike', search_query),
                            ('sale_order_id.user_id.name', 'ilike', search_query)]
        
        historical_recs = self.search(stat_domain)

        # 2. OPERATIVA (Activos de los últimos 7 días o sin liquidar)
        allowed_companies = self.env.companies.ids
        # Solo mostrar lo no liquidado de los últimos 7 días para evitar saturar el TPV con basura antigua
        active_date_limit = fields.Datetime.now() - timedelta(days=7)
        active_domain = [
            ('is_settled', '=', False), 
            ('company_id', 'in', allowed_companies),
            ('create_date', '>=', active_date_limit)
        ]
        if search_query:
            active_domain += ['|', '|', '|', '|', '|',
                             ('name', 'ilike', search_query), 
                             ('partner_id.name', 'ilike', search_query),
                             ('messenger_id.name', 'ilike', search_query),
                             ('partner_id.phone', 'ilike', search_query),
                             ('order_id.user_id.name', 'ilike', search_query),
                             ('sale_order_id.user_id.name', 'ilike', search_query)]
        
        active_recs = self.search(active_domain)

        data = {
            'draft': [], 'street': [], 'delivered': [], 'cancelled': [],
            'all_delivered_count': len(historical_recs.filtered(lambda x: x.state in ('delivered', 'settled'))),
            'stats': {
                'messenger_perf': [], 'seller_perf': [],
                'avg_time': 0, 'avg_rating': 0.0,
                'total_count': len(historical_recs)
            },
            'reconciliation': {'in_transit': "0.00"}
        }

        # Llenar columnas operativas
        for s in active_recs:
            if s.state == 'settled': continue
            if s.state == 'delivered' and s.is_liquidated: continue
            
            # Filtro: Solo mostrar COD en Ruta y Entregados (Dinero por Recibir)
            if s.state in ('street', 'delivered') and s.shipment_mode != 'cod':
                continue
                
            if s.state in data:
                data[s.state].append(self._prepare_single_shipment(s))

        # Calcular Estadísticas del Periodo (Incluyendo 'settled' como éxito)
        messengers, sellers = {}, {}
        total_rating, rating_count, total_minutes, delivery_count, in_transit_amount = 0, 0, 0, 0, 0.0
        
        for s in historical_recs:
            m_name = s.messenger_id.name or 'Sin Asignar'
            u_name = (s.sale_order_id.user_id.name or s.order_id.user_id.name) if (s.sale_order_id or s.order_id) else 'Sistema'

            if m_name not in messengers:
                messengers[m_name] = {'delivered': 0, 'returned': 0}
            if u_name not in sellers:
                sellers[u_name] = {'delivered': 0}

            if s.state in ('delivered', 'settled'):
                messengers[m_name]['delivered'] += 1
                sellers[u_name]['delivered'] += 1
                
                if s.customer_rating > 0: 
                    total_rating += s.customer_rating
                    rating_count += 1
                if s.delivery_time > 0: 
                    total_minutes += s.delivery_time
                    delivery_count += 1
            elif s.state == 'cancelled':
                messengers[m_name]['returned'] += 1
            
            # Reconciliación (Basada en lo activo, no en el histórico)
            if s.id in active_recs.ids and s.state == 'delivered' and not s.is_liquidated:
                if (s.shipment_mode or (s.sale_order_id.shipment_mode if s.sale_order_id else 'none')) == 'cod':
                    in_transit_amount += (s.sale_order_id.amount_total if s.sale_order_id else s.order_id.amount_total) or 0.0

        data['stats'].update({
            'messenger_perf': [
                {'name': k, 'count': v['delivered'], 'returned': v['returned']} 
                for k, v in sorted(messengers.items(), key=lambda x: x[1]['delivered'], reverse=True)[:5]
            ],
            'seller_perf': [
                {'name': k, 'count': v['delivered']} 
                for k, v in sorted(sellers.items(), key=lambda x: x[1]['delivered'], reverse=True)[:5]
            ],
            'avg_time': round(total_minutes / delivery_count, 1) if delivery_count else 0,
            'avg_rating': round(total_rating / rating_count, 1) if rating_count else 0.0
        })
        data['reconciliation']['in_transit'] = f"{in_transit_amount:,.2f}"
        return data

    def _get_time_ago(self, dt):
        if not dt: return ''
        diff = fields.Datetime.now() - dt
        if diff.days > 0: return f"{diff.days}d"
        minutes = int(diff.seconds / 60)
        if minutes > 60: return f"{int(minutes/60)}h"
        return f"{minutes}m"

    def action_open_share_wizard(self):
        self.ensure_one()
        return {'name': _('Compartir Envío %s') % self.name, 'type': 'ir.actions.act_window', 'res_model': 'pos.shipment.share.wizard', 'view_mode': 'form', 'target': 'new', 'context': {'default_shipment_id': self.id}}

    def action_cancel_delivery(self, note=None):
        self.ensure_one()
        self.write({'state': 'cancelled', 'messenger_note': note})
        if self.order_id:
            try: self.order_id.action_pos_order_cancel()
            except: pass
        if self.sale_order_id and self.sale_order_id.state not in ['cancel', 'done']:
            try: self.sale_order_id.action_cancel()
            except: pass

    def get_portal_url(self):
        self.ensure_one()
        return self.messenger_portal_url or "#"

    def get_share_data(self):
        self.ensure_one()
        partner = self.partner_id
        messenger = self.messenger_id
        
        mode = self.shipment_mode or (self.sale_order_id.shipment_mode if self.sale_order_id else 'none')
        is_cod = mode == 'cod'
        total_order = (self.sale_order_id.amount_total if self.sale_order_id else self.order_id.amount_total) or 0.0
        
        return {
            'id': self.id,
            'name': self.name,
            'total_order': total_order,
            'is_cod': is_cod,
            'customer_url': self.customer_portal_url or '',
            'customer_phone': (partner.phone or partner.mobile or '') if partner else '',
            'partner_name': partner.name if partner else _('Consumidor Final'),
            'messenger_url': self.messenger_portal_url or '',
            'messenger_phone': messenger.messenger_whatsapp if messenger else '',
        }


    def action_share_whatsapp(self):
        self.ensure_one()
        if not self.messenger_id or not self.messenger_id.messenger_whatsapp: raise UserError(_("El mensajero no tiene WhatsApp."))
        phone = "".join(filter(str.isdigit, self.messenger_id.messenger_whatsapp))
        
        mode = self.shipment_mode or (self.sale_order_id.shipment_mode if self.sale_order_id else 'none')
        amount = (self.sale_order_id.amount_total if self.sale_order_id else self.order_id.amount_total) or 0.0
        amount_label = f"RD$ {amount:,.2f} — ⚠ CONTRA ENTREGA" if mode == 'cod' else f"RD$ {self.shipping_charge:,.2f} (cargo)"
        
        message = _("🛵 Hola %s, nuevo envío: *%s*\n👤 Cliente: %s\n📦 Monto: %s\n✅ Link: %s", self.messenger_id.name, self.name, self.partner_id.name, amount_label, self.get_portal_url())
        return {'type': 'ir.actions.act_url', 'url': f"https://wa.me/{phone}?text={urllib.parse.quote(message)}", 'target': 'new'}

    def action_share_whatsapp_customer(self):
        self.ensure_one()
        if not self.partner_id or (not self.partner_id.phone and not self.partner_id.mobile): raise UserError(_("El cliente no tiene teléfono."))
        phone = "".join(filter(str.isdigit, self.partner_id.phone or self.partner_id.mobile))
        message = _("Hola %s, tu pedido %s está en camino. Síguelo aquí: %s", self.partner_id.name, self.name, self.customer_portal_url)
        return {'type': 'ir.actions.act_url', 'url': f"https://wa.me/{phone}?text={urllib.parse.quote(message)}", 'target': 'new'}

    def action_view_related_order(self):
        self.ensure_one()
        res_model = 'pos.order' if self.order_id else 'sale.order'
        res_id = self.order_id.id if self.order_id else self.sale_order_id.id
        return {'type': 'ir.actions.act_window', 'res_model': res_model, 'res_id': res_id, 'view_mode': 'form', 'target': 'current'}

    @api.model
    def action_create_shipment_from_draft_pos(self, order_data):
        partner_id = order_data.get('partner_id')
        if not partner_id:
            raise UserError(_("Debe seleccionar un cliente antes de generar el envío."))
            
        # 1. Crear la Cotización (sale.order)
        sale_order_vals = {
            'partner_id': partner_id,
            'shipment_mode': order_data.get('shipment_mode', 'none'),
            'messenger_id': order_data.get('messenger_id'),
            'manual_location_link': order_data.get('manual_location_link'),
            'distance_km': order_data.get('distance_km', 0.0),
            'shipping_fee': order_data.get('shipping_charge', 0.0),
            'order_line': [
                (0, 0, {
                    'product_id': line['product_id'],
                    'product_uom_qty': line['qty'],
                    'price_unit': line['price_unit'],
                }) for line in order_data.get('lines', [])
            ]
        }
        sale_order = self.env['sale.order'].create(sale_order_vals)
        sale_order._sync_shipping_line()
        
        # 2. Generar el envío (pos.shipment) en estado borrador (draft)
        shipment = self.env['pos.shipment'].create({
            'sale_order_id': sale_order.id,
            'partner_id': partner_id,
            'messenger_id': order_data.get('messenger_id'),
            'shipping_charge': order_data.get('shipping_charge', 0.0),
            'shipping_cost': order_data.get('shipping_charge', 0.0),
            'state': 'draft',
            'company_id': self.env.company.id,
            'shipment_mode': order_data.get('shipment_mode', 'none'),
        })
        
        # Notificar al dashboard
        shipment._notify_dashboard()
        
        return {
            'sale_order_id': sale_order.id,
            'sale_order_name': sale_order.name,
            'shipment_id': shipment.id,
        }
