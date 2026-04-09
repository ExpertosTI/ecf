# ECF Core: Generación de XML, Firma y envio a la DGII

from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree
from lxml.etree import _Element

logger = logging.getLogger(__name__)

# Constantes DGII

TIPOS_ECF = {
    31: "Factura de Crédito Fiscal Electrónica",
    32: "Factura de Consumo Electrónica",
    33: "Nota de Débito Electrónica",
    34: "Nota de Crédito Electrónica",
    41: "Compras Electrónico",
    43: "Gastos Menores Electrónico",
    44: "Regímenes Especiales Electrónico",
    45: "Gubernamental Electrónico",
    46: "Comprobante para Exportaciones Electrónico",
    47: "Comprobante para Pagos al Exterior Electrónico",
}

PREFIJOS_ECF = {t: f"E{t}" for t in TIPOS_ECF}

NAMESPACE_ECF   = "http://www.dgii.gov.do/ecf"
NAMESPACE_DS    = "http://www.w3.org/2000/09/xmldsig#"

# Rutas a los XSD oficiales de la DGII (debes descargarlos de la DGII)
XSD_DIR = Path(__file__).parent.parent / "xsd"


# Modelos de datos

@dataclass
class ItemECF:
    linea: int
    descripcion: str
    cantidad: Decimal
    precio_unitario: Decimal
    descuento: Decimal = Decimal("0")
    itbis_tasa: Decimal = Decimal("18")
    unidad: str = "Unidad"
    indicador_bien_servicio: int = 2    # 1=Bien, 2=Servicio

    @property
    def indicador_facturacion(self) -> int:
        """1=ITBIS 18%, 2=ITBIS 16%, 3=ITBIS otro, 4=Exento"""
        if self.itbis_tasa == Decimal("18"):
            return 1
        elif self.itbis_tasa == Decimal("16"):
            return 2
        elif self.itbis_tasa == Decimal("0"):
            return 4
        else:
            return 3

    @property
    def subtotal_bruto(self) -> Decimal:
        return (self.cantidad * self.precio_unitario - self.descuento).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )

    @property
    def itbis_monto(self) -> Decimal:
        return (self.subtotal_bruto * self.itbis_tasa / 100).quantize(
            Decimal("0.01"), ROUND_HALF_UP
        )

    @property
    def total_linea(self) -> Decimal:
        return self.subtotal_bruto + self.itbis_monto


