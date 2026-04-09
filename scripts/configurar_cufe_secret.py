#!/usr/bin/env python3
"""
configurar_cufe_secret.py — Registra el cufe_secret DGII de un tenant.

La DGII entrega un secreto único por contribuyente durante el proceso
de homologación. Este secreto es necesario para generar el CUFE de
cada comprobante fiscal electrónico.

Uso:
    python scripts/configurar_cufe_secret.py \\
        --tenant-id abc123-... \\
        --secret SECRETO_ENTREGADO_POR_DGII

O en modo interactivo (oculta el secreto mientras se escribe):
    python scripts/configurar_cufe_secret.py --tenant-id abc123-...
"""

import argparse
import getpass
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(
        description="Registra el cufe_secret DGII de un tenant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tenant-id", required=True, help="UUID del tenant")
    parser.add_argument("--secret", default=None,
                        help="Secreto CUFE entregado por la DGII (se pide interactivamente si se omite)")
    parser.add_argument("--api-url", default=None, help="URL del API Gateway")
    parser.add_argument("--admin-key", default=None,
                        help="Admin API Key (o variable ADMIN_API_KEY)")
    args = parser.parse_args()

    api_url   = args.api_url   or os.environ.get("ECF_API_URL", "http://localhost:8000")
    admin_key = args.admin_key or os.environ.get("ADMIN_API_KEY")

    if not admin_key:
        print("ERROR: ADMIN_API_KEY no configurada.", file=sys.stderr)
        print("  Usa --admin-key o define la variable ADMIN_API_KEY", file=sys.stderr)
        sys.exit(1)

    cufe_secret = args.secret
    if not cufe_secret:
        cufe_secret = getpass.getpass("cufe_secret (DGII): ")

    if len(cufe_secret.strip()) < 8:
        print("ERROR: El cufe_secret parece demasiado corto.", file=sys.stderr)
        sys.exit(1)

    url = f"{api_url.rstrip('/')}/v1/admin/tenants/{args.tenant_id}/cufe-secret"

    try:
        resp = requests.put(
            url,
            json={"cufe_secret": cufe_secret},
            headers={
                "Authorization": f"Bearer {admin_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.ConnectionError:
        print(f"ERROR: No se pudo conectar a {api_url}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 200:
        print()
        print("=" * 55)
        print("  cufe_secret configurado correctamente")
        print("=" * 55)
        print()
        print(f"  Tenant ID: {args.tenant_id}")
        print()
        print("  El secreto ha sido cifrado con AES-256-GCM.")
        print("  Los comprobantes de este tenant ya pueden generar CUFE.")
        print()
    elif resp.status_code == 404:
        print(f"ERROR: Tenant {args.tenant_id} no encontrado.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
