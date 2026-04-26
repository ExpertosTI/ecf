from odoo import api, SUPERUSER_ID
import logging

_logger = logging.getLogger(__name__)

def check_views(env):
    views = env['ir.ui.view'].search([('arch_db', 'ilike', 'nav_google_url')])
    for v in views:
        print(f"Found nav_google_url in view: {v.name} (ID: {v.id}, XML ID: {v.xml_id})")
        print(f"Arch: {v.arch_db}")
        print("-" * 40)

# To be run with odoo shell or similar
if __name__ == "__main__":
    pass
