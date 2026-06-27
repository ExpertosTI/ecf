# Tests API Gateway — FastAPI TestClient
#
# Corre con: pytest tests/test_api.py -v
#
# Estos tests usan mocks de DB y Redis para verificar
# la lógica de la API sin dependencias externas.

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

# Set env vars BEFORE importing the app
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# VAULT_MASTER_KEY must be exactly 32 bytes after base64 decoding (256 bits)
os.environ.setdefault("VAULT_MASTER_KEY", "YTVhNWE1YTVhNWE1YTVhNWE1YTVhNWE1YTVhNWE1YTU=") # 32 bytes of 'a'
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key-12345")


# ── Mocks ──────────────────────────────────────────────

class FakeRecord(dict):
    """Simula un asyncpg.Record que soporta acceso por atributo y por key."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


def make_record(**kwargs) -> FakeRecord:
    return FakeRecord(**kwargs)


class FakeTransaction:
    async def __aenter__(self):
        return None
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


class FakePool:
    """Mock de asyncpg pool con acquire() como async context manager."""

    def __init__(self):
        self.conn = AsyncMock()
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.conn.fetchval = AsyncMock(return_value=None)
        self.conn.fetch = AsyncMock(return_value=[])
        self.conn.execute = AsyncMock()
        self.conn.transaction = lambda: FakeTransaction()

    def acquire(self):
        return _AsyncCtx(self.conn)

    async def close(self):
        pass


class _AsyncCtx:
    def __init__(self, obj):
        self._obj = obj

    async def __aenter__(self):
        return self._obj

    async def __aexit__(self, *args):
        pass


class FakeRedis:
    """Mock de redis.asyncio."""

    def __init__(self):
        self._store = {}

    async def incr(self, key):
        self._store[key] = self._store.get(key, 0) + 1
        return self._store[key]

    async def expire(self, key, ttl):
        pass

    async def ttl(self, key):
        return 60

    async def get(self, key):
        return self._store.get(key)

    async def setex(self, key, ttl, value):
        self._store[key] = value

    async def aclose(self):
        pass

    async def lpush(self, key, *values):
        pass

    async def zadd(self, key, mapping):
        pass

    async def lrange(self, key, start, end):
        return []

    async def llen(self, key):
        return 0

    async def zcard(self, key):
        return 0

    async def rpush(self, key, *values):
        if key not in self._store:
            self._store[key] = []
        if isinstance(self._store[key], list):
            self._store[key].extend(values)

    async def set(self, key, value, ex=None):
        self._store[key] = value


# ── Fixtures ───────────────────────────────────────────

TENANT_SCHEMA = "tenant_130000001"
TEST_API_KEY = "sk_cert_testkey123456"
TENANT_ID = str(uuid.uuid4())


@pytest.fixture
def fake_pool():
    return FakePool()


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def client(fake_pool, fake_redis):
    """TestClient con mocks de DB y Redis inyectados via lifespan override."""
    from api_gateway.main import app

    # Override lifespan
    app.state.db_pool = fake_pool
    app.state.redis = fake_redis

    # Patch admin pool and redis
    from api_gateway import admin
    admin._db_pool_ref = fake_pool
    admin._redis_ref = fake_redis

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _setup_tenant_auth(fake_pool):
    """Configura el mock de DB para que resuelva el API key a un tenant."""
    api_key_hash = hashlib.sha256(TEST_API_KEY.encode()).hexdigest()
    fake_pool.conn.fetchrow.return_value = make_record(
        id=TENANT_ID,
        rnc="130000001",
        razon_social="Empresa Test SRL",
        schema_name=TENANT_SCHEMA,
        ambiente="certificacion",
        estado="activo",
        activo=True,
        max_ecf_mensual=1000,
        ecf_emitidos_mes=0,
        cert_vencimiento=None,
        odoo_webhook_url=None,
        odoo_webhook_secret=None,
    )
    return api_key_hash


# ── Tests: Health ──────────────────────────────────────

class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


# ── Tests: Auth ────────────────────────────────────────

class TestAuth:
    def test_missing_api_key(self, client):
        resp = client.post("/v1/ecf/emitir", json={})
        assert resp.status_code == 401

    def test_invalid_api_key(self, client, fake_pool):
        fake_pool.conn.fetchrow.return_value = None
        resp = client.post(
            "/v1/ecf/emitir",
            json={},
            headers={"X-API-Key": "sk_cert_invalidkey000"}
        )
        assert resp.status_code == 401

    def test_inactive_tenant(self, client, fake_pool):
        fake_pool.conn.fetchrow.return_value = make_record(
            id=TENANT_ID, rnc="130000001", razon_social="Test",
            schema_name=TENANT_SCHEMA, ambiente="certificacion",
            estado="suspendido", activo=False, max_ecf_mensual=1000,
            ecf_emitidos_mes=0, cert_vencimiento=None,
            odoo_webhook_url=None, odoo_webhook_secret=None,
        )
        resp = client.post(
            "/v1/ecf/emitir",
            json={},
            headers={"X-API-Key": TEST_API_KEY}
        )
        assert resp.status_code == 403


# ── Tests: Emitir ECF ─────────────────────────────────

class TestEmitirECF:
    def _valid_payload(self):
        return {
            "tipo_ecf": 31,
            "fecha_emision": "2025-01-15",
            "rnc_comprador": "101000001",
            "items": [
                {
                    "descripcion": "Servicio de consultoría",
                    "cantidad": 1,
                    "precio_unitario": 1000.00,
                    "itbis_tasa": 18,
                }
            ],
        }

    def test_emitir_sin_items_rechazado(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        payload = self._valid_payload()
        payload["items"] = []
        resp = client.post(
            "/v1/ecf/emitir",
            json=payload,
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 422

    def test_emitir_tipo_invalido(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        payload = self._valid_payload()
        payload["tipo_ecf"] = 99
        resp = client.post(
            "/v1/ecf/emitir",
            json=payload,
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 422


# ── Tests: Validar ECF ────────────────────────────────

class TestValidarECF:
    def test_validar_endpoint_exists(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        resp = client.post(
            "/v1/ecf/validar",
            json={"tipo_ecf": 31, "ncf": "E310000000001",
                  "fecha_emision": "2025-01-15", "rnc_comprador": "101000001",
                  "monto_total": 1180, "monto_itbis": 180,
                  "items": [{"descripcion": "Test", "cantidad": 1,
                             "precio_unitario": 1000, "itbis": 180}]},
            headers={"X-API-Key": TEST_API_KEY},
        )
        # 200 o 422 (validación del XML) — no 404
        assert resp.status_code != 404


# ── Tests: Estado ECF ──────────────────────────────────

class TestEstadoECF:
    def test_estado_not_found(self, client, fake_pool):
        tenant_record = make_record(
            id=TENANT_ID,
            rnc="130000001",
            razon_social="Empresa Test SRL",
            schema_name=TENANT_SCHEMA,
            ambiente="certificacion",
            estado="activo",
            activo=True,
            max_ecf_mensual=1000,
            ecf_emitidos_mes=0,
            cert_vencimiento=None,
            odoo_webhook_url=None,
            odoo_webhook_secret=None,
        )
        # 1ra llamada (auth): retorna tenant. 2da (consulta NCF): None.
        fake_pool.conn.fetchrow.side_effect = [tenant_record, None]
        resp = client.get(
            "/v1/ecf/E310000000099/estado",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 404


# ── Tests: Reportes ────────────────────────────────────

class TestReportes:
    def test_606_periodo_invalido(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        resp = client.get(
            "/v1/reportes/606?anio=2025&mes=13",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 422

    def test_607_anio_invalido(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        resp = client.get(
            "/v1/reportes/607?anio=1900&mes=1",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 422


# ── Tests: Admin API ──────────────────────────────────

class TestAdminAPI:
    ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key-12345"}

    def test_admin_no_auth(self, client):
        resp = client.get("/v1/admin/tenants")
        assert resp.status_code in (401, 403)

    def test_admin_bad_key(self, client):
        resp = client.get(
            "/v1/admin/tenants",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code in (401, 403)

    def test_admin_list_tenants(self, client, fake_pool):
        fake_pool.conn.fetch.return_value = [
            make_record(
                id=TENANT_ID, rnc="130000001", razon_social="Test SRL",
                ambiente="certificacion", plan="basico", activo=True,
                created_at=datetime.now(timezone.utc),
            )
        ]
        resp = client.get("/v1/admin/tenants", headers=self.ADMIN_HEADERS)
        assert resp.status_code == 200
        data = resp.json()
        assert "tenants" in data
        assert isinstance(data["tenants"], list)

    def test_admin_get_stats(self, client, fake_pool):
        fake_pool.conn.fetchval.return_value = 5
        resp = client.get("/v1/admin/stats", headers=self.ADMIN_HEADERS)
        assert resp.status_code == 200

    def test_admin_get_dlq(self, client, fake_redis):
        resp = client.get(
            "/v1/admin/dlq",
            headers=self.ADMIN_HEADERS,
        )
        assert resp.status_code == 200

    def test_admin_get_dlq_normalizes_dlq_error(self, client, fake_redis):
        async def fake_lrange(key, start, end):
            return ['{"ecf_id":"1","tenant_id":"2","dlq_error":"fallo auth"}']

        async def fake_llen(key):
            return 1

        fake_redis.lrange = fake_lrange
        fake_redis.llen = fake_llen

        resp = client.get(
            "/v1/admin/dlq",
            headers=self.ADMIN_HEADERS,
        )

        assert resp.status_code == 200
        assert resp.json()["messages"][0]["error"] == "fallo auth"


# ── Tests: Rate Limiting ──────────────────────────────

class TestRateLimiting:
    def test_rate_limit_header(self, client, fake_pool, fake_redis):
        _setup_tenant_auth(fake_pool)
        # Make a request — should get rate limit headers
        resp = client.post(
            "/v1/ecf/emitir",
            json={"tipo_ecf": 31, "ncf": "E310000000001",
                  "fecha_emision": "2025-01-15", "rnc_comprador": "101",
                  "monto_total": 100, "monto_itbis": 18,
                  "items": [{"descripcion": "X", "cantidad": 1,
                             "precio_unitario": 100, "itbis": 18}]},
            headers={"X-API-Key": TEST_API_KEY},
        )
        # Should not be 429 on first request
        assert resp.status_code != 429


# ── Tests: ERP-Agnostic Aliases and Endpoints ──────────

class TestErpAgnostic:
    ADMIN_HEADERS = {"Authorization": "Bearer test-admin-key-12345"}

    def test_emitir_with_external_id_alias(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        fake_pool.conn.fetchval.return_value = "E310000000001"
        payload = {
            "tipo_ecf": 31,
            "fecha_emision": "2025-01-15",
            "rnc_comprador": "130000001",
            "external_id": "citrus-move-12345",
            "external_name": "Citrus Invoice 12345",
            "items": [
                {
                    "descripcion": "Servicio de consultoría",
                    "cantidad": 1,
                    "precio_unitario": 1000.00,
                    "itbis_tasa": 18,
                }
            ],
        }
        resp = client.post(
            "/v1/ecf/emitir",
            json=payload,
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["estado"] == "pendiente"
        assert data["ncf"] == "E310000000001"

        # Verificar que se llamó al INSERT con los valores mapeados correctos
        args = fake_pool.conn.execute.call_args_list
        insert_ecf_call = None
        for call in args:
            sql_query = call[0][0]
            if "INSERT INTO" in sql_query and "odoo_move_id" in sql_query:
                insert_ecf_call = call
                break
        
        assert insert_ecf_call is not None
        query_params = insert_ecf_call[0][1:]
        # odoo_move_id es el parámetro $12 y odoo_move_name es $13
        # En el INSERT, los parámetros son:
        # $1: ecf_id, $2: ncf, $3: tipo_ecf, $4: rnc_comprador, $5: nombre_comprador,
        # $6: fecha_emision, $7: subtotal, $8: total_itbis, $9: total, $10: moneda,
        # $11: tipo_cambio, $12: odoo_move_id, $13: odoo_move_name
        # Así que index 11 (parámetro $12) debe ser "citrus-move-12345" y index 12 (parámetro $13) es "Citrus Invoice 12345"
        assert query_params[11] == "citrus-move-12345"
        assert query_params[12] == "Citrus Invoice 12345"

    def test_actualizar_estado_erp(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        fake_pool.conn.execute.return_value = "UPDATE 1"
        resp = client.patch(
            "/v1/compras/E310000000001/estado-erp?estado_erp=procesada&bill_id=citrus-bill-999",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["estado_erp"] == "procesada"
        assert data["bill_id"] == "citrus-bill-999"

        # Verificar fallback legacy
        resp_legacy = client.patch(
            "/v1/compras/E310000000001/estado-odoo?estado_odoo=procesada&odoo_bill_id=citrus-bill-999",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp_legacy.status_code == 200
        data_legacy = resp_legacy.json()
        assert data_legacy["estado_odoo"] == "procesada"
        assert data_legacy["odoo_bill_id"] == "citrus-bill-999"

    def test_listar_compras_erp_mapping(self, client, fake_pool):
        _setup_tenant_auth(fake_pool)
        fake_pool.conn.fetch.return_value = [
            make_record(
                ncf="E310000000001",
                rnc_proveedor="101000001",
                nombre_proveedor="Proveedor A",
                tipo_bienes=1,
                fecha_comprobante="2025-01-15",
                fecha_pago=None,
                monto_servicios=0,
                monto_bienes=1000,
                total_monto=1180,
                itbis_facturado=180,
                itbis_retenido=0,
                isr_retencion=0,
                estado_odoo="procesada",
                odoo_bill_id="citrus-bill-999",
                codigo_seguridad="XYZ",
                tipo_ecf=31,
                ambiente="certificacion"
            )
        ]
        resp = client.get(
            "/v1/compras?anio=2025&mes=1&estado_erp=procesada",
            headers={"X-API-Key": TEST_API_KEY},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["registros"]) == 1
        reg = data["registros"][0]
        assert reg["estado_erp"] == "procesada"
        assert reg["bill_id"] == "citrus-bill-999"

    def test_get_tenant_webhook_url_alias(self, client, fake_pool):
        fake_pool.conn.fetchrow.return_value = make_record(
            id=TENANT_ID,
            rnc="130000001",
            razon_social="Empresa Test SRL",
            nombre_comercial="Nombre Comercial",
            direccion="Dirección",
            telefono="809",
            email="admin@test.com",
            plan="basico",
            estado="activo",
            schema_name=TENANT_SCHEMA,
            ambiente="certificacion",
            odoo_webhook_url="https://citrus-erp.com/webhook",
            ecf_emitidos_mes=0,
            max_ecf_mensual=1000,
            cert_vencimiento=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        resp = client.get(
            f"/v1/admin/tenants/{TENANT_ID}",
            headers=self.ADMIN_HEADERS,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["webhook_url"] == "https://citrus-erp.com/webhook"
        assert data["odoo_webhook_url"] == "https://citrus-erp.com/webhook"


# ── Tests: Static Assets and DGII Mocks ─────────────────

class TestStaticAssets:
    def test_logo_retrieval(self, client):
        resp = client.get("/renacelogo.svg")
        assert resp.status_code == 200
        assert "image/svg+xml" in resp.headers["content-type"]

    def test_apple_touch_icon_retrieval(self, client):
        resp = client.get("/apple-touch-icon.png")
        assert resp.status_code == 200
        assert "image/png" in resp.headers["content-type"]

    def test_dgii_mock_endpoints(self, client):
        resp_aprobacion = client.post("/fe/aprobacioncomercial/api/ecf")
        assert resp_aprobacion.status_code == 200
        assert resp_aprobacion.json()["status"] == "received"

        resp_semilla = client.get("/fe/autenticacion/api/semilla")
        assert resp_semilla.status_code == 200
        assert "application/xml" in resp_semilla.headers["content-type"]
        assert "MockSeed" in resp_semilla.text

        resp_val_cert = client.post("/fe/autenticacion/api/validacioncertificado")
        assert resp_val_cert.status_code == 200
        assert resp_val_cert.json()["token"] == "mock_token"

        resp_semilla_nueva = client.get("/Autenticacion/api/Autenticacion/Semilla")
        assert resp_semilla_nueva.status_code == 200
        assert "MockSeed" in resp_semilla_nueva.text

        resp_val_cert_nuevo = client.post("/Autenticacion/api/Autenticacion/ValidarSemilla")
        assert resp_val_cert_nuevo.status_code == 200
        assert resp_val_cert_nuevo.json()["token"] == "mock_token"
