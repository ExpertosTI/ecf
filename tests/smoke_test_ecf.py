#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 Renace e-CF — Smoke Test Suite (Integration & Connectivity)
Basado en el estándar de pruebas de Catagce.

Este script verifica la salud y conectividad de los servicios de Facturación Electrónica.
"""

import os
import sys
import json
import time
import hashlib
import hmac
import urllib.request
from datetime import datetime, timezone
from typing import Optional

# Configuración ANSI para colores
class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def print_step(msg):
    print(f"{Colors.OKBLUE} {Colors.BOLD}●{Colors.ENDC} {msg}...", end=" ", flush=True)

def print_ok():
    print(f"{Colors.OKGREEN}OK{Colors.ENDC}")

def print_fail(msg="FAIL"):
    print(f"{Colors.FAIL}{msg}{Colors.ENDC}")

class ECFSmokeTest:
    def __init__(self, base_url: str, admin_key: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.admin_key = admin_key

    def _request(self, method, path, headers=None, data=None):
        url = f"{self.base_url}{path}"
        default_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "application/json"
        }
        if headers:
            default_headers.update(headers)
            
        req = urllib.request.Request(url, method=method, headers=default_headers)
        if data:
            req.data = json.dumps(data).encode('utf-8')
            req.add_header('Content-Type', 'application/json')
        
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.getcode(), json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode('utf-8'))
            except:
                body = e.reason
            return e.code, body
        except Exception as e:
            return 500, str(e)

    def run_all(self):
        print(f"\n{Colors.HEADER}{Colors.BOLD}=== INICIANDO PRUEBAS UNITARIAS E-CF (MODO SMOKE) ==={Colors.ENDC}")
        print(f"{Colors.OKCYAN}URL Base:{Colors.ENDC} {self.base_url}\n")

        success = True
        success &= self.test_health()
        
        if self.admin_key:
            success &= self.test_admin_auth()
            success &= self.test_admin_stats()
        else:
            print(f"{Colors.WARNING}⚠️  Saltando pruebas de Admin (ADMIN_API_KEY no provista){Colors.ENDC}")

        print(f"\n{Colors.HEADER}{Colors.BOLD}=== RESUMEN DE PRUEBAS ==={Colors.ENDC}")
        if success:
            print(f"{Colors.OKGREEN}✅ Todas las pruebas pasaron exitosamente.{Colors.ENDC}")
        else:
            print(f"{Colors.FAIL}❌ Algunas pruebas fallaron. Revise los logs arriba.{Colors.ENDC}")
        
        return success

    def test_health(self):
        print_step("Verificando /health")
        code, data = self._request("GET", "/health")
        if code == 200 and isinstance(data, dict) and data.get("status") == "ok":
            print_ok()
            return True
        print_fail(f"Status Code: {code} - {data}")
        return False

    def test_admin_auth(self):
        print_step("Verificando Admin Auth")
        headers = {"Authorization": f"Bearer {self.admin_key}"}
        code, data = self._request("GET", "/v1/admin/tenants", headers=headers)
        if code == 200:
            print_ok()
            return True
        print_fail(f"Status Code: {code}")
        return False

    def test_admin_stats(self):
        print_step("Obteniendo estadísticas del sistema")
        headers = {"Authorization": f"Bearer {self.admin_key}"}
        code, data = self._request("GET", "/v1/admin/stats", headers=headers)
        if code == 200:
            print_ok()
            if isinstance(data, dict):
                print(f"   {Colors.OKCYAN}→ Tenants activos:{Colors.ENDC} {data.get('total_tenants', 'N/A')}")
                print(f"   {Colors.OKCYAN}→ ECF en cola:{Colors.ENDC} {data.get('queue_pending', 'N/A')}")
            return True
        print_fail()
        return False

def main():
    # Intenta cargar del entorno o usa valores por defecto para pruebas locales
    base_url = os.environ.get("ECF_BASE_URL", "https://ecf.renace.tech")
    admin_key = os.environ.get("ADMIN_API_KEY")

    tester = ECFSmokeTest(base_url, admin_key)
    success = tester.run_all()
    
    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
