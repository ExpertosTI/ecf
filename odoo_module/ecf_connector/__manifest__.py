{
    'name': 'ECF Connector — DGII e-CF',
    'version': '18.0.1.0',
    'category': 'Accounting/Localizations',
    'summary': 'Conector DGII República Dominicana',
    'description': """
        Módulo para emisión de comprobantes electrónicos e-CF RD.
    """,
    'author': 'Tu Empresa',
    'website': 'https://tu-saas-ecf.do',
    'license': 'LGPL-3',
    'depends': [
        'account',
        'base_setup',
        'web',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/ecf_security.xml',
        'data/ecf_tipo_data.xml',
        'views/res_config_settings_views.xml',
        'views/account_move_views.xml',
        'views/ecf_log_views.xml',
        'wizard/ecf_anular_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'ecf_connector/static/src/js/ecf_status_widget.js',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
}
