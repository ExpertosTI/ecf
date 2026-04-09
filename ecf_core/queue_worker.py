"""
Queue Worker — Procesamiento asíncrono de e-CF con Redis
Implementa cola persistente, reintentos con backoff, DLQ y callbacks a Odoo.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import httpx
import redis.asyncio as aioredis

from ecf_core.ecf_core_service import ECFCoreService, FacturaECF, ItemECF
from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.dgii_client import (
    DGIIClient, EstadoDGII, DGIIClientError,
    generar_security_code, generar_qr_url,
)

import re

logger = logging.getLogger(__name__)

# Schema name validation (prevent SQL injection)
_SAFE_SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
_SCHEMA_BLACKLIST = frozenset({
    "public", "pg_catalog", "pg_toast", "information_schema",
    "pg_temp", "pg_toast_temp",
})


def _safe_schema(name: str) -> str:
    """Validate and return schema name. Raises ValueError if unsafe."""
    if not _SAFE_SCHEMA_RE.match(name):
        raise ValueError(f"Invalid schema name: {name!r}")
    if name in _SCHEMA_BLACKLIST:
        raise ValueError(f"Reserved schema name: {name!r}")
    return name

QUEUE_ECF_PENDING = "ecf:pending"       # Cola principal
QUEUE_ECF_RETRY   = "ecf:retry"         # Cola de reintentos
QUEUE_ECF_DLQ     = "ecf:dlq"           # Dead letter queue (fallos definitivos)
MAX_INTENTOS      = 5
RETRY_DELAYS      = [30, 120, 300, 900, 3600]   # segundos entre reintentos


class ECFQueueWorker:
    """
    Worker que consume la cola Redis y procesa cada e-CF.
    Diseñado para ejecutarse como proceso independiente.
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        db_pool: asyncpg.Pool,
        cert_repo: CertVaultRepository,
        ecf_service: ECFCoreService,
    ):
        self.redis       = redis
        self.db          = db_pool
        self.cert_repo   = cert_repo
        self.ecf_service = ecf_service
        self.vault       = cert_repo.vault  # CertVault — para descifrar campos sensibles
        self.running     = True

    async def run(self):
        """Loop principal del worker. Termina cuando self.running = False."""
        logger.info("ECF Queue Worker iniciado")
        while self.running:
            try:
                # Process retries first, then pending queue
                await self._procesar_reintentos()
                await self._procesar_cola(QUEUE_ECF_PENDING)
            except Exception as e:
                logger.exception("Error en el worker: %s", e)
                if self.running:
                    await asyncio.sleep(5)
        logger.info("ECF Queue Worker detenido")

    async def _procesar_cola(self, queue: str):
        """Procesa mensajes de la cola principal bloqueando hasta que haya uno."""
        result = await self.redis.blpop(queue, timeout=5)
        if not result:
            return
        _, mensaje_json = result
        mensaje = json.loads(mensaje_json)
        await self._procesar_mensaje(mensaje)

    async def _procesar_reintentos(self):
        """Revisa la cola de reintentos y encola los que ya cumplieron su delay."""
        ahora = datetime.now(timezone.utc).timestamp()
        items = await self.redis.zrangebyscore(
            QUEUE_ECF_RETRY, "-inf", ahora, start=0, num=10
        )
        for item in items:
            await self.redis.zrem(QUEUE_ECF_RETRY, item)
            await self.redis.rpush(QUEUE_ECF_PENDING, item)

    async def _procesar_mensaje(self, mensaje: dict):
        # Dispatch por tipo de mensaje
        tipo_msg = mensaje.get("tipo", "emision")
        if tipo_msg == "anulacion":
            await self._procesar_anulacion(mensaje)
            return

        tenant_id = mensaje["tenant_id"]
        ecf_id    = mensaje["ecf_id"]
        intento   = mensaje.get("intento", 1)

        logger.info("Procesando ECF %s tenant %s intento %d", ecf_id, tenant_id, intento)

        try:
            # Cargar datos del ECF desde la DB del tenant
            tenant = await self._get_tenant(tenant_id)
            ecf_data = await self._get_ecf(tenant["schema_name"], ecf_id)

            if ecf_data["estado"] in ("aprobado", "anulado"):
                logger.info("ECF %s ya en estado final %s, saltando", ecf_id, ecf_data["estado"])
                return

            # Recuperar certificado .p12 del vault
            p12_data = await self.cert_repo.obtener(tenant_id)
            p12_pass = self.vault.descifrar_campo(tenant.get("cert_password") or "").encode()

            # Construir FacturaECF desde los datos de la DB
            factura = self._construir_factura(ecf_data, tenant)

            # Procesar: generar XML, validar XSD, firmar
            cufe_secret = self.vault.descifrar_campo(tenant.get("cufe_secret") or "")
            resultado = self.ecf_service.procesar(
                factura, p12_data, p12_pass,
                clave_secreta_cufe=cufe_secret
            )

            # Enviar a DGII con autenticación por semilla
            async with DGIIClient(ambiente=tenant["ambiente"]) as dgii:
                dgii.set_certificate(p12_data, p12_pass)
                respuesta = await dgii.enviar_ecf(
                    xml_firmado=resultado["xml_firmado"],
                    rnc_emisor=tenant["rnc"],
                    tipo_ecf=factura.tipo_ecf,
                    ncf=factura.ncf,
                )

            # Generar SecurityCode y QR URL
            security_code = generar_security_code(resultado["xml_firmado"])
            qr_url = generar_qr_url(
                ambiente=tenant["ambiente"],
                rnc_emisor=tenant["rnc"],
                ncf=factura.ncf,
                total=str(factura.total),
                fecha_firma=datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S"),
                security_code=security_code,
                rnc_comprador=factura.rnc_comprador or "",
                tipo_ecf=factura.tipo_ecf,
            )

            # Persistir resultado
            await self._actualizar_ecf(
                schema     = tenant["schema_name"],
                ecf_id     = ecf_id,
                estado     = self._estado_dgii_a_local(respuesta.estado),
                cufe       = respuesta.cufe,
                xml_firmado= resultado["xml_firmado"],
                respuesta  = respuesta.raw,
                intento    = intento,
                track_id      = respuesta.track_id,
                security_code = security_code,
                qr_url        = qr_url,
            )

            # Callback a Odoo para TODOS los estados finales
            estado_local = self._estado_dgii_a_local(respuesta.estado)
            if estado_local in ("aprobado", "rechazado", "condicionado"):
                await self._callback_odoo(tenant, ecf_data, respuesta, estado_local,
                                         qr_url=qr_url)

            logger.info("ECF %s procesado: %s", ecf_id, respuesta.estado)

        except DGIIClientError as e:
            logger.warning("Error DGII en ECF %s: %s", ecf_id, e)
            await self._programar_reintento(mensaje, intento, str(e))

        except Exception as e:
            logger.exception("Error fatal procesando ECF %s: %s", ecf_id, e)
            await self._enviar_a_dlq(mensaje, str(e))
            await self._marcar_error(
                schema=mensaje.get("schema_name", ""), ecf_id=ecf_id, error=str(e)
            )

    def _estado_dgii_a_local(self, estado: EstadoDGII) -> str:
        mapping = {
            EstadoDGII.ACEPTADO:     "aprobado",
            EstadoDGII.RECHAZADO:    "rechazado",
            EstadoDGII.CONDICIONADO: "condicionado",
            EstadoDGII.PROCESANDO:   "enviado",
            EstadoDGII.RECIBIDO:     "enviado",
        }
        local = mapping.get(estado)
        if local is None:
            logger.warning("Estado DGII desconocido: %s — mapeando a 'enviado'", estado)
            return "enviado"
        return local

    async def _procesar_anulacion(self, mensaje: dict):
        """Procesa una solicitud de anulación enviando a DGII."""
        tenant_id = mensaje["tenant_id"]
        ecf_id    = mensaje["ecf_id"]
        ncf       = mensaje["ncf"]
        schema    = mensaje["schema_name"]
        intento   = mensaje.get("intento", 1)

        logger.info("Procesando anulación ECF %s NCF %s", ecf_id, ncf)

        try:
            tenant = await self._get_tenant(tenant_id)
            p12_data = await self.cert_repo.obtener(tenant_id)
            p12_pass = self.vault.descifrar_campo(tenant.get("cert_password") or "").encode()

            async with DGIIClient(ambiente=tenant["ambiente"]) as dgii:
                dgii.set_certificate(p12_data, p12_pass)
                respuesta = await dgii.anular_ecf(
                    rnc_emisor=tenant["rnc"],
                    ncf_desde=ncf,
                    ncf_hasta=ncf,
                )

            # Marcar como anulado SOLO si DGII aceptó; revertir si rechazó
            estado_final = "anulado" if respuesta.estado in (EstadoDGII.ACEPTADO,) else "anulacion_fallida"
            s = _safe_schema(schema)
            async with self.db.acquire() as conn:
                await conn.execute(
                    f"UPDATE {s}.ecf SET estado = $1, respuesta_dgii = $2, updated_at = NOW() WHERE id = $3",
                    estado_final, json.dumps(respuesta.raw), uuid.UUID(ecf_id),
                )
                await conn.execute(
                    f"INSERT INTO {s}.ecf_estado_log (ecf_id, estado_prev, estado_new, detalle) "
                    f"VALUES ($1, 'anulacion_pendiente', $2, $3)",
                    uuid.UUID(ecf_id), estado_final,
                    f"DGII: {respuesta.estado.value} — {respuesta.mensaje}",
                )

            # Callback a Odoo con el estado final real
            if tenant.get("odoo_webhook_url"):
                ecf_data = await self._get_ecf(schema, ecf_id)
                await self._callback_odoo(tenant, ecf_data, respuesta, estado_final)

            logger.info("Anulación DGII completada: NCF %s track_id=%s", ncf, respuesta.track_id)

        except DGIIClientError as e:
            logger.warning("Error DGII en anulación NCF %s: %s", ncf, e)
            await self._programar_reintento(mensaje, intento, str(e))

        except Exception as e:
            logger.exception("Error fatal en anulación NCF %s: %s", ncf, e)
            await self._enviar_a_dlq(mensaje, str(e))

    async def _programar_reintento(self, mensaje: dict, intento: int, error: str):
        if intento >= MAX_INTENTOS:
            logger.error("ECF %s alcanzó máximo de reintentos. Enviando a DLQ.", mensaje["ecf_id"])
            await self._enviar_a_dlq(mensaje, error)
            return

        delay = RETRY_DELAYS[min(intento - 1, len(RETRY_DELAYS) - 1)]
        mensaje["intento"] = intento + 1
        mensaje["ultimo_error"] = error

        score = datetime.now(timezone.utc).timestamp() + delay
        await self.redis.zadd(QUEUE_ECF_RETRY, {json.dumps(mensaje): score})
        logger.info("ECF %s programado para reintento en %ds", mensaje["ecf_id"], delay)

    async def _enviar_a_dlq(self, mensaje: dict, error: str):
        mensaje["dlq_error"] = error
        mensaje["dlq_at"] = datetime.now(timezone.utc).isoformat()
        await self.redis.rpush(QUEUE_ECF_DLQ, json.dumps(mensaje))

    async def _callback_odoo(self, tenant: dict, ecf_data: dict, respuesta, estado_local: str,
                              qr_url: str = None):
        """Notifica a Odoo el resultado mediante webhook firmado con HMAC-SHA256."""
        if not tenant.get("odoo_webhook_url"):
            return

        payload = json.dumps({
            "odoo_move_id": ecf_data.get("odoo_move_id"),
            "ncf":          ecf_data["ncf"],
            "cufe":         respuesta.cufe,
            "estado":       estado_local,
            "qr_code":      qr_url or respuesta.qr_code,
            "error_msg":    respuesta.mensaje if estado_local != "aprobado" else None,
            "detalles":     respuesta.detalles if estado_local != "aprobado" else [],
            "timestamp":    datetime.now(timezone.utc).isoformat(),
        }).encode()

        # Firma HMAC-SHA256 para que Odoo verifique la autenticidad
        webhook_secret = self.vault.descifrar_campo(tenant.get("odoo_webhook_secret") or "")
        if not webhook_secret:
            logger.error("Webhook secret vacío para tenant %s — callback abortado", tenant["rnc"])
            return
        firma = hmac.new(webhook_secret.encode(), payload, hashlib.sha256).hexdigest()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    tenant["odoo_webhook_url"],
                    content=payload,
                    headers={
                        "Content-Type":         "application/json",
                        "X-ECF-Signature":      firma,
                        "X-ECF-Tenant-RNC":     tenant["rnc"],
                    }
                )
                resp.raise_for_status()
            logger.info("Callback enviado a Odoo para move %s", ecf_data.get("odoo_move_id"))
        except Exception as e:
            logger.warning("Falló callback a Odoo (reintentando 1 vez): %s", e)
            try:
                await asyncio.sleep(2)
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        tenant["odoo_webhook_url"],
                        content=payload,
                        headers={
                            "Content-Type":         "application/json",
                            "X-ECF-Signature":      firma,
                            "X-ECF-Tenant-RNC":     tenant["rnc"],
                        }
                    )
                    resp.raise_for_status()
                logger.info("Callback a Odoo exitoso en reintento para move %s", ecf_data.get("odoo_move_id"))
            except Exception as e2:
                logger.error("Reintento callback a Odoo falló: %s", e2)

    def _construir_factura(self, ecf_data: dict, tenant: dict) -> FacturaECF:
        from decimal import Decimal
        raw_items = ecf_data.get("items") or []
        items = [
            ItemECF(
                linea                   = i["linea"],
                descripcion             = i["descripcion"],
                cantidad                = Decimal(str(i["cantidad"])),
                precio_unitario         = Decimal(str(i["precio_unitario"])),
                descuento               = Decimal(str(i.get("descuento", "0"))),
                itbis_tasa              = Decimal(str(i.get("itbis_tasa", "18"))),
                unidad                  = i.get("unidad", "Unidad"),
                indicador_bien_servicio = int(i.get("indicador_bien_servicio", 2)),
            )
            for i in raw_items if i is not None
        ]
        from datetime import date
        return FacturaECF(
            tipo_ecf                = ecf_data["tipo_ecf"],
            ncf                     = ecf_data["ncf"],
            rnc_emisor              = tenant["rnc"],
            razon_social_emisor     = tenant["razon_social"],
            direccion_emisor        = tenant.get("direccion") or "",
            fecha_emision           = date.fromisoformat(str(ecf_data["fecha_emision"])),
            rnc_comprador           = ecf_data.get("rnc_comprador"),
            nombre_comprador        = ecf_data.get("nombre_comprador"),
            tipo_rnc_comprador      = ecf_data.get("tipo_rnc_comprador", "1"),
            items                   = items,
            ncf_referencia          = ecf_data.get("referencia_ncf"),
            fecha_ncf_referencia    = date.fromisoformat(str(ecf_data["fecha_ncf_referencia"])) if ecf_data.get("fecha_ncf_referencia") else None,
            codigo_modificacion     = ecf_data.get("codigo_modificacion", "1"),
            tipo_pago               = ecf_data.get("tipo_pago", "1"),
            tipo_ingresos           = ecf_data.get("tipo_ingresos", "01"),
            indicador_envio_diferido = int(ecf_data.get("indicador_envio_diferido", 0)),
            nombre_comercial        = tenant.get("nombre_comercial"),
            municipio               = tenant.get("municipio"),
            provincia               = tenant.get("provincia"),
            direccion_comprador     = ecf_data.get("direccion_comprador"),
            moneda                  = ecf_data.get("moneda", "DOP"),
            tipo_cambio             = Decimal(str(ecf_data.get("tipo_cambio", "1"))),
        )

    async def _get_tenant(self, tenant_id: str) -> dict:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, rnc, razon_social, nombre_comercial, direccion, "
                "schema_name, ambiente, estado, cert_password, cufe_secret, "
                "odoo_webhook_url, odoo_webhook_secret "
                "FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
                uuid.UUID(tenant_id)
            )
        if not row:
            raise ValueError(f"Tenant no encontrado: {tenant_id}")
        return dict(row)

    async def _get_ecf(self, schema: str, ecf_id: str) -> dict:
        s = _safe_schema(schema)
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT e.*, array_agg(row_to_json(i)) AS items '
                f'FROM {s}.ecf e '
                f'LEFT JOIN {s}.ecf_items i ON i.ecf_id = e.id '
                f'WHERE e.id = $1 GROUP BY e.id',
                uuid.UUID(ecf_id)
            )
        return dict(row) if row else {}

    async def _actualizar_ecf(self, schema, ecf_id, estado, cufe, xml_firmado, respuesta, intento,
                              track_id=None, security_code=None, qr_url=None):
        s = _safe_schema(schema)
        async with self.db.acquire() as conn:
            await conn.execute(f"""
                UPDATE {s}.ecf SET
                    estado          = $1,
                    cufe            = COALESCE($2, cufe),
                    xml_firmado     = COALESCE($3, xml_firmado),
                    respuesta_dgii  = $4,
                    intentos_envio  = $5,
                    track_id        = COALESCE($6, track_id),
                    security_code   = COALESCE($7, security_code),
                    qr_url          = COALESCE($8, qr_url),
                    sent_at         = CASE WHEN sent_at IS NULL THEN NOW() ELSE sent_at END,
                    approved_at     = CASE WHEN $1 = 'aprobado' THEN NOW() ELSE NULL END,
                    updated_at      = NOW()
                WHERE id = $9
            """, estado, cufe, xml_firmado, json.dumps(respuesta), intento,
                track_id, security_code, qr_url, uuid.UUID(ecf_id))

    async def _marcar_error(self, schema, ecf_id, error):
        if not schema:
            return
        s = _safe_schema(schema)
        async with self.db.acquire() as conn:
            await conn.execute(
                f"UPDATE {s}.ecf SET ultimo_error=$1, updated_at=NOW() WHERE id=$2"
                f"  AND estado NOT IN ('aprobado', 'anulado')",
                error, uuid.UUID(ecf_id)
            )