@dataclass
class FacturaECF:
    # Identificación
    tipo_ecf: int
    ncf: str                          # E310000000001
    rnc_emisor: str
    razon_social_emisor: str
    direccion_emisor: str
    fecha_emision: date

    # Comprador
    rnc_comprador: Optional[str]
    nombre_comprador: Optional[str]
    tipo_rnc_comprador: str = "1"     # 1=RNC, 2=Cédula, 3=Pasaporte

    # Items
    items: List[ItemECF] = None

    # Referencia (notas de crédito/débito)
    ncf_referencia: Optional[str] = None
    fecha_ncf_referencia: Optional[date] = None
    codigo_modificacion: str = "1"    # 1=Descuento, 2=Devolución, 3=Anulación, 4=Otro

    # Configuración fiscal
    moneda: str = "DOP"
    tipo_cambio: Decimal = Decimal("1")
    indicador_itbis_incluido: bool = False
    tipo_pago: str = "1"              # 1=Contado, 2=Crédito, 3=Gratuito
    tipo_ingresos: str = "01"         # 01-05
    indicador_envio_diferido: int = 0  # 0=Tiempo real, 1=Diferido

    # Emisor adicional
    nombre_comercial: Optional[str] = None
    municipio: Optional[str] = None
    provincia: Optional[str] = None

    # Comprador adicional
    direccion_comprador: Optional[str] = None

    # Para 606/607 (compras)
    tipo_bienes_servicios: Optional[int] = None

    def __post_init__(self):
        if self.items is None:
            self.items = []

    @property
    def subtotal(self) -> Decimal:
        return sum(i.subtotal_bruto for i in self.items)

    @property
    def total_itbis(self) -> Decimal:
        return sum(i.itbis_monto for i in self.items)

    @property
    def total(self) -> Decimal:
        return self.subtotal + self.total_itbis

    @property
    def monto_gravado_i1(self) -> Decimal:
        """Monto gravado a tasa 18% (ITBIS1)"""
        return sum(i.subtotal_bruto for i in self.items if i.itbis_tasa == Decimal("18"))

    @property
    def monto_gravado_i2(self) -> Decimal:
        """Monto gravado a tasa 16% (ITBIS2)"""
        return sum(i.subtotal_bruto for i in self.items if i.itbis_tasa == Decimal("16"))

    @property
    def monto_exento(self) -> Decimal:
        """Monto exento (tasa 0%)"""
        return sum(i.subtotal_bruto for i in self.items if i.itbis_tasa == Decimal("0"))

    @property
    def total_itbis1(self) -> Decimal:
        """Total ITBIS a 18%"""
        return sum(i.itbis_monto for i in self.items if i.itbis_tasa == Decimal("18"))

    @property
    def total_itbis2(self) -> Decimal:
        """Total ITBIS a 16%"""
        return sum(i.itbis_monto for i in self.items if i.itbis_tasa == Decimal("16"))

    @property
    def total_paginas(self) -> int:
        """Número de páginas del documento (1 página por cada 50 items)"""
        return max(1, (len(self.items) + 49) // 50)


# Generador de XML

class ECFXMLGenerator:
    """
    Genera el XML del e-CF según el esquema oficial de la DGII.
    Cada método _build_* construye una sección del estándar.
    """

    def generar(self, factura: FacturaECF) -> bytes:
        nsmap = {
            None: NAMESPACE_ECF,
            "ds":  NAMESPACE_DS,
        }
        root = etree.Element(
            f"{{{NAMESPACE_ECF}}}ECF",
            nsmap=nsmap
        )
        root.set("Version", "1.0")

        root.append(self._build_encabezado(factura))
        root.append(self._build_detalles(factura))
        root.append(self._build_paginacion(factura))
        root.append(self._build_resumen(factura))

        if factura.ncf_referencia:
            root.append(self._build_referencia(factura))

        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True
        )

    def _e(self, parent: _Element, tag: str, text: str = None, **attrib) -> _Element:
        """Helper: crea elemento hijo."""
        el = etree.SubElement(parent, f"{{{NAMESPACE_ECF}}}{tag}", **attrib)
        if text is not None:
            el.text = str(text)
        return el

    def _build_encabezado(self, f: FacturaECF) -> _Element:
        enc = etree.Element(f"{{{NAMESPACE_ECF}}}Encabezado")

        # IdDoc
        id_doc = self._e(enc, "IdDoc")
        self._e(id_doc, "TipoeCF",        str(f.tipo_ecf))
        self._e(id_doc, "eNCF",           f.ncf)
        self._e(id_doc, "IndicadorEnvioDiferido", str(f.indicador_envio_diferido))
        self._e(id_doc, "IndicadorMontoGravado",
                "1" if f.indicador_itbis_incluido else "0")
        self._e(id_doc, "TipoIngresos",   f.tipo_ingresos)
        self._e(id_doc, "TipoPago",       f.tipo_pago)
        self._e(id_doc, "FechaLimitePago", f.fecha_emision.strftime("%d-%m-%Y"))
        self._e(id_doc, "TotalPaginas",   str(f.total_paginas))

        # Emisor
        emisor = self._e(enc, "Emisor")
        self._e(emisor, "RNCEmisor",      f.rnc_emisor)
        self._e(emisor, "RazonSocialEmisor", f.razon_social_emisor)
        if f.nombre_comercial:
            self._e(emisor, "NombreComercial", f.nombre_comercial)
        self._e(emisor, "DireccionEmisor",   f.direccion_emisor)
        if f.municipio:
            self._e(emisor, "Municipio", f.municipio)
        if f.provincia:
            self._e(emisor, "Provincia", f.provincia)
        self._e(emisor, "FechaEmision",
                f.fecha_emision.strftime("%d-%m-%Y"))

        # Comprador (opcional en tipo 32 consumo final)
        if f.rnc_comprador:
            comprador = self._e(enc, "Comprador")
            self._e(comprador, "TipoIdentificacion", f.tipo_rnc_comprador)
            self._e(comprador, "RNCComprador",  f.rnc_comprador)
            self._e(comprador, "RazonSocialComprador", f.nombre_comprador or "")
            if f.direccion_comprador:
                self._e(comprador, "DireccionComprador", f.direccion_comprador)

        # Totales de encabezado — separados por tasa ITBIS
        totales = self._e(enc, "Totales")
        self._e(totales, "MontoGravadoTotal", str(f.subtotal))
        self._e(totales, "MontoGravadoI1", str(f.monto_gravado_i1))
        self._e(totales, "MontoGravadoI2", str(f.monto_gravado_i2))
        self._e(totales, "MontoExento",    str(f.monto_exento))
        self._e(totales, "ITBIS1",         str(f.total_itbis1))
        self._e(totales, "ITBIS2",         str(f.total_itbis2))
        self._e(totales, "TotalITBIS",     str(f.total_itbis))
        self._e(totales, "MontoTotal",     str(f.total))

        if f.moneda != "DOP":
            self._e(totales, "MontoTotalTransaccionado",
                    str((f.total * f.tipo_cambio).quantize(Decimal("0.01"))))

        return enc

    def _build_detalles(self, f: FacturaECF) -> _Element:
        detalles = etree.Element(f"{{{NAMESPACE_ECF}}}DetallesItems")

        for item in f.items:
            linea = self._e(detalles, "Item")
            self._e(linea, "NumeroLinea",      str(item.linea))
            self._e(linea, "IndicadorFacturacion", str(item.indicador_facturacion))
            self._e(linea, "NombreItem",       item.descripcion)
            self._e(linea, "IndicadorBienoServicio", str(item.indicador_bien_servicio))
            self._e(linea, "UnidadMedida",     item.unidad)
            self._e(linea, "CantidadItem",     str(item.cantidad))
            self._e(linea, "PrecioUnitarioItem", str(item.precio_unitario))
            if item.descuento > 0:
                self._e(linea, "DescuentoMonto",   str(item.descuento))
            self._e(linea, "MontoItem",        str(item.subtotal_bruto))

        return detalles

    def _build_paginacion(self, f: FacturaECF) -> _Element:
        pag = etree.Element(f"{{{NAMESPACE_ECF}}}Paginacion")
        self._e(pag, "PaginaActual", "1")
        self._e(pag, "TotalPaginas", str(f.total_paginas))
        return pag

    def _build_resumen(self, f: FacturaECF) -> _Element:
        resumen = etree.Element(f"{{{NAMESPACE_ECF}}}Resumen")

        self._e(resumen, "CodigoMoneda",         f.moneda)
        self._e(resumen, "TipoCambio",           str(f.tipo_cambio))
        self._e(resumen, "MontoGravadoTotal",    str(f.subtotal))
        self._e(resumen, "MontoGravadoI1",       str(f.monto_gravado_i1))
        self._e(resumen, "MontoGravadoI2",       str(f.monto_gravado_i2))
        self._e(resumen, "MontoExento",          str(f.monto_exento))
        self._e(resumen, "ITBIS1",              str(f.total_itbis1))
        self._e(resumen, "ITBIS2",              str(f.total_itbis2))
        self._e(resumen, "TotalITBIS",           str(f.total_itbis))
        self._e(resumen, "MontoTotal",           str(f.total))
        self._e(resumen, "TotalPagos",           str(f.total))
        self._e(resumen, "FechaHoraFirma",
                datetime.now(timezone.utc).strftime("%d-%m-%Y %H:%M:%S"))

        return resumen

    def _build_referencia(self, f: FacturaECF) -> _Element:
        ref = etree.Element(f"{{{NAMESPACE_ECF}}}InformacionReferencia")
        self._e(ref, "NCFModificado",       f.ncf_referencia)
        if f.fecha_ncf_referencia:
            self._e(ref, "FechaNCFModificado",
                    f.fecha_ncf_referencia.strftime("%d-%m-%Y"))
        self._e(ref, "CodigoModificacion",  f.codigo_modificacion)
        return ref


