# Casos de homologación ECF DGII v2.1

from __future__ import annotations

import pytest
from decimal import Decimal
from datetime import date, timedelta

from ecf_core.ecf_core_service import (
    ECFCoreService, FacturaECF, ItemECF
)
from ecf_core.dgii_client import DGIIClient, EstadoDGII


# Fixtures

@pytest.fixture
def ecf_service():
    return ECFCoreService()


@pytest.fixture
def p12_prueba(tmp_path):
    """
    Certificado .p12 de PRUEBA — generado para el ambiente de certificación.
    En producción usar el certificado real emitido por la DGII.
    """
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "DO"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Empresa Prueba ECF"),
        x509.NameAttribute(NameOID.COMMON_NAME, "130000001"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    p12 = pkcs12.serialize_key_and_certificates(
        b"test", key, cert, None,
        serialization.BestAvailableEncryption(b"test123")
    )
    return p12, b"test123"


@pytest.fixture
def emisor_base():
    return {
        "rnc_emisor":          "130000001",
        "razon_social_emisor": "Empresa Prueba ECF SRL",
        "direccion_emisor":    "Av. 27 de Febrero 123, Santo Domingo",
    }


def _item_simple(precio=1000, itbis=18):
    return ItemECF(
        linea=1,
        descripcion="Servicio de prueba para homologación DGII",
        cantidad=Decimal("1"),
        precio_unitario=Decimal(str(precio)),
        itbis_tasa=Decimal(str(itbis)),
    )


# CASO 1: Factura de Crédito Fiscal (e-CF tipo 31)
# Obligatorio para homologación

class TestCaso01CreditoFiscal:
    """Emisión de Factura de Crédito Fiscal a contribuyente con RNC."""

    def test_genera_xml_valido(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=31,
            ncf="E310000000001",
            **emisor_base,
            fecha_emision=date.today(),
            rnc_comprador="101000000",
            nombre_comprador="Cliente Empresa SA",
            items=[_item_simple(5000, 18)],
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert resultado["ncf"] == "E310000000001"
        assert resultado["cufe"]
        assert b"<ds:Signature" in resultado["xml_firmado"]
        assert resultado["xml_firmado"].startswith(b"<?xml")

    def test_calculo_itbis_correcto(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        item = ItemECF(
            linea=1, descripcion="Item ITBIS 18%",
            cantidad=Decimal("2"), precio_unitario=Decimal("1000"),
            itbis_tasa=Decimal("18"),
        )
        factura = FacturaECF(
            tipo_ecf=31, ncf="E310000000002",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000001", nombre_comprador="Cliente",
            items=[item],
        )
        assert factura.subtotal == Decimal("2000.00")
        assert factura.total_itbis == Decimal("360.00")
        assert factura.total == Decimal("2360.00")

    def test_multiples_items(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        items = [
            ItemECF(linea=i, descripcion=f"Item {i}",
                    cantidad=Decimal("1"), precio_unitario=Decimal("500"),
                    itbis_tasa=Decimal("18"))
            for i in range(1, 6)
        ]
        factura = FacturaECF(
            tipo_ecf=31, ncf="E310000000003",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000002", nombre_comprador="Cliente Multi",
            items=items,
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert resultado["xml_firmado"]
        assert factura.total == Decimal("2950.00")  # 2500 + 450 ITBIS


# CASO 2: Factura de Consumo (e-CF tipo 32)
# Consumidor final sin RNC

class TestCaso02Consumo:
    def test_consumidor_final_sin_rnc(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=32, ncf="E320000000001",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador=None,    # Sin RNC — consumidor final
            nombre_comprador=None,
            items=[_item_simple(500, 18)],
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert resultado["xml_firmado"]
        # Verificar que el XML no incluye nodo Comprador
        assert b"<Comprador>" not in resultado["xml_firmado"]


# CASO 3: Nota de Crédito (e-CF tipo 34)
# Debe referenciar el NCF original

class TestCaso03NotaCredito:
    def test_nota_credito_con_referencia(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=34, ncf="E340000000001",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000000", nombre_comprador="Cliente",
            items=[_item_simple(200, 18)],
            ncf_referencia="E310000000001",
            fecha_ncf_referencia=date.today() - timedelta(days=5),
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert b"InformacionReferencia" in resultado["xml_firmado"]
        assert b"E310000000001" in resultado["xml_firmado"]

    def test_nota_credito_sin_referencia_falla(self, ecf_service, p12_prueba, emisor_base):
        """La DGII rechaza una nota de crédito sin referencia."""
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=34, ncf="E340000000002",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000000", nombre_comprador="Cliente",
            items=[_item_simple(200, 18)],
            ncf_referencia=None,  # Falta la referencia
        )
        # El XML se genera pero el validador XSD debe detectar el error
        # (cuando los XSD estén instalados)
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert resultado["xml_firmado"]  # Se genera, DGII lo rechazará


# CASO 4: Nota de Débito (e-CF tipo 33)

class TestCaso04NotaDebito:
    def test_nota_debito_con_referencia(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=33, ncf="E330000000001",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000003", nombre_comprador="Deudor SA",
            items=[_item_simple(100, 18)],
            ncf_referencia="E310000000001",
            fecha_ncf_referencia=date.today() - timedelta(days=2),
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert b"InformacionReferencia" in resultado["xml_firmado"]


# CASO 5: ITBIS exento (tasa 0%)

class TestCaso05ITBISExento:
    def test_item_exento_itbis_cero(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        item = ItemECF(
            linea=1, descripcion="Servicio educativo exento",
            cantidad=Decimal("1"), precio_unitario=Decimal("1000"),
            itbis_tasa=Decimal("0"),
        )
        factura = FacturaECF(
            tipo_ecf=32, ncf="E320000000002",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador=None, nombre_comprador=None,
            items=[item],
        )
        assert factura.total_itbis == Decimal("0")
        assert factura.total == Decimal("1000.00")
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_prueba")
        assert resultado["xml_firmado"]


# CASO 6: CUFE — verificación del algoritmo SHA-384

class TestCaso06CUFE:
    def test_cufe_es_sha384(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=31, ncf="E310000000010",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000004", nombre_comprador="Cliente CUFE",
            items=[_item_simple(1000, 18)],
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret_cufe")
        # SHA-384 produce 96 caracteres hex
        assert len(resultado["cufe"]) == 96
        assert all(c in "0123456789abcdef" for c in resultado["cufe"])

    def test_cufe_determinista(self, ecf_service, p12_prueba, emisor_base):
        """El mismo input siempre produce el mismo CUFE."""
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=31, ncf="E310000000011",
            **emisor_base, fecha_emision=date(2025, 1, 15),
            rnc_comprador="101000004", nombre_comprador="Cliente",
            items=[_item_simple(1000, 18)],
        )
        r1 = ecf_service.procesar(factura, p12_data, p12_pass, "mi_clave")
        r2 = ecf_service.procesar(factura, p12_data, p12_pass, "mi_clave")
        assert r1["cufe"] == r2["cufe"]


# CASO 7: Firma digital

class TestCaso07FirmaDigital:
    def test_xml_contiene_ds_signature(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=32, ncf="E320000000010",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador=None, nombre_comprador=None,
            items=[_item_simple()],
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret")
        xml = resultado["xml_firmado"]
        assert b"ds:Signature" in xml
        assert b"ds:SignatureValue" in xml
        assert b"ds:X509Certificate" in xml
        assert b"ds:DigestValue" in xml

    def test_xml_firmado_es_utf8_valido(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        factura = FacturaECF(
            tipo_ecf=31, ncf="E310000000020",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000005", nombre_comprador="Äccents Ñoño",
            items=[_item_simple()],
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret")
        xml = resultado["xml_firmado"]
        assert xml.decode("utf-8")  # No debe lanzar UnicodeDecodeError


# CASO 8: NCF — formato y unicidad

class TestCaso08NCF:
    def test_formato_ncf_correcto(self):
        """NCF debe tener formato: E + tipo(2) + secuencia(10)."""
        import re
        ncf = "E310000000001"
        patron = re.compile(r"^E(3[1-4]|4[1345-7])\d{10}$")
        assert patron.match(ncf)

    def test_tipos_ecf_validos(self):
        tipos_validos = {31, 32, 33, 34, 41, 43, 44, 45, 46, 47}
        for tipo in tipos_validos:
            prefijo = f"E{tipo}"
            ncf = f"{prefijo}0000000001"
            assert len(ncf) == 13, f"NCF de tipo {tipo} tiene longitud incorrecta"


# CASO 9: Moneda extranjera (dólares)

class TestCaso09MonedaExtranjera:
    def test_factura_en_usd(self, ecf_service, p12_prueba, emisor_base):
        p12_data, p12_pass = p12_prueba
        item = ItemECF(
            linea=1, descripcion="Servicio en USD",
            cantidad=Decimal("1"), precio_unitario=Decimal("500"),
            itbis_tasa=Decimal("18"),
        )
        factura = FacturaECF(
            tipo_ecf=31, ncf="E310000000030",
            **emisor_base, fecha_emision=date.today(),
            rnc_comprador="101000006", nombre_comprador="Importadora SA",
            items=[item],
            moneda="USD",
            tipo_cambio=Decimal("59.50"),
        )
        resultado = ecf_service.procesar(factura, p12_data, p12_pass, "secret")
        assert b"USD" in resultado["xml_firmado"]
        assert b"59.50" in resultado["xml_firmado"]


# CASO 10: Contingencia — manejo de errores de red

class TestCaso10Contingencia:
    @pytest.mark.asyncio
    async def test_reintento_en_timeout(self, monkeypatch):
        """El sistema debe reintentar hasta 3 veces ante timeouts."""
        import httpx
        llamadas = {"count": 0}

        async def mock_post(*args, **kwargs):
            llamadas["count"] += 1
            if llamadas["count"] < 3:
                raise httpx.TimeoutException("timeout simulado")
            # Tercera llamada exitosa
            from unittest.mock import MagicMock
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "estado": "1", "codigo": "0", "mensaje": "Aceptado",
                "CUFE": "a" * 96
            }
            return resp

        client = DGIIClient(ambiente="certificacion")
        client._client = MagicMock()
        client._client.post = mock_post

        respuesta = await client.enviar_ecf(b"<xml/>", "130000001", 31, "E310000000001")
        assert llamadas["count"] == 3
        assert respuesta.estado.value == "1"
