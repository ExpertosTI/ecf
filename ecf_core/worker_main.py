# Async worker para ECF

import asyncio
import json
import logging
import os
import signal
import sys

import asyncpg
import redis.asyncio as aioredis

from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.ecf_core_service import ECFCoreService
from ecf_core.queue_worker import ECFQueueWorker


def _setup_logging():
    """Configura JSON logging para producción o texto para desarrollo."""
    level = logging.INFO
    if os.environ.get("LOG_FORMAT", "text") == "json":

        class JSONFormatter(logging.Formatter):
            def format(self, record):
                return json.dumps({
                    "ts": self.formatTime(record),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                    **({"exc": self.formatException(record.exc_info)} if record.exc_info else {}),
                })

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JSONFormatter())
        logging.root.addHandler(handler)
        logging.root.setLevel(level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stdout,
        )


_setup_logging()
logger = logging.getLogger(__name__)


async def main():
    logger.info("Iniciando ECF Queue Worker...")

    db_pool = await asyncpg.create_pool(
        dsn=os.environ["DATABASE_URL"],
        min_size=2,
        max_size=10,
    )

    redis_client = await aioredis.from_url(
        os.environ["REDIS_URL"],
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
    )

    vault = CertVault()
    cert_repo = CertVaultRepository(db_pool, vault)
    ecf_service = ECFCoreService()

    worker = ECFQueueWorker(
        redis=redis_client,
        db_pool=db_pool,
        cert_repo=cert_repo,
        ecf_service=ecf_service,
    )

    # Graceful shutdown con señales SIGTERM/SIGINT
    shutdown_event = asyncio.Event()

    def _shutdown_handler():
        logger.info("Señal de shutdown recibida. Cerrando worker gracefully...")
        worker.running = False
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown_handler)

    try:
        await worker.run()
    finally:
        logger.info("Cerrando conexiones...")
        await db_pool.close()
        await redis_client.aclose()
        logger.info("Worker detenido.")


if __name__ == "__main__":
    asyncio.run(main())
