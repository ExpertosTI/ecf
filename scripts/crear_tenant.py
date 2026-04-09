#!/usr/bin/env python3
"""
crear_tenant.py — Crea un nuevo tenant en la plataforma SaaS ECF.

Uso:
    python scripts/crear_tenant.py \
        --rnc 130000001 \
        --razon-social "Mi Empresa SRL" \
        --email admin@miempresa.do \
        --plan basico \
        --ambiente certificacion

Requiere:
    - ADMIN_API_KEY en .env o como variable de entorno
    - API Gateway corriendo en ECF_API_URL (default: http://localhost:8000)
"""

import argparse
import json
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description="Crear tenant en SaaS ECF DGII")
    parser.add_argument("--rnc", required=True, help="RNC del tenant (9-11 dígitos)")
    parser.add_argument("--razon-social", required=True, help="Razón social de la empresa")
    parser.add_argument("--email", required=True, help="Email del administrador")
    parser.add_argument("--plan", default="basico", choices=["basico", "profesional", "enterprise"])
    parser.add_argument("--ambiente", default="certificacion", choices=["certificacion", "produccion"])
    parser.add_argument("--webhook-url", default=None, help="URL webhook de Odoo")
    parser.add_argument("--max-ecf", type=int, default=1000, help="Máximo e-CF por mes")
    parser.add_argument("--api-url", default=None, help="URL del API Gateway")
    parser.add_argument("--admin-key", default=None, help="Admin API Key (o ADMIN_API_KEY env)")

    args = parser.parse_args()

    api_url = args.api_url or os.environ.get("ECF_API_URL", "http://localhost:8000")
    admin_key = args.admin_key or os.environ.get("ADMIN_API_KEY")

    if not admin_key:
        print("ERROR: ADMIN_API_KEY no configurada.", file=sys.stderr)
        print("  Usa --admin-key o define la variable ADMIN_API_KEY", file=sys.stderr)
        sys.exit(1)

    payload = {
        "rnc": args.rnc,
        "razon_social": args.razon_social,
        "email": args.email,
        "plan": args.plan,
        "ambiente": args.ambiente,
        "max_ecf_mensual": args.max_ecf,
    }
    if args.webhook_url:
        payload["odoo_webhook_url"] = args.webhook_url

    url = f"{api_url.rstrip('/')}/v1/admin/tenants"

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {admin_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
    except requests.ConnectionError:
        print(f"ERROR: No se pudo conectar a {api_url}", file=sys.stderr)
        print("  Verifica que el API Gateway esté corriendo.", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 201:
        data = resp.json()
        print()
        print("=" * 60)
        print("  TENANT CREADO EXITOSAMENTE")
        print("=" * 60)
        print()
        print(f"  Tenant ID:      {data['tenant_id']}")
        print(f"  RNC:            {data['rnc']}")
        print(f"  Razón Social:   {data['razon_social']}")
        print(f"  Schema:         {data['schema_name']}")
        print(f"  Ambiente:       {data['ambiente']}")
        print()
        print("  ┌─────────────────────────────────────────────────┐")
        print(f"  │ API Key:        {data['api_key']}")
        print(f"  │ Webhook Secret: {data['webhook_secret']}")
        print("  └─────────────────────────────────────────────────┘")
        print()
        print("  IMPORTANTE: Guarda estos valores en un lugar seguro.")
        print("  No se pueden recuperar después.")
        print()
        print("  Próximos pasos:")
        print("    1. Subir certificado .p12:")
        print(f"       python scripts/subir_certificado.py --tenant-id {data['tenant_id']} --cert mi_cert.p12")
        print("    2. Configurar en Odoo:")
        print(f"       - URL SaaS:        {api_url}")
        print(f"       - API Key:         {data['api_key']}")
        print(f"       - Webhook Secret:  {data['webhook_secret']}")
        print()
    elif resp.status_code == 409:
        print(f"ERROR: {resp.json().get('detail', 'Tenant ya existe')}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