# Firma Digital XML (XAdES-BES compatible con DGII)

class ECFSigner:
    """
    Firma el XML del e-CF con RSA-SHA256 usando el certificado .p12 del tenant.
    Implementa la firma enveloped según los requisitos de la DGII.
    """

    def firmar(
        self,
        xml_bytes: bytes,
        p12_data: bytes,
        p12_password: bytes
    ) -> bytes:
        # Cargar el .p12
        private_key, certificate, chain = pkcs12.load_key_and_certificates(
            p12_data, p12_password
        )

        # Parsear el XML
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_bytes, parser)

        # Canonicalizar el documento (C14N exclusivo)
        xml_c14n = self._canonicalizar(root)

        # Calcular digest del documento
        digest = self._sha256_b64(xml_c14n)

        # Construir el bloque SignedInfo
        signed_info_xml = self._build_signed_info(digest)
        signed_info_c14n = self._canonicalizar_string(signed_info_xml)

        # Firmar el SignedInfo
        firma_bytes = private_key.sign(
            signed_info_c14n,
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        firma_b64 = base64.b64encode(firma_bytes).decode()

        # Serializar el certificado
        cert_b64 = base64.b64encode(
            certificate.public_bytes(serialization.Encoding.DER)
        ).decode()

        # Construir el bloque <ds:Signature> completo
        signature_node = self._build_signature_node(
            signed_info_xml, firma_b64, cert_b64, digest, certificate
        )

        # Insertar la firma en el XML
        root.append(signature_node)

        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True
        )

    def _canonicalizar(self, element: _Element) -> bytes:
        import io
        output = io.BytesIO()
        element.getroottree().write_c14n(output, exclusive=True)
        return output.getvalue()

    def _canonicalizar_string(self, xml_str: str) -> bytes:
        parser = etree.XMLParser(remove_blank_text=True)
        el = etree.fromstring(xml_str.encode(), parser)
        return self._canonicalizar(el)

    def _sha256_b64(self, data: bytes) -> str:
        digest = hashlib.sha256(data).digest()
        return base64.b64encode(digest).decode()

    def _build_signed_info(self, digest_value: str) -> str:
        return f"""<ds:SignedInfo xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
  <ds:CanonicalizationMethod Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/>
  <ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/>
  <ds:Reference URI="">
    <ds:Transforms>
      <ds:Transform Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature"/>
      <ds:Transform Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#"/>
    </ds:Transforms>
    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
    <ds:DigestValue>{digest_value}</ds:DigestValue>
  </ds:Reference>
</ds:SignedInfo>"""

    def _build_signature_node(
        self,
        signed_info_xml: str,
        firma_b64: str,
        cert_b64: str,
        digest_value: str,
        certificate: x509.Certificate
    ) -> _Element:
        # Extraer datos del certificado para KeyInfo
        serial = str(certificate.serial_number)
        issuer = certificate.issuer.rfc4514_string()
        cert_digest = base64.b64encode(
            certificate.fingerprint(hashes.SHA256())
        ).decode()

        sig_xml = f"""<ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#" Id="xmldsig-{uuid.uuid4()}">
  {signed_info_xml}
  <ds:SignatureValue>{firma_b64}</ds:SignatureValue>
  <ds:KeyInfo>
    <ds:X509Data>
      <ds:X509Certificate>{cert_b64}</ds:X509Certificate>
      <ds:X509IssuerSerial>
        <ds:X509IssuerName>{issuer}</ds:X509IssuerName>
        <ds:X509SerialNumber>{serial}</ds:X509SerialNumber>
      </ds:X509IssuerSerial>
    </ds:X509Data>
  </ds:KeyInfo>
</ds:Signature>"""

        parser = etree.XMLParser(remove_blank_text=True)
        return etree.fromstring(sig_xml.encode(), parser)


