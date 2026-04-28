"""
ecf_interchange_service.py — Manejo de Aceptación Comercial e Intercambio
Responsabilidades:
- Generar XML de Recibo de Entrega (RECF)
- Generar XML de Aprobación Comercial (ACECF)
- Generar XML de Rechazo Comercial (ARECF)
- Firmar y enviar estos eventos a la DGII o al emisor
"""

import base64
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from lxml import etree
from ecf_core.ecf_core_service import ECFSigner

logger = logging.getLogger(__name__)

NAMESPACE_ECF = "http://www.dgii.gov.do/ecf"
NAMESPACE_DS  = "http://www.w3.org/2000/09/xmldsig#"

class ECFInterchangeService:
    def __init__(self, signer: ECFSigner):
        self.signer = signer

    def generar_recibo_entrega(self, ncf: str, rnc_emisor: str, rnc_receptor: str) -> bytes:
        """Genera el XML de Recibo de Entrega (RECF)."""
        root = etree.Element(f"{{{NAMESPACE_ECF}}}RECF", nsmap={None: NAMESPACE_ECF})
        
        etree.SubElement(root, "RNCEmisor").text = rnc_emisor
        etree.SubElement(root, "RNCReceptor").text = rnc_receptor
        etree.SubElement(root, "eNCF").text = ncf
        etree.SubElement(root, "FechaRecepcion").text = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S")
        etree.SubElement(root, "Estado").text = "0"  # 0 = Recibido
        
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    def generar_aprobacion_comercial(self, ncf: str, rnc_emisor: str, rnc_receptor: str) -> bytes:
        """Genera el XML de Aprobación Comercial (ACECF)."""
        root = etree.Element(f"{{{NAMESPACE_ECF}}}ACECF", nsmap={None: NAMESPACE_ECF})
        
        etree.SubElement(root, "RNCEmisor").text = rnc_emisor
        etree.SubElement(root, "RNCReceptor").text = rnc_receptor
        etree.SubElement(root, "eNCF").text = ncf
        etree.SubElement(root, "FechaAprobacion").text = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S")
        etree.SubElement(root, "Estado").text = "0"  # 0 = Aprobado
        
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    def generar_rechazo_comercial(self, ncf: str, rnc_emisor: str, rnc_receptor: str, motivo: str) -> bytes:
        """Genera el XML de Rechazo Comercial (ARECF)."""
        root = etree.Element(f"{{{NAMESPACE_ECF}}}ARECF", nsmap={None: NAMESPACE_ECF})
        
        etree.SubElement(root, "RNCEmisor").text = rnc_emisor
        etree.SubElement(root, "RNCReceptor").text = rnc_receptor
        etree.SubElement(root, "eNCF").text = ncf
        etree.SubElement(root, "FechaRechazo").text = datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S")
        etree.SubElement(root, "MotivoRechazo").text = motivo
        etree.SubElement(root, "Estado").text = "2"  # 2 = Rechazo Total
        
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    async def procesar_evento(self, tipo: str, ncf: str, rnc_emisor: str, tenant: dict, p12_data: bytes, p12_password: bytes, motivo: str = None) -> bytes:
        """Genera, firma y retorna el XML del evento comercial."""
        if tipo == "RECF":
            xml = self.generar_recibo_entrega(ncf, rnc_emisor, tenant["rnc"])
        elif tipo == "ACECF":
            xml = self.generar_aprobacion_comercial(ncf, rnc_emisor, tenant["rnc"])
        elif tipo == "ARECF":
            xml = self.generar_rechazo_comercial(ncf, rnc_emisor, tenant["rnc"], motivo)
        else:
            raise ValueError(f"Tipo de evento comercial desconocido: {tipo}")

        # Firmar con XAdES-BES
        xml_firmado = self.signer.firmar(xml, p12_data, p12_password)
        return xml_firmado
