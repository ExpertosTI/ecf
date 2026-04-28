# -*- coding: utf-8 -*-
{
    'name': 'Renace e-CF — Facturación Electrónica DGII (Odoo 19)',
    'version': '19.0.2.0',
    'category': 'Accounting/Localizations',
    'summary': 'Renace e-CF · Facturación Electrónica DGII RD (e-CF, ITBIS, retenciones, 606/607/608, IT-1)',
    'description': """
Renace e-CF — Conector oficial Odoo 19 ↔ Renace e-CF Gateway

Mismas capacidades que la versión 18.x, adaptado a Odoo 19.
Ver odoo_module/ecf_connector/__manifest__.py para el detalle.
""",
    'author': 'Renace.tech',
    'website': 'https://renace.tech',
    'license': 'LGPL-3',
    'depends': [
        'account',
        'base_setup',
        'web',
        'point_of_sale',
        'mail',
    ],
    'data': [
        'data/ecf_v19_master.xml',
        'data/ecf_cron_data.xml',
        'views/res_config_settings_views.xml',
        'views/res_partner_views.xml',
        'views/pos_order_views.xml',
        'views/account_move_views.xml',
        'views/ecf_log_views.xml',
        'views/ecf_dashboard_views.xml',
        'views/ecf_pending_views.xml',
        'views/ecf_compras_views.xml',
        'views/report_ecf_summary.xml',
        'report/ecf_invoice_report.xml',
        'wizard/ecf_anular_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ecf_connector_v19/static/src/css/ecf_backend.css',
            'ecf_connector_v19/static/src/js/ecf_status_widget.js',
            'ecf_connector_v19/static/src/js/ecf_dashboard.js',
            'ecf_connector_v19/static/src/xml/ecf_dashboard.xml',
            'ecf_connector_v19/static/lib/chartjs/chart.umd.min.js',
        ],
        'web.report_assets_common': [
            'ecf_connector_v19/static/src/css/ecf_report.css',
        ],
        'point_of_sale._assets_pos': [
            'ecf_connector_v19/static/src/js/ecf_pos.js',
            'ecf_connector_v19/static/src/js/ecf_type_button.js',
            'ecf_connector_v19/static/src/xml/ecf_pos_templates.xml',
            'ecf_connector_v19/static/src/xml/ecf_pos_receipt.xml',
        ],
    },
    'images': [
        'static/description/icon.png',
        'static/description/banner.png'
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
