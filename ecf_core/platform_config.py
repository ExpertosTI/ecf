"""
Configuración de plataforma RENECF — certificados PSFE (mTLS DGII).
Almacenamiento cifrado en PostgreSQL; fallback a variables .env.

Nombre de software registrado ante la DGII: RENECF (debe coincidir en postulación).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import NamedTuple

import asyncpg

from ecf_core.cert_vault import CertVault, CertVaultError

logger = logging.getLogger(__name__)

# Identidad del software ante DGII (Paso 1 postulación / PSFE).
# Debe coincidir exactamente con el nombre registrado en dgii.gov.do.
DGII_SOFTWARE_NAME = os.environ.get("DGII_SOFTWARE_NAME", "RENECF").strip() or "RENECF"
# XSD DGII: VersionSoftware es xs:double → un solo punto decimal (ej. 2.5), no semver 2.5.0
DGII_SOFTWARE_VERSION = os.environ.get("DGII_SOFTWARE_VERSION", "2.5").strip() or "2.5"
DGII_SOFTWARE_TIPO = os.environ.get("DGII_SOFTWARE_TIPO", "PROPIO").strip() or "PROPIO"


def software_identity() -> dict:
    """Datos oficiales del software para postulación DGII y UI."""
    return {
        "nombre": DGII_SOFTWARE_NAME,
        "version": DGII_SOFTWARE_VERSION,
        "tipo": DGII_SOFTWARE_TIPO,
    }


class PSFECredentials(NamedTuple):
    cert_b64: str
    key_b64: str
    ca_b64: str

    @property
    def configured(self) -> bool:
        return bool(self.cert_b64 and self.key_b64 and self.ca_b64)


_cache: PSFECredentials | None = None


def invalidate_psfe_cache() -> None:
    global _cache
    _cache = None


def _from_env() -> PSFECredentials:
    return PSFECredentials(
        os.environ.get("PSFE_CERT_B64", ""),
        os.environ.get("PSFE_KEY_B64", ""),
        os.environ.get("DGII_CA_B64", ""),
    )


def get_psfe_credentials() -> PSFECredentials:
    """Credenciales PSFE: caché DB → variables de entorno."""
    if _cache is not None:
        return _cache
    return _from_env()


async def load_psfe_from_db(pool: asyncpg.Pool) -> bool:
    """Carga PSFE desde platform_psfe. Retorna True si hay registro en DB."""
    global _cache
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT payload_enc, iv, tag FROM public.platform_psfe WHERE id = 1"
            )
    except asyncpg.UndefinedTableError:
        logger.warning("Tabla platform_psfe no existe — ejecutar db/012_platform_psfe.sql")
        return False

    if not row:
        _cache = None
        return False

    try:
        vault = CertVault()
        plaintext = vault.descifrar(row["payload_enc"], row["iv"], row["tag"])
        data = json.loads(plaintext.decode("utf-8"))
        _cache = PSFECredentials(
            data.get("cert_b64", ""),
            data.get("key_b64", ""),
            data.get("ca_b64", ""),
        )
        logger.info("PSFE cargado desde base de datos (platform_psfe)")
        return _cache.configured
    except (CertVaultError, json.JSONDecodeError, KeyError) as exc:
        logger.error("No se pudo descifrar platform_psfe: %s", exc)
        _cache = None
        return False


async def save_psfe_to_db(
    pool: asyncpg.Pool,
    cert_pem: bytes,
    key_pem: bytes,
    ca_pem: bytes,
) -> None:
    """Guarda certificado PSFE cifrado (singleton id=1)."""
    payload = json.dumps(
        {
            "cert_b64": base64.b64encode(cert_pem).decode("ascii"),
            "key_b64": base64.b64encode(key_pem).decode("ascii"),
            "ca_b64": base64.b64encode(ca_pem).decode("ascii"),
        }
    ).encode("utf-8")

    vault = CertVault()
    ct, iv, tag = vault.cifrar(payload)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.platform_psfe (id, payload_enc, iv, tag, updated_at)
            VALUES (1, $1, $2, $3, NOW())
            ON CONFLICT (id) DO UPDATE
                SET payload_enc = EXCLUDED.payload_enc,
                    iv = EXCLUDED.iv,
                    tag = EXCLUDED.tag,
                    updated_at = NOW()
            """,
            ct,
            iv,
            tag,
        )

    invalidate_psfe_cache()
    await load_psfe_from_db(pool)


async def signal_psfe_reload(redis) -> None:
    """Avisa a workers/scheduler que deben recargar PSFE desde DB."""
    if redis is None:
        return
    try:
        await redis.set("ecf:psfe:reload", "1", ex=86400)
    except Exception as exc:
        logger.warning("No se pudo señalizar reload PSFE en Redis: %s", exc)


async def maybe_reload_psfe_from_redis(pool: asyncpg.Pool, redis) -> bool:
    """Si hay bandera Redis, recarga PSFE. Retorna True si recargó."""
    if redis is None:
        return False
    try:
        flag = await redis.get("ecf:psfe:reload")
        if not flag:
            return False
        await load_psfe_from_db(pool)
        await redis.delete("ecf:psfe:reload")
        logger.info("PSFE recargado desde DB por señal Redis")
        return True
    except Exception as exc:
        logger.warning("Reload PSFE vía Redis falló: %s", exc)
        return False


async def psfe_status(pool: asyncpg.Pool) -> dict:
    """Estado PSFE sin exponer secretos."""
    await load_psfe_from_db(pool)
    creds = get_psfe_credentials()
    source = "database" if _cache is not None else ("env" if creds.configured else "none")
    try:
        async with pool.acquire() as conn:
            updated = await conn.fetchval(
                "SELECT updated_at FROM public.platform_psfe WHERE id = 1"
            )
    except asyncpg.UndefinedTableError:
        updated = None

    return {
        "configured": creds.configured,
        "source": source,
        "updated_at": updated.isoformat() if updated else None,
    }
