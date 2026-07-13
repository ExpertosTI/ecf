# Renace e-CF · Núcleo de generación de XML, firma XAdES-BES y validación XSD.

from __future__ import annotations

import base64
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree
from lxml.etree import _Element

from ecf_core.utils import fmt_fecha_hora_dgii, now_rd

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

# Campos IdDoc / Totales / Items varían por XSD oficial (ECF-NN.xsd).
TIPOS_CON_INDICADOR_MONTO_GRAVADO = frozenset({31, 32, 33, 34, 41, 45})
TIPOS_CON_TIPO_INGRESOS = frozenset({31, 32, 33, 34, 44, 45, 46})
TIPOS_SIN_COMPRADOR = frozenset({43})  # Emisor → Totales
TIPOS_COMPRADOR_EXTRANJERO = frozenset({47})  # IdentificadorExtranjero, no RNCComprador
TIPOS_ITEM_CON_RETENCION = frozenset({41, 47})
# E46 exportaciones: tasa 0% = ITBIS3 (IndicadorFacturacion=3), no MontoExento.
TIPOS_CERO_COMO_ITBIS3 = frozenset({46})
# Totales solo MontoExento + MontoTotal (sin bloques gravados ITBIS1/2).
TIPOS_TOTALES_SOLO_EXENTO = frozenset({43, 44, 47})

