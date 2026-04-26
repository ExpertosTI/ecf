# -*- coding: utf-8 -*-
{
    'name': 'POS Shipment Management',
    'version': '18.0.1.0.0',
    'category': 'Point of Sale',
    'summary': 'Gestión avanzada de envíos, mensajeros y cobros contra entrega',
    'description': """
        Módulo de gestión de envíos para POS:
        - Portal público para mensajeros (confirmación vía link).
        - Cálculo de envío por KM (tipo Uber).
        - Métricas de tiempo de procesamiento y entrega.
        - Integración con Dashboard POS y Cotizaciones.
    """,
    'author': 'Renace Tech',
    'website': 'https://renace.tech',
    'depends': ['point_of_sale', 'pos_sale', 'sale_management', 'dashboard_pos', 'salesperson_pos_order_line', 'mail', 'web'],
    'data': [
        'security/ir.model.access.csv',
        'security/shipment_security.xml',
        'data/ir_sequence_data.xml',
        'data/paperformat_data.xml',
        'report/shipping_label_reports.xml',
        'report/settlement_reports.xml',
        'report/settlement_report_templates.xml',
        'views/shipment_portal_templates.xml',
        'views/customer_portal_templates.xml',
        'views/pos_shipment_views.xml',
        'views/res_users_views.xml',
        'views/res_partner_views.xml',
        'views/res_config_settings_views.xml',
        'views/sale_order_views.xml',
        'views/pos_order_views.xml',
        'views/pos_payment_method_views.xml',
        'views/shipment_menus.xml',
        'views/shipment_share_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'pos_shipment_manager/static/src/js/ShipmentDashboard.js',
            'pos_shipment_manager/static/src/xml/ShipmentDashboard.xml',
            'pos_shipment_manager/static/src/scss/shipment_dashboard.scss',
            'pos_shipment_manager/static/src/scss/sale_order_backend.scss',
            'pos_shipment_manager/static/src/js/pos_dashboard_overrides.js',
            'pos_shipment_manager/static/src/xml/pos_dashboard_overrides.xml',
        ],
        'point_of_sale._assets_pos': [
            'pos_shipment_manager/static/src/js/pos_settle_patch.js',
            'pos_shipment_manager/static/src/js/shipment_settle_dialog.js',
            'pos_shipment_manager/static/src/js/shipment_config_dialog.js',
            'pos_shipment_manager/static/src/js/shipment_share_dialog.js',
            'pos_shipment_manager/static/src/js/shipment_settle_receipt.js',
            'pos_shipment_manager/static/src/js/pos_shipment_button.js',
            'pos_shipment_manager/static/src/xml/pos_shipment_button.xml',
            'pos_shipment_manager/static/src/xml/ShipmentSettleReceipt.xml',
            'pos_shipment_manager/static/src/scss/pos_shipment_button.scss',
            'pos_shipment_manager/static/src/js/pos_ticket_screen_overrides.js',
            'pos_shipment_manager/static/src/js/ShipmentModeButton.js',
            'pos_shipment_manager/static/src/xml/ShipmentModeButton.xml',
            'pos_shipment_manager/static/src/xml/pos_sale_overrides.xml',
            'pos_shipment_manager/static/src/xml/OrderReceipt.xml',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
