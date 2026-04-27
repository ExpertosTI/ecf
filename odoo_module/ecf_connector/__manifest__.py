# -*- coding: utf-8 -*-
{
    'name': 'ECF Connector — DGII e-CF República Dominicana',
    'version': '18.0.3.5',
    'category': 'Accounting/Localizations',
    'summary': 'Facturación Electrónica e-CF DGII — SaaS Renace.tech (Premium Dashboard)',
    'description': """
        Módulo oficial de Renace.tech para emisión de comprobantes fiscales
        electrónicos (e-CF) ante la DGII de la República Dominicana.

        Funcionalidades Premium:
        - Dashboard Interactivo (OWL + Chart.js): KPIs de facturación y estados DGII
        - Integración POS Premium: Selección de e-CF (E32/E31) con Health Check dinámico
        - Emisión manual de e-CF (trigger NUNCA automático por defecto)
        - Flujo POS diferido: facturas de envíos/créditos quedan pendientes de conciliación
        - Impresión DGII-compliant: NCF, CUFE, QR, timestamp de aprobación, ambiente
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
        'security/ecf_security.xml',
        'security/ir.model.access.csv',
        'data/ecf_tipo_data.xml',
        'data/ecf_cron_data.xml',
        'views/res_config_settings_views.xml',
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
            'ecf_connector/static/src/css/ecf_backend.css',
            'ecf_connector/static/src/js/ecf_status_widget.js',
            'ecf_connector/static/src/js/ecf_dashboard.js',
            'ecf_connector/static/src/xml/ecf_dashboard.xml',
            'ecf_connector/static/lib/chartjs/chart.umd.min.js',
        ],
        'web.report_assets_common': [
            'ecf_connector/static/src/css/ecf_report.css',
        ],
        'point_of_sale._assets_pos': [
            'ecf_connector/static/src/js/ecf_pos.js',
            'ecf_connector/static/src/js/ecf_type_button.js',
            'ecf_connector/static/src/xml/ecf_pos_templates.xml',
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