# Validador XSD

class ECFValidator:
    """
    Valida el XML del e-CF contra los esquemas XSD oficiales de la DGII.
    Los XSD deben descargarse desde la web de la DGII y colocarse en /xsd/
    """

    _schemas: dict[int, etree.XMLSchema] = {}

    def validar(self, xml_bytes: bytes, tipo_ecf: int) -> tuple[bool, list[str]]:
        schema = self._get_schema(tipo_ecf)
        if schema is None:
            raise ValueError(
                f"XSD obligatorio no disponible para tipo e-CF {tipo_ecf}. "
                f"Coloque los archivos XSD en el directorio /xsd/ antes de operar."
            )

        doc = etree.fromstring(xml_bytes)
        valido = schema.validate(doc)
        errores = [str(e) for e in schema.error_log]
        return valido, errores

    def _get_schema(self, tipo_ecf: int) -> Optional[etree.XMLSchema]:
        if tipo_ecf in self._schemas:
            return self._schemas[tipo_ecf]

        xsd_path = XSD_DIR / f"ECF-{tipo_ecf}.xsd"
        if not xsd_path.exists():
            # Intentar con el XSD genérico
            xsd_path = XSD_DIR / "ECF.xsd"
            if not xsd_path.exists():
                return None

        schema_doc = etree.parse(str(xsd_path))
        schema = etree.XMLSchema(schema_doc)
        self._schemas[tipo_ecf] = schema
        return schema


