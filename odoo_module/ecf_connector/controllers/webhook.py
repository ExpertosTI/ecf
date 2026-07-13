# -*- coding: utf-8 -*-
"""
Webhook Controller — Recibe callbacks del Renace e-CF
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


def _rnc_solo_digitos(rnc: str) -> str:
    return ''.join(c for c in (rnc or '') if c.isdigit())


class ECFWebhookController(http.Controller):

    def _buscar_company_por_rnc(self, tenant_rnc: str):
        """Encuentra la compañía aunque el VAT en Odoo tenga guiones (132-84231-6)."""
        digits = _rnc_solo_digitos(tenant_rnc)
        if not digits:
            return request.env['res.company']
        Company = request.env['res.company'].sudo()
        # Búsqueda acotada por fragmentos del RNC (evita cargar todas las compañías)
        if len(digits) >= 9:
            dashed = f"{digits[:3]}-{digits[3:8]}-{digits[8:]}" if len(digits) == 11 else None
            domain = ['|', ('vat', 'ilike', digits), ('vat', '=', dashed)] if dashed else [('vat', 'ilike', digits)]
        else:
            domain = [('vat', 'ilike', digits)]
        for company in Company.search(domain, limit=10):
            if _rnc_solo_digitos(company.vat) == digits:
                return company
        return Company

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

            company = self._buscar_company_por_rnc(tenant_rnc)
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

            if data.get('event') == 'ping':
                return request.make_response('OK', status=200)

            status = self._procesar_callback(data, company)
            if status == 404:
                return request.make_response('Not Found', status=404)
            if status == 403:
                return request.make_response('Forbidden', status=403)
            return request.make_response('OK', status=200)

        except Exception as e:
            _logger.exception("Error procesando callback ECF: %s", e)
            return request.make_response('Error', status=500)

    def _verificar_firma(self, body: bytes, sig_header: str, secret: bytes) -> bool:
        """Verifica que el callback proviene del gateway autorizado."""
        sig_clean = sig_header.replace('sha256=', '', 1)
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig_clean)

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

    def _procesar_callback(self, data: dict, company):
        """Actualiza la factura y el log con el resultado de la DGII. Retorna HTTP status hint."""
        odoo_move_id = data.get('odoo_move_id')
        ncf          = data.get('ncf')
        estado       = data.get('estado')
        codigo_seguridad = data.get('codigo_seguridad') or data.get('cufe')
        qr_code      = data.get('qr_code')
        track_id     = data.get('track_id')
        error_msg    = data.get('error_msg')

        if not odoo_move_id or not ncf:
            _logger.warning("Callback ECF sin odoo_move_id o ncf: %s", data)
            return 404

        env  = request.env(su=True)
        move = env['account.move'].browse(int(odoo_move_id))

        if not move.exists():
            _logger.warning("account.move %s no encontrado en callback ECF", odoo_move_id)
            return 404

        # Verificar que la factura pertenece a la compañía del RNC (anti-IDOR)
        if move.company_id and _rnc_solo_digitos(move.company_id.vat) != _rnc_solo_digitos(company.vat):
            _logger.warning(
                "Callback IDOR rechazado: move %s (company %s) no coincide con RNC %s",
                odoo_move_id, move.company_id.vat, company.vat,
            )
            return 403

        # Actualizar factura
        vals = {'ecf_estado': estado}
        if codigo_seguridad:
            vals['ecf_codigo_seguridad'] = codigo_seguridad
        if qr_code:
            vals['ecf_qr'] = qr_code
        if track_id:
            vals['ecf_track_id'] = track_id

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
                'codigo_seguridad': codigo_seguridad,
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
        track_text = f" — Track: <code>{track_id}</code>" if track_id else ""
        move.message_post(
            body=f"{icono} e-CF {estado.upper()}. NCF: <strong>{ncf}</strong>"
                 + (f" — Cód. Seguridad: <strong>{codigo_seguridad}</strong>" if codigo_seguridad else "")
                 + track_text
                 + error_text,
            message_type='comment',
        )

        _logger.info("Callback procesado: move=%s ncf=%s estado=%s track_id=%s", odoo_move_id, ncf, estado, track_id)
        return 200

    # ─────────────────────────────────────────────────────────────────────────
    # Webhook: e-CF RECIBIDAS (Compras desde DGII)
    # ─────────────────────────────────────────────────────────────────────────

    @http.route(
        '/ecf/webhook/recibida',
        type='http',
        auth='none',
        methods=['POST'],
        csrf=False,
    )
    def ecf_recibida(self, **kwargs):
        """
        Recibe notificación del SaaS con e-CF recibidas desde la DGII.
        Verifica firma HMAC-SHA256, luego crea registros ecf.compra.recibida
        y opcionalmente genera las facturas de proveedor en borrador.
        """
        try:
            body_bytes  = request.httprequest.get_data()
            sig_header  = request.httprequest.headers.get('X-ECF-Signature', '')
            tenant_rnc  = request.httprequest.headers.get('X-ECF-Tenant-RNC', '')

            # La compañía SIEMPRE se resuelve por RNC. Sin RNC no hay forma
            # segura de saber a qué compañía pertenecen las compras (IDOR).
            if not tenant_rnc:
                _logger.warning("Webhook recibida sin header X-ECF-Tenant-RNC — rechazado")
                return request.make_response('Bad Request', status=400)

            company = self._buscar_company_por_rnc(tenant_rnc)
            if not company:
                _logger.warning("Webhook recibida: compañía no encontrada para RNC %s", tenant_rnc)
                return request.make_response('Bad Request', status=400)

            # Firma HMAC obligatoria (mismo estándar que /callback)
            secret = company.ecf_webhook_secret or ''
            if not secret.strip():
                _logger.error("Webhook recibida: secret no configurado — rechazado")
                return request.make_response('Forbidden', status=403)
            if not sig_header:
                _logger.warning("Webhook recibida sin firma X-ECF-Signature")
                return request.make_response('Unauthorized', status=401)
            if not self._verificar_firma(body_bytes, sig_header, secret.encode()):
                _logger.warning("Webhook recibida: firma HMAC inválida")
                return request.make_response('Unauthorized', status=401)

            data = json.loads(body_bytes)

            if not self._verificar_timestamp(data):
                _logger.warning("Webhook recibida: timestamp expirado")
                return request.make_response('Request Expired', status=408)

            compras = data.get('compras', [])
            if not compras:
                return request.make_response('OK — sin compras', status=200)

            self._procesar_compras_recibidas(compras, company)

            _logger.info("Webhook recibida OK: %d e-CF procesadas para %s", len(compras), tenant_rnc)
            return request.make_response(f'OK — {len(compras)} registros', status=200)

        except Exception as e:
            _logger.exception("Error procesando webhook e-CF recibidas: %s", e)
            return request.make_response('Error', status=500)

    def _procesar_compras_recibidas(self, compras: list, company):
        """
        Crea registros ecf.compra.recibida para cada e-CF del payload.
        Usa ON CONFLICT lógico (búsqueda previa) para evitar duplicados.
        """
        env = request.env(su=True)
        Model = env['ecf.compra.recibida']
        creados = 0
        duplicados = 0

        for compra in compras:
            ncf = compra.get('ncf', '')
            if not ncf:
                continue

            # Deduplicación
            existing = Model.search([
                ('ncf', '=', ncf),
                ('company_id', '=', company.id),
            ], limit=1)

            if existing:
                duplicados += 1
                continue

            try:
                from datetime import date as _date
                fecha_str = compra.get('fecha_comprobante', '')
                try:
                    fecha = _date.fromisoformat(fecha_str) if fecha_str else _date.today()
                except ValueError:
                    fecha = _date.today()

                def _dec(k):
                    try:
                        return float(compra.get(k, 0) or 0)
                    except (TypeError, ValueError):
                        return 0.0

                Model.create({
                    'ncf':              ncf,
                    'rnc_proveedor':    compra.get('rnc_proveedor', ''),
                    'nombre_proveedor': (compra.get('nombre_proveedor', '') or '')[:255],
                    'tipo_ecf':         compra.get('tipo_ecf') or 31,
                    'codigo_seguridad': compra.get('codigo_seguridad') or compra.get('cufe') or False,
                    'fecha_comprobante': fecha,
                    'total_monto':      _dec('total_monto'),
                    'itbis_facturado':  _dec('itbis_facturado'),
                    'monto_servicios':  _dec('monto_servicios'),
                    'monto_bienes':     _dec('monto_bienes'),
                    'ambiente':         compra.get('ambiente', 'produccion'),
                    'estado_odoo':      'nueva',
                    'company_id':       company.id,
                })
                creados += 1

            except Exception as e:
                _logger.error("Error creando ecf.compra.recibida NCF %s: %s", ncf, e)

        _logger.info("e-CF Recibidas: %d creadas, %d duplicadas", creados, duplicados)
