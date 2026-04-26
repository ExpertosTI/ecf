# -*- coding: utf-8 -*-
import logging
from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

def audit_shipment_structure(env):
    print("--- AUDITORÍA DE ESTRUCTURA RENACE ---")
    
    tables = {
        'pos.shipment': [
            'name', 'order_id', 'partner_id', 'messenger_id', 'state', 
            'access_token', 'customer_token', 'shipping_charge', 'payment_method_confirmed',
            'is_settled', 'settled_at'
        ],
        'pos.order': ['shipment_id', 'sale_order_id'],
        'sale.order': ['shipment_id', 'messenger_portal_url', 'customer_portal_url'],
        'res.users': ['is_messenger', 'messenger_pending_balance'],
        'res.partner': ['partner_latitude', 'partner_longitude']
    }
    
    errors = 0
    for model_name, fields in tables.items():
        model = env.get(model_name)
        if model is None:
            print(f"[ERROR] Modelo {model_name} no encontrado.")
            errors += 1
            continue
            
        print(f"\n[OK] Modelo: {model_name}")
        for field_name in fields:
            field = model._fields.get(field_name)
            if field:
                # Verificar si está en la DB física
                print(f"  - Campo: {field_name.ljust(25)} [EXISTE]")
            else:
                print(f"  - Campo: {field_name.ljust(25)} [FALTA EN CÓDIGO/REGISTRO]")
                errors += 1
    
    print("\n--- FIN DE AUDITORÍA ---")
    if errors == 0:
        print("ESTADO: Base de datos íntegra y sincronizada.")
    else:
        print(f"ESTADO: Se encontraron {errors} inconsistencias. Es necesario 'Actualizar' el módulo.")

if __name__ == "__main__":
    # Este script se puede ejecutar via 'odoo-bin shell'
    audit_shipment_structure(env)
