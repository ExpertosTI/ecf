# -*- coding: utf-8 -*-
import urllib.parse
from odoo import api, fields, models, _


class ResUsers(models.Model):
    _inherit = 'res.users'

    is_messenger = fields.Boolean(
        string='Es Mensajero',
        default=False,
        help="Marcar para habilitar como personal de entrega en el POS."
    )
    messenger_whatsapp = fields.Char(string='WhatsApp del Mensajero')
    nav_google_url = fields.Char(string='Google Maps (Envío)')
    nav_waze_url = fields.Char(string='Waze (Envío)')

    messenger_pending_balance = fields.Monetary(
        string='Saldo Pendiente', compute='_compute_messenger_balance',
        help="Efectivo que el mensajero tiene actualmente en la calle."
    )
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('is_messenger') and not vals.get('login'):
                # Auto-generar login basado en el nombre si no se provee correo
                name = vals.get('name', 'messenger')
                vals['login'] = "".join(filter(str.isalnum, name)).lower() + "@renace.internal"
        return super().create(vals_list)

    def _compute_messenger_balance(self):
        for user in self:
            shipments = self.env['pos.shipment'].search([
                ('messenger_id', '=', user.id),
                ('state', 'in', ['street', 'delivered']),
                ('is_settled', '=', False)
            ])
            total_balance = 0.0
            for s in shipments:
                # Si es Contra Entrega, el mensajero es responsable del total desde que sale
                # Verificamos tanto en el pedido de POS como en la SO original
                order_mode = s.sale_order_id.shipment_mode or s.order_id.sale_order_id.shipment_mode
                if order_mode == 'cod':
                    total_balance += s.order_id.amount_total or s.sale_order_id.amount_total
                else:
                    # Para prepagos, solo se liquida cuando se marca como entregado
                    if s.state == 'delivered':
                        total_balance += s.shipping_charge
            user.messenger_pending_balance = total_balance

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
