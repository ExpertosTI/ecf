# -*- coding: utf-8 -*-
import requests
import logging
import urllib.parse
from odoo import fields, models, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    partner_latitude = fields.Float(string='Latitud', digits=(16, 5))
    partner_longitude = fields.Float(string='Longitud', digits=(16, 5))

    is_messenger = fields.Boolean(
        string='Es Mensajero',
        default=False,
        help="Marcar para habilitar como personal de entrega en el POS."
    )
    messenger_whatsapp = fields.Char(string='WhatsApp del Mensajero')

    messenger_pending_balance = fields.Monetary(
        string='Saldo Pendiente', compute='_compute_messenger_balance',
        help="Efectivo que el mensajero tiene actualmente en la calle."
    )
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')

    def action_geolocalize(self):
        """Consume OpenStreetMap Nominatim API (Free) to get coordinates."""
        for partner in self:
            if not partner.street or not partner.city:
                continue
            
            address_str = f"{partner.street}, {partner.city}, {partner.state_id.name or ''}, {partner.country_id.name or ''}"
            url = "https://nominatim.openstreetmap.org/search"
            params = {
                'q': address_str,
                'format': 'json',
                'limit': 1
            }
            # Odoo Best Practice: Use a descriptive User-Agent
            headers = {'User-Agent': 'Renace-POS-Shipment/1.0'}
            
            try:
                response = requests.get(url, params=params, headers=headers, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    if data:
                        partner.write({
                            'partner_latitude': float(data[0]['lat']),
                            'partner_longitude': float(data[0]['lon']),
                        })
            except Exception as e:
                _logger.error("OSM Geocoding Error: %s", str(e))
                continue
                
        return True

    def _compute_messenger_balance(self):
        for partner in self:
            shipments = self.env['pos.shipment'].search([
                ('messenger_id', '=', partner.id),
                ('state', 'in', ['street', 'delivered']),
                ('is_settled', '=', False)
            ])
            total_balance = 0.0
            for s in shipments:
                # Si es Contra Entrega, el mensajero es responsable del total desde que sale
                order_mode = s.shipment_mode or (s.sale_order_id.shipment_mode if s.sale_order_id else 'none')
                if order_mode == 'cod':
                    total_balance += s.order_id.amount_total or s.sale_order_id.amount_total
                else:
                    if s.state == 'delivered':
                        total_balance += s.shipping_charge
            partner.messenger_pending_balance = total_balance

    def action_share_whatsapp(self):
        self.ensure_one()
        if not self.messenger_whatsapp:
            return False
        phone = "".join(filter(str.isdigit, self.messenger_whatsapp))
        message = _("Hola %s, contacto de Renace Logística.", self.name)
        url = f"https://wa.me/{phone}?text={urllib.parse.quote(message)}"
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }

    @api.model
    def action_create_pos_messenger(self, name, phone):
        """Método seguro para crear un mensajero desde la vista rápida del POS."""
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise UserError(_("No tienes permisos para crear mensajeros desde el POS."))
            
        if not name or not phone:
            return {'error': _("El nombre y número de teléfono son obligatorios.")}
            
        partner = self.sudo().create({
            'name': name,
            'is_messenger': True,
            'messenger_whatsapp': phone,
        })
        return {'id': partner.id, 'name': partner.name}

    @api.model
    def search_by_phone_pos(self, phone):
        """Busca partner por teléfono normalizado. Usado desde el POS vía RPC."""
        if not phone:
            return []
        digits = ''.join(filter(str.isdigit, phone))
        if len(digits) < 7:
            return []
        partners = self.search([
            '|', ('phone', 'ilike', digits[-10:]), ('mobile', 'ilike', digits[-10:])
        ], limit=5)
        return [{
            'id': p.id,
            'name': p.name,
            'display_name': p.display_name,
            'phone': p.phone or '',
            'mobile': p.mobile or '',
        } for p in partners]

    @api.model
    def quick_create_from_phone_pos(self, phone, name):
        """Crea un partner rápido desde el POS con teléfono + nombre."""
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise UserError(_("No tienes permisos para crear clientes desde el POS."))
        if not phone or not name:
            return {'error': _("Teléfono y nombre son obligatorios.")}
        # Verificar si ya existe
        digits = ''.join(filter(str.isdigit, phone))
        existing = self.search([
            '|', ('phone', 'ilike', digits[-10:]), ('mobile', 'ilike', digits[-10:])
        ], limit=1)
        if existing:
            return {'id': existing.id, 'name': existing.name, 'phone': existing.phone or existing.mobile, 'existing': True}
        partner = self.sudo().create({
            'name': name,
            'phone': phone,
            'mobile': phone,
        })
        return {'id': partner.id, 'name': partner.name, 'phone': phone, 'existing': False}