# Generador de CUFE

class CUFEGenerator:
    """
    Genera el Código Único de Factura Electrónica (CUFE).
    Algoritmo: SHA-384 sobre la concatenación de campos según la DGII.
    """

    def generar(
        self,
        ncf: str,
        rnc_emisor: str,
        fecha_emision: date,
        monto_total: Decimal,
        itbis: Decimal,
        rnc_comprador: str,
        tipo_ecf: int,
        clave_secreta: str,        # Clave secreta del emisor registrada en DGII
    ) -> str:
        # Concatenación según especificación DGII
        cadena = (
            f"{ncf}"
            f"{rnc_emisor}"
            f"{fecha_emision.strftime('%Y%m%d')}"
            f"{monto_total:.2f}"
            f"{itbis:.2f}"
            f"{rnc_comprador or ''}"
            f"{tipo_ecf:02d}"
            f"{clave_secreta}"
        )

        cufe = hashlib.sha384(cadena.encode("utf-8")).hexdigest()
        return cufe


# Servicio principal: orquesta todo el flujo

class ECFCoreService:
    """
    Orquestador principal del procesamiento de e-CF.
    Coordina: generación XML → validación XSD → firma → CUFE
    """

    def __init__(self):
        self.generator  = ECFXMLGenerator()
        self.signer     = ECFSigner()
        self.validator  = ECFValidator()
        self.cufe_gen   = CUFEGenerator()

    def procesar(
        self,
        factura: FacturaECF,
        p12_data: bytes,
        p12_password: bytes,
        clave_secreta_cufe: str,
    ) -> dict:
        """
        Flujo completo:
        1. Generar XML
        2. Validar contra XSD
        3. Firmar digitalmente
        4. Calcular CUFE
        Retorna dict con xml_firmado, cufe, y metadatos.
        """

        # 1. Generar XML
        logger.info("Generando XML para NCF %s", factura.ncf)
        xml_original = self.generator.generar(factura)

        # 2. Validar XSD
        logger.info("Validando XSD para tipo %s", factura.tipo_ecf)
        valido, errores = self.validator.validar(xml_original, factura.tipo_ecf)
        if not valido:
            raise ValueError(f"XML no válido contra XSD DGII: {'; '.join(errores)}")

        # 3. Firmar
        logger.info("Firmando XML con certificado del tenant")
        xml_firmado = self.signer.firmar(xml_original, p12_data, p12_password)

        # 4. CUFE
        cufe = self.cufe_gen.generar(
            ncf            = factura.ncf,
            rnc_emisor     = factura.rnc_emisor,
            fecha_emision  = factura.fecha_emision,
            monto_total    = factura.total,
            itbis          = factura.total_itbis,
            rnc_comprador  = factura.rnc_comprador,
            tipo_ecf       = factura.tipo_ecf,
            clave_secreta  = clave_secreta_cufe,
        )

        logger.info("e-CF procesado correctamente. NCF=%s CUFE=%s...", factura.ncf, cufe[:16])

        return {
            "ncf":          factura.ncf,
            "cufe":         cufe,
            "xml_original": xml_original,
            "xml_firmado":  xml_firmado,
            "tipo_ecf":     factura.tipo_ecf,
            "total":        str(factura.total),
            "itbis":        str(factura.total_itbis),
        }
