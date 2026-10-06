# -*- coding: utf-8 -*-
"""
🎯 Test Suite: Integración End-to-End (Estilo Catagce)
Este archivo implementa pruebas de flujo completo para el software ecf.renace.tech.

Para correr: pytest tests/test_integration.py -v
"""

import os
import sys
import uuid
import hashlib
from datetime import date
from unittest.mock import AsyncMock, patch

# Configuración automática de PATH para encontrar api_gateway
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir) # Esto es la carpeta /ecf
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Importamos la app
try:
    from api_gateway.main import app
    from fastapi.testclient import TestClient
except ImportError:
    print("❌ Error: No se pudo importar FastAPI o api_gateway.")
    print("   Asegúrate de estar en el entorno virtual o tener las dependencias instaladas.")
    sys.exit(1)

# Fixture (solo para compatibilidad con pytest si se usa después)
def client():
    # Inyectamos mocks básicos para no depender de DB real durante el unit test
    # Pero permitimos que el flujo de la API se ejecute
    with TestClient(app) as c:
        yield c

class TestFlowCatagce:
    """
    Simula el flujo completo que un cliente (como Odoo) realizaría.
    1. Health Check
    2. Validación de Payload
    3. Emisión (Mockeada la DB)
    4. Consulta de Estado
    """

    def test_01_health_check(self, client):
        """Verifica que el servicio esté vivo y responda."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_02_emitir_happy_path(self, client):
        """Simula la emisión exitosa de una factura E31."""
        from api_gateway.main import get_tenant, get_db
        
        tenant_id = str(uuid.uuid4())
        api_key = "sk_test_12345"
        mock_tenant = {
            "id": tenant_id,
            "rnc": "130000001",
            "razon_social": "Empresa Test",
            "schema_name": "tenant_130000001",
            "ambiente": "certificacion",
            "estado": "activo",
            "ecf_emitidos_mes": 0,
            "max_ecf_mensual": 1000,
            "cert_vencimiento": date(2030, 1, 1),
        }
        
        payload = {
            "tipo_ecf": 31,
            "rnc_comprador": "131654665",
            "nombre_comprador": "Cliente Prueba",
            "fecha_emision": str(date.today()),
            "items": [
                {
                    "descripcion": "Producto Test",
                    "cantidad": 1,
                    "precio_unitario": 1000,
                    "itbis_tasa": 18
                }
            ]
        }

        # Mock de dependencias
        async def mock_get_tenant(): return mock_tenant
        async def mock_get_db(): return app.state.db_pool

        app.dependency_overrides[get_tenant] = mock_get_tenant
        app.dependency_overrides[get_db] = mock_get_db

        # Mock del resultado de la DB (Asignación de NCF)
        # conn.fetchval se llama dentro de emitir_ecf
        mock_conn = app.state.mock_conn
        mock_conn.fetchval.return_value = "E310000000001"

        try:
            resp = client.post("/v1/ecf/emitir", json=payload, headers={"X-API-Key": api_key})
            if resp.status_code != 202:
                print(f"\n   Error en /emitir: {resp.status_code} - {resp.text}")
            assert resp.status_code == 202
            assert resp.json()["ncf"] == "E310000000001"
        finally:
            app.dependency_overrides = {}

    def test_03_validar_negativo(self, client):
        """Verifica que el validador rechace payloads incorrectos."""
        # NCF de referencia faltante para Nota de Crédito (Tipo 34)
        payload = {
            "tipo_ecf": 34,
            "rnc_comprador": "101000001",
            "fecha_emision": "2026-05-15",
            "items": [{"descripcion": "Test", "cantidad": 1, "precio_unitario": 100, "itbis_tasa": 18}]
        }
        
        # Este test no necesita mock de auth porque falla en la validación de Pydantic
        resp = client.post("/v1/ecf/validar", json=payload, headers={"X-API-Key": "any"})
        assert resp.status_code == 422 # Error de validación de esquema
        

# Soporte para ejecución directa sin pytest
if __name__ == "__main__":
    print("\n🚀 Ejecutando Test de Integración ECF (Modo Directo)...\n")
    
    # Simulación simple de runner
    class MockClient:
        def __init__(self, app):
            self.client = TestClient(app)
        def __enter__(self): return self.client
        def __exit__(self, *args): pass

    test_suite = TestFlowCatagce()
    
    from unittest.mock import MagicMock
    
    # Inyectamos mocks en el estado de la app
    app.state.db_pool = MagicMock()
    app.state.redis = AsyncMock()
    
    # Configurar db_pool para soportar 'async with db.acquire() as conn'
    mock_conn = AsyncMock()
    app.state.mock_conn = mock_conn
    # acquire() devuelve un objeto con __aenter__ y __aexit__
    app.state.db_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    app.state.db_pool.acquire.return_value.__aexit__ = AsyncMock()
    
    # Soportar 'async with conn.transaction()'
    mock_conn.transaction = MagicMock()
    mock_conn.transaction.return_value.__aenter__ = AsyncMock()
    mock_conn.transaction.return_value.__aexit__ = AsyncMock()
    
    # Configuramos redis para que el rate limiting no falle
    app.state.redis.incr = AsyncMock(return_value=1)
    
    client_instance = TestClient(app)
    
    try:
        print("● Probando Health Check...", end=" ", flush=True)
        test_suite.test_01_health_check(client_instance)
        print("✅ OK")
        
        print("● Probando Flujo de Emisión...", end=" ", flush=True)
        test_suite.test_02_emitir_happy_path(client_instance)
        print("✅ OK")
        
        print("● Probando Validaciones Negativas...", end=" ", flush=True)
        test_suite.test_03_validar_negativo(client_instance)
        print("✅ OK")
        
        print("\n🎉 Pruebas de integración completadas con éxito.")
    except Exception as e:
        import traceback
        print(f"\n❌ Fallo en las pruebas: {e}")
        traceback.print_exc()
        sys.exit(1)