# Los XSD oficiales de la DGII no declaran targetNamespace; los elementos del
# e-CF se emiten sin namespace y `ds:Signature` se inserta en el slot xs:any.
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
    # Código DGII de unidad de medida (43 = "UND" Unidad). Ver UnidadMedidaType
    # en los XSD oficiales para el catálogo completo (1..XX).
    unidad: str = "43"
    indicador_bien_servicio: int = 2    # 1=Bien, 2=Servicio

    def indicador_facturacion(self, tipo_ecf: int | None = None) -> int:
        """1=ITBIS 18%, 2=ITBIS 16%, 3=ITBIS 0%/otro, 4=Exento."""
        if self.itbis_tasa == Decimal("18"):
            return 1
        if self.itbis_tasa == Decimal("16"):
            return 2
        if self.itbis_tasa == Decimal("0"):
            # ECF-46: 0% va como ITBIS3, no como Exento (no existe MontoExento).
            if tipo_ecf in TIPOS_CERO_COMO_ITBIS3:
                return 3
            return 4
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
    rnc_comprador: str | None
    nombre_comprador: str | None
    tipo_rnc_comprador: str = "1"     # 1=RNC, 2=Cédula, 3=Pasaporte

    # Items
    items: list[ItemECF] = field(default_factory=list)

    # Referencia (notas de crédito/débito)
    ncf_referencia: str | None = None
    fecha_ncf_referencia: date | None = None
    codigo_modificacion: str = "1"    # 1=Descuento, 2=Devolución, 3=Anulación, 4=Otro

    # Configuración fiscal
    moneda: str = "DOP"
    tipo_cambio: Decimal = Decimal("1")
    indicador_itbis_incluido: bool = False
    tipo_pago: str = "1"              # 1=Contado, 2=Crédito, 3=Gratuito
    tipo_ingresos: str = "01"         # 01-05
    indicador_envio_diferido: int = 0  # 0=Tiempo real, 1=Diferido

    # Emisor adicional
    nombre_comercial: str | None = None
    municipio: str | None = None
    provincia: str | None = None

    # Comprador adicional
    direccion_comprador: str | None = None

    # Crédito: FechaLimitePago (opcional en XSD; no inventar = fecha_emision)
    fecha_limite_pago: date | None = None

    # Para 606/607 (compras)
    tipo_bienes_servicios: int | None = None

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
        """Monto exento (tasa 0%) — no aplica a tipos que usan ITBIS3 (p.ej. E46)."""
        if self.tipo_ecf in TIPOS_CERO_COMO_ITBIS3:
            return Decimal("0")
        return sum(i.subtotal_bruto for i in self.items if i.itbis_tasa == Decimal("0"))

    @property
    def monto_gravado_i3(self) -> Decimal:
        """Monto a tasa ITBIS3 / 0% (exportaciones E46)."""
        if self.tipo_ecf in TIPOS_CERO_COMO_ITBIS3:
            return sum(i.subtotal_bruto for i in self.items if i.itbis_tasa == Decimal("0"))
        return sum(
            i.subtotal_bruto for i in self.items
            if i.itbis_tasa not in (Decimal("0"), Decimal("16"), Decimal("18"))
        )

    @property
    def total_itbis1(self) -> Decimal:
        """Total ITBIS a 18%"""
        return sum(i.itbis_monto for i in self.items if i.itbis_tasa == Decimal("18"))

    @property
    def total_itbis2(self) -> Decimal:
        """Total ITBIS a 16%"""
        return sum(i.itbis_monto for i in self.items if i.itbis_tasa == Decimal("16"))

    @property
    def total_itbis3(self) -> Decimal:
        """Total ITBIS a tasa 3 (0% en exportaciones)."""
        if self.tipo_ecf in TIPOS_CERO_COMO_ITBIS3:
            return Decimal("0")
        return sum(
            i.itbis_monto for i in self.items
            if i.itbis_tasa not in (Decimal("0"), Decimal("16"), Decimal("18"))
        )

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
        # Los XSD oficiales de la DGII NO declaran targetNamespace; los elementos
        # del e-CF se emiten sin namespace. La firma XAdES se inserta después en
        # el lugar permitido por <xs:any> al final del documento.
        root = etree.Element("ECF")

        root.append(self._build_encabezado(factura))
        root.append(self._build_detalles(factura))

        # Paginacion sólo si hay > 1 página (Integer4ValidationTypeMayorUno).
        if factura.total_paginas > 1:
            root.append(self._build_paginacion(factura))

        if factura.ncf_referencia:
            root.append(self._build_referencia(factura))

        # FechaHoraFirma es elemento de nivel ECF (NO va dentro de Resumen).
        # Placeholder reemplazado por ECFSigner.firmar() para coincidir con
        # xades:SigningTime.
        fhf = etree.SubElement(root, "FechaHoraFirma")
        fhf.text = "PENDING_SIGN"

        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            pretty_print=True
        )

    def _e(self, parent: _Element, tag: str, text: str = None, **attrib) -> _Element:
        """Helper: crea elemento hijo."""
        el = etree.SubElement(parent, tag, **attrib)
        if text is not None:
            el.text = str(text)
        return el

    def _build_encabezado(self, f: FacturaECF) -> _Element:
        enc = etree.Element("Encabezado")

        # Version (REQUIRED) — primer elemento de Encabezado por XSD.
        self._e(enc, "Version", "1.0")

        # IdDoc — secuencia distinta por tipo (ver ECF-NN.xsd).
        id_doc = self._e(enc, "IdDoc")
        self._e(id_doc, "TipoeCF", str(f.tipo_ecf))
        self._e(id_doc, "eNCF", f.ncf)
        # ECF-34 (Nota de Crédito) reemplaza FechaVencimientoSecuencia con
        # IndicadorNotaCredito (1 = nota de crédito normal).
        if f.tipo_ecf == 34:
            self._e(id_doc, "IndicadorNotaCredito", "1")
        # FechaVencimientoSecuencia aplica a {31, 33, 41, 43, 44, 45, 46, 47}.
        # Los XSD ECF-32 y ECF-34 no la incluyen.
        elif f.tipo_ecf != 32:
            self._e(
                id_doc, "FechaVencimientoSecuencia",
                date(f.fecha_emision.year, 12, 31).strftime("%d-%m-%Y"),
            )
        # IndicadorEnvioDiferido sólo admite valor 1 — omitir cuando no aplica.
        if f.indicador_envio_diferido == 1 and f.tipo_ecf not in (41, 43, 47):
            self._e(id_doc, "IndicadorEnvioDiferido", "1")
        # IndicadorMontoGravado: NO existe en E43/E44/E46/E47.
        if f.tipo_ecf in TIPOS_CON_INDICADOR_MONTO_GRAVADO:
            self._e(
                id_doc, "IndicadorMontoGravado",
                "1" if f.indicador_itbis_incluido else "0",
            )
        # TipoIngresos: NO existe en E41/E43/E47.
        if f.tipo_ecf in TIPOS_CON_TIPO_INGRESOS:
            self._e(id_doc, "TipoIngresos", f.tipo_ingresos)
        # TipoPago: obligatorio en la mayoría; opcional en 41/43/47 — siempre emitir.
        if f.tipo_ecf != 43 or f.tipo_pago:
            self._e(id_doc, "TipoPago", f.tipo_pago)
        # FechaLimitePago es opcional (XSD minOccurs=0). Solo emitir si hay
        # vencimiento real — nunca sustituir con fecha_emision.
        if f.tipo_pago == "2" and f.fecha_limite_pago and f.tipo_ecf not in (43,):
            self._e(
                id_doc, "FechaLimitePago",
                f.fecha_limite_pago.strftime("%d-%m-%Y"),
            )
        # TotalPaginas tiene minExclusive=1 — sólo emitir cuando > 1.
        if f.total_paginas > 1:
            self._e(id_doc, "TotalPaginas", str(f.total_paginas))

        # Emisor
        emisor = self._e(enc, "Emisor")
        self._e(emisor, "RNCEmisor", f.rnc_emisor)
        self._e(emisor, "RazonSocialEmisor", f.razon_social_emisor)
        if f.nombre_comercial:
            self._e(emisor, "NombreComercial", f.nombre_comercial)
        self._e(emisor, "DireccionEmisor", f.direccion_emisor)
        if f.municipio:
            self._e(emisor, "Municipio", f.municipio)
        if f.provincia:
            self._e(emisor, "Provincia", f.provincia)
        self._e(emisor, "FechaEmision", f.fecha_emision.strftime("%d-%m-%Y"))

        # Comprador — E43 no lo incluye; E47 usa IdentificadorExtranjero.
        if f.tipo_ecf not in TIPOS_SIN_COMPRADOR:
            if f.tipo_ecf in TIPOS_COMPRADOR_EXTRANJERO:
                if f.rnc_comprador or f.nombre_comprador:
                    comprador = self._e(enc, "Comprador")
                    if f.rnc_comprador:
                        self._e(comprador, "IdentificadorExtranjero", f.rnc_comprador[:20])
                    if f.nombre_comprador:
                        self._e(comprador, "RazonSocialComprador", f.nombre_comprador)
            else:
                comprador_obligatorio = f.tipo_ecf in (31, 32, 41, 44, 45, 46)
                if f.rnc_comprador or comprador_obligatorio:
                    comprador = self._e(enc, "Comprador")
                    if f.rnc_comprador:
                        self._e(comprador, "RNCComprador", f.rnc_comprador)
                    if f.nombre_comprador:
                        self._e(comprador, "RazonSocialComprador", f.nombre_comprador)
                    if f.direccion_comprador:
                        self._e(comprador, "DireccionComprador", f.direccion_comprador)

        enc.append(self._build_totales(f))

        # OtraMoneda — bloque con todos los datos en moneda extranjera.
        if f.moneda != "DOP":
            otra = self._e(enc, "OtraMoneda")
            self._e(otra, "TipoMoneda", f.moneda)
            self._e(otra, "TipoCambio", str(f.tipo_cambio))
            self._e(
                otra, "MontoTotalOtraMoneda",
                str((f.total * f.tipo_cambio).quantize(Decimal("0.01"), ROUND_HALF_UP)),
            )

        return enc

    def _build_totales(self, f: FacturaECF) -> _Element:
        """Totales según XSD del tipo (E43/E44/E47 ≠ E31/E32 ≠ E46)."""
        totales = etree.Element("Totales")

        if f.tipo_ecf in TIPOS_TOTALES_SOLO_EXENTO:
            # Solo MontoExento (+ opcionales) y MontoTotal.
            montox = f.subtotal if f.total_itbis == 0 else f.monto_exento
            if montox > 0 or f.tipo_ecf in (43, 44, 47):
                # Régimen especial / gastos menores / exterior: monto base como exento.
                if f.tipo_ecf in (43, 44, 47) and f.total_itbis == 0:
                    montox = f.subtotal
                if montox > 0:
                    self._e(totales, "MontoExento", str(montox))
            self._e(totales, "MontoTotal", str(f.total))
            return totales

        if f.tipo_ecf == 46:
            # Exportación: MontoGravadoTotal / I3 / ITBIS3 — sin MontoExento.
            monto_i3 = f.monto_gravado_i3
            monto_gravado_total = f.monto_gravado_i1 + f.monto_gravado_i2 + monto_i3
            if monto_gravado_total > 0:
                self._e(totales, "MontoGravadoTotal", str(monto_gravado_total))
            if monto_i3 > 0:
                self._e(totales, "MontoGravadoI3", str(monto_i3))
                self._e(totales, "ITBIS3", "0")
            if f.total_itbis > 0:
                self._e(totales, "TotalITBIS", str(f.total_itbis))
            if f.total_itbis3 > 0:
                self._e(totales, "TotalITBIS3", str(f.total_itbis3))
            self._e(totales, "MontoTotal", str(f.total))
            return totales

        # Estándar E31/E32/E33/E34/E41/E45
        monto_gravado_total = f.subtotal - f.monto_exento
        if monto_gravado_total > 0:
            self._e(totales, "MontoGravadoTotal", str(monto_gravado_total))
        if f.monto_gravado_i1 > 0:
            self._e(totales, "MontoGravadoI1", str(f.monto_gravado_i1))
        if f.monto_gravado_i2 > 0:
            self._e(totales, "MontoGravadoI2", str(f.monto_gravado_i2))
        if f.monto_exento > 0:
            self._e(totales, "MontoExento", str(f.monto_exento))
        if f.total_itbis1 > 0:
            self._e(totales, "ITBIS1", "18")
        if f.total_itbis2 > 0:
            self._e(totales, "ITBIS2", "16")
        if f.total_itbis > 0:
            self._e(totales, "TotalITBIS", str(f.total_itbis))
        if f.total_itbis1 > 0:
            self._e(totales, "TotalITBIS1", str(f.total_itbis1))
        if f.total_itbis2 > 0:
            self._e(totales, "TotalITBIS2", str(f.total_itbis2))
        self._e(totales, "MontoTotal", str(f.total))
        return totales

    def _build_detalles(self, f: FacturaECF) -> _Element:
        # Orden de campos por XSD: NumeroLinea, IndicadorFacturacion,
        # [Retencion E41/E47], NombreItem, IndicadorBienoServicio, ...
        detalles = etree.Element("DetallesItems")

        for item in f.items:
            linea = self._e(detalles, "Item")
            self._e(linea, "NumeroLinea", str(item.linea))
            self._e(linea, "IndicadorFacturacion", str(item.indicador_facturacion(f.tipo_ecf)))
            if f.tipo_ecf in TIPOS_ITEM_CON_RETENCION:
                ret = self._e(linea, "Retencion")
                # 1=Retención, 2=Percepción — requerido aunque montos sean 0.
                self._e(ret, "IndicadorAgenteRetencionoPercepcion", "1")
                if f.tipo_ecf == 47:
                    # ECF-47 exige MontoISRRetenido (minOccurs=1).
                    self._e(ret, "MontoISRRetenido", "0.00")
            self._e(linea, "NombreItem", (item.descripcion or "")[:80])
            self._e(linea, "IndicadorBienoServicio", str(item.indicador_bien_servicio))
            self._e(linea, "CantidadItem", str(item.cantidad))
            if item.unidad:
                self._e(linea, "UnidadMedida", item.unidad)
            self._e(linea, "PrecioUnitarioItem", str(item.precio_unitario))
            if item.descuento > 0:
                self._e(linea, "DescuentoMonto", str(item.descuento))
            self._e(linea, "MontoItem", str(item.subtotal_bruto))

        return detalles

    def _build_paginacion(self, f: FacturaECF) -> _Element:
        # Sólo se emite cuando hay split físico real (>1 página). Cada Pagina
        # describe el rango de líneas que cubre.
        pag = etree.Element("Paginacion")
        items_per_page = 50
        for page_no in range(1, f.total_paginas + 1):
            pagina = self._e(pag, "Pagina")
            self._e(pagina, "PaginaNo", str(page_no))
            desde = (page_no - 1) * items_per_page + 1
            hasta = min(page_no * items_per_page, len(f.items))
            self._e(pagina, "NoLineaDesde", str(desde))
            self._e(pagina, "NoLineaHasta", str(hasta))
        return pag

    def _build_referencia(self, f: FacturaECF) -> _Element:
        ref = etree.Element("InformacionReferencia")
        self._e(ref, "NCFModificado", f.ncf_referencia)
        if f.fecha_ncf_referencia:
            self._e(
                ref, "FechaNCFModificado",
                f.fecha_ncf_referencia.strftime("%d-%m-%Y"),
            )
        self._e(ref, "CodigoModificacion", f.codigo_modificacion)
        return ref


