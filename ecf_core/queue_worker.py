"""
Queue Worker — Procesamiento asíncrono de e-CF con Redis
Implementa cola persistente, reintentos con backoff, DLQ y callbacks a Odoo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis

from ecf_core.cert_vault import CertVaultRepository
from ecf_core.dgii_client import (
    DGIIClient,
    DGIIClientError,
    EstadoDGII,
    generar_qr_url,
    generar_security_code,
)
from ecf_core.ecf_core_service import ECFCoreService, FacturaECF, ItemECF
from ecf_core.odoo_webhook import notify_odoo_ecf_result
from ecf_core.rfce_service import RFCEService, requiere_rfce
from ecf_core.utils import fmt_fecha_hora_dgii, safe_schema as _safe_schema

logger = logging.getLogger(__name__)

QUEUE_ECF_PENDING = "ecf:pending"       # Cola principal
QUEUE_ECF_RETRY   = "ecf:retry"         # Cola de reintentos
QUEUE_ECF_DLQ     = "ecf:dlq"           # Dead letter queue (fallos definitivos)
MAX_INTENTOS      = 5
RETRY_DELAYS      = [30, 120, 300, 900, 3600]   # segundos entre reintentos

# Estados que no deben reenviarse a DGII (salvo force_reprocess)
_ESTADOS_TERMINALES = frozenset({
    "aprobado", "anulado", "rechazado", "condicionado", "anulacion_fallida",
})
_ESTADOS_CLAIMABLES = frozenset({"pendiente"})  # claim → enviado antes de DGII


def _extraer_fecha_firma(xml_firmado: bytes) -> str | None:
    """Extrae FechaHoraFirma (dd-mm-yyyy HH:MM:SS) del XML firmado."""
    try:
        from lxml import etree
        root = etree.fromstring(xml_firmado)
        for elem in root.iter():
            if etree.QName(elem.tag).localname == "FechaHoraFirma" and elem.text:
                return elem.text.strip()
    except Exception as e:
        logger.warning("No se pudo extraer FechaHoraFirma del XML: %s", e)
    return None


# Códigos DGII de unidad de medida más comunes (UnidadMedidaType del XSD).
# Odoo suele mandar el nombre de la UoM en texto; se normaliza al código.
_UNIDADES_DGII = {
    "unidad": "43", "unidades": "43", "und": "43", "unit": "43", "units": "43",
    "servicio": "47", "servicios": "47",
    "caja": "10", "cajas": "10",
    "docena": "15", "docenas": "15",
    "galon": "23", "galón": "23", "galones": "23",
    "gramo": "24", "gramos": "24", "g": "24",
    "hora": "25", "horas": "25",
    "kilogramo": "26", "kilogramos": "26", "kg": "26",
    "litro": "31", "litros": "31", "l": "31",
    "metro": "34", "metros": "34", "m": "34",
    "libra": "32", "libras": "32", "lb": "32",
    "paquete": "39", "paquetes": "39",
}


def _normalizar_unidad_dgii(unidad: str | None) -> str:
    """Convierte la unidad al código numérico DGII (default 43 = Unidad)."""
    if not unidad:
        return "43"
    unidad = str(unidad).strip()
    if unidad.isdigit():
        return unidad
    return _UNIDADES_DGII.get(unidad.lower(), "43")


def _normalizar_items_ecf(raw_items) -> list[dict]:
    """Convierte items de json_agg(row_to_json) a list[dict].

    asyncpg puede devolver el array completo o cada fila como str JSON;
    indexar con claves falla con TypeError: string indices must be integers.
    """
    if not raw_items:
        return []
    if isinstance(raw_items, str):
        try:
            raw_items = json.loads(raw_items)
        except json.JSONDecodeError:
            logger.error("Error decodificando items: no es JSON válido: %s", raw_items)
            return []
    if not isinstance(raw_items, list):
        return []
    items: list[dict] = []
    for raw in raw_items:
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                logger.error("Error decodificando item individual: %s", raw)
                continue
        if isinstance(raw, dict):
            items.append(raw)
        else:
            logger.error("Item de ECF descartado porque no es un dict: %s (tipo %s)", raw, type(raw))
    return items


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
                from ecf_core.platform_config import maybe_reload_psfe_from_redis
                await maybe_reload_psfe_from_redis(self.db, self.redis)
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
        """Revisa la cola de reintentos y encola los que ya cumplieron su delay.

        ZREM devuelve cuántos elementos removió: si otro worker ya tomó el
        item, retorna 0 y NO se re-encola (evita duplicados con N workers).
        """
        ahora = datetime.now(timezone.utc).timestamp()
        items = await self.redis.zrangebyscore(
            QUEUE_ECF_RETRY, "-inf", ahora, start=0, num=10
        )
        for item in items:
            removed = await self.redis.zrem(QUEUE_ECF_RETRY, item)
            if removed:
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
            schema = tenant["schema_name"]
            ecf_data = await self._get_ecf(schema, ecf_id)

            estado_actual = ecf_data["estado"]
            if estado_actual in _ESTADOS_TERMINALES and not mensaje.get("force_reprocess"):
                logger.info("ECF %s ya en estado final %s, saltando", ecf_id, estado_actual)
                return

            # EnProceso con track_id: el poller del scheduler resuelve el estado
            if (
                estado_actual == "enviado"
                and ecf_data.get("track_id")
                and not mensaje.get("force_reprocess")
            ):
                logger.info("ECF %s ya enviado (track=%s), saltando", ecf_id, ecf_data["track_id"])
                return

            # Claim atómico: pendiente → enviado (evita doble envío concurrente)
            if estado_actual in _ESTADOS_CLAIMABLES or mensaje.get("force_reprocess"):
                claimed = await self._claim_ecf(schema, ecf_id)
                if not claimed and estado_actual in _ESTADOS_CLAIMABLES:
                    logger.info("ECF %s ya reclamado por otro worker, saltando", ecf_id)
                    return
            elif estado_actual == "enviado" and not ecf_data.get("track_id"):
                # Reintento tras fallo mid-flight (claim previo sin respuesta DGII)
                pass
            elif not mensaje.get("force_reprocess"):
                logger.info("ECF %s estado=%s no procesable, saltando", ecf_id, estado_actual)
                return

            # Asegurar schema en el mensaje para release/retry
            mensaje.setdefault("schema_name", schema)

            ambiente_efectivo = mensaje.get("ambiente_emision") or tenant["ambiente"]
            if mensaje.get("ambiente_emision") == "simulacion":
                ambiente_efectivo = "simulacion"
            elif mensaje.get("ambiente_emision") in ("certificacion", "produccion"):
                ambiente_efectivo = mensaje["ambiente_emision"]

            # Recuperar certificado .p12 del vault
            p12_data = await self.cert_repo.obtener(tenant_id)
            p12_pass = self.vault.descifrar_campo(tenant.get("cert_password") or "").encode()

            # Construir FacturaECF desde los datos de la DB
            factura = self._construir_factura(ecf_data, tenant)
            if mensaje.get("fecha_limite_pago"):
                from datetime import date as date_cls
                factura.fecha_limite_pago = date_cls.fromisoformat(
                    str(mensaje["fecha_limite_pago"])
                )

            # Procesar: generar XML, validar XSD, firmar.
            # `cufe_secret` (algoritmo Colombia) ya no se usa — DGII RD usa
            # CodigoSeguridad de 6 chars derivado del SignatureValue.
            resultado = self.ecf_service.procesar(factura, p12_data, p12_pass)

            # Enviar a DGII con autenticación por semilla
            # MOCK MODE: Solo activo cuando ECF_AMBIENTE != eCF/produccion
            _sistema_ambiente = os.environ.get("ECF_AMBIENTE", "").lower()
            _es_produccion = _sistema_ambiente in {"ecf", "produccion"}
            if ambiente_efectivo == "simulacion":
                if _es_produccion:
                    raise RuntimeError(
                        f"Tenant {tenant['rnc']} emite en simulación pero el sistema está "
                        f"en producción (ECF_AMBIENTE={_sistema_ambiente}). Corrija el ambiente."
                    )
                logger.warning("MOCK MODE activo para tenant %s — e-CF NO se envía a DGII", tenant["rnc"])
                from ecf_core.dgii_client import EstadoDGII, RespuestaDGII
                # Lógica de prueba para desarrolladores en Odoo:
                # Si el total de la factura termina en .99 -> Rechazado
                # Si el total termina en .98 -> Condicionado
                # Si no -> Aceptado
                estado_mock = EstadoDGII.ACEPTADO
                mensaje_mock = "Aceptado Local (Modo Simulación SaaS)"

                total_str = f"{factura.total:.2f}"
                if total_str.endswith(".99"):
                    estado_mock = EstadoDGII.RECHAZADO
                    mensaje_mock = "Rechazado (Simulación por monto terminado en .99)"
                elif total_str.endswith(".98"):
                    estado_mock = EstadoDGII.CONDICIONADO
                    mensaje_mock = "Aceptado Condicional (Simulación por monto terminado en .98)"

                respuesta = RespuestaDGII(
                    estado=estado_mock,
                    track_id=f"MOCK-{uuid.uuid4().hex[:8].upper()}",
                    mensaje=mensaje_mock,
                    codigo_seguridad=None,
                    qr_code=None,  # Se genera localmente abajo
                    detalles=[{"codigo": "MOCK01", "mensaje": mensaje_mock}],
                    raw={"mock": True, "ambiente": ambiente_efectivo}
                )
            elif requiere_rfce(factura.tipo_ecf, factura.total):
                # Factura de Consumo < RD$250,000: la DGII exige el RFCE
                # (resumen) en fc.dgii.gov.do — el XML completo firmado se
                # conserva localmente (retención 10 años).
                security_code_rfce = generar_security_code(resultado["xml_firmado"])
                rfce_service = RFCEService(self.db)
                respuesta = await rfce_service.emitir_rfce(
                    tenant=tenant,
                    ecf_id=ecf_id,
                    factura=factura,
                    security_code=security_code_rfce,
                    p12_data=p12_data,
                    p12_password=p12_pass,
                    ambiente=ambiente_efectivo,
                )
            else:
                async with DGIIClient(ambiente=ambiente_efectivo) as dgii:
                    dgii.set_certificate(p12_data, p12_pass)
                    respuesta = await dgii.enviar_ecf(
                        xml_firmado=resultado["xml_firmado"],
                        rnc_emisor=tenant["rnc"],
                        tipo_ecf=factura.tipo_ecf,
                        ncf=factura.ncf,
                    )

            # Generar SecurityCode y QR URL.
            # FechaFirma del QR DEBE coincidir con FechaHoraFirma del XML
            # firmado — la consulta de timbre DGII valida ambos valores.
            security_code = generar_security_code(resultado["xml_firmado"])
            fecha_firma_qr = _extraer_fecha_firma(resultado["xml_firmado"]) or \
                fmt_fecha_hora_dgii()
            fecha_emision_qr = factura.fecha_emision.strftime("%d-%m-%Y")
            qr_url = generar_qr_url(
                ambiente=ambiente_efectivo,
                rnc_emisor=tenant["rnc"],
                ncf=factura.ncf,
                total=str(factura.total),
                fecha_firma=fecha_firma_qr,
                security_code=security_code,
                rnc_comprador=factura.rnc_comprador or "",
                tipo_ecf=factura.tipo_ecf,
                fecha_emision=fecha_emision_qr,
            )

            # Persistir resultado
            codigo_final = respuesta.codigo_seguridad or security_code
            await self._actualizar_ecf(
                schema     = tenant["schema_name"],
                ecf_id     = ecf_id,
                estado     = self._estado_dgii_a_local(respuesta.estado),
                codigo_seguridad = codigo_final,
                xml_original = resultado.get("xml_original"),
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
                await self._callback_odoo(
                    tenant, ecf_data, respuesta, estado_local,
                    qr_url=qr_url, security_code=codigo_final,
                )

            logger.info("ECF %s procesado: %s", ecf_id, respuesta.estado)

        except DGIIClientError as e:
            logger.warning("Error DGII en ECF %s: %s", ecf_id, e)
            schema_release = mensaje.get("schema_name") or ""
            if schema_release:
                await self._release_claim(schema_release, ecf_id)
            await self._programar_reintento(mensaje, intento, str(e))

        except TypeError as e:
            logger.exception("TypeError procesando ECF %s: %s. Revisa la estructura de los datos.", ecf_id, e)
            error_msg = f"TypeError (Posible dato mal formado): {e}"
            schema_release = mensaje.get("schema_name") or ""
            if schema_release:
                await self._release_claim(schema_release, ecf_id)
            await self._enviar_a_dlq(mensaje, error_msg)
            await self._marcar_error(
                schema=schema_release, ecf_id=ecf_id, error=error_msg, terminal=True,
            )

        except Exception as e:
            logger.exception("Error fatal procesando ECF %s: %s", ecf_id, e)
            schema_release = mensaje.get("schema_name") or ""
            if schema_release:
                await self._release_claim(schema_release, ecf_id)
            await self._enviar_a_dlq(mensaje, str(e))
            await self._marcar_error(
                schema=schema_release, ecf_id=ecf_id, error=str(e), terminal=True,
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

            # Tipo e-CF: deducir del NCF (E31xxxxxxxxxx → 31)
            try:
                tipo_ecf = int(ncf[1:3])
            except (ValueError, IndexError):
                tipo_ecf = 31

            async with DGIIClient(ambiente=tenant["ambiente"]) as dgii:
                dgii.set_certificate(p12_data, p12_pass)
                respuesta = await dgii.anular_ecf(
                    rnc_emisor=tenant["rnc"],
                    ncf_desde=ncf,
                    ncf_hasta=ncf,
                    tipo_ecf=tipo_ecf,
                )

            # Marcar según estado DGII (async: Recibido/EnProceso no son fallo)
            if respuesta.estado in (EstadoDGII.ACEPTADO, EstadoDGII.CONDICIONADO):
                estado_final = "anulado"
            elif respuesta.estado in (EstadoDGII.RECIBIDO, EstadoDGII.PROCESANDO):
                estado_final = "anulacion_pendiente"
            else:
                estado_final = "anulacion_fallida"
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

            # Callback a Odoo solo en estados definitivos (no EnProceso)
            if estado_final != "anulacion_pendiente" and tenant.get("odoo_webhook_url"):
                ecf_data = await self._get_ecf(schema, ecf_id)
                await self._callback_odoo(tenant, ecf_data, respuesta, estado_final)

            logger.info("Anulación DGII completada: NCF %s track_id=%s estado=%s",
                        ncf, respuesta.track_id, estado_final)

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
            await self._marcar_error(
                schema=mensaje.get("schema_name", ""),
                ecf_id=mensaje["ecf_id"],
                error=f"DLQ tras {intento} intentos: {error}",
                terminal=True,
            )
            # Notificar Odoo del rechazo definitivo
            try:
                tenant = await self._get_tenant(mensaje["tenant_id"])
                ecf_data = {
                    "id": mensaje["ecf_id"],
                    "ncf": mensaje.get("ncf"),
                    "odoo_move_id": mensaje.get("odoo_move_id"),
                    "tipo_ecf": mensaje.get("tipo_ecf"),
                }
                # Cargar odoo_move_id desde DB si falta
                schema = mensaje.get("schema_name") or ""
                if schema and not ecf_data.get("odoo_move_id"):
                    s = _safe_schema(schema)
                    async with self.db.acquire() as conn:
                        row = await conn.fetchrow(
                            f"SELECT odoo_move_id, ncf, tipo_ecf FROM {s}.ecf WHERE id=$1",
                            uuid.UUID(mensaje["ecf_id"]),
                        )
                        if row:
                            ecf_data["odoo_move_id"] = row["odoo_move_id"]
                            ecf_data["ncf"] = row["ncf"] or ecf_data.get("ncf")
                            ecf_data["tipo_ecf"] = row["tipo_ecf"] or ecf_data.get("tipo_ecf")
                from ecf_core.dgii_client import RespuestaDGII, EstadoDGII
                fake = RespuestaDGII(
                    estado=EstadoDGII.RECHAZADO,
                    track_id=None,
                    codigo_seguridad=None,
                    mensaje=error,
                    qr_code=None,
                    detalles=[],
                    raw={"error": error, "fuente": "dlq"},
                )
                await self._callback_odoo(tenant, ecf_data, fake, "rechazado")
            except Exception as cb_err:
                logger.warning("Webhook DLQ no enviado para %s: %s", mensaje.get("ecf_id"), cb_err)
            return

        delay = RETRY_DELAYS[min(intento - 1, len(RETRY_DELAYS) - 1)]
        mensaje["intento"] = intento + 1
        mensaje["ultimo_error"] = error

        score = datetime.now(timezone.utc).timestamp() + delay
        await self.redis.zadd(QUEUE_ECF_RETRY, {json.dumps(mensaje): score})
        # Persistir progreso de reintentos para visibilidad operativa
        await self._registrar_reintento(
            schema=mensaje.get("schema_name", ""),
            ecf_id=mensaje["ecf_id"],
            intento=intento,
            error=error,
        )
        logger.info("ECF %s programado para reintento en %ds", mensaje["ecf_id"], delay)

    async def _registrar_reintento(self, schema: str, ecf_id: str, intento: int, error: str):
        if not schema:
            return
        try:
            s = _safe_schema(schema)
            async with self.db.acquire() as conn:
                await conn.execute(
                    f"UPDATE {s}.ecf SET intentos_envio=$1, ultimo_error=$2, updated_at=NOW() "
                    f"WHERE id=$3 AND estado NOT IN ('aprobado', 'anulado')",
                    intento, error, uuid.UUID(ecf_id),
                )
        except Exception as e:
            logger.warning("No se pudo registrar reintento de %s: %s", ecf_id, e)

    async def _enviar_a_dlq(self, mensaje: dict, error: str):
        mensaje["dlq_error"] = error
        mensaje["dlq_at"] = datetime.now(timezone.utc).isoformat()
        await self.redis.rpush(QUEUE_ECF_DLQ, json.dumps(mensaje))

    async def _callback_odoo(self, tenant: dict, ecf_data: dict, respuesta, estado_local: str,
                              qr_url: str = None, security_code: str = None):
        """Notifica a Odoo el resultado mediante webhook firmado con HMAC-SHA256."""
        await notify_odoo_ecf_result(
            tenant=tenant,
            vault=self.vault,
            ecf_data=ecf_data,
            estado_local=estado_local,
            track_id=respuesta.track_id,
            codigo_seguridad=security_code or respuesta.codigo_seguridad,
            qr_code=qr_url or respuesta.qr_code,
            error_msg=respuesta.mensaje if estado_local != "aprobado" else None,
            detalles=respuesta.detalles if estado_local != "aprobado" else [],
            redis=self.redis,
        )

    def _construir_factura(self, ecf_data: dict, tenant: dict) -> FacturaECF:
        from decimal import Decimal
        raw_items = _normalizar_items_ecf(ecf_data.get("items"))
        items = [
            ItemECF(
                linea                   = i["linea"],
                descripcion             = i["descripcion"],
                cantidad                = Decimal(str(i["cantidad"])),
                precio_unitario         = Decimal(str(i["precio_unitario"])),
                descuento               = Decimal(str(i.get("descuento", "0"))),
                itbis_tasa              = Decimal(str(i.get("itbis_tasa", "18"))),
                unidad                  = _normalizar_unidad_dgii(i.get("unidad")),
                indicador_bien_servicio = int(i.get("indicador_bien_servicio", 2)),
            )
            for i in raw_items
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

    async def _claim_ecf(self, schema: str, ecf_id: str) -> bool:
        """Claim atómico pendiente→enviado. Retorna False si otro worker ya lo tomó."""
        s = _safe_schema(schema)
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"UPDATE {s}.ecf SET estado = 'enviado', updated_at = NOW() "
                f"WHERE id = $1 AND estado = 'pendiente' RETURNING id",
                uuid.UUID(ecf_id),
            )
        return row is not None

    async def _release_claim(self, schema: str, ecf_id: str) -> None:
        """Revierte enviado→pendiente si aún no hay track_id (fallo pre-respuesta)."""
        if not schema:
            return
        try:
            s = _safe_schema(schema)
            async with self.db.acquire() as conn:
                await conn.execute(
                    f"UPDATE {s}.ecf SET estado = 'pendiente', updated_at = NOW() "
                    f"WHERE id = $1 AND estado = 'enviado' AND track_id IS NULL",
                    uuid.UUID(ecf_id),
                )
        except Exception as e:
            logger.warning("No se pudo liberar claim de %s: %s", ecf_id, e)

    async def _get_tenant(self, tenant_id: str) -> dict:
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, rnc, razon_social, nombre_comercial, direccion, "
                "schema_name, ambiente, estado, cert_password, "
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
                f"""
                SELECT e.*,
                    COALESCE(
                        (
                            SELECT json_agg(row_to_json(i))
                            FROM {s}.ecf_items i
                            WHERE i.ecf_id = e.id
                        ),
                        '[]'::json
                    ) AS items
                FROM {s}.ecf e
                WHERE e.id = $1
                """,
                uuid.UUID(ecf_id),
            )
        if not row:
            return {}
        data = dict(row)
        data["items"] = _normalizar_items_ecf(data.get("items"))
        return data

    async def _actualizar_ecf(self, schema, ecf_id, estado, codigo_seguridad, xml_firmado, respuesta, intento,
                              track_id=None, security_code=None, qr_url=None,
                              xml_original=None):
        s = _safe_schema(schema)
        async with self.db.acquire() as conn:
            await conn.execute(f"""
                UPDATE {s}.ecf SET
                    estado          = $1,
                    codigo_seguridad = COALESCE($2, codigo_seguridad),
                    xml_original    = COALESCE($3, xml_original),
                    xml_firmado     = COALESCE($4, xml_firmado),
                    respuesta_dgii  = $5,
                    intentos_envio  = $6,
                    track_id        = COALESCE($7, track_id),
                    security_code   = COALESCE($8, security_code),
                    qr_url          = COALESCE($9, qr_url),
                    sent_at         = CASE WHEN sent_at IS NULL THEN NOW() ELSE sent_at END,
                    approved_at     = CASE WHEN $1 = 'aprobado' AND approved_at IS NULL THEN NOW() ELSE approved_at END,
                    updated_at      = NOW()
                WHERE id = $10
            """, estado, codigo_seguridad, xml_original, xml_firmado, json.dumps(respuesta), intento,
                track_id, security_code, qr_url, uuid.UUID(ecf_id))

    async def _marcar_error(self, schema, ecf_id, error, terminal: bool = False):
        if not schema:
            return
        s = _safe_schema(schema)
        async with self.db.acquire() as conn:
            if terminal:
                # 'rechazado' es estado terminal válido en el CHECK del schema
                await conn.execute(
                    f"UPDATE {s}.ecf SET estado='rechazado', ultimo_error=$1, updated_at=NOW() "
                    f"WHERE id=$2 AND estado NOT IN ('aprobado', 'anulado')",
                    error, uuid.UUID(ecf_id),
                )
            else:
                await conn.execute(
                    f"UPDATE {s}.ecf SET ultimo_error=$1, updated_at=NOW() WHERE id=$2"
                    f"  AND estado NOT IN ('aprobado', 'anulado')",
                    error, uuid.UUID(ecf_id),
                )
