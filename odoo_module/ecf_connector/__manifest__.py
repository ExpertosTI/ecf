# -*- coding: utf-8 -*-
{
    'name': 'Renace e-CF — Facturación Electrónica DGII',
    'version': '18.0.4.0',
    'category': 'Accounting/Localizations',
    'summary': 'Renace e-CF · Facturación Electrónica DGII RD (e-CF, ITBIS, retenciones, 606/607/608, IT-1)',
    'description': """
Renace e-CF — Conector oficial Odoo ↔ Renace e-CF Gateway

Capacidades fiscales DGII República Dominicana:
- Emisión de los 10 tipos de e-CF (E31..E47) con XAdES-BES, validación XSD, NCF atómico.
- Anulación e-CF conforme a ANECF.xsd (rangos firmados).
- Aceptación / Rechazo Comercial (ACECF / ARECF).
- Recepción automática de e-CF desde la DGII (compras 606).
- Reportes 606 (Compras), 607 (Ventas), 608 (Anulaciones), IT-1 (ITBIS mensual), IR-17 (Retenciones).
- Detección de saltos y agotamiento de secuencias NCF.
- Asientos contables con marcado analítico ECF para conciliación.

POS premium:
- Selección de tipo e-CF (E32/E31) con Health Check Renace e-CF.
- Modo diferido para créditos: emisión manual tras conciliación de pago.

Seguridad:
- Multi-empresa, HMAC-SHA256 con anti-replay, rate limit, TLS+mTLS hacia DGII.
- Trigger ``ecf_emision_automatica`` apagado por defecto (también en POS).
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
            'ecf_connector/static/src/js/EcfSelectionDialog.js',
            'ecf_connector/static/src/xml/ecf_pos_templates.xml',
            'ecf_connector/static/src/xml/ecf_pos_receipt.xml',
            'ecf_connector/static/src/xml/EcfSelectionDialog.xml',
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