# Firma Digital XML (XAdES-BES compatible con DGII)

XADES_NS = "http://uri.etsi.org/01903/v1.3.2#"


class ECFSigner:
    """Firma el XML del e-CF con RSA-SHA256 usando XAdES-BES (requisito DGII).

    Diferencias clave vs versiones previas:

    1. ``xades:QualifyingProperties`` se inserta **antes** de calcular su digest.
       La canonicalización c14n exclusiva incluye los namespaces heredados del
       contexto del nodo, así que el digest debe calcularse sobre el árbol ya
       inserto, no sobre el fragmento aislado.
    2. El placeholder ``PENDING_SIGN`` que `_build_resumen` deja en
       ``FechaHoraFirma`` se sustituye por el ``signing_time`` real, garantizando
       coherencia con ``xades:SigningTime``.
    """

    def firmar(self, xml_bytes: bytes, p12_data: bytes, p12_password: bytes, exclusive: bool = True) -> bytes:
        # 1. Cargar el .p12
        private_key, certificate, _ = pkcs12.load_key_and_certificates(p12_data, p12_password)

        # 2. Parsear el XML original (sin pretty-print ni blanks colgantes)
        parser = etree.XMLParser(remove_blank_text=True)
        root = etree.fromstring(xml_bytes, parser)

        # 3. Sincronizar FechaHoraFirma (AST DGII) con SigningTime XAdES (UTC ISO)
        signing_time_utc = datetime.now(timezone.utc)
        signing_time_iso = signing_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        signing_time_dgii = fmt_fecha_hora_dgii(now_rd())
        for elem in root.iter():
            tag_local = etree.QName(elem.tag).localname
            if tag_local == "FechaHoraFirma" and (elem.text or "").strip() in {"", "PENDING_SIGN"}:
                elem.text = signing_time_dgii

        # 4. Metadatos del certificado
        cert_digest = base64.b64encode(certificate.fingerprint(hashes.SHA256())).decode()
        cert_serial = str(certificate.serial_number)
        cert_issuer = certificate.issuer.rfc4514_string()
        cert_b64 = base64.b64encode(
            certificate.public_bytes(serialization.Encoding.DER)
        ).decode()

        signature_id    = f"Signature-{uuid.uuid4()}"
        signed_props_id = f"SignedProperties-{uuid.uuid4()}"

        # 5. Construir <ds:Signature> envelope SIN SignedInfo aún
        sig_node = etree.SubElement(root, f"{{{NAMESPACE_DS}}}Signature",
                                    nsmap={"ds": NAMESPACE_DS})
        sig_node.set("Id", signature_id)

        # 6. <ds:Object> con <xades:QualifyingProperties>
        obj_node = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}Object")
        qprops = etree.SubElement(
            obj_node, f"{{{XADES_NS}}}QualifyingProperties",
            nsmap={"xades": XADES_NS},
        )
        qprops.set("Target", f"#{signature_id}")

        signed_props = etree.SubElement(qprops, f"{{{XADES_NS}}}SignedProperties")
        signed_props.set("Id", signed_props_id)

        ssp = etree.SubElement(signed_props, f"{{{XADES_NS}}}SignedSignatureProperties")
        etree.SubElement(ssp, f"{{{XADES_NS}}}SigningTime").text = signing_time_iso

        sc = etree.SubElement(ssp, f"{{{XADES_NS}}}SigningCertificate")
        cert_el = etree.SubElement(sc, f"{{{XADES_NS}}}Cert")

        cert_digest_el = etree.SubElement(cert_el, f"{{{XADES_NS}}}CertDigest")
        dm = etree.SubElement(cert_digest_el, f"{{{NAMESPACE_DS}}}DigestMethod")
        dm.set("Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
        etree.SubElement(cert_digest_el, f"{{{NAMESPACE_DS}}}DigestValue").text = cert_digest

        issuer_serial = etree.SubElement(cert_el, f"{{{XADES_NS}}}IssuerSerial")
        etree.SubElement(issuer_serial, f"{{{NAMESPACE_DS}}}X509IssuerName").text = cert_issuer
        etree.SubElement(issuer_serial, f"{{{NAMESPACE_DS}}}X509SerialNumber").text = cert_serial

        # 7. Calcular el digest de SignedProperties con el árbol ya construido
        signed_props_digest = self._sha256_b64(self._c14n_node(signed_props, exclusive=exclusive))

        # 8. Calcular el digest del documento (transform enveloped + c14n)
        # Para "enveloped-signature": tomamos el árbol root sin <ds:Signature>
        # y lo canonicalizamos. Como ds:Signature ya está adjunto, hacemos una
        # copia y removemos la firma para canonicalizar.
        from copy import deepcopy
        root_copy = deepcopy(root)
        for sig in root_copy.findall(f"{{{NAMESPACE_DS}}}Signature"):
            root_copy.remove(sig)
        doc_digest = self._sha256_b64(self._c14n_node(root_copy, exclusive=exclusive))

        # 9. Construir <ds:SignedInfo>
        signed_info = etree.Element(
            f"{{{NAMESPACE_DS}}}SignedInfo", nsmap={"ds": NAMESPACE_DS},
        )
        cm = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}CanonicalizationMethod")
        if exclusive:
            cm.set("Algorithm", "http://www.w3.org/2001/10/xml-exc-c14n#")
        else:
            cm.set("Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315")

        sm = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}SignatureMethod")
        sm.set("Algorithm", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")

        ref_doc = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}Reference")
        ref_doc.set("URI", "")
        transforms = etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}Transforms")
        t1 = etree.SubElement(transforms, f"{{{NAMESPACE_DS}}}Transform")
        t1.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#enveloped-signature")
        if exclusive:
            t2 = etree.SubElement(transforms, f"{{{NAMESPACE_DS}}}Transform")
            t2.set("Algorithm", "http://www.w3.org/2001/10/xml-exc-c14n#")

        dm_doc = etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}DigestMethod")
        dm_doc.set("Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
        etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}DigestValue").text = doc_digest

        ref_props = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}Reference")
        ref_props.set("Type", "http://uri.etsi.org/01903#SignedProperties")
        ref_props.set("URI", f"#{signed_props_id}")
        dm_p = etree.SubElement(ref_props, f"{{{NAMESPACE_DS}}}DigestMethod")
        dm_p.set("Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
        etree.SubElement(ref_props, f"{{{NAMESPACE_DS}}}DigestValue").text = signed_props_digest

        # 10. Insertar SignedInfo al inicio de Signature y firmar
        sig_node.insert(0, signed_info)
        signed_info_c14n = self._c14n_node(signed_info, exclusive=exclusive)
        signature_value = base64.b64encode(
            private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA256()),
        ).decode()

        sig_value_el = etree.Element(f"{{{NAMESPACE_DS}}}SignatureValue")
        sig_value_el.text = signature_value
        # SignatureValue después de SignedInfo, antes de KeyInfo
        sig_node.insert(1, sig_value_el)

        # 11. KeyInfo (entre SignatureValue y Object)
        key_info = etree.Element(f"{{{NAMESPACE_DS}}}KeyInfo")
        x509_data = etree.SubElement(key_info, f"{{{NAMESPACE_DS}}}X509Data")
        etree.SubElement(x509_data, f"{{{NAMESPACE_DS}}}X509Certificate").text = cert_b64
        sig_node.insert(2, key_info)
        # obj_node ya está al final de sig_node — el orden final es:
        #   SignedInfo, SignatureValue, KeyInfo, Object  ✅

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=False)

    def _c14n_node(self, node: _Element, exclusive: bool = True) -> bytes:
        """Canonicalización (exclusiva o inclusive) de un nodo del árbol."""
        return etree.tostring(node, method="c14n", exclusive=exclusive, with_comments=False)

    def _sha256_b64(self, data: bytes) -> str:
        return base64.b64encode(hashlib.sha256(data).digest()).decode()


