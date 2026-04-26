# -*- coding: utf-8 -*-
{
    'name': 'ECF Connector — DGII e-CF República Dominicana',
    'version': '18.0.3.0',
    'category': 'Accounting/Localizations',
    'summary': 'Facturación Electrónica e-CF DGII — SaaS Renace.tech (e-CF Recibidas)',
    'description': """
        Módulo oficial de Renace.tech para emisión de comprobantes fiscales
        electrónicos (e-CF) ante la DGII de la República Dominicana.

        Funcionalidades:
        - Emisión manual de e-CF (trigger NUNCA automático por defecto)
        - Flujo POS diferido: facturas de envíos/créditos quedan pendientes de conciliación
        - e-CF Recibidas: descarga automática de facturas de proveedor desde DGII
        - Impresión DGII-compliant: NCF, CUFE, QR, timestamp de aprobación, ambiente
        - Dashboard Kanban de e-CF con KPIs por estado
        - Cron de conciliación: detecta facturas listas para emitir
        - Test de conexión al SaaS desde Ajustes
        - Soporte multi-empresa (aislado por company_id)
        - Webhook HMAC-SHA256 anti-replay
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
        'report/ecf_invoice_report.xml',
        'wizard/ecf_anular_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ecf_connector/static/src/js/ecf_status_widget.js',
            'ecf_connector/static/src/js/ecf_dashboard.js',
        ],
        'web.report_assets_common': [
            'ecf_connector/static/src/css/ecf_report.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
