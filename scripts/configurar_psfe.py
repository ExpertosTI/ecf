#!/usr/bin/env python3
"""
configurar_psfe.py — Configura los certificados PSFE de la plataforma RENACE.

La DGII entrega tres archivos para que la plataforma (como PSFE autorizado)
pueda comunicarse con sus APIs mediante mTLS:

  1. Certificado de cliente PSFE  (psfe_cert.pem)
  2. Llave privada del cert PSFE  (psfe_key.pem)
  3. CA raíz de la DGII           (dgii_ca.pem)

Este script los convierte a Base64 y los agrega al .env automáticamente.

Uso:
    python scripts/configurar_psfe.py \\
        --cert psfe_cert.pem \\
        --key  psfe_key.pem \\
        --ca   dgii_ca.pem \\
        --env  .env
"""

import argparse
import base64
import os
import re
import sys
from pathlib import Path


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def update_env(env_path: Path, key: str, value: str) -> bool:
    """Actualiza una variable en el .env. Retorna True si la creó, False si la actualizó."""
    text = env_path.read_text()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    new_line = f"{key}={value}"

    if pattern.search(text):
        env_path.write_text(pattern.sub(new_line, text))
        return False  # updated
    else:
        with env_path.open("a") as f:
            f.write(f"\n{new_line}\n")
        return True  # created


def main():
    parser = argparse.ArgumentParser(
        description="Configura certificados PSFE de la DGII en .env",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cert", required=True, help="Certificado de cliente PSFE (.pem o .crt)")
    parser.add_argument("--key",  required=True, help="Llave privada del cert PSFE (.pem o .key)")
    parser.add_argument("--ca",   required=True, help="CA raíz de la DGII (.pem o .crt)")
    parser.add_argument("--env",  default=".env", help="Ruta al archivo .env (default: .env)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Muestra los valores sin modificar el .env")
    args = parser.parse_args()

    cert_path = Path(args.cert)
    key_path  = Path(args.key)
    ca_path   = Path(args.ca)
    env_path  = Path(args.env)

    # Validaciones
    for p in [cert_path, key_path, ca_path]:
        if not p.exists():
            print(f"ERROR: Archivo no encontrado: {p}", file=sys.stderr)
            sys.exit(1)

    if not env_path.exists():
        print(f"ERROR: .env no encontrado en {env_path}", file=sys.stderr)
        print("  Ejecuta primero: bash scripts/setup.sh", file=sys.stderr)
        sys.exit(1)

    # Convertir a Base64
    cert_b64 = b64(cert_path)
    key_b64  = b64(key_path)
    ca_b64   = b64(ca_path)

    if args.dry_run:
        print()
        print("[DRY RUN] Valores que se escribirían en .env:")
        print()
        print(f"  PSFE_CERT_B64={cert_b64[:40]}...  ({len(cert_b64)} chars)")
        print(f"  PSFE_KEY_B64= {key_b64[:40]}...  ({len(key_b64)} chars)")
        print(f"  DGII_CA_B64=  {ca_b64[:40]}...  ({len(ca_b64)} chars)")
        print()
        return

    # Escribir en .env
    fields = {
        "PSFE_CERT_B64": cert_b64,
        "PSFE_KEY_B64":  key_b64,
        "DGII_CA_B64":   ca_b64,
    }

    print()
    print("=" * 60)
    print("  Configurando certificados PSFE en .env")
    print("=" * 60)
    print()

    for key, value in fields.items():
        created = update_env(env_path, key, value)
        status = "CREADO" if created else "ACTUALIZADO"
        print(f"  [{status}] {key}")

    print()
    print("  Certificados configurados. Para aplicar los cambios:")
    print("    docker compose up -d --force-recreate api worker")
    print()
    print("  IMPORTANTE:")
    print("    - El .env contiene llaves privadas. Nunca comitearlo.")
    print("    - Mantener un backup cifrado del .env fuera del servidor.")
    print()


if __name__ == "__main__":
    main()
