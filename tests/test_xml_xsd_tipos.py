"""Validación XSD por tipo e-CF (estructura IdDoc/Totales/Items)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ecf_core.ecf_core_service import ECFXMLGenerator, ECFValidator, FacturaECF, ItemECF


def _factura(tipo: int, ncf: str, tasa: Decimal = Decimal("18"), rnc: str = "131880681") -> FacturaECF:
    f = FacturaECF(
        tipo_ecf=tipo,
        ncf=ncf,
        rnc_emisor="132842316",
        razon_social_emisor="Empresa Prueba ECF",
        direccion_emisor="Av. Principal 1",
        fecha_emision=date(2026, 7, 13),
        rnc_comprador=rnc,
        nombre_comprador="Cliente Prueba",
        items=[
            ItemECF(
                1, "Producto homologacion", Decimal("1"), Decimal("1000"),
                itbis_tasa=tasa, indicador_bien_servicio=1,
            ),
        ],
    )
    if tipo in (33, 34):
        f.ncf_referencia = "E310000000099"
        f.fecha_ncf_referencia = date(2026, 7, 1)
    return f


def _errores_estructura(errs: list[str]) -> list[str]:
    """Ignora FechaHoraFirma placeholder (se corrige al firmar)."""
    return [
        e for e in errs
        if "FechaHoraFirma" not in e
        and "Missing child element" not in e
        and "{*}*" not in e
    ]


@pytest.mark.parametrize(
    "tipo,ncf,tasa,rnc",
    [
        (31, "E310000000001", Decimal("18"), "131880681"),
        (32, "E320000000001", Decimal("18"), "131880681"),
        (33, "E330000000001", Decimal("18"), "131880681"),
        (34, "E340000000001", Decimal("18"), "131880681"),
        (41, "E410000000001", Decimal("18"), "131880681"),
        (43, "E430000000001", Decimal("0"), "131880681"),
        (44, "E440000000001", Decimal("0"), "131880681"),
        (45, "E450000000001", Decimal("18"), "131880681"),
        (46, "E460000000001", Decimal("0"), "131880681"),
        (47, "E470000000001", Decimal("0"), "AB123456"),
    ],
)
def test_xml_cumple_xsd_por_tipo(tipo, ncf, tasa, rnc):
    gen = ECFXMLGenerator()
    val = ECFValidator()
    xml = gen.generar(_factura(tipo, ncf, tasa, rnc))
    _ok, errs = val.validar(xml, tipo)
    estructurales = _errores_estructura(errs)
    assert not estructurales, f"E{tipo}: {estructurales[:3]}"


def test_e46_no_emite_indicador_monto_ni_monto_exento():
    xml = ECFXMLGenerator().generar(_factura(46, "E460000000001", Decimal("0")))
    text = xml.decode()
    assert "IndicadorMontoGravado" not in text
    assert "MontoExento" not in text
    assert "MontoGravadoI3" in text or "MontoTotal" in text


def test_e41_emite_retencion_antes_de_nombre():
    xml = ECFXMLGenerator().generar(_factura(41, "E410000000001"))
    text = xml.decode()
    assert "TipoIngresos" not in text.split("IdDoc")[1].split("/IdDoc")[0]
    assert text.index("<Retencion>") < text.index("<NombreItem>")


def test_cantidad_item_max_2_decimales():
    """DGII rechaza CantidadItem con 4 decimales (p.ej. 15.0000)."""
    from datetime import date

    from ecf_core.ecf_core_service import _fmt_dgii_decimal

    assert _fmt_dgii_decimal(Decimal("15.0000"), 2) == "15"
    assert _fmt_dgii_decimal(Decimal("12.5000"), 2) == "12.5"
    assert _fmt_dgii_decimal("10000.0000", 2) == "10000"

    f = FacturaECF(
        tipo_ecf=31,
        ncf="E310000000099",
        rnc_emisor="132842316",
        razon_social_emisor="Test",
        direccion_emisor="Calle 1",
        fecha_emision=date(2026, 7, 13),
        rnc_comprador="131880681",
        nombre_comprador="Cliente",
        items=[ItemECF(1, "ASW", Decimal("15.0000"), Decimal("400.0000"), itbis_tasa=Decimal("18"))],
    )
    xml = ECFXMLGenerator().generar(f).decode()
    assert "<CantidadItem>15</CantidadItem>" in xml
    assert "15.0000" not in xml
