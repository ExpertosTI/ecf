"""
ecf_recibidas_service.py — Servicio de sincronización de e-CF Recibidas desde DGII

Responsabilidades:
- Consultar el endpoint /fe/consultas/api/consultaecfrecibidos de la DGII
- Parsear el XML de respuesta y cada e-CF individual
- Persistir en {schema}.compras con deduplicación atómica
- Notificar a Odoo vía webhook con HMAC-SHA256
- Registrar el tracking de sincronización en ecf_recibidas_sync
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import asyncpg
import httpx
from lxml import etree

from ecf_core.cert_vault import CertVaultRepository
from ecf_core.dgii_client import DGIIClient, DGIIClientError

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Estructuras de datos
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ECFRecibida:
    """Representa un e-CF recibido desde la DGII."""
    ncf:              str
    rnc_emisor:       str
    nombre_emisor:    str
    tipo_ecf:         int
    fecha_emision:    date
    total_monto:      Decimal
    itbis_facturado:  Decimal
    subtotal:         Decimal
    cufe:             Optional[str] = None
    xml_original:     Optional[bytes] = None


@dataclass
class ResultadoSync:
    """Resultado de una sincronización de e-CF recibidas para un tenant."""
    tenant_id:     str
    schema_name:   str
    nuevos:        int = 0
    duplicados:    int = 0
    errores:       int = 0
    notificados:   int = 0
    error_global:  Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Parser XML de respuesta DGII
# ─────────────────────────────────────────────────────────────────────────────

def _parse_recibidas_xml(xml_text: str) -> list[ECFRecibida]:
    """
    Parsea el XML de respuesta del endpoint consultaecfrecibidos de la DGII.
    Soporta namespace o sin namespace.
    """
    recibidas: list[ECFRecibida] = []

    def _get(el, *tags) -> str:
        """Busca un tag en múltiples namespaces."""
        for tag in tags:
            found = el.find(tag)
            if found is not None and found.text:
                return found.text.strip()
            # Con namespace wildcard
            found = el.find(f"{{*}}{tag}")
            if found is not None and found.text:
                return found.text.strip()
        return ""

    try:
        root = etree.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
        # Buscar nodos ECF con o sin namespace
        ecf_nodes = root.findall(".//ECF") or root.findall(".//{*}ECF")

        for node in ecf_nodes:
            ncf = _get(node, "ENCF", "NCF")
            if not ncf:
                continue

            fecha_str = _get(node, "FechaEmision", "Fecha")
            try:
                fecha = date.fromisoformat(fecha_str) if fecha_str else date.today()
            except ValueError:
                fecha = date.today()

            def _decimal(s: str) -> Decimal:
                try:
                    return Decimal(s.replace(",", "")) if s else Decimal("0")
                except Exception:
                    return Decimal("0")

            tipo_str = _get(node, "TipoECF", "Tipo")
            try:
                tipo = int(tipo_str)
            except (ValueError, TypeError):
                tipo = 31

            recibidas.append(ECFRecibida(
                ncf=ncf,
                rnc_emisor=_get(node, "RNCEmisor", "RNC"),
                nombre_emisor=_get(node, "NombreEmisor", "Nombre")[:255],
                tipo_ecf=tipo,
                fecha_emision=fecha,
                total_monto=_decimal(_get(node, "MontoTotal", "Total")),
                itbis_facturado=_decimal(_get(node, "ITBIS", "Itbis")),
                subtotal=_decimal(_get(node, "Subtotal", "MontoSinImpuesto")),
                cufe=_get(node, "CUFE") or None,
            ))

    except etree.XMLSyntaxError as e:
        logger.error("Error parseando XML de e-CF recibidas: %s", e)

    return recibidas


# ─────────────────────────────────────────────────────────────────────────────
# Servicio principal
# ─────────────────────────────────────────────────────────────────────────────

class ECFRecibidasService:
    """
    Servicio que consulta la DGII para obtener e-CF recibidos por un tenant,
    los persiste en la base de datos y notifica a Odoo.
    """

    # Endpoints DGII por ambiente
    EP_RECIBIDAS = "/fe/consultas/api/consultaecfrecibidos"
    EP_ECF_XML   = "/fe/consultas/api/ecfxml"

    def __init__(self, db_pool: asyncpg.Pool, cert_repo: CertVaultRepository):
        self.db        = db_pool
        self.cert_repo = cert_repo

    # ─────────────────────────────────────────────────────────────────────────
    # Punto de entrada: sincronizar un tenant
    # ─────────────────────────────────────────────────────────────────────────

    async def sincronizar_tenant(self, tenant: dict) -> ResultadoSync:
        """
        Sincroniza los e-CF recibidos de un tenant con la DGII.
        Utiliza la última fecha consultada para hacer polling incremental.
        """
        schema    = tenant["schema_name"]
        tenant_id = str(tenant["id"])
        resultado = ResultadoSync(tenant_id=tenant_id, schema_name=schema)

        try:
            # Obtener última fecha sincronizada
            fecha_desde = await self._obtener_ultima_fecha(schema)
            fecha_hasta = date.today()

            # No consultar si ya está al día
            if fecha_desde >= fecha_hasta:
                logger.info("[%s] e-CF recibidas: ya sincronizado hasta %s", tenant["rnc"], fecha_hasta)
                return resultado

            # Obtener certificado del tenant
            cert = await self.cert_repo.obtener_certificado(tenant_id)
            if not cert:
                logger.warning("[%s] Sin certificado activo — omitiendo sync", tenant["rnc"])
                resultado.error_global = "Sin certificado activo"
                return resultado

            # Consultar DGII en ventanas de 30 días
            recibidas = await self._consultar_dgii_ventanas(
                tenant=tenant,
                cert_data=cert["cert_data"],
                cert_password=cert["cert_password"].encode() if cert["cert_password"] else b"",
                fecha_desde=fecha_desde,
                fecha_hasta=fecha_hasta,
            )

            # Persistir en base de datos (deduplicación atómica + descarga XML)
            nuevos, duplicados = await self._persistir_recibidas(
                tenant=tenant,
                cert_data=cert["cert_data"],
                cert_password=cert["cert_password"].encode() if cert["cert_password"] else b"",
                recibidas=recibidas
            )
            resultado.nuevos     = nuevos
            resultado.duplicados = duplicados

            # Notificar a Odoo por webhook para las nuevas
            if nuevos > 0 and tenant.get("odoo_webhook_url"):
                notificados = await self._notificar_odoo(tenant, schema)
                resultado.notificados = notificados

            # Actualizar tracking
            await self._actualizar_sync(schema, fecha_hasta, nuevos, 0)
            logger.info(
                "[%s] Sync OK: %d nuevas, %d duplicadas, %d notificadas",
                tenant["rnc"], nuevos, duplicados, resultado.notificados
            )

        except DGIIClientError as e:
            logger.error("[%s] Error DGII consultando recibidas: %s", tenant.get("rnc"), e)
            resultado.error_global = str(e)
            resultado.errores = 1
            await self._actualizar_sync(schema, date.today(), 0, 1, str(e))

        except Exception as e:
            logger.exception("[%s] Error inesperado en sync recibidas: %s", tenant.get("rnc"), e)
            resultado.error_global = str(e)
            resultado.errores = 1
            await self._actualizar_sync(schema, date.today(), 0, 1, str(e))

        return resultado

    # ─────────────────────────────────────────────────────────────────────────
    # Consulta a la DGII en ventanas de 30 días
    # ─────────────────────────────────────────────────────────────────────────

    async def _consultar_dgii_ventanas(
        self,
        tenant: dict,
        cert_data: bytes,
        cert_password: bytes,
        fecha_desde: date,
        fecha_hasta: date,
    ) -> list[ECFRecibida]:
        """Consulta la DGII en ventanas de máximo 30 días para evitar timeouts."""
        todas: list[ECFRecibida] = []

        ventana_inicio = fecha_desde
        while ventana_inicio < fecha_hasta:
            ventana_fin = min(ventana_inicio + timedelta(days=29), fecha_hasta)

            async with DGIIClient(tenant["ambiente"]) as client:
                client.set_certificate(cert_data, cert_password)
                lote = await self._consultar_recibidas(
                    client=client,
                    rnc_receptor=tenant["rnc"],
                    fecha_desde=ventana_inicio,
                    fecha_hasta=ventana_fin,
                )
            todas.extend(lote)
            logger.debug(
                "[%s] Ventana %s→%s: %d e-CF recibidas",
                tenant["rnc"], ventana_inicio, ventana_fin, len(lote)
            )
            ventana_inicio = ventana_fin + timedelta(days=1)
            # Pequeña pausa para no saturar la API DGII
            await asyncio.sleep(0.5)

        return todas

    async def _consultar_recibidas(
        self,
        client: DGIIClient,
        rnc_receptor: str,
        fecha_desde: date,
        fecha_hasta: date,
    ) -> list[ECFRecibida]:
        """Consulta el endpoint de e-CF recibidas de la DGII."""
        await client._authenticate()

        resp = await client._client.get(
            self.EP_RECIBIDAS,
            params={
                "RncReceptor": rnc_receptor,
                "FechaDesde":  fecha_desde.isoformat(),
                "FechaHasta":  fecha_hasta.isoformat(),
            },
            headers=client._auth_headers(),
        )

        if resp.status_code == 404:
            return []  # Sin e-CF recibidas en ese período

        if resp.status_code != 200:
            raise DGIIClientError(
                f"Error consultando e-CF recibidas: HTTP {resp.status_code} — {resp.text[:500]}"
            )

        # La respuesta puede ser XML o JSON según la DGII
        content_type = resp.headers.get("content-type", "")
        if "xml" in content_type or resp.text.strip().startswith("<"):
            return _parse_recibidas_xml(resp.text)
        else:
            # Fallback JSON si la DGII devuelve JSON
            try:
                data = resp.json()
                return self._parse_recibidas_json(data)
            except Exception:
                logger.warning("Respuesta DGII en formato desconocido: %s", resp.text[:200])
                return []

    def _parse_recibidas_json(self, data: dict | list) -> list[ECFRecibida]:
        """Parser de respuesta JSON (fallback si DGII no usa XML)."""
        recibidas = []
        items = data if isinstance(data, list) else data.get("ecfs", data.get("ECFs", []))
        for item in items:
            try:
                fecha_str = item.get("fechaEmision") or item.get("FechaEmision", "")
                try:
                    fecha = date.fromisoformat(fecha_str[:10]) if fecha_str else date.today()
                except ValueError:
                    fecha = date.today()

                recibidas.append(ECFRecibida(
                    ncf=item.get("encf") or item.get("ENCF", ""),
                    rnc_emisor=item.get("rncEmisor") or item.get("RNCEmisor", ""),
                    nombre_emisor=(item.get("nombreEmisor") or item.get("NombreEmisor", ""))[:255],
                    tipo_ecf=int(item.get("tipoECF") or item.get("TipoECF", 31)),
                    fecha_emision=fecha,
                    total_monto=Decimal(str(item.get("montoTotal", 0))),
                    itbis_facturado=Decimal(str(item.get("itbis", 0))),
                    subtotal=Decimal(str(item.get("subtotal", 0))),
                    cufe=item.get("cufe") or item.get("CUFE"),
                ))
            except Exception as e:
                logger.warning("Error parseando e-CF recibida JSON: %s — %s", e, item)
        return recibidas

    # ─────────────────────────────────────────────────────────────────────────
    # Persistencia con deduplicación atómica
    # ─────────────────────────────────────────────────────────────────────────

    async def _persistir_recibidas(
        self,
        tenant: dict,
        cert_data: bytes,
        cert_password: bytes,
        recibidas: list[ECFRecibida],
    ) -> tuple[int, int]:
        """
        Inserta las e-CF recibidas en la tabla compras.
        Intenta descargar el XML original de la DGII para cada nueva.
        Retorna (nuevos, duplicados).
        """
        schema   = tenant["schema_name"]
        ambiente = tenant["ambiente"]
        nuevos = 0
        duplicados = 0

        async with self.db.acquire() as conn:
            for ecf in recibidas:
                # 1. Verificar si ya existe para no descargar XML innecesariamente
                exists = await conn.fetchval(f"SELECT 1 FROM {schema}.compras WHERE ncf = $1", ecf.ncf)
                if exists:
                    duplicados += 1
                    continue

                # 2. Descargar XML original de la DGII
                xml_data = None
                try:
                    async with DGIIClient(ambiente) as client:
                        client.set_certificate(cert_data, cert_password)
                        await client._authenticate()
                        # El endpoint ecfxml suele requerir NCF y RNC Emisor
                        resp = await client._client.get(
                            self.EP_ECF_XML,
                            params={"ncf": ecf.ncf, "rncEmisor": ecf.rnc_emisor},
                            headers=client._auth_headers()
                        )
                        if resp.status_code == 200:
                            xml_data = resp.content
                except Exception as e:
                    logger.warning("[%s] No se pudo descargar XML para %s: %s", tenant["rnc"], ecf.ncf, e)

                # 3. Clasificar tipo de bien/servicio según tipo_ecf
                tipo_bienes = _tipo_bienes_por_defecto(ecf.tipo_ecf)

                result = await conn.execute(f"""
                    INSERT INTO {schema}.compras (
                        id, ncf, rnc_proveedor, nombre_proveedor,
                        tipo_bienes, tipo_ecf, cufe, xml_original,
                        fecha_comprobante, total_monto,
                        itbis_facturado, monto_servicios, monto_bienes,
                        ambiente, estado_odoo
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,'nueva')
                    ON CONFLICT (ncf) DO NOTHING
                """,
                    uuid.uuid4(),
                    ecf.ncf,
                    ecf.rnc_emisor,
                    ecf.nombre_emisor,
                    tipo_bienes,
                    ecf.tipo_ecf,
                    ecf.cufe,
                    xml_data,
                    ecf.fecha_emision,
                    ecf.total_monto,
                    ecf.itbis_facturado,
                    ecf.subtotal if tipo_bienes == 2 else Decimal("0"),
                    ecf.subtotal if tipo_bienes == 1 else Decimal("0"),
                    ambiente,
                )
                if result == "INSERT 0 1":
                    nuevos += 1
                else:
                    duplicados += 1

        return nuevos, duplicados

    # ─────────────────────────────────────────────────────────────────────────
    # Notificación a Odoo
    # ─────────────────────────────────────────────────────────────────────────

    async def _notificar_odoo(self, tenant: dict, schema: str) -> int:
        """
        Envía las compras pendientes de estado 'nueva' al webhook de Odoo.
        Usa HMAC-SHA256 para autenticación.
        Retorna el número de notificaciones exitosas.
        """
        webhook_url    = tenant.get("odoo_webhook_url", "")
        webhook_secret = tenant.get("odoo_webhook_secret", "")

        if not webhook_url:
            return 0

        # Obtener compras nuevas
        async with self.db.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT ncf, rnc_proveedor, nombre_proveedor, tipo_ecf,
                       fecha_comprobante, total_monto, itbis_facturado,
                       monto_servicios, monto_bienes, cufe, ambiente
                FROM {schema}.compras
                WHERE estado_odoo = 'nueva'
                ORDER BY fecha_comprobante, ncf
                LIMIT 50
            """)

        if not rows:
            return 0

        compras_list = []
        for r in rows:
            compra = dict(r)
            # Serializar fechas
            if isinstance(compra.get("fecha_comprobante"), date):
                compra["fecha_comprobante"] = compra["fecha_comprobante"].isoformat()
            # Serializar Decimals
            for k, v in compra.items():
                if isinstance(v, Decimal):
                    compra[k] = str(v)
            compras_list.append(compra)

        payload = {
            "evento":     "ecf_recibidas",
            "tenant_rnc": tenant["rnc"],
            "compras":    compras_list,
            "timestamp":  datetime.now(timezone.utc).isoformat(),
        }
        payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        # Firma HMAC-SHA256
        headers = {
            "Content-Type": "application/json",
            "X-ECF-Event":  "ecf_recibidas",
        }
        if webhook_secret:
            sig = hmac.new(
                webhook_secret.encode("utf-8"),
                payload_bytes,
                hashlib.sha256,
            ).hexdigest()
            headers["X-ECF-Signature"] = f"sha256={sig}"

        notificados = 0
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(webhook_url + "/ecf/webhook/recibida", content=payload_bytes, headers=headers)
                if resp.status_code in (200, 201, 202, 204):
                    # Marcar como enviadas
                    ncfs = [r["ncf"] for r in rows]
                    async with self.db.acquire() as conn:
                        await conn.execute(
                            f"UPDATE {schema}.compras SET estado_odoo='enviada', updated_at=NOW() WHERE ncf = ANY($1)",
                            ncfs,
                        )
                    notificados = len(ncfs)
                    logger.info("Webhook Odoo OK: %d compras notificadas", notificados)
                else:
                    logger.warning("Webhook Odoo respondió %d: %s", resp.status_code, resp.text[:200])
        except httpx.RequestError as e:
            logger.error("Error enviando webhook a Odoo: %s", e)

        return notificados

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers de tracking
    # ─────────────────────────────────────────────────────────────────────────

    async def _obtener_ultima_fecha(self, schema: str) -> date:
        """Obtiene la última fecha consultada. Por defecto retorna ayer."""
        async with self.db.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT ultima_fecha_consultada FROM {schema}.ecf_recibidas_sync "
                f"ORDER BY created_at DESC LIMIT 1"
            )
        if row:
            return row["ultima_fecha_consultada"]
        # Primera sync: los últimos 30 días
        return date.today() - timedelta(days=30)

    async def _actualizar_sync(
        self,
        schema: str,
        fecha_hasta: date,
        nuevos: int,
        errores: int,
        error_msg: str | None = None,
    ):
        """Registra el resultado de la sincronización."""
        async with self.db.acquire() as conn:
            await conn.execute(f"""
                INSERT INTO {schema}.ecf_recibidas_sync
                    (ultima_fecha_consultada, total_nuevos, total_errores, error_mensaje)
                VALUES ($1, $2, $3, $4)
            """, fecha_hasta, nuevos, errores, error_msg)

    # ─────────────────────────────────────────────────────────────────────────
    # Sincronizar todos los tenants activos
    # ─────────────────────────────────────────────────────────────────────────

    async def sincronizar_todos_los_tenants(self) -> list[ResultadoSync]:
        """
        Sincroniza todos los tenants activos con la DGII.
        Diseñado para ejecutarse desde el scheduler (cada 30 minutos).
        """
        async with self.db.acquire() as conn:
            tenants = await conn.fetch("""
                SELECT id, rnc, razon_social, schema_name, ambiente,
                       odoo_webhook_url, odoo_webhook_secret
                FROM public.tenants
                WHERE estado = 'activo'
                  AND deleted_at IS NULL
                  AND cert_vencimiento >= CURRENT_DATE
                ORDER BY razon_social
            """)

        resultados = []
        for tenant in tenants:
            tenant_dict = dict(tenant)
            resultado = await self.sincronizar_tenant(tenant_dict)
            resultados.append(resultado)
            # Pausa entre tenants para no saturar la API DGII
            await asyncio.sleep(2)

        total_nuevos = sum(r.nuevos for r in resultados)
        total_errores = sum(r.errores for r in resultados)
        logger.info(
            "Sync global completada: %d tenants, %d nuevas e-CF, %d errores",
            len(resultados), total_nuevos, total_errores
        )
        return resultados


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _tipo_bienes_por_defecto(tipo_ecf: int) -> int:
    """
    Clasifica como bien (1) o servicio (2) según el tipo de e-CF.
    Tipos 41 (compras) y 43 (gastos menores) incluyen bienes.
    El resto se trata como servicio por defecto.
    """
    if tipo_ecf in (41, 43):
        return 1  # Bien
    return 2  # Servicio
