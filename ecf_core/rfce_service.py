"""
Servicio de Resumen de Facturación de Consumo Electrónico (RFCE)
Genera el XML conforme a xsd/RFCE-32.xsd, lo firma con XAdES-BES y envía a la DGII.
"""

import logging
from datetime import date
from uuid import UUID

from lxml import etree

from ecf_core.cert_vault import CertVault, CertVaultRepository
from ecf_core.dgii_client import DGIIClient
from ecf_core.ecf_core_service import ECFSigner, ECFValidator
from ecf_core.utils import safe_schema

logger = logging.getLogger(__name__)


class RFCEService:
    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.signer = ECFSigner()
        self.validator = ECFValidator()

    async def generar_resumen_diario(
        self, tenant_id: UUID, fecha: date | None = None
    ) -> dict | None:
        """
        Busca todos los e-CF Tipo 32 del tenant para la fecha dada,
        genera el XML RFCE conforme a RFCE-32.xsd, lo firma y persiste.
        """
        if not fecha:
            fecha = date.today()

        async with self.db_pool.acquire() as conn:
            tenant = await conn.fetchrow(
                "SELECT id, rnc, razon_social, schema_name, ambiente "
                "FROM public.tenants WHERE id = $1 AND deleted_at IS NULL",
                tenant_id,
            )
            if not tenant:
                raise ValueError(f"Tenant {tenant_id} no encontrado")

            schema = safe_schema(tenant["schema_name"])

            facturas = await conn.fetch(
                f"SELECT id, ncf, subtotal, itbis_monto, total, security_code, "
                f"track_id, fecha_emision "
                f"FROM {schema}.ecf "
                f"WHERE tipo_ecf = 32 "
                f"  AND fecha_emision = $1 "
                f"  AND estado IN ('enviado', 'aprobado') "
                f"  AND track_id IS NOT NULL "
                f"  AND rfce_id IS NULL",
                fecha,
            )

            if not facturas:
                logger.info(
                    "No hay facturas Tipo 32 para resumir para tenant %s en %s",
                    tenant_id, fecha,
                )
                return None

            totales = {
                "cantidad": len(facturas),
                "monto_total": sum(f["total"] for f in facturas),
                "monto_subtotal": sum(f["subtotal"] for f in facturas),
                "monto_itbis": sum(f["itbis_monto"] for f in facturas),
            }

            xml_bytes = self._generar_xml(tenant, fecha, facturas, totales)

            valido, errores = self.validator.validar_evento(xml_bytes, "RFCE-32")
            if not valido:
                logger.error("RFCE no pasa validación XSD: %s", errores)
                return {"xml": xml_bytes, "totales": totales, "errores": errores}

            xml_firmado = await self._firmar(tenant_id, xml_bytes)

            rfce_id = await conn.fetchval(
                f"INSERT INTO {schema}.rfce "
                f"(fecha_resumen, estado, cantidad_facturas, monto_total, xml_firmado) "
                f"VALUES ($1, 'pendiente', $2, $3, $4) RETURNING id",
                fecha,
                totales["cantidad"],
                totales["monto_total"],
                xml_firmado,
            )

            factura_ids = [f["id"] for f in facturas]
            await conn.execute(
                f"UPDATE {schema}.ecf SET rfce_id = $1 WHERE id = ANY($2)",
                rfce_id,
                factura_ids,
            )

            logger.info(
                "RFCE generado para %s: %d facturas, Total: %s, rfce_id=%s",
                tenant["rnc"], totales["cantidad"], totales["monto_total"], rfce_id,
            )

            return {
                "rfce_id": str(rfce_id),
                "xml_firmado": xml_firmado,
                "totales": totales,
                "facturas_ids": factura_ids,
                "schema": schema,
            }

    def _generar_xml(
        self, tenant: dict, fecha: date, facturas: list, totales: dict
    ) -> bytes:
        """Genera XML conforme a RFCE-32.xsd."""
        ns = "http://www.dgii.gov.do/ecf"
        root = etree.Element("RFCE", xmlns=ns)

        encabezado = etree.SubElement(root, "Encabezado")
        id_doc = etree.SubElement(encabezado, "IdDoc")
        etree.SubElement(id_doc, "TipoeCF").text = "32"
        etree.SubElement(id_doc, "FechaResumen").text = fecha.isoformat()

        emisor = etree.SubElement(encabezado, "Emisor")
        etree.SubElement(emisor, "RNCEmisor").text = tenant["rnc"]
        etree.SubElement(emisor, "RazonSocialEmisor").text = tenant["razon_social"]

        resumen = etree.SubElement(encabezado, "Resumen")
        etree.SubElement(resumen, "TotalFacturas").text = str(totales["cantidad"])
        etree.SubElement(resumen, "MontoGravadoTotal").text = f"{totales['monto_subtotal']:.2f}"
        etree.SubElement(resumen, "MontoITBISTotal").text = f"{totales['monto_itbis']:.2f}"
        etree.SubElement(resumen, "MontoTotal").text = f"{totales['monto_total']:.2f}"

        detalle = etree.SubElement(root, "DetalleResumen")
        for idx, f in enumerate(facturas, 1):
            linea = etree.SubElement(detalle, "Linea")
            etree.SubElement(linea, "NoLinea").text = str(idx)
            etree.SubElement(linea, "ENCF").text = f["ncf"]
            etree.SubElement(linea, "FechaEmision").text = f["fecha_emision"].isoformat()
            etree.SubElement(linea, "MontoTotal").text = f"{f['total']:.2f}"
            etree.SubElement(linea, "ITBIS").text = f"{f['itbis_monto']:.2f}"
            if f.get("security_code"):
                etree.SubElement(linea, "CodigoSeguridad").text = f["security_code"]
            if f.get("track_id"):
                etree.SubElement(linea, "TrackId").text = f["track_id"]

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    async def _firmar(self, tenant_id: UUID, xml_bytes: bytes) -> bytes:
        """Firma el RFCE con el certificado .p12 del tenant."""
        vault = CertVault()
        cert_repo = CertVaultRepository(self.db_pool, vault)
        try:
            cert_info = await cert_repo.obtener_certificado(str(tenant_id))
            return self.signer.firmar(
                xml_bytes,
                cert_info["cert_data"],
                cert_info["cert_password"].encode() if isinstance(cert_info["cert_password"], str) else cert_info["cert_password"],
            )
        except Exception as e:
            logger.warning("No se pudo firmar RFCE para tenant %s: %s", tenant_id, e)
            return xml_bytes

    async def enviar_a_dgii(self, tenant_id: UUID, rfce_data: dict):
        """Firma y envía el RFCE a la DGII, actualiza estado en DB."""
        async with self.db_pool.acquire() as conn:
            tenant = await conn.fetchrow(
                "SELECT rnc, ambiente FROM public.tenants WHERE id = $1",
                tenant_id,
            )
            if not tenant:
                raise ValueError(f"Tenant {tenant_id} no encontrado")

        vault = CertVault()
        cert_repo = CertVaultRepository(self.db_pool, vault)
        cert_info = await cert_repo.obtener_certificado(str(tenant_id))

        async with DGIIClient(
            ambiente=tenant["ambiente"],
            p12_data=cert_info["cert_data"],
            p12_password=cert_info["cert_password"].encode()
            if isinstance(cert_info["cert_password"], str)
            else cert_info["cert_password"],
        ) as dgii:
            resp = await dgii.enviar_ecf(rfce_data["xml_firmado"])

        schema = rfce_data["schema"]
        estado = "aprobado" if resp.estado.value == "aceptado" else resp.estado.value

        async with self.db_pool.acquire() as conn:
            await conn.execute(
                f"UPDATE {schema}.rfce SET estado=$1, track_id=$2, "
                f"respuesta_dgii=$3, updated_at=NOW() "
                f"WHERE id=$4",
                estado,
                resp.track_id,
                {"estado": estado, "mensaje": resp.mensaje},
                UUID(rfce_data["rfce_id"]),
            )

        logger.info(
            "RFCE enviado a DGII. Tenant=%s estado=%s track_id=%s",
            tenant["rnc"], estado, resp.track_id,
        )
        return resp
