# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
from datetime import datetime, timezone

from odoo.tests.common import HttpCase
from odoo.tools import mute_logger


class TestECFConnection(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.ecf_webhook_secret = 'test_secret_key'
        if not self.company.vat:
            self.company.vat = '131793916'
        self.webhook_url = '/ecf/webhook/recibida'

    def _sign_payload(self, payload: dict, secret: str) -> str:
        body = json.dumps(payload).encode()
        return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    @mute_logger('odoo.addons.ecf_connector.controllers.webhook')
    def test_webhook_recibida_valid(self):
        """Webhook recibida acepta firma HMAC válida y crea el registro."""
        payload = {
            'compras': [{
                'ncf': 'E310000000001',
                'rnc_proveedor': '131793916',
                'nombre_proveedor': 'PROVEEDOR TEST',
                'tipo_ecf': 31,
                'codigo_seguridad': 'test_seg_123',
                'fecha_comprobante': '2026-04-26',
                'total_monto': 1180.00,
                'itbis_facturado': 180.00,
                'monto_bienes': 1000.00,
                'monto_servicios': 0.00,
                'ambiente': 'certificacion',
            }],
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(payload).encode()
        signature = self._sign_payload(payload, self.company.ecf_webhook_secret)

        response = self.url_open(
            self.webhook_url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-ECF-Signature': signature,
                'X-ECF-Tenant-RNC': self.company.vat,
            },
        )

        self.assertEqual(response.status_code, 200)
        recibida = self.env['ecf.compra.recibida'].search([
            ('ncf', '=', 'E310000000001'),
            ('company_id', '=', self.company.id),
        ], limit=1)
        self.assertTrue(recibida, 'No se creó el registro de e-CF recibida')
        self.assertEqual(recibida.nombre_proveedor, 'PROVEEDOR TEST')
        self.assertEqual(recibida.total_monto, 1180.00)

    @mute_logger('odoo.addons.ecf_connector.controllers.webhook')
    def test_webhook_invalid_signature(self):
        """Webhook rechaza firmas inválidas."""
        payload = {
            'compras': [{'ncf': 'E310000000002'}],
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        response = self.url_open(
            self.webhook_url,
            data=json.dumps(payload).encode(),
            headers={
                'Content-Type': 'application/json',
                'X-ECF-Signature': 'sha256=invalid_sig',
                'X-ECF-Tenant-RNC': self.company.vat,
            },
        )
        self.assertEqual(response.status_code, 401)
