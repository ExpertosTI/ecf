# -*- coding: utf-8 -*-
from odoo import fields, models, api


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_shipment_price_per_km = fields.Float(
        string='Precio de Venta por KM',
        config_parameter='pos_shipment.price_per_km',
        default=0.0,
        help="Monto que se le cobra al cliente por cada kilómetro."
    )
    
    pos_shipment_cost_per_km = fields.Float(
        string='Costo de Mensajería por KM',
        config_parameter='pos_shipment.cost_per_km',
        default=0.0,
        help="Monto que se le paga al mensajero por cada kilómetro."
    )

    pos_shipment_product_id = fields.Many2one(
        'product.product',
        string='Producto para Cargos de Envío',
        config_parameter='pos_shipment.product_id',
        domain="[('type', '=', 'service')]",
        help="Producto de tipo servicio que se usará para las líneas de envío en las órdenes."
    )
