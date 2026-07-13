"""Tests para verificación de firma XML y helpers DGII."""

from datetime import date

from ecf_core.utils import format_fecha_dgii


def test_format_fecha_dgii():
    assert format_fecha_dgii(date(2026, 4, 26)) == "26-04-2026"


def test_verificar_firma_xml_sin_signature():
    from ecf_core.xml_signature import verificar_firma_xml

    ok, motivo = verificar_firma_xml(b"<ECF><eNCF>E310000000001</eNCF></ECF>")
    assert not ok
    assert "Signature" in motivo
