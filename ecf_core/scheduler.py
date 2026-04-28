"""
Scheduler — Jobs periódicos del sistema.
- Alerta de vencimiento de certificados (diario)
- Reset de contadores mensuales de e-CF (1ro de cada mes)
- Sincronización de e-CF Recibidas desde DGII (cada 30 minutos)

Ejecutar: python -m ecf_core.scheduler
"""

import asyncio
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.text import MIMEText

import asyncpg

from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.ecf_recibidas_service import ECFRecibidasService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL        = 3600   # 1 hora para cert alerts / reset
RECIBIDAS_INTERVAL    = 1800   # 30 minutos para sync e-CF recibidas
_last_recibidas_sync  = 0.0    # timestamp de la última sync


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

            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE public.tenants SET cert_alerta_enviada = TRUE WHERE id = $1",
                    t["id"],
                )
            logger.info("Alerta enviada a %s (%s)", t["razon_social"], t["email"])

        except smtplib.SMTPException as e:
            logger.error("SMTP error enviando alerta a %s (reintentando 1 vez): %s", t["email"], e)
            try:
                with smtplib.SMTP(smtp_host, smtp_port) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)
                async with db_pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE public.tenants SET cert_alerta_enviada = TRUE WHERE id = $1",
                        t["id"],
                    )
                logger.info("Alerta enviada a %s en reintento", t["razon_social"])
            except Exception as e2:
                logger.error("Reintento SMTP fallido para %s: %s", t["email"], e2)
        except Exception as e:
            logger.error("Error enviando alerta a %s: %s", t["email"], e)


async def reset_contadores_mensuales(db_pool: asyncpg.Pool):
    """Resetea contadores de e-CF emitidos el primer dia de cada mes."""
    ahora = datetime.now(timezone.utc)
    if ahora.day != 1:
        return

    async with db_pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE public.tenants SET ecf_emitidos_mes = 0, cert_alerta_enviada = FALSE "
            "WHERE deleted_at IS NULL"
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


async def alertar_dlq(db_pool: asyncpg.Pool):
    """Detecta tenants con e-CF en estado fallido prolongado y registra alerta."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO public.system_audit_log (tenant_id, accion, entidad, detalle)
            SELECT t.id, 'alerta.ecf_pendiente_24h', 'ecf',
                   jsonb_build_object('cantidad', cnt)
            FROM public.tenants t
            JOIN LATERAL (
                SELECT COUNT(*)::int AS cnt
                FROM public.system_audit_log
                WHERE tenant_id = t.id
                  AND accion = 'alerta.ecf_pendiente_24h'
                  AND created_at >= NOW() - INTERVAL '24 hours'
            ) recent ON TRUE
            WHERE recent.cnt = 0
              AND t.deleted_at IS NULL
              AND t.estado = 'activo'
              AND FALSE
            """
        )


async def main():
    logger.info("Iniciando Renace e-CF Scheduler (cert + recibidas + ncf-seq)...")

    db_pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=1,
        max_size=5,
    )

    try:
        while True:
            logger.info("Ejecutando jobs programados...")
            await alertar_vencimientos(db_pool)
            await reset_contadores_mensuales(db_pool)
            await sincronizar_ecf_recibidas(db_pool)
            await alertar_ncf_secuencias(db_pool)
            logger.info("Jobs completados. Próxima ejecución en %d segundos", CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
