"""
Servicio RFCE — Resumen de Factura de Consumo Electrónica (Tipo 32 < RD$250,000).

Conforme al Manual Técnico e-CF de la DGII y al esquema ``xsd/RFCE-32.xsd``:

- El RFCE es un documento *por factura* (no un batch diario): cuando una
  Factura de Consumo (tipo 32) tiene un monto total menor a RD$250,000, el
  emisor NO envía el XML completo del e-CF a la DGII; envía este resumen al
  host ``fc.dgii.gov.do``. El XML completo firmado se conserva localmente
  (retención 10 años) y respalda el Código de Seguridad del QR.

Estructura XSD (RFCE-32.xsd)::

    RFCE
    └── Encabezado
        ├── Version (1.0)
        ├── IdDoc        (TipoeCF, eNCF, TipoIngresos, TipoPago)
        ├── Emisor       (RNCEmisor, RazonSocialEmisor, FechaEmision dd-mm-yyyy)
        ├── Comprador    (RNCComprador?, RazonSocialComprador?)
        ├── Totales      (MontoGravadoTotal?, ..., MontoTotal, ...)
        └── CodigoSeguridadeCF  (6 chars — del e-CF de consumo original)
    └── <ds:Signature>   (xs:any — firma XAdES)
"""

from __future__ import annotations

import json
import logging
import uuid as uuid_mod
from decimal import Decimal
from uuid import UUID

from lxml import etree

from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.dgii_client import DGIIClient, EstadoDGII, RespuestaDGII
from ecf_core.ecf_core_service import ECFSigner, ECFValidator, FacturaECF
from ecf_core.utils import safe_schema

logger = logging.getLogger(__name__)

# Umbral DGII: facturas de consumo por debajo de este monto se reportan con RFCE
UMBRAL_RFCE = Decimal("250000.00")


def requiere_rfce(tipo_ecf: int, total: Decimal) -> bool:
    """True si el e-CF debe reportarse a la DGII como RFCE (resumen)."""
    return tipo_ecf == 32 and Decimal(str(total)) < UMBRAL_RFCE


class RFCEGenerator:
    """Genera el XML del RFCE conforme a ``xsd/RFCE-32.xsd``."""

    def generar(self, factura: FacturaECF, security_code: str) -> bytes:
        if not security_code or len(security_code) != 6:
            raise ValueError(
                f"CodigoSeguridadeCF inválido ({security_code!r}) — deben ser "
                f"exactamente 6 caracteres del SignatureValue del e-CF original"
            )

        root = etree.Element("RFCE")
        enc = etree.SubElement(root, "Encabezado")

        etree.SubElement(enc, "Version").text = "1.0"

        id_doc = etree.SubElement(enc, "IdDoc")
        etree.SubElement(id_doc, "TipoeCF").text = "32"
        etree.SubElement(id_doc, "eNCF").text = factura.ncf
        etree.SubElement(id_doc, "TipoIngresos").text = factura.tipo_ingresos
        etree.SubElement(id_doc, "TipoPago").text = factura.tipo_pago

        emisor = etree.SubElement(enc, "Emisor")
        etree.SubElement(emisor, "RNCEmisor").text = factura.rnc_emisor
        etree.SubElement(emisor, "RazonSocialEmisor").text = factura.razon_social_emisor
        etree.SubElement(emisor, "FechaEmision").text = factura.fecha_emision.strftime("%d-%m-%Y")

        # Comprador es obligatorio en el XSD; sus hijos son opcionales
        comprador = etree.SubElement(enc, "Comprador")
        if factura.rnc_comprador:
            etree.SubElement(comprador, "RNCComprador").text = factura.rnc_comprador
        if factura.nombre_comprador:
            etree.SubElement(comprador, "RazonSocialComprador").text = factura.nombre_comprador[:150]

        totales = etree.SubElement(enc, "Totales")
        monto_gravado_total = factura.subtotal - factura.monto_exento
        if monto_gravado_total > 0:
            etree.SubElement(totales, "MontoGravadoTotal").text = f"{monto_gravado_total:.2f}"
        if factura.monto_gravado_i1 > 0:
            etree.SubElement(totales, "MontoGravadoI1").text = f"{factura.monto_gravado_i1:.2f}"
        if factura.monto_gravado_i2 > 0:
            etree.SubElement(totales, "MontoGravadoI2").text = f"{factura.monto_gravado_i2:.2f}"
        if factura.monto_exento > 0:
            etree.SubElement(totales, "MontoExento").text = f"{factura.monto_exento:.2f}"
        if factura.total_itbis > 0:
            etree.SubElement(totales, "TotalITBIS").text = f"{factura.total_itbis:.2f}"
        if factura.total_itbis1 > 0:
            etree.SubElement(totales, "TotalITBIS1").text = f"{factura.total_itbis1:.2f}"
        if factura.total_itbis2 > 0:
            etree.SubElement(totales, "TotalITBIS2").text = f"{factura.total_itbis2:.2f}"
        etree.SubElement(totales, "MontoTotal").text = f"{factura.total:.2f}"

        etree.SubElement(enc, "CodigoSeguridadeCF").text = security_code

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


