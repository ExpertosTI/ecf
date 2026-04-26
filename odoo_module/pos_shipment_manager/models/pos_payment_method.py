# -*- coding: utf-8 -*-
from odoo import fields, models


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    is_messenger_method = fields.Boolean(
        string='Es Método de Envío',
        help="Indica si este método se usa para pagos recolectados por mensajeros."
    )