# Validador XSD

# SKIP_XSD_VALIDATION=true SÓLO se honra cuando ECF_AMBIENTE=simulacion.
# En cualquier otro ambiente (TesteCF, CerteCF, eCF/produccion) la validación XSD
# es obligatoria y SKIP_XSD_VALIDATION se ignora — la DGII puede rechazar XML que
# pasa nuestro pipeline pero no su esquema oficial, y permitir el bypass invita
# a producir comprobantes inválidos en certificación.
_SKIP_XSD_VALIDATION = os.environ.get("SKIP_XSD_VALIDATION", "false").lower() == "true"
_AMBIENTE_RAW = os.environ.get("ECF_AMBIENTE", "").lower()
_PROD_AMBIENTE = _AMBIENTE_RAW in {"ecf", "produccion", "certificacion", "certeecf", "testecf"}
_SIM_AMBIENTE  = _AMBIENTE_RAW in {"simulacion", "sim", ""}


class ECFValidator:
    """Valida XML contra esquemas XSD oficiales de la DGII (e-CF y eventos).

    En producción la validación es **obligatoria**: ``SKIP_XSD_VALIDATION``
    se ignora cuando ``ECF_AMBIENTE=eCF`` o ``produccion``.
    """

    _schemas: dict[str, etree.XMLSchema] = {}

    # API alta: valida e-CF por tipo numérico.
    def validar(self, xml_bytes: bytes, tipo_ecf: int) -> tuple[bool, list[str]]:
        schema_name = f"ECF-{tipo_ecf}"
        return self._validar_por_nombre(xml_bytes, schema_name, fallback="ECF")

    # API alta: valida un evento (ANECF / ACECF / ARECF / Semilla / RFCE-32).
    def validar_evento(self, xml_bytes: bytes, evento: str) -> tuple[bool, list[str]]:
        return self._validar_por_nombre(xml_bytes, evento)

    def _validar_por_nombre(
        self,
        xml_bytes: bytes,
        schema_name: str,
        fallback: str | None = None,
    ) -> tuple[bool, list[str]]:
        schema = self._get_schema(schema_name) or (
            self._get_schema(fallback) if fallback else None
        )
        if schema is None:
            # Bypass SOLO en simulación local (modo desarrollo): cualquier otro
            # ambiente exige el XSD presente en disco — ejecutar el script
            # de descarga es parte del despliegue.
            if _SKIP_XSD_VALIDATION and _SIM_AMBIENTE:
                logger.warning(
                    "XSD no disponible para %s — validación omitida (modo simulación). "
                    "Esto NUNCA debe ocurrir en certificación o producción.",
                    schema_name,
                )
                return True, []
            raise ValueError(
                f"XSD obligatorio no disponible para {schema_name}. "
                f"Ejecuta: bash scripts/actualizar_xsd.sh "
                f"(ECF_AMBIENTE={_AMBIENTE_RAW or 'no_definido'})"
            )

        doc = etree.fromstring(xml_bytes)
        valido = schema.validate(doc)
        errores = [str(e) for e in schema.error_log]
        return valido, errores

    def _get_schema(self, name: str | None) -> etree.XMLSchema | None:
        if not name:
            return None
        if name in self._schemas:
            return self._schemas[name]
        xsd_path = XSD_DIR / f"{name}.xsd"
        if not xsd_path.exists():
            return None
        schema_doc = etree.parse(str(xsd_path))
        schema = etree.XMLSchema(schema_doc)
        self._schemas[name] = schema
        return schema


