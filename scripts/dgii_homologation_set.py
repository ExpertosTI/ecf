"""
dgii_homologation_set.py — Automatización de Casos de Prueba para Certificación
Este script envía los casos de prueba requeridos por la DGII para los pasos 2, 3 y 4.
"""

import asyncio
import json
import httpx
import sys

API_URL = "https://ecf.renace.tech"  # Cambiar según entorno
API_KEY = "tu_api_key_aqui"

# Casos de prueba típicos de la DGII para homologación
TEST_CASES = [
    {
        "id": "SC1",
        "nombre": "E31 - Crédito Fiscal (Consumo Local)",
        "payload": {
            "tipo_ecf": 31,
            "rnc_comprador": "131123456",
            "items": [
                {"descripcion": "Producto A", "cantidad": 1, "precio_unitario": 1000, "itbis_tasa": 18}
            ]
        }
    },
    {
        "id": "SC2",
        "nombre": "E32 - Consumo Final (Monto < 250,000)",
        "payload": {
            "tipo_ecf": 32,
            "items": [
                {"descripcion": "Servicio B", "cantidad": 1, "precio_unitario": 500, "itbis_tasa": 18}
            ]
        }
    },
    {
        "id": "SC3",
        "nombre": "E34 - Nota de Crédito (Referencia E31)",
        "payload": {
            "tipo_ecf": 34,
            "ncf_referencia": "E310000000001",
            "items": [
                {"descripcion": "Devolución", "cantidad": 1, "precio_unitario": 1000, "itbis_tasa": 18}
            ]
        }
    }
]

async def submit_test_cases():
    async with httpx.AsyncClient(timeout=30) as client:
        print(f"🚀 Iniciando envío de Set de Pruebas a {API_URL}")
        
        for case in TEST_CASES:
            print(f"📦 Enviando {case['id']}: {case['nombre']}...")
            try:
                resp = await client.post(
                    f"{API_URL}/v1/ecf/emitir",
                    json=case['payload'],
                    headers={"X-API-Key": API_KEY}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    print(f"✅ Éxito. NCF asignado: {data['ncf']}")
                else:
                    print(f"❌ Error {resp.status_code}: {resp.text}")
            except Exception as e:
                print(f"💥 Error crítico: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        API_KEY = sys.argv[1]
    asyncio.run(submit_test_cases())
