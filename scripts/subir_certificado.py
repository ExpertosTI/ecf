#!/usr/bin/env python3
"""
subir_certificado.py — Sube un certificado .p12 a un tenant.

Uso:
    python scripts/subir_certificado.py \
        --tenant-id abc123-... \
        --cert /ruta/a/mi_cert.p12 \
        --password mi_password_p12

Requiere:
    - ADMIN_API_KEY en .env o como variable de entorno
    - API Gateway corriendo en ECF_API_URL (default: http://localhost:8000)
"""

import argparse
import getpass
import os
import sys

import requests


def main():
    parser = argparse.ArgumentParser(description="Subir certificado .p12 a tenant")
    parser.add_argument("--tenant-id", required=True, help="UUID del tenant")
    parser.add_argument("--cert", required=True, help="Ruta al archivo .p12")
    parser.add_argument("--password", default=None, help="Password del .p12 (se pide interactivamente si se omite)")
    parser.add_argument("--api-url", default=None, help="URL del API Gateway")
    parser.add_argument("--admin-key", default=None, help="Admin API Key (o ADMIN_API_KEY env)")

    args = parser.parse_args()

    api_url = args.api_url or os.environ.get("ECF_API_URL", "http://localhost:8000")
    admin_key = args.admin_key or os.environ.get("ADMIN_API_KEY")

    if not admin_key:
        print("ERROR: ADMIN_API_KEY no configurada.", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(args.cert):
        print(f"ERROR: Archivo no encontrado: {args.cert}", file=sys.stderr)
        sys.exit(1)

    cert_password = args.password
    if not cert_password:
        cert_password = getpass.getpass("Password del .p12: ")

    url = f"{api_url.rstrip('/')}/v1/admin/tenants/{args.tenant_id}/certs"

    try:
        with open(args.cert, "rb") as f:
            resp = requests.post(
                url,
                files={"cert_file": (os.path.basename(args.cert), f, "application/x-pkcs12")},
                data={"cert_password": cert_password},
                headers={"Authorization": f"Bearer {admin_key}"},
                timeout=30,
            )
    except requests.ConnectionError:
        print(f"ERROR: No se pudo conectar a {api_url}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code == 201:
        data = resp.json()
        print()
        print("=" * 60)
        print("  CERTIFICADO SUBIDO EXITOSAMENTE")
        print("=" * 60)
        print()
        print(f"  Cert ID:    {data['cert_id']}")
        print(f"  Serial:     {data['serial']}")
        print(f"  Subject:    {data['subject']}")
        print(f"  Válido:     {data['valid_from']} → {data['valid_to']}")
        print()
        print("  El certificado ha sido cifrado con AES-256-GCM y almacenado.")
        print("  Los certificados anteriores fueron desactivados automáticamente.")
        print()
    elif resp.status_code == 422:
        print(f"ERROR: {resp.json().get('detail', 'Certificado inválido')}", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"ERROR ({resp.status_code}): {resp.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
