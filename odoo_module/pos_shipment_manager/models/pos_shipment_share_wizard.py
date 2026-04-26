# -*- coding: utf-8 -*-
import urllib.parse
from odoo import api, fields, models, _


class PosShipmentShareWizard(models.TransientModel):
    _name = 'pos.shipment.share.wizard'
    _description = 'Compartir Links de Envío'

    shipment_id = fields.Many2one('pos.shipment', required=True)

    # Links de portal
    messenger_url = fields.Char(
        string='🛵 Link Mensajero',
        related='shipment_id.messenger_portal_url',
        readonly=True,
    )
    customer_url = fields.Char(
        string='👤 Link Cliente',
        related='shipment_id.customer_portal_url',
        readonly=True,
    )

    # Info de monto para que sea visible en el wizard
    is_cod = fields.Boolean(compute='_compute_amounts')
    amount_label = fields.Char(string='Monto a Cobrar', compute='_compute_amounts')
    partner_name = fields.Char(related='shipment_id.partner_id.name', readonly=True)

    @api.depends('shipment_id')
    def _compute_amounts(self):
        for w in self:
            s = w.shipment_id
            # Usar el modo guardado en el envío o fallback a la SO
            mode = s.shipment_mode or (s.sale_order_id.shipment_mode if s.sale_order_id else 'none')
            is_cod = mode == 'cod'
            w.is_cod = is_cod
            currency = s.currency_id.name or 'RD$'
            if is_cod:
                amount = (s.order_id.amount_total if s.order_id else (s.sale_order_id.amount_total if s.sale_order_id else 0.0))
                w.amount_label = f"{currency} {amount:,.2f} — CONTRA ENTREGA"
            else:
                amount = s.shipping_charge or 0.0
                w.amount_label = f"{currency} {amount:,.2f} (cargo de envío)"

    def action_open_messenger_whatsapp(self):
        """Abre WhatsApp para el mensajero."""
        self.ensure_one()
        s = self.shipment_id
        if not s.messenger_id or not getattr(s.messenger_id, 'messenger_whatsapp', None):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'message': _("El mensajero no tiene WhatsApp configurado."), 'type': 'warning'},
            }
        phone = s.messenger_id.messenger_whatsapp
        # Limpiar y formatear con +1 si es necesario
        phone = "".join(filter(str.isdigit, phone))
        if len(phone) == 10: phone = "1" + phone
        
        is_cod = self.is_cod
        if is_cod:
            amount = (s.order_id.amount_total if s.order_id else (s.sale_order_id.amount_total if s.sale_order_id else 0.0))
            amount_label = f"RD$ {amount:,.2f} — ⚠ CONTRA ENTREGA (cobrar al cliente)"
        else:
            amount = s.shipping_charge or 0.0
            amount_label = f"RD$ {amount:,.2f} (cargo de envío)"
        message = _(
            "🛵 Hola %s, nuevo envío: *%s*\n"
            "👤 Cliente: %s\n"
            "📦 Monto a cobrar: %s\n"
            "✅ Confirmar entrega: %s",
            s.messenger_id.name, s.name,
            s.partner_id.name, amount_label,
            s.messenger_portal_url
        )
        url = f"https://wa.me/{phone}?text={urllib.parse.quote(message)}"
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}

    def action_open_customer_whatsapp(self):
        """Abre WhatsApp para el cliente."""
        self.ensure_one()
        s = self.shipment_id
        if not s.partner_id or (not s.partner_id.phone and not s.partner_id.mobile):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {'message': _("El cliente no tiene teléfono configurado."), 'type': 'warning'},
            }
        phone = s.partner_id.phone or s.partner_id.mobile
        phone = "".join(filter(str.isdigit, phone))
        if len(phone) == 10: phone = "1" + phone
        
        message = _(
            "Hola %s, tu pedido %s está en camino. "
            "Puedes seguirlo y confirmar aquí: %s",
            s.partner_id.name, s.name, s.customer_portal_url
        )
        url = f"https://wa.me/{phone}?text={urllib.parse.quote(message)}"
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}
