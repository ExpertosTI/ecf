"""ecf_interchange_service.py — Eventos de intercambio DGII (ACECF y ARECF).

Conforme a los esquemas oficiales en ``xsd/``:

ACECF — Aprobación Comercial (`xsd/ACECF.xsd`)::

    <ACECF>
      <DetalleAprobacionComercial>
        <Version>1.0</Version>
        <RNCEmisor>...</RNCEmisor>                       <!-- emisor del e-CF original -->
        <eNCF>...</eNCF>
        <FechaEmision>DD-MM-YYYY</FechaEmision>
        <MontoTotal>0.00</MontoTotal>
        <RNCComprador>...</RNCComprador>                 <!-- quien aprueba comercialmente -->
        <Estado>1|2</Estado>                             <!-- 1=Aceptado, 2=Rechazado -->
        <DetalleMotivoRechazo>...</DetalleMotivoRechazo> <!-- requerido si Estado=2 -->
        <FechaHoraAprobacionComercial>DD-MM-YYYY HH:MM:SS</FechaHoraAprobacionComercial>
      </DetalleAprobacionComercial>
      <ds:Signature .../>
    </ACECF>

ARECF — Acuse de Recibo (`xsd/ARECF.xsd`)::

    <ARECF>
      <DetalleAcusedeRecibo>
        <Version>1.0</Version>
        <RNCEmisor>...</RNCEmisor>
        <RNCComprador>...</RNCComprador>
        <eNCF>...</eNCF>
        <Estado>0|1</Estado>                             <!-- 0=Recibido, 1=No Recibido -->
        <CodigoMotivoNoRecibido>1..4</CodigoMotivoNoRecibido>  <!-- si Estado=1 -->
        <FechaHoraAcuseRecibo>DD-MM-YYYY HH:MM:SS</FechaHoraAcuseRecibo>
      </DetalleAcusedeRecibo>
      <ds:Signature .../>
    </ARECF>

Importante: ARECF (acuse de recibo) y ACECF (aprobación comercial) son eventos
distintos. El rechazo comercial NO es un ARECF, es un ACECF con Estado=2.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from lxml import etree

from ecf_core.ecf_core_service import ECFSigner, ECFValidator
from ecf_core.utils import fmt_fecha_hora_dgii

logger = logging.getLogger(__name__)


def _fmt_fecha(d) -> str:
    """DGII espera DD-MM-YYYY (no YYYY-MM-DD)."""
    if isinstance(d, str):
        return d
    return d.strftime("%d-%m-%Y")


def _fmt_fecha_hora(dt: datetime | None = None) -> str:
    return fmt_fecha_hora_dgii(dt)


def _fmt_monto(monto) -> str:
    """Decimal con 2 fracciones obligatorias (Decimal18D2Validation)."""
    if monto is None:
        return "0.00"
    return f"{Decimal(str(monto)):.2f}"


class ECFInterchangeService:
    """Genera, valida XSD y firma los eventos de intercambio DGII (ACECF/ARECF)."""

    def __init__(self, signer: ECFSigner, validator: ECFValidator | None = None):
        self.signer = signer
        self.validator = validator or ECFValidator()

    # ─────────────────────────────────────────────────────────────────────
    # Generadores XML conforme a XSD
    # ─────────────────────────────────────────────────────────────────────

    def generar_aprobacion_comercial(
        self,
        ncf: str,
        rnc_emisor: str,
        rnc_comprador: str,
        fecha_emision,
        monto_total,
        estado: int = 1,
        motivo_rechazo: str | None = None,
        fecha_hora_aprobacion: datetime | None = None,
        version: str = "1.0",
    ) -> bytes:
        """Genera el XML ACECF (Aprobación Comercial) conforme a `xsd/ACECF.xsd`.

        :param estado: 1=Aceptado, 2=Rechazado (DetalleMotivoRechazo requerido)
        """
        if estado not in (1, 2):
            raise ValueError(f"Estado ACECF inválido: {estado} (debe ser 1 o 2)")
        if estado == 2 and not motivo_rechazo:
            raise ValueError("ACECF Estado=2 (Rechazado) requiere motivo_rechazo")

        root = etree.Element("ACECF")
        detalle = etree.SubElement(root, "DetalleAprobacionComercial")

        etree.SubElement(detalle, "Version").text = version
        etree.SubElement(detalle, "RNCEmisor").text = str(rnc_emisor)
        etree.SubElement(detalle, "eNCF").text = ncf
        etree.SubElement(detalle, "FechaEmision").text = _fmt_fecha(fecha_emision)
        etree.SubElement(detalle, "MontoTotal").text = _fmt_monto(monto_total)
        etree.SubElement(detalle, "RNCComprador").text = str(rnc_comprador)
        etree.SubElement(detalle, "Estado").text = str(estado)
        if estado == 2 and motivo_rechazo:
            etree.SubElement(detalle, "DetalleMotivoRechazo").text = motivo_rechazo[:250]
        etree.SubElement(detalle, "FechaHoraAprobacionComercial").text = _fmt_fecha_hora(
            fecha_hora_aprobacion
        )

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    def generar_acuse_recibo(
        self,
        ncf: str,
        rnc_emisor: str,
        rnc_comprador: str,
        estado: int = 0,
        codigo_motivo_no_recibido: int | None = None,
        fecha_hora_acuse: datetime | None = None,
        version: str = "1.0",
    ) -> bytes:
        """Genera el XML ARECF (Acuse de Recibo) conforme a `xsd/ARECF.xsd`.

        :param estado: 0=Recibido, 1=No Recibido (CodigoMotivoNoRecibido requerido)
        :param codigo_motivo_no_recibido: 1=Error Especificación, 2=Error Firma,
                                           3=Envío Duplicado, 4=RNC Comprador no Corresponde
        """
        if estado not in (0, 1):
            raise ValueError(f"Estado ARECF inválido: {estado} (debe ser 0 o 1)")
        if estado == 1 and codigo_motivo_no_recibido not in (1, 2, 3, 4):
            raise ValueError(
                "ARECF Estado=1 requiere CodigoMotivoNoRecibido en (1,2,3,4)"
            )

        root = etree.Element("ARECF")
        detalle = etree.SubElement(root, "DetalleAcusedeRecibo")

        etree.SubElement(detalle, "Version").text = version
        etree.SubElement(detalle, "RNCEmisor").text = str(rnc_emisor)
        etree.SubElement(detalle, "RNCComprador").text = str(rnc_comprador)
        etree.SubElement(detalle, "eNCF").text = ncf
        etree.SubElement(detalle, "Estado").text = str(estado)
        if estado == 1 and codigo_motivo_no_recibido:
            etree.SubElement(detalle, "CodigoMotivoNoRecibido").text = str(
                codigo_motivo_no_recibido
            )
        etree.SubElement(detalle, "FechaHoraAcuseRecibo").text = _fmt_fecha_hora(
            fecha_hora_acuse
        )

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8")

    # ─────────────────────────────────────────────────────────────────────
    # Orquestación: generar → validar XSD → firmar
    # ─────────────────────────────────────────────────────────────────────

    async def procesar_aprobacion_comercial(
        self,
        ncf: str,
        rnc_emisor: str,
        rnc_comprador: str,
        fecha_emision,
        monto_total,
        cert_data: bytes,
        cert_password: bytes,
        estado: int = 1,
        motivo_rechazo: str | None = None,
    ) -> bytes:
        """Genera ACECF, valida contra XSD y firma con XAdES-BES."""
        xml = self.generar_aprobacion_comercial(
            ncf=ncf,
            rnc_emisor=rnc_emisor,
            rnc_comprador=rnc_comprador,
            fecha_emision=fecha_emision,
            monto_total=monto_total,
            estado=estado,
            motivo_rechazo=motivo_rechazo,
        )

        valido, errores = self.validator.validar_evento(xml, "ACECF")
        if not valido:
            logger.error("ACECF no pasa validación XSD: %s", errores)
            raise ValueError(f"ACECF no válido contra ACECF.xsd: {errores}")

        return self.signer.firmar(xml, cert_data, cert_password)

    async def procesar_acuse_recibo(
        self,
        ncf: str,
        rnc_emisor: str,
        rnc_comprador: str,
        cert_data: bytes,
        cert_password: bytes,
        estado: int = 0,
        codigo_motivo_no_recibido: int | None = None,
    ) -> bytes:
        """Genera ARECF, valida contra XSD y firma con XAdES-BES."""
        xml = self.generar_acuse_recibo(
            ncf=ncf,
            rnc_emisor=rnc_emisor,
            rnc_comprador=rnc_comprador,
            estado=estado,
            codigo_motivo_no_recibido=codigo_motivo_no_recibido,
        )

        valido, errores = self.validator.validar_evento(xml, "ARECF")
        if not valido:
            logger.error("ARECF no pasa validación XSD: %s", errores)
            raise ValueError(f"ARECF no válido contra ARECF.xsd: {errores}")

        return self.signer.firmar(xml, cert_data, cert_password)
