"""Servicio de anulación de e-CF — genera el XML ANECF conforme a `xsd/ANECF.xsd`.

Estructura DGII oficial::

    ANECF
    ├── Encabezado
    │   ├── Version (1.0)
    │   ├── RncEmisor
    │   ├── CantidadeNCFAnulados
    │   └── FechaHoraAnulacioneNCF (YYYY-MM-DDTHH:mm:ss)
    └── DetalleAnulacion
        └── Anulacion (1..10)
            ├── NoLinea
            ├── TipoeCF (31..47)
            ├── TablaRangoSecuenciasAnuladaseNCF
            │   └── Secuencias[1..10000]
            │       ├── SecuenciaeNCFDesde
            │       └── SecuenciaeNCFHasta
            └── CantidadeNCFAnulados

El XML va firmado con XAdES-BES (igual que un e-CF) antes de enviarse a la DGII.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from lxml import etree

from ecf_core.ecf_core_service import ECFSigner

logger = logging.getLogger(__name__)


@dataclass
class RangoNCF:
    tipo_ecf:    int
    desde:       str   # E310000000001
    hasta:       str   # E310000000003
    cantidad:    int


class ECFAnulacionGenerator:
    """Genera el XML ANECF (Anulación e-CF) conforme al esquema DGII."""

    def generar(
        self,
        rnc_emisor: str,
        rangos: List[RangoNCF],
        fecha_hora: datetime | None = None,
    ) -> bytes:
        if not rangos:
            raise ValueError("Debe enviar al menos un rango")
        if len(rangos) > 10:
            raise ValueError("DGII permite máximo 10 bloques de anulación por solicitud")

        fecha_hora = fecha_hora or datetime.now(timezone.utc)
        cantidad_total = sum(r.cantidad for r in rangos)

        root = etree.Element("ANECF")

        encabezado = etree.SubElement(root, "Encabezado")
        etree.SubElement(encabezado, "Version").text = "1.0"
        etree.SubElement(encabezado, "RncEmisor").text = rnc_emisor
        etree.SubElement(encabezado, "CantidadeNCFAnulados").text = str(cantidad_total)
        etree.SubElement(encabezado, "FechaHoraAnulacioneNCF").text = (
            fecha_hora.strftime("%Y-%m-%dT%H:%M:%S")
        )

        detalle = etree.SubElement(root, "DetalleAnulacion")
        for idx, rango in enumerate(rangos, 1):
            anulacion = etree.SubElement(detalle, "Anulacion")
            etree.SubElement(anulacion, "NoLinea").text = str(idx)
            etree.SubElement(anulacion, "TipoeCF").text = str(rango.tipo_ecf)

            tabla = etree.SubElement(anulacion, "TablaRangoSecuenciasAnuladaseNCF")
            secuencias = etree.SubElement(tabla, "Secuencias")
            etree.SubElement(secuencias, "SecuenciaeNCFDesde").text = rango.desde
            etree.SubElement(secuencias, "SecuenciaeNCFHasta").text = rango.hasta

            etree.SubElement(anulacion, "CantidadeNCFAnulados").text = str(rango.cantidad)

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


class ECFAnulacionService:
    """Orquesta generación + firma XAdES + envío del ANECF."""

    def __init__(self, signer: ECFSigner | None = None):
        self.generator = ECFAnulacionGenerator()
        self.signer = signer or ECFSigner()

    def generar_y_firmar(
        self,
        rnc_emisor: str,
        rangos: List[RangoNCF],
        p12_data: bytes,
        p12_password: bytes,
        fecha_hora: datetime | None = None,
    ) -> bytes:
        xml = self.generator.generar(rnc_emisor, rangos, fecha_hora=fecha_hora)
        return self.signer.firmar(xml, p12_data, p12_password)

    @staticmethod
    def rango_unico(tipo_ecf: int, ncf: str) -> RangoNCF:
        """Helper para anular un único NCF (caso típico)."""
        return RangoNCF(tipo_ecf=tipo_ecf, desde=ncf, hasta=ncf, cantidad=1)