class RFCEService:
    """Orquesta generación, firma, validación XSD, persistencia y envío del RFCE."""

    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.generator = RFCEGenerator()
        self.signer = ECFSigner()
        self.validator = ECFValidator()

    def generar_y_firmar(
        self,
        factura: FacturaECF,
        security_code: str,
        p12_data: bytes,
        p12_password: bytes,
    ) -> bytes:
        """Genera el RFCE, lo firma y lo valida contra RFCE-32.xsd.

        Lanza excepción si la firma o la validación fallan — un RFCE sin firma
        o inválido NUNCA debe persistirse ni enviarse a la DGII.
        """
        xml = self.generator.generar(factura, security_code)
        xml_firmado = self.signer.firmar(xml, p12_data, p12_password)
        valido, errores = self.validator.validar_evento(xml_firmado, "RFCE-32")
        if not valido:
            raise ValueError(f"RFCE no válido contra XSD DGII: {'; '.join(errores)}")
        return xml_firmado

    async def emitir_rfce(
        self,
        tenant: dict,
        ecf_id: str,
        factura: FacturaECF,
        security_code: str,
        p12_data: bytes,
        p12_password: bytes,
        ambiente: str,
    ) -> RespuestaDGII:
        """Flujo completo para un e-CF tipo 32 < 250k: RFCE → fc.dgii.gov.do.

        Persiste el RFCE en ``{schema}.rfce`` y enlaza ``ecf.rfce_id``.
        """
        schema = safe_schema(tenant["schema_name"])
        xml_firmado = self.generar_y_firmar(factura, security_code, p12_data, p12_password)

        rfce_id = await self._upsert_rfce(
            schema, factura.ncf, factura.fecha_emision, factura.total, xml_firmado,
        )

        async with DGIIClient(ambiente=ambiente) as dgii:
            dgii.set_certificate(p12_data, p12_password)
            resp = await dgii.enviar_rfce(xml_firmado, ncf=factura.ncf)

        estado = self._estado_local(resp.estado)
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {schema}.rfce SET estado=$1, track_id=$2, "
                f"respuesta_dgii=$3::jsonb, updated_at=NOW() WHERE id=$4",
                estado, resp.track_id, json.dumps(resp.raw), rfce_id,
            )
            # Enlazar solo tras respuesta DGII (evita FK a UUID fantasma en reintentos)
            await conn.execute(
                f"UPDATE {schema}.ecf SET rfce_id = $1, updated_at = NOW() WHERE id = $2",
                rfce_id, UUID(ecf_id),
            )

        logger.info(
            "RFCE %s enviado a DGII (fc). Tenant=%s estado=%s track_id=%s",
            factura.ncf, tenant["rnc"], estado, resp.track_id,
        )
        return resp

    async def _upsert_rfce(
        self,
        schema: str,
        ncf: str,
        fecha_resumen,
        monto_total,
        xml_firmado: bytes,
    ) -> UUID:
        """INSERT o UPDATE idempotente por NCF; siempre RETURNing el id real."""
        nuevo_id = uuid_mod.uuid4()
        async with self.db_pool.acquire() as conn:
            rfce_id = await conn.fetchval(
                f"INSERT INTO {schema}.rfce "
                f"(id, ncf, fecha_resumen, estado, cantidad_facturas, monto_total, xml_firmado) "
                f"VALUES ($1, $2, $3, 'pendiente', 1, $4, $5) "
                f"ON CONFLICT (ncf) DO UPDATE SET "
                f"  xml_firmado = EXCLUDED.xml_firmado, "
                f"  monto_total = EXCLUDED.monto_total, "
                f"  estado = CASE "
                f"    WHEN {schema}.rfce.estado = 'aprobado' THEN {schema}.rfce.estado "
                f"    ELSE 'pendiente' END, "
                f"  updated_at = NOW() "
                f"RETURNING id",
                nuevo_id, ncf, fecha_resumen, monto_total, xml_firmado,
            )
        return rfce_id

    @staticmethod
    def _estado_local(estado: EstadoDGII) -> str:
        # CHECK del schema rfce: pendiente | enviado | aprobado | rechazado
        return {
            EstadoDGII.ACEPTADO:     "aprobado",
            EstadoDGII.CONDICIONADO: "aprobado",
            EstadoDGII.RECHAZADO:    "rechazado",
            EstadoDGII.PROCESANDO:   "enviado",
            EstadoDGII.RECIBIDO:     "enviado",
        }.get(estado, "enviado")

    # ─────────────────────────────────────────────────────────────────────────
    # Job de reconciliación (scheduler): RFCE que quedaron sin enviar
    # ─────────────────────────────────────────────────────────────────────────

    async def procesar_rfce_pendientes(self, tenant_id: UUID) -> dict:
        """Red de seguridad: reenvía RFCE de e-CF tipo 32 < 250k sin rfce_id.

        El flujo normal emite el RFCE en el worker al momento de la emisión;
        este job cubre e-CF históricos o fallos transitorios del host fc.
        """
        async with self.db_pool.acquire() as conn:
            tenant = await conn.fetchrow(
                "SELECT id, rnc, razon_social, nombre_comercial, direccion, "
                "schema_name, ambiente, cert_password "
                "FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
                tenant_id,
            )
            if not tenant:
                raise ValueError(f"Tenant {tenant_id} no encontrado")

            schema = safe_schema(tenant["schema_name"])
            # Sin rfce_id, o con RFCE atascado en pendiente/rechazado (reintento).
            pendientes = await conn.fetch(
                f"SELECT e.id, e.ncf, e.tipo_ecf, e.rnc_comprador, e.nombre_comprador, "
                f"e.fecha_emision, e.subtotal, e.itbis, e.total, e.security_code, "
                f"e.tipo_pago, e.tipo_ingresos "
                f"FROM {schema}.ecf e "
                f"LEFT JOIN {schema}.rfce r ON r.id = e.rfce_id "
                f"WHERE e.tipo_ecf = 32 AND e.total < $1 "
                f"  AND e.estado IN ('enviado', 'aprobado') "
                f"  AND e.security_code IS NOT NULL "
                f"  AND (e.rfce_id IS NULL OR r.estado IN ('pendiente', 'rechazado')) "
                f"LIMIT 100",
                UMBRAL_RFCE,
            )

        if not pendientes:
            return {"procesados": 0, "errores": 0}

        vault = CertVault()
        cert_repo = CertVaultRepository(self.db_pool, vault)
        cert_info = await cert_repo.obtener_certificado(str(tenant_id))
        p12_data = cert_info["cert_data"]
        p12_password = (
            cert_info["cert_password"].encode()
            if isinstance(cert_info["cert_password"], str)
            else cert_info["cert_password"]
        )

        procesados = 0
        errores = 0
        for row in pendientes:
            try:
                factura = FacturaECF(
                    tipo_ecf=32,
                    ncf=row["ncf"],
                    rnc_emisor=tenant["rnc"],
                    razon_social_emisor=tenant["razon_social"],
                    direccion_emisor=tenant["direccion"] or "",
                    fecha_emision=row["fecha_emision"],
                    rnc_comprador=row["rnc_comprador"],
                    nombre_comprador=row["nombre_comprador"],
                    tipo_pago=row["tipo_pago"] or "1",
                    tipo_ingresos=row["tipo_ingresos"] or "01",
                )
                # Totales del RFCE de reconciliación: usar montos persistidos.
                # Sin items no hay desglose por tasa: el RFCE lleva MontoTotal
                # (obligatorio) y TotalITBIS.
                await self._emitir_rfce_desde_montos(
                    tenant=dict(tenant),
                    ecf_id=str(row["id"]),
                    factura=factura,
                    subtotal=row["subtotal"],
                    itbis=row["itbis"],
                    total=row["total"],
                    security_code=row["security_code"],
                    p12_data=p12_data,
                    p12_password=p12_password,
                )
                procesados += 1
            except Exception as e:
                errores += 1
                logger.error("Error reenviando RFCE para NCF %s: %s", row["ncf"], e)

        return {"procesados": procesados, "errores": errores}

    async def _emitir_rfce_desde_montos(
        self,
        tenant: dict,
        ecf_id: str,
        factura: FacturaECF,
        subtotal: Decimal,
        itbis: Decimal,
        total: Decimal,
        security_code: str,
        p12_data: bytes,
        p12_password: bytes,
    ):
        """Variante de emisión para reconciliación (sin items detallados)."""
        root = etree.Element("RFCE")
        enc = etree.SubElement(root, "Encabezado")
        etree.SubElement(enc, "Version").text = "1.0"
        id_doc = etree.SubElement(enc, "IdDoc")
        etree.SubElement(id_doc, "TipoeCF").text = "32"
        etree.SubElement(id_doc, "eNCF").text = factura.ncf
        etree.SubElement(id_doc, "TipoIngresos").text = factura.tipo_ingresos
        etree.SubElement(id_doc, "TipoPago").text = factura.tipo_pago
        emisor = etree.SubElement(enc, "Emisor")
        etree.SubElement(emisor, "RNCEmisor").text = factura.rnc_emisor
        etree.SubElement(emisor, "RazonSocialEmisor").text = factura.razon_social_emisor
        etree.SubElement(emisor, "FechaEmision").text = factura.fecha_emision.strftime("%d-%m-%Y")
        comprador = etree.SubElement(enc, "Comprador")
        if factura.rnc_comprador:
            etree.SubElement(comprador, "RNCComprador").text = factura.rnc_comprador
        if factura.nombre_comprador:
            etree.SubElement(comprador, "RazonSocialComprador").text = factura.nombre_comprador[:150]
        totales = etree.SubElement(enc, "Totales")
        if subtotal and subtotal > 0 and itbis and itbis > 0:
            etree.SubElement(totales, "MontoGravadoTotal").text = f"{subtotal:.2f}"
            etree.SubElement(totales, "TotalITBIS").text = f"{itbis:.2f}"
        elif subtotal and subtotal > 0:
            etree.SubElement(totales, "MontoExento").text = f"{subtotal:.2f}"
        etree.SubElement(totales, "MontoTotal").text = f"{total:.2f}"
        etree.SubElement(enc, "CodigoSeguridadeCF").text = security_code

        xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
        xml_firmado = self.signer.firmar(xml, p12_data, p12_password)
        valido, errores = self.validator.validar_evento(xml_firmado, "RFCE-32")
        if not valido:
            raise ValueError(f"RFCE no válido contra XSD DGII: {'; '.join(errores)}")

        schema = safe_schema(tenant["schema_name"])
        rfce_id = await self._upsert_rfce(
            schema, factura.ncf, factura.fecha_emision, total, xml_firmado,
        )

        async with DGIIClient(ambiente=tenant["ambiente"]) as dgii:
            dgii.set_certificate(p12_data, p12_password)
            resp = await dgii.enviar_rfce(xml_firmado, ncf=factura.ncf)

        estado = self._estado_local(resp.estado)
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {schema}.rfce SET estado=$1, track_id=$2, "
                f"respuesta_dgii=$3::jsonb, updated_at=NOW() WHERE id=$4",
                estado, resp.track_id, json.dumps(resp.raw), rfce_id,
            )
            await conn.execute(
                f"UPDATE {schema}.ecf SET rfce_id = $1, updated_at = NOW() WHERE id = $2",
                rfce_id, UUID(ecf_id),
            )
        return resp