# Servicio principal: orquesta todo el flujo

class ECFCoreService:
    """Orquestador del procesamiento de un e-CF.

    Flujo: generar XML → validar XSD → firmar XAdES.

    En la DGII RD el identificador único de un e-CF es el ``CodigoSeguridad``
    (6 alfanuméricos del ``SignatureValue``) + ``TrackId`` retornado por la DGII
    al recibir el envío. NO se usa CUFE — esa convención es de Colombia.
    El ``CodigoSeguridad`` se calcula post-firma en
    ``ecf_core.dgii_client.generar_security_code``.
    """

    def __init__(self):
        self.generator = ECFXMLGenerator()
        self.signer    = ECFSigner()
        self.validator = ECFValidator()

    def procesar(
        self,
        factura: FacturaECF,
        p12_data: bytes,
        p12_password: bytes,
    ) -> dict:
        # 1. XML (sin firma)
        logger.info("Generando XML para NCF %s", factura.ncf)
        xml_original = self.generator.generar(factura)

        # 2. Firma XAdES-BES — el XSD del e-CF exige <ds:Signature> (xs:any
        # minOccurs=1 al final), por lo que la validación XSD se hace sobre
        # el XML ya firmado.
        logger.info("Firmando XML con certificado del tenant")
        xml_firmado = self.signer.firmar(xml_original, p12_data, p12_password)

        # 3. XSD oficial DGII — sobre el XML firmado.
        logger.info("Validando XSD para tipo %s", factura.tipo_ecf)
        valido, errores = self.validator.validar(xml_firmado, factura.tipo_ecf)
        if not valido:
            raise ValueError(f"XML no válido contra XSD DGII: {'; '.join(errores)}")

        return {
            "ncf":          factura.ncf,
            "codigo_seguridad": None,  # siempre None — DGII devuelve esto en su respuesta, no en el XML generado
            "xml_original": xml_original,
            "xml_firmado":  xml_firmado,
            "tipo_ecf":     factura.tipo_ecf,
            "total":        str(factura.total),
            "itbis":        str(factura.total_itbis),
        }
