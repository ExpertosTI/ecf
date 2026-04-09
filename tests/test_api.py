# Tests API Gateway — FastAPI TestClient
#
# Corre con: pytest tests/test_api.py -v
#
# Estos tests usan mocks de DB y Redis para verificar
# la lógica de la API sin dependencias externas.

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Set env vars BEFORE importing the app
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("VAULT_MASTER_KEY", "a" * 64)
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


class FakePool:
    """Mock de asyncpg pool con acquire() como async context manager."""

    def __init__(self):
        self.conn = AsyncMock()
        self.conn.fetchrow = AsyncMock(return_value=None)
        self.conn.fetchval = AsyncMock(return_value=None)
        self.conn.fetch = AsyncMock(return_value=[])
        self.conn.execute = AsyncMock()

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
        assert resp.status_code == 403

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
            "ncf": "E310000000001",
            "fecha_emision": "2025-01-15",
            "rnc_comprador": "101000001",
            "monto_total": 1180.00,
            "monto_itbis": 180.00,
            "items": [
                {
                    "descripcion": "Servicio de consultoría",
                    "cantidad": 1,
                    "precio_unitario": 1000.00,
                    "itbis": 180.00,
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
        _setup_tenant_auth(fake_pool)
        # fetchrow returns None (no ecf found)
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
        assert isinstance(data, list)

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
