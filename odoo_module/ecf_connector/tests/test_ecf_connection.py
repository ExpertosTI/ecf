# -*- coding: utf-8 -*-
import json
import hashlib
import hmac
from odoo.tests.common import TransactionCase, HttpCase
from odoo.tools import mute_logger

class TestECFConnection(HttpCase):

    def setUp(self):
        super(TestECFConnection, self).setUp()
        self.company = self.env.company
        self.company.ecf_webhook_secret = 'test_secret_key'
        self.webhook_url = '/ecf/webhook/recibida'

    def _generate_signature(self, payload, secret):
        payload_str = json.dumps(payload, separators=(',', ':'))
        return hmac.new(
            secret.encode(),
            payload_str.encode(),
            hashlib.sha256
        ).hexdigest()

    @mute_logger('odoo.addons.ecf_connector.controllers.webhook')
    def test_webhook_recibida_valid(self):
        """Prueba que el webhook acepte una firma válida y cree el registro."""
        payload = {
            "ncf": "E310000000001",
            "rnc_proveedor": "131793916",
            "nombre_proveedor": "PROVEEDOR TEST",
            "tipo_ecf": 31,
            "cufe": "test_cufe_123",
            "fecha_comprobante": "2026-04-26",
            "total_monto": 1180.00,
            "itbis_facturado": 180.00,
            "monto_bienes": 1000.00,
            "monto_servicios": 0.00,
            "ambiente": "certificacion"
        }
        
        signature = self._generate_signature(payload, self.company.ecf_webhook_secret)
        
        response = self.url_open(
            self.webhook_url,
            data=json.dumps(payload),
            headers={
                'Content-Type': 'application/json',
                'X-Signature': signature,
                'X-RNC-Tenant': self.company.vat or '123456789'
            }
        )
        
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result.get('success'))
        
        # Verificar que se creó el registro en Odoo
        recibida = self.env['ecf.compra.recibida'].search([('ncf', '=', 'E310000000001')], limit=1)
        self.assertTrue(recibida, "No se creó el registro de e-CF recibida")
        self.assertEqual(recibida.nombre_proveedor, "PROVEEDOR TEST")
        self.assertEqual(recibida.total_monto, 1180.00)

    def test_webhook_invalid_signature(self):
        """Prueba que el webhook rechace firmas inválidas."""
        payload = {"ncf": "FAIL"}
        response = self.url_open(
            self.webhook_url,
            data=json.dumps(payload),
            headers={
                'Content-Type': 'application/json',
                'X-Signature': 'invalid_sig'
            }
        )
        # El controlador retorna 401 para firmas inválidas
        self.assertEqual(response.status_code, 401)
