"""Verificación de una firma XMLDSig enveloped (rsa-sha256, c14n exclusiva).

La usa el paso de comunicación de DGII: DGII firma una semilla y la
envía a /fe/autenticacion/api/validacioncertificado. Aquí se comprueba
que el digest del documento y la firma del SignedInfo coinciden con el
certificado X.509 embebido.
"""
from __future__ import annotations

import base64
import hashlib
import io

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_der_x509_certificate
from lxml import etree

DSIG = "http://www.w3.org/2000/09/xmldsig#"


class FirmaInvalida(Exception):
    pass


def _c14n_documento(nodo: etree._Element) -> bytes:
    buf = io.BytesIO()
    nodo.getroottree().write_c14n(buf, exclusive=True, with_comments=False)
    return buf.getvalue()


def _c14n_propio(nodo: etree._Element) -> bytes:
    """Canonicaliza el nodo como raíz, sin el padre que lo contenía."""
    aislado = etree.fromstring(etree.tostring(nodo))
    return _c14n_documento(aislado)


def verificar_firma_xml(xml_bytes: bytes) -> None:
    """Lanza FirmaInvalida si el XML no trae una firma enveloped válida."""
    parser = etree.XMLParser(remove_blank_text=True)
    try:
        root = etree.fromstring(xml_bytes, parser)
    except etree.XMLSyntaxError as exc:
        raise FirmaInvalida("XML ilegible") from exc

    firma = root.find(f"{{{DSIG}}}Signature")
    if firma is None:
        raise FirmaInvalida("el documento no trae firma")
    signed_info = firma.find(f"{{{DSIG}}}SignedInfo")
    valor = (firma.findtext(f"{{{DSIG}}}SignatureValue") or "").strip()
    digest = (firma.findtext(f".//{{{DSIG}}}DigestValue") or "").strip()
    cert_b64 = (firma.findtext(f".//{{{DSIG}}}X509Certificate") or "").strip()
    if signed_info is None or not valor or not digest or not cert_b64:
        raise FirmaInvalida("la firma está incompleta")

    firma.getparent().remove(firma)
    calculado = base64.b64encode(hashlib.sha256(_c14n_documento(root)).digest()).decode()
    if calculado != digest:
        raise FirmaInvalida("el digest del documento no coincide")

    try:
        cert = load_der_x509_certificate(base64.b64decode(cert_b64))
        cert.public_key().verify(
            base64.b64decode(valor),
            _c14n_propio(signed_info),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except FirmaInvalida:
        raise
    except Exception as exc:
        raise FirmaInvalida("la firma del certificado no verifica") from exc
