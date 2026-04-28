"""Helpers compartidos entre api_gateway y ecf_core."""

from __future__ import annotations

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
    suma = sum(int(d) * w for d, w in zip(cuerpo, _RNC_WEIGHTS))
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


# Decimal helpers ------------------------------------------------------------

TWO_PLACES = Decimal("0.01")


def q2(d: Decimal | int | float | str) -> Decimal:
    """Cuantiza un valor a 2 decimales (HALF_UP) — usado en montos DGII."""
    if not isinstance(d, Decimal):
        d = Decimal(str(d))
    return d.quantize(TWO_PLACES)
