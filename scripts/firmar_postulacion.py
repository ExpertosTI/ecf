#!/usr/bin/env python3
"""
firmar_postulacion.py — Firma el archivo XML de postulación DGII con un
certificado .p12 usando XAdES-BES (RSA-SHA256), igual que los e-CF.

Uso:
    python scripts/firmar_postulacion.py \
        --xml 202606225704499.xml \
        --p12 /ruta/a/certificado.p12 \
        --password "clave_del_p12" \
        --out postulacion_firmada.xml
"""

import argparse
import sys
import uuid
import base64
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs12
from lxml import etree

NAMESPACE_DS = "http://www.w3.org/2000/09/xmldsig#"
XADES_NS     = "http://uri.etsi.org/01903/v1.3.2#"


def firmar_xml(xml_bytes: bytes, p12_data: bytes, p12_password: bytes) -> bytes:
    private_key, certificate, _ = pkcs12.load_key_and_certificates(p12_data, p12_password)

    parser = etree.XMLParser(remove_blank_text=True)
    root = etree.fromstring(xml_bytes, parser)

    signing_time     = datetime.now(timezone.utc)
    signing_time_iso = signing_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    cert_digest = base64.b64encode(certificate.fingerprint(hashes.SHA256())).decode()
    cert_serial = str(certificate.serial_number)
    cert_issuer = certificate.issuer.rfc4514_string()
    cert_b64    = base64.b64encode(
        certificate.public_bytes(serialization.Encoding.DER)
    ).decode()

    signature_id    = f"Signature-{uuid.uuid4()}"
    signed_props_id = f"SignedProperties-{uuid.uuid4()}"

    # ── ds:Signature ──────────────────────────────────────────────────
    sig_node = etree.SubElement(root, f"{{{NAMESPACE_DS}}}Signature",
                                nsmap={"ds": NAMESPACE_DS})
    sig_node.set("Id", signature_id)

    # ── ds:Object > xades:QualifyingProperties ────────────────────────
    obj_node = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}Object")
    qprops   = etree.SubElement(obj_node, f"{{{XADES_NS}}}QualifyingProperties",
                                nsmap={"xades": XADES_NS})
    qprops.set("Target", f"#{signature_id}")

    signed_props = etree.SubElement(qprops, f"{{{XADES_NS}}}SignedProperties")
    signed_props.set("Id", signed_props_id)
    ssp = etree.SubElement(signed_props, f"{{{XADES_NS}}}SignedSignatureProperties")
    etree.SubElement(ssp, f"{{{XADES_NS}}}SigningTime").text = signing_time_iso

    # Cert metadata
    scerts   = etree.SubElement(ssp, f"{{{XADES_NS}}}SigningCertificate")
    scert    = etree.SubElement(scerts, f"{{{XADES_NS}}}Cert")
    cv       = etree.SubElement(scert, f"{{{XADES_NS}}}CertDigest")
    etree.SubElement(cv, f"{{{NAMESPACE_DS}}}DigestMethod").set(
        "Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
    etree.SubElement(cv, f"{{{NAMESPACE_DS}}}DigestValue").text = cert_digest
    issuer_serial = etree.SubElement(scert, f"{{{XADES_NS}}}IssuerSerial")
    etree.SubElement(issuer_serial, f"{{{NAMESPACE_DS}}}X509IssuerName").text   = cert_issuer
    etree.SubElement(issuer_serial, f"{{{NAMESPACE_DS}}}X509SerialNumber").text = cert_serial

    # ── Canonicalizar documento sin ds:Signature para el digest ──────
    root_copy = etree.fromstring(etree.tostring(root))
    for sig in root_copy.findall(f"{{{NAMESPACE_DS}}}Signature"):
        root_copy.remove(sig)
    doc_c14n   = etree.tostring(root_copy, method="c14n", exclusive=False)
    doc_digest = base64.b64encode(
        hashes.Hash(hashes.SHA256()).__class__(hashes.SHA256()).update(doc_c14n) or  # noqa
        __import__('hashlib').sha256(doc_c14n).digest()
    ).decode()

    # ── ds:SignedInfo ─────────────────────────────────────────────────
    signed_info = etree.Element(f"{{{NAMESPACE_DS}}}SignedInfo",
                                nsmap={"ds": NAMESPACE_DS})
    etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}CanonicalizationMethod").set(
        "Algorithm", "http://www.w3.org/TR/2001/REC-xml-c14n-20010315")
    sm = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}SignatureMethod")
    sm.set("Algorithm", "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")

    ref_doc = etree.SubElement(signed_info, f"{{{NAMESPACE_DS}}}Reference")
    ref_doc.set("URI", "")
    transforms = etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}Transforms")
    t1 = etree.SubElement(transforms, f"{{{NAMESPACE_DS}}}Transform")
    t1.set("Algorithm", "http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}DigestMethod").set(
        "Algorithm", "http://www.w3.org/2001/04/xmlenc#sha256")
    etree.SubElement(ref_doc, f"{{{NAMESPACE_DS}}}DigestValue").text = doc_digest

    # Canonicalizar SignedInfo para firmar
    si_bytes = etree.tostring(signed_info, method="c14n", exclusive=False)

    # ── Firma RSA-SHA256 ──────────────────────────────────────────────
    raw_sig = private_key.sign(si_bytes, padding.PKCS1v15(), hashes.SHA256())
    sig_value_b64 = base64.b64encode(raw_sig).decode()

    # ── Insertar SignedInfo + SignatureValue + KeyInfo en ds:Signature ─
    sig_node.insert(0, signed_info)
    sv_el = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}SignatureValue")
    sv_el.text = sig_value_b64

    ki = etree.SubElement(sig_node, f"{{{NAMESPACE_DS}}}KeyInfo")
    x509 = etree.SubElement(ki, f"{{{NAMESPACE_DS}}}X509Data")
    etree.SubElement(x509, f"{{{NAMESPACE_DS}}}X509Certificate").text = cert_b64

    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def main():
    ap = argparse.ArgumentParser(description="Firma XML de postulación DGII")
    ap.add_argument("--xml",      required=True, help="Ruta al XML de postulación")
    ap.add_argument("--p12",      required=True, help="Ruta al certificado .p12")
    ap.add_argument("--password", required=True, help="Contraseña del .p12")
    ap.add_argument("--out",      default=None,  help="Ruta de salida (default: <input>_firmado.xml)")
    args = ap.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"[ERROR] No se encontró el XML: {xml_path}", file=sys.stderr)
        sys.exit(1)

    p12_path = Path(args.p12)
    if not p12_path.exists():
        print(f"[ERROR] No se encontró el .p12: {p12_path}", file=sys.stderr)
        sys.exit(1)

    out_path = Path(args.out) if args.out else xml_path.with_stem(xml_path.stem + "_firmado")

    print(f"[INFO] Leyendo XML:        {xml_path}")
    print(f"[INFO] Leyendo certificado: {p12_path}")

    xml_bytes = xml_path.read_bytes()
    p12_bytes = p12_path.read_bytes()
    password  = args.password.encode()

    try:
        signed_bytes = firmar_xml(xml_bytes, p12_bytes, password)
        out_path.write_bytes(signed_bytes)
        print(f"[OK] Archivo firmado guardado en: {out_path}")
    except Exception as exc:
        print(f"[ERROR] Fallo al firmar: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
