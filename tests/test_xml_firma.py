"""Comprueba que la semilla firmada por DGIIClient verifica en xml_firma."""
from __future__ import annotations

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from ecf_core.dgii_client import DGIIClient
from ecf_core.xml_firma import FirmaInvalida, verificar_firma_xml


def _p12() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "DO"),
        x509.NameAttribute(NameOID.COMMON_NAME, "132842316"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    data = pkcs12.serialize_key_and_certificates(
        b"renace", key, cert, None, serialization.BestAvailableEncryption(b"clave"),
    )
    return data, b"clave"


def test_semilla_firmada_verifica():
    cliente = DGIIClient("certificacion")
    p12, clave = _p12()
    cliente.set_certificate(p12, clave)
    firmado = cliente._sign_seed_xml(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SemillaModel xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        "<valor>semilla-de-prueba</valor>"
        "<fecha>2026-10-06T16:00:00</fecha>"
        "</SemillaModel>"
    )
    verificar_firma_xml(firmado)


def test_semilla_sin_firma_falla():
    with pytest.raises(FirmaInvalida):
        verificar_firma_xml(b"<SemillaModel><valor>x</valor></SemillaModel>")
