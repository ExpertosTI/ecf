"""Notificación HMAC a Odoo — compartido entre queue_worker y scheduler."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

import httpx
import redis.asyncio as aioredis

from ecf_core.utils import normalize_odoo_webhook_url

logger = logging.getLogger(__name__)


async def notify_odoo_ecf_result(
    *,
    tenant: dict,
    vault,
    ecf_data: dict,
    estado_local: str,
    track_id: str | None = None,
    codigo_seguridad: str | None = None,
    qr_code: str | None = None,
    error_msg: str | None = None,
    detalles: list | None = None,
    redis: aioredis.Redis | None = None,
) -> bool:
    """Envía callback firmado a Odoo. Devuelve True si Odoo respondió 2xx."""
    if not tenant.get("odoo_webhook_url"):
        return False

    webhook_url = normalize_odoo_webhook_url(tenant["odoo_webhook_url"])

    payload = json.dumps({
        "odoo_move_id": ecf_data.get("odoo_move_id"),
        "external_id":  ecf_data.get("odoo_move_id"),
        "ncf":          ecf_data["ncf"],
        "codigo_seguridad": codigo_seguridad,
        "estado":       estado_local,
        "track_id":     track_id,
        "qr_code":      qr_code,
        "error_msg":    error_msg if estado_local != "aprobado" else None,
        "detalles":     detalles or [] if estado_local != "aprobado" else [],
        "timestamp":    datetime.now(timezone.utc).isoformat(),
    }).encode()

    webhook_secret = vault.descifrar_campo(tenant.get("odoo_webhook_secret") or "")
    if not webhook_secret:
        logger.error("Webhook secret vacío para tenant %s — callback abortado", tenant.get("rnc"))
        return False

    async def _post_webhook(secret: str) -> httpx.Response:
        firma = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await client.post(
                webhook_url,
                content=payload,
                headers={
                    "Content-Type":     "application/json",
                    "X-ECF-Signature":  f"sha256={firma}",
                    "X-ECF-Tenant-RNC": tenant["rnc"],
                },
            )

    try:
        resp = await _post_webhook(webhook_secret)
        resp.raise_for_status()
        logger.info("Callback enviado a Odoo para move %s", ecf_data.get("odoo_move_id"))
        return True
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403) and redis is not None:
            prev_secret = await redis.get(f"whk:prev:{tenant['id']}")
            if prev_secret:
                logger.warning(
                    "Retrying webhook con secret anterior (rotación) tenant=%s",
                    tenant.get("rnc"),
                )
                try:
                    resp2 = await _post_webhook(prev_secret)
                    resp2.raise_for_status()
                    logger.info(
                        "Callback a Odoo exitoso con secret anterior para move %s",
                        ecf_data.get("odoo_move_id"),
                    )
                    return True
                except Exception as e_prev:
                    logger.error("Retry con secret anterior también falló: %s", e_prev)
        logger.warning("Falló callback a Odoo (reintentando en 2 s): %s", e)
        await asyncio.sleep(2)
        try:
            resp3 = await _post_webhook(webhook_secret)
            resp3.raise_for_status()
            return True
        except Exception as e2:
            logger.error("Callback a Odoo falló definitivamente: %s", e2)
            return False
    except Exception as e:
        logger.error("Error enviando callback a Odoo: %s", e)
        return False
