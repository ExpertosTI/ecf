"""Verificación de firma XML-DSig / XAdES en e-CF recibidos."""

from __future__ import annotations

import base64
import hashlib
import logging
from copy import deepcopy

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_der_x509_certificate
from lxml import etree

logger = logging.getLogger(__name__)

NS_DS = "http://www.w3.org/2000/09/xmldsig#"
XADES_NS = "http://uri.etsi.org/01903/v1.3.2#"
NSMAP = {"ds": NS_DS}


def _c14n_node(node: etree._Element, exclusive: bool = True) -> bytes:
    return etree.tostring(node, method="c14n", exclusive=exclusive, with_comments=False)


def _sha256_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _find_by_id(root: etree._Element, element_id: str) -> etree._Element | None:
    for elem in root.iter():
        if elem.get("Id") == element_id or elem.get(f"{{{NS_DS}}}Id") == element_id:
            return elem
    return None


def _digest_doc_enveloped(root: etree._Element, exclusive: bool = True) -> str:
    root_copy = deepcopy(root)
    for sig in root_copy.findall(f"{{{NS_DS}}}Signature"):
        root_copy.remove(sig)
    return _sha256_b64(_c14n_node(root_copy, exclusive=exclusive))


def verificar_firma_xml(xml_bytes: bytes) -> tuple[bool, str]:
    """Verifica la firma digital XML-DSig (enveloped + XAdES) de un e-CF.

    Returns:
        (True, "") si la firma es válida.
        (False, motivo) si falla la verificación.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        return False, f"XML mal formado: {exc}"

    sig_node = root.find(f".//{{{NS_DS}}}Signature")
    if sig_node is None:
        return False, "Sin elemento ds:Signature"

    signed_info = sig_node.find(f"{{{NS_DS}}}SignedInfo")
    sig_value_el = sig_node.find(f"{{{NS_DS}}}SignatureValue")
    cert_el = sig_node.find(f".//{{{NS_DS}}}X509Certificate")

    if signed_info is None or sig_value_el is None or cert_el is None:
        return False, "Estructura de firma incompleta (SignedInfo/SignatureValue/X509Certificate)"

    cert_b64 = (cert_el.text or "").replace("\n", "").replace("\r", "").strip()
    if not cert_b64:
        return False, "Certificado X509 vacío"

    try:
        cert = load_der_x509_certificate(base64.b64decode(cert_b64))
        public_key = cert.public_key()
    except Exception as exc:
        return False, f"Certificado X509 inválido: {exc}"

    cm = signed_info.find(f"{{{NS_DS}}}CanonicalizationMethod")
    exclusive = True
    if cm is not None:
        algo = cm.get("Algorithm", "")
        exclusive = "xml-exc-c14n" in algo

    signed_info_c14n = _c14n_node(signed_info, exclusive=exclusive)
    sig_value = base64.b64decode((sig_value_el.text or "").replace("\n", "").replace("\r", ""))

    try:
        public_key.verify(sig_value, signed_info_c14n, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        return False, "SignatureValue no coincide con SignedInfo"

    for ref in signed_info.findall(f"{{{NS_DS}}}Reference"):
        digest_value_el = ref.find(f"{{{NS_DS}}}DigestValue")
        if digest_value_el is None:
            return False, "Reference sin DigestValue"

        expected_digest = (digest_value_el.text or "").strip()
        uri = ref.get("URI", "")
        ref_type = ref.get("Type", "")

        if ref_type == "http://uri.etsi.org/01903#SignedProperties":
            if not uri.startswith("#"):
                return False, "Reference SignedProperties sin URI interno"
            props = _find_by_id(root, uri[1:])
            if props is None:
                return False, f"SignedProperties {uri[1:]} no encontrado"
            actual = _sha256_b64(_c14n_node(props, exclusive=exclusive))
        elif uri == "" or uri.startswith("#"):
            actual = _digest_doc_enveloped(root, exclusive=exclusive)
        else:
            logger.warning("Reference URI no soportada en verificación: %s", uri)
            continue

        if actual != expected_digest:
            return False, f"Digest mismatch en Reference URI={uri!r}"

    return True, ""
