# -*- coding: utf-8 -*-
"""
Webhook Controller — Recibe callbacks del SaaS ECF
Verifica la firma HMAC-SHA256 y timestamp anti-replay antes de actualizar el estado en Odoo.
"""

import hashlib
import hmac
import json
import logging
import time

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Máximo desfase permitido para el timestamp del callback (5 minutos)
WEBHOOK_MAX_AGE_SECONDS = 300


class ECFWebhookController(http.Controller):

    @http.route(
        '/ecf/webhook/callback',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
    )
    def ecf_callback(self, **kwargs):
        """
        Recibe el callback del SaaS con el resultado de la DGII.
        Verifica la firma HMAC-SHA256 y el timestamp anti-replay.
        """
        try:
            body_bytes = request.httprequest.get_data()
            sig_header = request.httprequest.headers.get('X-ECF-Signature', '')

            # Obtener secret — rechazar si no está configurado
            # Buscar por RNC del tenant en el header para identificar la compañía
            tenant_rnc = request.httprequest.headers.get('X-ECF-Tenant-RNC', '')
            if not tenant_rnc:
                _logger.warning("Callback sin header X-ECF-Tenant-RNC — rechazado")
                return request.make_response('Bad Request', status=400)

            company = request.env['res.company'].sudo().search(
                [('vat', '=', tenant_rnc)], limit=1
            )
            if not company:
                _logger.warning("Callback con RNC desconocido: %s", tenant_rnc)
                return request.make_response('Bad Request', status=400)

            secret = company.ecf_webhook_secret or ''

            if not secret or not secret.strip():
                _logger.error("Webhook secret no configurado — callback rechazado")
                return request.make_response('Forbidden', status=403)

            if not sig_header:
                _logger.warning("Callback sin firma X-ECF-Signature")
                return request.make_response('Unauthorized', status=401)

            if not self._verificar_firma(body_bytes, sig_header, secret.encode()):
                _logger.warning("Firma HMAC inválida en callback ECF")
                return request.make_response('Unauthorized', status=401)

            data = json.loads(body_bytes)

            # Anti-replay: verificar que el timestamp del payload no sea demasiado viejo
            if not self._verificar_timestamp(data):
                _logger.warning("Callback ECF rechazado por timestamp expirado o ausente")
                return request.make_response('Request Expired', status=408)

            self._procesar_callback(data)

            return request.make_response('OK', status=200)

        except Exception as e:
            _logger.exception("Error procesando callback ECF: %s", e)
            return request.make_response('Error', status=500)

    def _verificar_firma(self, body: bytes, sig_header: str, secret: bytes) -> bool:
        """Verifica que el callback proviene del SaaS autorizado."""
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        # Comparación en tiempo constante para evitar timing attacks
        return hmac.compare_digest(expected, sig_header)

    def _verificar_timestamp(self, data: dict) -> bool:
        """
        Protección anti-replay: rechaza callbacks con timestamp ausente
        o con más de WEBHOOK_MAX_AGE_SECONDS de antigüedad.
        """
        ts = data.get('timestamp')
        if not ts:
            return False
        try:
            from datetime import datetime, timezone
            callback_time = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            age = abs((now - callback_time).total_seconds())
            return age <= WEBHOOK_MAX_AGE_SECONDS
        except (ValueError, TypeError):
            return False

    def _procesar_callback(self, data: dict):
        """Actualiza la factura y el log con el resultado de la DGII."""
        odoo_move_id = data.get('odoo_move_id')
        ncf          = data.get('ncf')
        estado       = data.get('estado')
        cufe         = data.get('cufe')
        qr_code      = data.get('qr_code')
        error_msg    = data.get('error_msg')

        if not odoo_move_id or not ncf:
            _logger.warning("Callback ECF sin odoo_move_id o ncf: %s", data)
            return

        env  = request.env(su=True)
        move = env['account.move'].browse(int(odoo_move_id))

        if not move.exists():
            _logger.warning("account.move %s no encontrado en callback ECF", odoo_move_id)
            return

        # Actualizar factura
        vals = {'ecf_estado': estado}
        if cufe:
            vals['ecf_cufe'] = cufe
        if qr_code:
            vals['ecf_qr'] = qr_code

        move.write(vals)

        # Actualizar log
        log = env['ecf.log'].search(
            [('move_id', '=', move.id), ('ncf', '=', ncf)],
            limit=1,
            order='create_date desc',
        )
        if log:
            log_vals = {
                'estado':       estado,
                'cufe':         cufe,
                'qr_code':      qr_code,
            }
            if error_msg:
                log_vals['error_msg'] = error_msg
            if estado == 'aprobado' and not log.approved_at:
                from odoo import fields as odoo_fields
                log_vals['approved_at'] = odoo_fields.Datetime.now()
            log.write(log_vals)

        # Mensaje en el chatter
        icono = {'aprobado': '✅', 'rechazado': '❌', 'condicionado': '⚠️'}.get(estado, 'ℹ️')
        error_text = f" — Error: {error_msg}" if error_msg else ""
        move.message_post(
            body=f"{icono} e-CF {estado.upper()}. NCF: <strong>{ncf}</strong>"
                 + (f" — CUFE: {cufe[:20]}..." if cufe else "")
                 + error_text,
            message_type='comment',
        )

        _logger.info("Callback procesado: move=%s ncf=%s estado=%s", odoo_move_id, ncf, estado)
