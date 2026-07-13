"""
Scheduler — Jobs periódicos del sistema.
- Alerta de vencimiento de certificados (diario)
- Reset de contadores mensuales de e-CF (1ro de cada mes)
- Sincronización de e-CF Recibidas desde DGII (cada 30 minutos)
- Reconciliación RFCE (facturas consumo < 250k sin resumen enviado)
- Polling de e-CF en estado 'enviado' con track_id (EnProceso en DGII)
- Re-encolado de e-CF 'pendiente' huérfanos (falla post-commit de Redis)
- Alerta de DLQ con elementos acumulados

Ejecutar: python -m ecf_core.scheduler
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

import asyncpg
import redis.asyncio as aioredis

from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.dgii_client import DGIIClient, generar_qr_url
from ecf_core.ecf_recibidas_service import ECFRecibidasService
from ecf_core.odoo_webhook import notify_odoo_ecf_result
from ecf_core.rfce_service import RFCEService
from ecf_core.utils import safe_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL        = int(os.environ.get("SCHEDULER_CHECK_INTERVAL", "120"))  # poll/requeue default 2 min
RECIBIDAS_INTERVAL    = 1800   # 30 minutos para sync e-CF recibidas
CERT_ALERT_INTERVAL   = 3600   # alertas de cert / reset mensual
_last_recibidas_sync  = 0.0    # timestamp de la última sync
_last_cert_jobs       = 0.0


def _send_email_sync(smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str, msg: MIMEText):
    """Realiza la llamada bloqueante de SMTP síncronamente."""
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        if smtp_user and smtp_pass:
            server.login(smtp_user, smtp_pass)
        server.send_message(msg)


async def alertar_vencimientos(db_pool: asyncpg.Pool):
    """Envía alertas por email a tenants con certificados por vencer (< 30 días)."""
    vault = CertVault()
    repo = CertVaultRepository(db_pool, vault)
    tenants = await repo.verificar_vencimientos(db_pool)

    if not tenants:
        logger.info("No hay certificados por vencer")
        return

    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    from_email = os.environ.get("ALERT_FROM_EMAIL", "no-reply@ecf-saas.do")

    if not smtp_host:
        logger.warning("SMTP no configurado, omitiendo alertas por email")
        return

    for t in tenants:
        dias = (t["cert_vencimiento"] - datetime.now(timezone.utc).date()).days
        asunto = f"[Renace e-CF] Certificado .p12 vence en {dias} días - {t['razon_social']}"
        cuerpo = (
            f"Estimado {t['razon_social']} (RNC: {t['rnc']}),\n\n"
            f"Su certificado digital (.p12) registrado en Renace e-CF vence el "
            f"{t['cert_vencimiento']}.\n\n"
            f"Debe renovarlo ante la DGII antes de esa fecha para evitar "
            f"interrupciones en la emisión de e-CF.\n\n"
            f"Atentamente,\nRenace e-CF"
        )

        try:
            msg = MIMEText(cuerpo, "plain", "utf-8")
            msg["Subject"] = asunto
            msg["From"] = from_email
            msg["To"] = t["email"]

            await asyncio.to_thread(_send_email_sync, smtp_host, smtp_port, smtp_user, smtp_pass, msg)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE public.tenants SET cert_alerta_enviada = TRUE WHERE id = $1",
                    t["id"],
                )
            logger.info("Alerta enviada a %s (%s)", t["razon_social"], t["email"])

        except Exception as e:
            logger.error("Error enviando alerta a %s (reintentando 1 vez): %s", t["email"], e)
            try:
                await asyncio.to_thread(_send_email_sync, smtp_host, smtp_port, smtp_user, smtp_pass, msg)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE public.tenants SET cert_alerta_enviada = TRUE WHERE id = $1",
                        t["id"],
                    )
                logger.info("Alerta enviada a %s en reintento", t["razon_social"])
            except Exception as e2:
                logger.error("Reintento SMTP fallido para %s: %s", t["email"], e2)


async def reset_contadores_mensuales(db_pool: asyncpg.Pool):
    """Resetea contadores de e-CF emitidos el primer dia de cada mes (una sola vez)."""
    ahora = datetime.now(timezone.utc)
    if ahora.day != 1:
        return

    async with db_pool.acquire() as conn:
        # Evitar spam horario: el día 1 el scheduler corre cada hora; solo un reset/mes.
        ya_reseteado = await conn.fetchval(
            "SELECT 1 FROM public.system_audit_log "
            "WHERE accion = 'reset.contadores_mensuales' "
            "  AND created_at >= date_trunc('month', NOW() AT TIME ZONE 'UTC') "
            "LIMIT 1"
        )
        if ya_reseteado:
            return

        result = await conn.execute(
            "UPDATE public.tenants SET ecf_emitidos_mes = 0, cert_alerta_enviada = FALSE "
            "WHERE deleted_at IS NULL"
        )
        await conn.execute(
            "INSERT INTO public.system_audit_log (tenant_id, accion, entidad, detalle) "
            "VALUES (NULL, 'reset.contadores_mensuales', 'tenants', $1::jsonb)",
            json.dumps({"result": result}),
        )
    logger.info("Contadores mensuales reseteados: %s", result)


async def sincronizar_ecf_recibidas(db_pool: asyncpg.Pool):
    """
    Sincroniza e-CF recibidas desde la DGII para todos los tenants activos.
    Se ejecuta cada 30 minutos. Usa certificados del Cert Vault.
    """
    global _last_recibidas_sync
    import time
    ahora = time.monotonic()
    if ahora - _last_recibidas_sync < RECIBIDAS_INTERVAL:
        return

    logger.info("Iniciando sincronización de e-CF Recibidas para todos los tenants...")
    vault = CertVault()
    cert_repo = CertVaultRepository(db_pool, vault)
    servicio = ECFRecibidasService(db_pool, cert_repo)

    try:
        resultados = await servicio.sincronizar_todos_los_tenants()
        total_nuevos = sum(r.nuevos for r in resultados)
        total_errores = sum(r.errores for r in resultados)
        logger.info(
            "e-CF Recibidas sync: %d tenants procesados, %d nuevas, %d errores",
            len(resultados), total_nuevos, total_errores
        )
        _last_recibidas_sync = ahora
    except Exception as e:
        logger.exception("Error en sincronización de e-CF recibidas: %s", e)


async def alertar_ncf_secuencias(db_pool: asyncpg.Pool, umbral: int = 1000):
    """Alerta al equipo cuando una secuencia NCF tiene < ``umbral`` disponibles.

    Critical para DGII: emitir un NCF más allá de ``secuencia_max`` rompe
    homologación.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.rnc, t.razon_social, t.email, s.tipo_ecf,
                   s.secuencia_actual, s.secuencia_max,
                   (s.secuencia_max - s.secuencia_actual) AS disponibles
            FROM public.ncf_sequences s
            JOIN public.tenants t ON t.id = s.tenant_id
            WHERE s.activo = TRUE
              AND t.deleted_at IS NULL
              AND t.estado = 'activo'
              AND (s.secuencia_max - s.secuencia_actual) < $1
            ORDER BY disponibles ASC
            """,
            umbral,
        )
    for row in rows:
        logger.warning(
            "[NCF-SEQ] %s · E%s: %d/%d disponibles",
            row["rnc"], row["tipo_ecf"], row["disponibles"], row["secuencia_max"],
        )


async def alertar_dlq(redis: aioredis.Redis | None, db_pool: asyncpg.Pool):
    """Alerta cuando la DLQ de Redis tiene elementos acumulados."""
    if redis is None:
        return
    try:
        dlq_len = await redis.llen("ecf:dlq")
    except Exception as e:
        logger.warning("No se pudo consultar la DLQ: %s", e)
        return
    if not dlq_len:
        return
    logger.warning("[DLQ] %d e-CF en Dead Letter Queue — requieren intervención", dlq_len)
    try:
        async with db_pool.acquire() as conn:
            # Máximo una alerta cada 24h en el audit log
            reciente = await conn.fetchval(
                "SELECT 1 FROM public.system_audit_log "
                "WHERE accion = 'alerta.dlq' AND created_at >= NOW() - INTERVAL '24 hours' "
                "LIMIT 1"
            )
            if not reciente:
                await conn.execute(
                    "INSERT INTO public.system_audit_log (tenant_id, accion, entidad, detalle) "
                    "VALUES (NULL, 'alerta.dlq', 'ecf', $1::jsonb)",
                    json.dumps({"cantidad": dlq_len}),
                )
    except Exception as e:
        logger.warning("No se pudo registrar alerta DLQ: %s", e)


async def reencolar_pendientes(redis: aioredis.Redis | None, db_pool: asyncpg.Pool):
    """Re-encola e-CF 'pendiente' huérfanos (> 10 min sin procesar).

    Cubre la ventana post-commit: el gateway asigna NCF + INSERT en la
    transacción y encola en Redis después; si Redis falla en ese instante,
    el e-CF quedaría 'pendiente' para siempre sin este job.
    """
    if redis is None:
        return
    async with db_pool.acquire() as conn:
        tenants = await conn.fetch(
            "SELECT id, schema_name FROM public.tenants "
            "WHERE estado = 'activo' AND deleted_at IS NULL"
        )
        for t in tenants:
            try:
                schema = safe_schema(t["schema_name"])
                rows = await conn.fetch(
                    f"SELECT id, ncf, tipo_ecf FROM {schema}.ecf "
                    f"WHERE ("
                    f"  (estado = 'pendiente' AND created_at < NOW() - INTERVAL '10 minutes'"
                    f"   AND (ultimo_error IS NULL OR intentos_envio < 5))"
                    f"  OR (estado = 'enviado' AND track_id IS NULL "
                    f"      AND updated_at < NOW() - INTERVAL '10 minutes')"
                    f") "
                    f"LIMIT 50"
                )
            except Exception as e:
                logger.warning("reencolar_pendientes: schema %s: %s", t["schema_name"], e)
                continue
            for row in rows:
                mensaje = json.dumps({
                    "ecf_id":      str(row["id"]),
                    "tenant_id":   str(t["id"]),
                    "schema_name": schema,
                    "ncf":         row["ncf"],
                    "tipo_ecf":    row["tipo_ecf"],
                    "intento":     1,
                    "enqueued_at": datetime.now(timezone.utc).isoformat(),
                    "requeued_by": "scheduler",
                })
                await redis.rpush("ecf:pending", mensaje)
                logger.info("Re-encolado e-CF huérfano NCF=%s (tenant %s)", row["ncf"], t["id"])


async def poll_ecf_en_proceso(db_pool: asyncpg.Pool, redis=None):
    """Consulta por track_id los e-CF que quedaron 'enviado' (EnProceso DGII).

    Sin este polling, un e-CF que la DGII deja EnProceso nunca alcanza su
    estado final (Aceptado/Rechazado) en la plataforma. Al resolver el estado
    notifica a Odoo vía webhook (mismo flujo que el queue_worker).
    """
    vault = CertVault()
    cert_repo = CertVaultRepository(db_pool, vault)

    async with db_pool.acquire() as conn:
        tenants = await conn.fetch(
            "SELECT id, rnc, schema_name, ambiente, cert_password, "
            "odoo_webhook_url, odoo_webhook_secret "
            "FROM public.tenants WHERE estado = 'activo' AND deleted_at IS NULL"
        )

    for t in tenants:
        try:
            schema = safe_schema(t["schema_name"])
        except ValueError:
            continue
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT id, ncf, track_id, odoo_move_id, codigo_seguridad, "
                f"tipo_ecf, total, qr_url, security_code, fecha_emision "
                f"FROM {schema}.ecf "
                f"WHERE estado = 'enviado' AND track_id IS NOT NULL "
                f"  AND updated_at < NOW() - INTERVAL '5 minutes' "
                f"LIMIT 20"
            )
        if not rows:
            continue
        try:
            p12_data = await cert_repo.obtener(str(t["id"]))
            p12_pass = vault.descifrar_campo(t["cert_password"] or "").encode()
        except Exception as e:
            logger.warning("poll_en_proceso: sin certificado para %s: %s", t["rnc"], e)
            continue

        try:
            async with DGIIClient(ambiente=t["ambiente"]) as dgii:
                dgii.set_certificate(p12_data, p12_pass)
                for row in rows:
                    try:
                        resp = await dgii.consultar_por_track_id(row["track_id"])
                        estado_local = {
                            "Aceptado":           "aprobado",
                            "AceptadoCondicional": "condicionado",
                            "Rechazado":          "rechazado",
                        }.get(resp.estado.value)
                        if not estado_local:
                            continue  # sigue EnProceso
                        async with db_pool.acquire() as conn:
                            await conn.execute(
                                f"UPDATE {schema}.ecf SET estado=$1, respuesta_dgii=$2::jsonb, "
                                f"codigo_seguridad = COALESCE($4, codigo_seguridad), "
                                f"approved_at = CASE WHEN $1='aprobado' THEN NOW() ELSE approved_at END, "
                                f"updated_at=NOW() WHERE id=$3",
                                estado_local, json.dumps(resp.raw), row["id"],
                                resp.codigo_seguridad or row["codigo_seguridad"],
                            )
                        logger.info(
                            "Polling DGII: NCF %s → %s (track %s)",
                            row["ncf"], estado_local, row["track_id"],
                        )
                        if t.get("odoo_webhook_url"):
                            codigo = resp.codigo_seguridad or row["codigo_seguridad"] or row["security_code"]
                            qr_url = resp.qr_code or row["qr_url"]
                            if not qr_url and codigo:
                                # Preferir FechaHoraFirma del XML firmado; nunca midnight de fecha_emision
                                fecha_firma = ""
                                try:
                                    async with db_pool.acquire() as conn:
                                        xml_firmado = await conn.fetchval(
                                            f"SELECT xml_firmado FROM {schema}.ecf WHERE id = $1",
                                            row["id"],
                                        )
                                    if xml_firmado:
                                        from ecf_core.queue_worker import _extraer_fecha_firma
                                        fecha_firma = _extraer_fecha_firma(xml_firmado) or ""
                                except Exception as xml_exc:
                                    logger.warning("FechaFirma XML polling NCF %s: %s", row["ncf"], xml_exc)
                                if not fecha_firma:
                                    fecha_em = row["fecha_emision"]
                                    # Solo fecha (dd-mm-yyyy) — no inventar 00:00:00 como FechaFirma
                                    fecha_firma = fecha_em.strftime("%d-%m-%Y") if fecha_em else ""
                                fecha_emision_qr = (
                                    row["fecha_emision"].strftime("%d-%m-%Y")
                                    if row["fecha_emision"] else ""
                                )
                                try:
                                    qr_url = generar_qr_url(
                                        ambiente=t["ambiente"],
                                        rnc_emisor=t["rnc"],
                                        ncf=row["ncf"],
                                        total=str(row["total"] or "0"),
                                        fecha_firma=fecha_firma,
                                        security_code=codigo,
                                        tipo_ecf=int(row["tipo_ecf"] or 31),
                                        fecha_emision=fecha_emision_qr,
                                    )
                                except Exception as qr_exc:
                                    logger.warning("QR URL polling NCF %s: %s", row["ncf"], qr_exc)
                            await notify_odoo_ecf_result(
                                tenant=dict(t),
                                vault=vault,
                                ecf_data={"ncf": row["ncf"], "odoo_move_id": row["odoo_move_id"]},
                                estado_local=estado_local,
                                track_id=row["track_id"],
                                codigo_seguridad=codigo,
                                qr_code=qr_url,
                                error_msg=resp.mensaje if estado_local != "aprobado" else None,
                                detalles=resp.detalles if estado_local != "aprobado" else [],
                                redis=redis,
                            )
                    except Exception as e:
                        logger.warning("Polling track %s falló: %s", row["track_id"], e)
        except Exception as e:
            logger.warning("poll_en_proceso: cliente DGII para %s: %s", t["rnc"], e)


async def procesar_rfce_pendientes(db_pool: asyncpg.Pool):
    """Reconciliación RFCE: facturas de consumo < RD$250,000 sin resumen enviado.

    El flujo principal emite el RFCE en el worker al momento de la emisión;
    este job cubre históricos y fallos transitorios del host fc.dgii.gov.do.
    """
    rfce_service = RFCEService(db_pool)

    async with db_pool.acquire() as conn:
        tenants = await conn.fetch(
            "SELECT id, rnc, razon_social FROM public.tenants "
            "WHERE estado = 'activo' AND deleted_at IS NULL"
        )

    for t in tenants:
        try:
            resultado = await rfce_service.procesar_rfce_pendientes(t["id"])
            if resultado["procesados"] or resultado["errores"]:
                logger.info(
                    "RFCE reconciliación %s: %d enviados, %d errores",
                    t["rnc"], resultado["procesados"], resultado["errores"],
                )
        except Exception as e:
            logger.error("Error en reconciliación RFCE para %s: %s", t["razon_social"], e)


async def main():
    logger.info("Iniciando Renace e-CF Scheduler (cert + recibidas + ncf-seq + rfce + polling)...")

    db_pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=1,
        max_size=5,
    )

    # Redis (opcional): re-encolado de pendientes y alerta DLQ
    redis = None
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        try:
            redis = aioredis.from_url(
                redis_url,
                password=os.environ.get("REDIS_PASSWORD") or None,
                decode_responses=True,
            )
            await redis.ping()
            logger.info("Redis conectado para jobs de reconciliación")
        except Exception as exc:
            logger.warning("Redis no disponible en scheduler: %s", exc)
            redis = None

    try:
        from ecf_core.platform_config import load_psfe_from_db

        if await load_psfe_from_db(db_pool):
            logger.info("PSFE plataforma listo (DB)")
        elif os.environ.get("PSFE_CERT_B64"):
            logger.info("PSFE plataforma listo (.env)")
    except Exception as exc:
        logger.warning("PSFE startup check: %s", exc)

    jobs = (
        ("alertar_vencimientos",      lambda: alertar_vencimientos(db_pool)),
        ("reset_contadores",          lambda: reset_contadores_mensuales(db_pool)),
        ("sincronizar_recibidas",     lambda: sincronizar_ecf_recibidas(db_pool)),
        ("alertar_ncf_secuencias",    lambda: alertar_ncf_secuencias(db_pool)),
        ("reencolar_pendientes",      lambda: reencolar_pendientes(redis, db_pool)),
        ("poll_ecf_en_proceso",       lambda: poll_ecf_en_proceso(db_pool, redis)),
        ("procesar_rfce_pendientes",  lambda: procesar_rfce_pendientes(db_pool)),
        ("alertar_dlq",               lambda: alertar_dlq(redis, db_pool)),
    )

    try:
        while True:
            logger.info("Ejecutando jobs programados...")
            for nombre, job in jobs:
                try:
                    await job()
                except Exception as e:
                    # Un job caído no debe tumbar el ciclo completo
                    logger.exception("Job %s falló: %s", nombre, e)
            logger.info("Jobs completados. Próxima ejecución en %d segundos", CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)
    finally:
        if redis is not None:
            await redis.aclose()
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
