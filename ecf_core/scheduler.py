"""
Scheduler — Jobs periódicos del sistema.
- Alerta de vencimiento de certificados (diario)
- Reset de contadores mensuales de e-CF (1ro de cada mes)

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

CHECK_INTERVAL = 3600  # 1 hora


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
        asunto = f"[ECF SaaS] Certificado .p12 vence en {dias} dias - {t['razon_social']}"
        cuerpo = (
            f"Estimado {t['razon_social']} (RNC: {t['rnc']}),\n\n"
            f"Su certificado digital (.p12) registrado en la plataforma e-CF "
            f"vence el {t['cert_vencimiento']}.\n\n"
            f"Debe renovarlo ante la DGII antes de esa fecha para evitar "
            f"interrupciones en la emision de comprobantes fiscales electronicos.\n\n"
            f"Atentamente,\nSaaS ECF DGII"
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


async def main():
    logger.info("Iniciando ECF Scheduler...")

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
            logger.info("Jobs completados. Proxima ejecucion en %d segundos", CHECK_INTERVAL)
            await asyncio.sleep(CHECK_INTERVAL)
    finally:
        await db_pool.close()


if __name__ == "__main__":
    asyncio.run(main())
