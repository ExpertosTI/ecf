# -*- coding: utf-8 -*-
from odoo import api, fields, models


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    is_messenger_method = fields.Boolean(
        string='Es Método de Envío',
        help="Indica si este método se usa para pagos recolectados por mensajeros."
    )

    @api.model
    def _load_pos_data_fields(self, config_id):
        # Odoo 18: expone el flag al frontend del POS
        return super()._load_pos_data_fields(config_id) + ['is_messenger_method']
