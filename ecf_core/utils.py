"""Helpers compartidos entre api_gateway y ecf_core."""

from __future__ import annotations

import base64
import os
import re
from decimal import Decimal

_SAFE_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_SCHEMA_BLACKLIST = frozenset({
    "public", "pg_catalog", "pg_toast", "information_schema",
    "pg_temp", "pg_toast_temp",
})


def safe_schema(name: str) -> str:
    """Valida que un nombre de schema sea seguro para interpolar en SQL.

    Lanza ``ValueError`` si el nombre no cumple el patrón o pertenece a la
    blacklist de schemas de PostgreSQL.
    """
    if not isinstance(name, str) or not _SAFE_SCHEMA_RE.match(name):
        raise ValueError(f"Invalid schema name: {name!r}")
    if name in _SCHEMA_BLACKLIST:
        raise ValueError(f"Reserved schema name: {name!r}")
    return name


# RNC validation -------------------------------------------------------------
#
# Algoritmo oficial DGII para validación de RNC (9 dígitos) y Cédula (11 dígitos).
# Mod-11 con pesos específicos. Si la suma cae en residuo 0 o 1 el dígito
# verificador se fuerza a 1 ó 0 respectivamente.

_RNC_WEIGHTS = (7, 9, 8, 6, 5, 4, 3, 2)
_CEDULA_WEIGHTS = (1, 2, 1, 2, 1, 2, 1, 2, 1, 2)


def validar_rnc_dgii(rnc: str) -> bool:
    """Valida un RNC dominicano de 9 dígitos (algoritmo oficial DGII)."""
    if not isinstance(rnc, str) or len(rnc) != 9 or not rnc.isdigit():
        return False
    cuerpo = rnc[:8]
    digito_check = int(rnc[8])
    suma = sum(int(d) * w for d, w in zip(cuerpo, _RNC_WEIGHTS, strict=True))
    residuo = suma % 11
    if residuo == 0:
        esperado = 2
    elif residuo == 1:
        esperado = 1
    else:
        esperado = 11 - residuo
    return esperado == digito_check


def validar_cedula_dgii(cedula: str) -> bool:
    """Valida una Cédula dominicana de 11 dígitos (algoritmo Luhn variante)."""
    if not isinstance(cedula, str) or len(cedula) != 11 or not cedula.isdigit():
        return False
    suma = 0
    for i, peso in enumerate(_CEDULA_WEIGHTS[:10]):
        producto = int(cedula[i]) * peso
        if producto > 9:
            producto -= 9
        suma += producto
    digito = (10 - (suma % 10)) % 10
    return digito == int(cedula[10])


def validar_rnc_o_cedula(documento: str) -> bool:
    """True si el documento (9 ó 11 dígitos) es un RNC o Cédula válido."""
    if not documento:
        return False
    if len(documento) == 9:
        return validar_rnc_dgii(documento)
    if len(documento) == 11:
        return validar_cedula_dgii(documento)
    return False


# MFA secret encryption (AES-256-GCM) ----------------------------------------
#
# Usa VAULT_MASTER_KEY (32 bytes en base64) para cifrar el TOTP secret antes de
# persistirlo en portal_users.mfa_secret_enc. El valor almacenado es base64url
# del formato: nonce(12B) || ciphertext || tag(16B).


def _get_vault_key() -> bytes:
    raw = os.environ.get("VAULT_MASTER_KEY", "")
    if not raw:
        raise RuntimeError("VAULT_MASTER_KEY no configurada")
    key = base64.b64decode(raw)
    if len(key) != 32:
        raise RuntimeError("VAULT_MASTER_KEY debe ser exactamente 32 bytes en base64")
    return key


def encrypt_mfa_secret(plaintext: str) -> str:
    """Cifra un TOTP secret con AES-256-GCM. Devuelve base64url."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _get_vault_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_mfa_secret(token: str) -> str:
    """Descifra un TOTP secret cifrado con ``encrypt_mfa_secret``."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = _get_vault_key()
    raw = base64.urlsafe_b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(key).decrypt(nonce, ct, None).decode()


# Decimal helpers ------------------------------------------------------------

TWO_PLACES = Decimal("0.01")


def q2(d: Decimal | int | float | str) -> Decimal:
    """Cuantiza un valor a 2 decimales (HALF_UP) — usado en montos DGII."""
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    return d.quantize(TWO_PLACES)
