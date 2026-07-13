# -*- coding: utf-8 -*-
"""
Renace e-CF — Modelos principales
Facturación Electrónica DGII República Dominicana

Arquitectura de campos en account.move:
  - ecf_tipo_id      → tipo de comprobante (E31-E47)
  - ecf_modo         → 'inmediato' | 'diferido' | 'excento'
  - ecf_ncf          → NCF asignado por el SaaS (readonly)
  - ecf_estado       → estado ante DGII
  - ecf_pendiente_conciliacion → True mientras la factura POS no esté pagada
  - ecf_listo_para_emitir     → True cuando el pago fue conciliado

Regla de oro: ecf_emision_automatica = False por defecto.
El trigger NUNCA se dispara solo — solo acción manual del usuario.
"""

import hashlib
import hmac
import json
import logging
import re
import requests
from datetime import date, datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

# Regex de validación para API Keys del SaaS (sk_cert_ o sk_prod_ + 48 hex)
_API_KEY_RE = re.compile(r'^sk_(cert|prod)_[a-f0-9]{48}$')

# Algoritmo oficial DGII para validar RNC (9 dígitos) y Cédula (11 dígitos).
_RNC_WEIGHTS = (7, 9, 8, 6, 5, 4, 3, 2)
_CEDULA_WEIGHTS = (1, 2, 1, 2, 1, 2, 1, 2, 1, 2)


def _validar_rnc(rnc):
    if not isinstance(rnc, str) or len(rnc) != 9 or not rnc.isdigit():
        return False
    suma = sum(int(d) * w for d, w in zip(rnc[:8], _RNC_WEIGHTS))
    residuo = suma % 11
    if residuo == 0:
        esperado = 2
    elif residuo == 1:
        esperado = 1
    else:
        esperado = 11 - residuo
    return esperado == int(rnc[8])


def _validar_cedula(cedula):
    if not isinstance(cedula, str) or len(cedula) != 11 or not cedula.isdigit():
        return False
    suma = 0
    for i, peso in enumerate(_CEDULA_WEIGHTS[:10]):
        producto = int(cedula[i]) * peso
        if producto > 9:
            producto -= 9
        suma += producto
    return ((10 - (suma % 10)) % 10) == int(cedula[10])


def _validar_rnc_o_cedula(documento):
    if not documento:
        return False
    clean = ''.join(filter(str.isdigit, documento))
    if len(clean) == 9:
        return _validar_rnc(clean)
    if len(clean) == 11:
        return _validar_cedula(clean)
    return False


# Mapeo de nombres de UoM de Odoo a códigos numéricos DGII (UnidadMedidaType).
# Si la UoM no se encuentra aquí, se usa '43' (Unidad) por defecto.
_UOM_DGII_MAP = {
    'unidad': '43', 'unidades': '43', 'unit': '43', 'units': '43', 'und': '43',
    'pieza': '11', 'piezas': '11', 'pz': '11',
    'kg': '2', 'kilogramo': '2', 'kilogramos': '2',
    'lb': '4', 'libra': '4', 'libras': '4',
    'galón': '7', 'galon': '7', 'galones': '7', 'gal': '7',
    'litro': '6', 'litros': '6', 'lt': '6', 'l': '6',
    'metro': '1', 'metros': '1', 'm': '1',
    'hora': '14', 'horas': '14', 'hr': '14', 'hrs': '14',
    'día': '15', 'dia': '15', 'días': '15', 'dias': '15',
    'servicio': '43', 'servicios': '43',
    'caja': '12', 'cajas': '12',
    'paquete': '13', 'paquetes': '13',
}


def _uom_to_dgii_code(uom_name: str) -> str:
    """Convierte nombre de UoM de Odoo al código numérico DGII."""
    if not uom_name:
        return '43'
    key = uom_name.strip().lower()
    return _UOM_DGII_MAP.get(key, '43')


# ─────────────────────────────────────────────────────────────────────────────
#  Contactos (res.partner) — Validación RNC para e-CF
# ─────────────────────────────────────────────────────────────────────────────

class ResPartner(models.Model):
    _inherit = 'res.partner'

    ecf_valid_rnc = fields.Boolean(
        string='RNC Válido para e-CF',
        compute='_compute_ecf_valid_rnc',
        help='Indica si el RNC/Cédula tiene el formato correcto (9 u 11 dígitos)',
    )
    ecf_count = fields.Integer(
        string='e-CF Emitidos',
        compute='_compute_ecf_count',
    )

    @api.depends('vat')
    def _compute_ecf_valid_rnc(self):
        for partner in self:
            vat = partner.vat or ''
            # Limpiar guiones y espacios
            clean_vat = ''.join(filter(str.isdigit, vat))
            partner.ecf_valid_rnc = len(clean_vat) in (9, 11)

    def _compute_ecf_count(self):
        for partner in self:
            partner.ecf_count = self.env['ecf.log'].search_count([
                ('move_id.partner_id', '=', partner.id)
            ])

    def action_view_ecf_logs(self):
        self.ensure_one()
        return {
            'name': _('Historial e-CF — %s', self.name),
            'type': 'ir.actions.act_window',
            'res_model': 'ecf.log',
            'view_mode': 'list,form',
            'domain': [('move_id.partner_id', '=', self.id)],
            'context': {'default_partner_id': self.id},
        }


# ─────────────────────────────────────────────────────────────────────────────
#  Configuración ECF por compañía (aislamiento multi-empresa)
# ─────────────────────────────────────────────────────────────────────────────

class ResCompany(models.Model):
    _inherit = 'res.company'

    ecf_saas_url = fields.Char(
        string='URL del Renace e-CF',
        default='https://ecf.renace.tech',
        help='URL base del API Gateway del SaaS de facturación electrónica',
    )
    ecf_api_key = fields.Char(
        string='API Key del Tenant',
        help='API Key asignada a esta empresa en el Renace e-CF (formato: sk_cert_... o sk_prod_...)',
    )
    ecf_webhook_secret = fields.Char(
        string='Webhook Secret',
        help='Secret para verificar los callbacks del Renace e-CF (HMAC-SHA256)',
    )
    ecf_ambiente = fields.Selection(
        selection=[('simulacion', 'Simulación (Mock Local)'), ('certificacion', 'Certificación'), ('produccion', 'Producción')],
        string='Ambiente DGII',
        default='certificacion',
    )
    # CRÍTICO: Por defecto FALSE — ningún e-CF se dispara automáticamente
    ecf_emision_automatica = fields.Boolean(
        string='Emisión automática al confirmar',
        default=False,
        help='DESACTIVADO por defecto. Si se activa, el e-CF se emite al confirmar '
             'facturas que NO estén en modo diferido. Para POS diferido siempre es manual.',
    )

    @api.model
    def pos_check_ecf_health(self):
        """Ping al SaaS ejecutado en el servidor — ecf_api_key nunca llega al navegador."""
        import time
        company = self.env.company
        if not company.ecf_saas_url or not company.ecf_api_key:
            return {'status': 'not_configured'}
        try:
            t0 = time.monotonic()
            resp = requests.get(
                f"{company.ecf_saas_url}/v1/health",
                headers={'X-API-Key': company.ecf_api_key},
                timeout=3,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.ok:
                return {'status': 'online', 'latency_ms': latency_ms}
            return {'status': 'error', 'http_status': resp.status_code}
        except Exception:
            return {'status': 'offline'}

    def get_ecf_saas_connectivity(self):
        """Ping SaaS: /health (liveness) + /v1/health (API Key).

        - online:  /v1/health OK con API Key
        - warning: SaaS responde pero falta/falla la API Key
        - offline: no hay URL o el host no responde
        """
        import time
        self.ensure_one()
        if not self.ecf_saas_url:
            return {'status': 'offline', 'reason': 'URL del SaaS no configurada'}

        base = self.ecf_saas_url.rstrip('/')

        # 1) Liveness pública (no requiere API Key)
        live_ok = False
        live_latency = None
        try:
            t0 = time.monotonic()
            live = requests.get(f"{base}/health", timeout=6)
            live_latency = int((time.monotonic() - t0) * 1000)
            live_ok = live.ok
        except requests.Timeout:
            return {'status': 'offline', 'reason': 'Timeout al contactar el SaaS'}
        except requests.ConnectionError:
            return {'status': 'offline', 'reason': 'No se pudo conectar al SaaS (DNS/red)'}
        except Exception as exc:
            return {'status': 'offline', 'reason': str(exc)}

        if not self.ecf_api_key:
            if live_ok:
                return {
                    'status': 'warning',
                    'reason': 'API Key no configurada',
                    'latency_ms': live_latency,
                }
            return {'status': 'offline', 'reason': 'SaaS no responde y falta API Key'}

        # 2) Health autenticado del tenant
        try:
            t0 = time.monotonic()
            resp = requests.get(
                f"{base}/v1/health",
                headers={'X-API-Key': self.ecf_api_key},
                timeout=8,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.ok:
                data = resp.json() if resp.content else {}
                return {
                    'status': 'online',
                    'latency_ms': latency_ms,
                    'ambiente': data.get('ambiente'),
                    'version': data.get('version'),
                }
            if resp.status_code in (401, 403):
                return {
                    'status': 'warning',
                    'reason': 'API Key inválida o sin permiso',
                    'http_code': resp.status_code,
                    'latency_ms': latency_ms,
                }
            if live_ok:
                return {
                    'status': 'warning',
                    'reason': f'/v1/health HTTP {resp.status_code}',
                    'latency_ms': latency_ms,
                }
            return {
                'status': 'offline',
                'reason': f'HTTP {resp.status_code}',
                'latency_ms': latency_ms,
            }
        except requests.Timeout:
            if live_ok:
                return {'status': 'warning', 'reason': 'Timeout en /v1/health', 'latency_ms': live_latency}
            return {'status': 'offline', 'reason': 'Timeout al conectar con el SaaS'}
        except requests.ConnectionError:
            return {'status': 'offline', 'reason': 'Conexión rechazada al SaaS'}
        except Exception as exc:
            if live_ok:
                return {'status': 'warning', 'reason': str(exc), 'latency_ms': live_latency}
            return {'status': 'offline', 'reason': str(exc)}


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    ecf_saas_url = fields.Char(
        related='company_id.ecf_saas_url',
        readonly=False,
        string='URL del Renace e-CF',
    )
    ecf_api_key = fields.Char(
        related='company_id.ecf_api_key',
        readonly=False,
        string='API Key del Tenant',
    )
    ecf_webhook_secret = fields.Char(
        related='company_id.ecf_webhook_secret',
        readonly=False,
        string='Webhook Secret',
    )
    ecf_ambiente = fields.Selection(
        related='company_id.ecf_ambiente',
        readonly=False,
        string='Ambiente DGII',
    )
    ecf_emision_automatica = fields.Boolean(
        related='company_id.ecf_emision_automatica',
        readonly=False,
        string='Emisión automática al confirmar',
    )
    ecf_rnc_empresa = fields.Char(
        related='company_id.vat',
        readonly=False,
        string='RNC de la empresa',
    )

    def set_values(self):
        super().set_values()
        api_key = self.company_id.ecf_api_key
        if api_key and not _API_KEY_RE.match(api_key):
            raise ValidationError(_(
                'La API Key debe tener formato sk_cert_... o sk_prod_... '
                '(48 caracteres hexadecimales, proporcionada al crear el tenant en el SaaS)'
            ))

    def action_test_conexion_ecf(self):
        """Prueba la conexión al Renace e-CF y muestra latencia + versión."""
        self.ensure_one()
        company = self.company_id
        api_url = company.ecf_saas_url or ''
        api_key = company.ecf_api_key or ''

        if not api_url or not api_key:
            raise UserError(_('Configure la URL y API Key del Renace e-CF primero'))

        import time
        try:
            start = time.monotonic()
            response = requests.get(
                f"{api_url}/v1/health",
                headers={"X-API-Key": api_key},
                timeout=10,
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            response.raise_for_status()
            data = response.json()
            version = data.get('version', 'N/D')
            ambiente = data.get('ambiente', company.ecf_ambiente)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('✅ Conexión exitosa'),
                    'message': _(
                        'Renace e-CF v%s conectado. Ambiente: %s. Latencia: %sms',
                        version, ambiente.upper(), latency_ms
                    ),
                    'type': 'success',
                    'sticky': False,
                },
            }
        except requests.RequestException as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('❌ Error de conexión'),
                    'message': _('No se pudo conectar al Renace e-CF: %s', str(e)),
                    'type': 'danger',
                    'sticky': True,
                },
            }


# ─────────────────────────────────────────────────────────────────────────────
#  Tipos de e-CF
# ─────────────────────────────────────────────────────────────────────────────

class ECFTipo(models.Model):
    _name = 'ecf.tipo'
    _description = 'Tipos de Comprobante Fiscal Electrónico'
    _order = 'codigo'
    _rec_name = 'nombre'
    # Odoo 19 POS: sin este mixin falta _load_pos_data_search_read
    _inherit = ['pos.load.mixin']

    codigo  = fields.Integer(string='Código', required=True)
    nombre  = fields.Char(string='Nombre', required=True)
    prefijo = fields.Char(string='Prefijo', required=True)  # E31, E32...
    activo  = fields.Boolean(default=True)
    # Indica si este tipo aplica para consumidor final (sin RNC requerido)
    consumidor_final = fields.Boolean(
        string='Consumidor Final',
        default=False,
        help='Si es True, no se requiere RNC del comprador (ej: E32)',
    )

    @api.depends('prefijo', 'nombre', 'codigo')
    def _compute_display_name(self):
        for rec in self:
            pref = rec.prefijo or (f'E{rec.codigo}' if rec.codigo else '')
            rec.display_name = f"{pref} — {rec.nombre}" if pref else (rec.nombre or '')

    # ── Loader POS (Odoo 18/19 via pos.load.mixin) ──
    @api.model
    def _load_pos_data_domain(self, data, config):
        return [('activo', '=', True)]

    @api.model
    def _load_pos_data_fields(self, config):
        return ['id', 'nombre', 'codigo', 'prefijo', 'consumidor_final']


# ─────────────────────────────────────────────────────────────────────────────
#  Log de e-CF
# ─────────────────────────────────────────────────────────────────────────────

class ECFLog(models.Model):
    _name = 'ecf.log'
    _description = 'Registro de e-CF emitidos'
    _order = 'create_date desc'
    _rec_name = 'ncf'
    _check_company_auto = True

    _sql_constraints = [
        ('ncf_move_unique', 'UNIQUE(move_id, ncf)',
         'Ya existe un registro con este NCF para esta factura'),
    ]

    move_id      = fields.Many2one(
        'account.move', string='Factura', ondelete='cascade', check_company=True,
    )
    ncf          = fields.Char(string='NCF', index=True)
    ecf_id       = fields.Char(string='ID en SaaS')
    tipo_ecf     = fields.Integer(string='Tipo e-CF')
    estado       = fields.Selection([
        ('pendiente',           'Pendiente'),
        ('enviado',             'Enviado'),
        ('aprobado',            'Aprobado'),
        ('rechazado',           'Rechazado'),
        ('condicionado',        'Condicionado'),
        ('anulacion_pendiente', 'Anulación Pendiente'),
        ('anulado',             'Anulado'),
        ('anulacion_fallida',   'Anulación Fallida'),
    ], string='Estado', default='pendiente', index=True)
    codigo_seguridad = fields.Char(string='Código de Seguridad')
    qr_code      = fields.Text(string='Código QR')
    error_msg    = fields.Text(string='Error')
    raw_response = fields.Text(string='Respuesta raw DGII')
    create_date  = fields.Datetime(string='Fecha emisión', readonly=True)
    approved_at  = fields.Datetime(string='Fecha aprobación DGII')
    ambiente     = fields.Char(string='Ambiente', help='certificacion o produccion')
    company_id   = fields.Many2one('res.company', string='Compañía', related='move_id.company_id', store=True, index=True)


    def action_view_move(self):
        self.ensure_one()
        return {
            'name': _('Factura Relacionada'),
            'view_mode': 'form',
            'res_model': 'account.move',
            'res_id': self.move_id.id,
            'type': 'ir.actions.act_window',
        }

    @api.model
    def _ecf_report_dates(self):
        """Rango de fechas del dashboard (contexto date_from / date_to)."""
        ctx = self.env.context
        today = fields.Date.context_today(self)
        if ctx.get('date_from'):
            date_from = fields.Date.from_string(ctx['date_from'])
        else:
            date_from = today.replace(day=1)
        if ctx.get('date_to'):
            date_to = fields.Date.from_string(ctx['date_to'])
        else:
            date_to = today
        return date_from, date_to

    @api.model
    def get_dashboard_stats(self, domain=None):
        """Estadísticas para el dashboard e-CF — usa read_group (escalable)."""
        domain = list(domain or [])
        date_from, date_to = self._ecf_report_dates()
        domain.append(('create_date', '>=', fields.Datetime.to_datetime(date_from)))
        domain.append(('create_date', '<=', fields.Datetime.to_datetime(date_to) + timedelta(days=1)))
        domain.append(('company_id', '=', self.env.company.id))

        stats_estado = {'aprobado': 0, 'rechazado': 0, 'pendiente': 0, 'condicionado': 0}
        for grp in self.read_group(domain, ['estado'], ['estado']):
            estado = grp['estado']
            if estado in stats_estado:
                stats_estado[estado] = grp['estado_count']

        tipos = self.env['ecf.tipo'].search([])
        codigo_to_prefijo = {t.codigo: t.prefijo for t in tipos}
        stats_tipo = {}
        for grp in self.read_group(domain, ['tipo_ecf'], ['tipo_ecf']):
            codigo = grp['tipo_ecf']
            if codigo in codigo_to_prefijo and grp['tipo_ecf_count']:
                stats_tipo[codigo_to_prefijo[codigo]] = grp['tipo_ecf_count']

        date_limit = fields.Datetime.now() - timedelta(days=30)
        daily_groups = self.read_group(
            domain + [('create_date', '>=', date_limit)],
            ['create_date:day'],
            ['create_date:day'],
            orderby='create_date',
        )
        daily_volume = [{
            'day': str(grp.get('create_date:day') or ''),
            'count': grp.get('create_date_count', 0),
        } for grp in daily_groups]

        moves_domain = [
            ('company_id', '=', self.env.company.id),
            ('ecf_estado', 'in', ('aprobado', 'condicionado', 'enviado', 'pendiente')),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
        ]
        amount_groups = self.env['account.move'].read_group(
            moves_domain, ['amount_total'], [],
        )
        total_amount = (amount_groups[0]['amount_total'] if amount_groups else 0.0) or 0.0
        total_count = self.search_count(domain)

        recent = self.search(domain, limit=5, order='create_date desc')
        recent_logs = [{
            'id': l.id,
            'move_id': l.move_id.id if l.move_id else False,
            'ncf': l.ncf or '---',
            'cliente': (l.move_id.partner_id.name if l.move_id else '') or '---',
            'monto': (l.move_id.amount_total if l.move_id else 0.0) or 0.0,
            'estado': l.estado,
            'fecha': l.create_date.strftime('%Y-%m-%d %H:%M') if l.create_date else '',
        } for l in recent]

        return {
            'stats_estado': stats_estado,
            'stats_tipo': stats_tipo,
            'daily_volume': daily_volume,
            'total_amount': total_amount,
            'total_count': total_count,
            'recent_logs': recent_logs,
        }

    @api.model
    def get_fiscal_summary(self, period='month'):
        """Resumen fiscal 606/607 para el rango del dashboard."""
        company_id = self.env.company[:1].id
        start_date, end_date = self._ecf_report_dates()

        ventas_domain = [
            ('company_id', '=', company_id),
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', start_date),
            ('invoice_date', '<=', end_date),
            ('state', '=', 'posted'),
        ]
        ventas_groups = self.env['account.move'].read_group(
            domain=ventas_domain,
            fields=['amount_untaxed', 'amount_tax', 'amount_total'],
            groupby=[],
        )
        v = ventas_groups[0] if ventas_groups else {}

        compras_domain = [
            ('company_id', '=', company_id),
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '>=', start_date),
            ('invoice_date', '<=', end_date),
            ('state', '=', 'posted'),
        ]
        compras_groups = self.env['account.move'].read_group(
            domain=compras_domain,
            fields=['amount_untaxed', 'amount_tax', 'amount_total'],
            groupby=[],
        )
        c = compras_groups[0] if compras_groups else {}

        return {
            'ventas': {
                'total':  float(v.get('amount_total') or 0.0),
                'base':   float(v.get('amount_untaxed') or 0.0),
                'itbis':  float(v.get('amount_tax') or 0.0),
                'count':  self.env['account.move'].search_count(ventas_domain),
            },
            'compras': {
                'total': float(c.get('amount_total') or 0.0),
                'base':  float(c.get('amount_untaxed') or 0.0),
                'itbis': float(c.get('amount_tax') or 0.0),
                'count': self.env['account.move'].search_count(compras_domain),
            },
            'periodo': f"{start_date.strftime('%d/%m/%Y')} — {end_date.strftime('%d/%m/%Y')}",
        }

    @api.model
    def get_report_rows(self, report_type):
        """Filas 606/607 para el visor y exportaciones (PDF/TXT)."""
        date_from, date_to = self._ecf_report_dates()
        move_type = 'in_invoice' if report_type == '606' else 'out_invoice'
        moves = self.env['account.move'].search([
            ('company_id', '=', self.env.company.id),
            ('move_type', '=', move_type),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
        ], order='invoice_date desc, id desc')

        rows = []
        for move in moves:
            partner_vat = ''.join(c for c in (move.partner_id.vat or '') if c.isdigit())
            rows.append({
                'id': move.id,
                'ncf': move.ecf_ncf or move.l10n_latam_document_number or move.name,
                'date': move.invoice_date,
                'rnc': partner_vat or '---',
                'partner': move.partner_id.name or '---',
                'base': move.amount_untaxed,
                'tax': move.amount_tax,
                'total': move.amount_total,
                'status': 'Pagado' if move.payment_state == 'paid' else 'Pendiente',
            })
        return rows

    @api.model
    def get_saas_connectivity(self):
        """Estado de conexión al SaaS (ping /v1/health)."""
        return self.env.company[:1].get_ecf_saas_connectivity()

    @api.model
    def check_dgii_compliance(self):
        """Verifica el estado de salud del sistema consultando el SaaS Renace e-CF."""
        company = self.env.company[:1]
        issues = []

        if not company.ecf_saas_url:
            issues.append({'type': 'error', 'msg': 'URL de Renace e-CF no configurada'})
        if not company.ecf_api_key:
            issues.append({'type': 'error', 'msg': 'API Key ausente'})
        if not company.ecf_webhook_secret:
            issues.append({'type': 'warning', 'msg': 'Webhook Secret no configurado (callbacks Odoo desactivados)'})

        cert_dias = None
        consumo_pct = None
        if company.ecf_saas_url and company.ecf_api_key:
            try:
                base = company.ecf_saas_url.rstrip('/')
                resp = requests.get(
                    f"{base}/v1/health",
                    headers={"X-API-Key": company.ecf_api_key},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    cert_dias = data.get('cert_dias_restantes')
                    if cert_dias is not None and cert_dias < 30:
                        issues.append({'type': 'warning', 'msg': f'Certificado vence en {cert_dias} días'})
                    else:
                        issues.append({'type': 'info', 'msg': f'Certificado activo ({cert_dias} días)'})
                    max_ecf = data.get('max_ecf_mensual') or 1
                    emitidos = data.get('ecf_emitidos_mes', 0)
                    consumo_pct = round(emitidos / max_ecf * 100, 1)
                    if consumo_pct >= 90:
                        issues.append({'type': 'warning', 'msg': f'Consumo mensual al {consumo_pct}% del límite'})
                else:
                    issues.append({'type': 'error', 'msg': f'SaaS respondió HTTP {resp.status_code}'})
            except Exception as e:
                issues.append({'type': 'error', 'msg': f'No se pudo conectar al SaaS: {e}'})

        failed_logs = self.search_count([('estado', '=', 'rechazado'), ('create_date', '>=', fields.Datetime.now() - timedelta(days=7))])
        if failed_logs > 0:
            issues.append({'type': 'warning', 'msg': f'{failed_logs} rechazos en los últimos 7 días'})

        n_err = sum(1 for i in issues if i['type'] == 'error')
        n_warn = sum(1 for i in issues if i['type'] == 'warning')
        if n_err:
            status = 'critical'
        elif n_warn:
            status = 'warning'
        else:
            status = 'ready'

        return {
            'status': status,
            'issues': issues,
            'cert_dias_restantes': cert_dias,
            'consumo_mensual_pct': consumo_pct,
            'compliance_score': max(0, min(100, 100 - 25 * n_err - 5 * n_warn)),
        }

    @api.model
    def get_saas_status(self):
        """Estado de conexión real vía GET /v1/health (usado por widgets legacy)."""
        result = self.check_dgii_compliance()
        if result['status'] == 'ready':
            return 'online'
        if result['status'] == 'critical':
            return 'offline'
        return 'warning'

    def action_export_excel(self, move_ids):
        """Descarga reporte XLSX de e-CF vía controller /ecf/export/xlsx."""
        return {
            'type': 'ir.actions.act_url',
            'url': f'/ecf/export/xlsx?ids={",".join(str(i) for i in move_ids)}',
            'target': 'new',
        }







# ─────────────────────────────────────────────────────────────────────────────
#  Extensión de account.move (factura)
# ─────────────────────────────────────────────────────────────────────────────

class AccountMove(models.Model):
    _inherit = 'account.move'

    # ── Tipo y modo de emisión ──
    ecf_tipo_id = fields.Many2one(
        'ecf.tipo',
        string='Tipo e-CF',
        ondelete='restrict',
        default=lambda self: self.env['ecf.tipo'].search([('codigo', '=', 32)], limit=1),
        help='Tipo de comprobante fiscal electrónico según norma DGII. Por defecto: Consumidor Final (32)',
    )
    ecf_modo = fields.Selection([
        ('inmediato', 'Inmediato'),
        ('diferido',  'Diferido (POS / Crédito)'),
        ('excento',   'Exento de e-CF'),
    ], string='Modo e-CF', default='inmediato',
        help='Diferido: la factura viene del POS y aún no ha sido pagada completamente. '
             'El e-CF se emitirá manualmente tras conciliar el pago.')

    # ── Datos del e-CF ──
    ecf_ncf    = fields.Char(string='NCF', readonly=True, copy=False,
                              help='Número de Comprobante Fiscal asignado por el SaaS')
    ecf_estado = fields.Selection([
        ('pendiente',           'Pendiente'),
        ('enviado',             'Enviado'),
        ('aprobado',            'Aprobado'),
        ('rechazado',           'Rechazado'),
        ('condicionado',        'Condicionado'),
        ('anulacion_pendiente', 'Anulación Pendiente'),
        ('anulado',             'Anulado'),
        ('anulacion_fallida',   'Anulación Fallida'),
    ], string='Estado e-CF', readonly=True, copy=False, index=True)
    ecf_codigo_seguridad = fields.Char(string='Código de Seguridad', readonly=True, copy=False,
                                        help='Código de Seguridad e-CF (128 chars del SignatureValue, DGII RD)')
    ecf_qr     = fields.Text(string='QR Code', readonly=True, copy=False)
    ecf_track_id = fields.Char(string='Track ID DGII', readonly=True, copy=False,
                                help='Track ID retornado por la DGII para consultas de estado')
    ecf_log_ids = fields.One2many('ecf.log', 'move_id', string='Historial e-CF')

    # ── Flujo POS diferido ──
    ecf_pendiente_conciliacion = fields.Boolean(
        string='Pendiente conciliación e-CF',
        default=False,
        copy=False,
        help='True cuando la factura viene del POS y aún no está pagada completamente. '
             'No se emitirá e-CF hasta que este campo sea False.',
    )
    ecf_listo_para_emitir = fields.Boolean(
        string='Listo para emitir e-CF',
        default=False,
        copy=False,
        help='True cuando el pago fue conciliado y el e-CF puede emitirse manualmente.',
    )

    # ── Datos del cliente ──
    partner_rnc = fields.Char(
        related='partner_id.vat',
        string='RNC/Cédula del cliente',
        readonly=True,
    )

    # ─────────────────────────────────────────────────────────────────────────
    #  Override action_post: NUNCA dispara automáticamente en modo diferido
    # ─────────────────────────────────────────────────────────────────────────

    def action_post(self):
        res = super().action_post()

        for move in self.filtered(
            lambda m: m.move_type in ('out_invoice', 'out_refund') and m.ecf_tipo_id
        ):
            # Si es diferido → marcar pendiente conciliación, NO emitir
            if move.ecf_modo == 'diferido':
                move.sudo().write({'ecf_pendiente_conciliacion': True})
                move.message_post(
                    body=_(
                        'Factura confirmada en modo diferido. '
                        'El e-CF se emitirá manualmente tras conciliar el pago completo.'
                    ),
                    message_type='comment',
                )
                continue

            # Si es exento → no hacer nada
            if move.ecf_modo == 'excento':
                continue

            # Solo emite automáticamente si el toggle está ACTIVO (está en False por defecto)
            emision_auto = move.company_id.ecf_emision_automatica
            if emision_auto:
                try:
                    move._validar_pre_emision()
                    move._emitir_ecf()
                except Exception as e:
                    _logger.exception('Error emitiendo e-CF para %s: %s', move.name, e)
                    move.message_post(
                        body=_('⚠️ Error al emitir e-CF: %s', str(e)),
                        message_type='comment',
                    )

        return res

    # ─────────────────────────────────────────────────────────────────────────
    #  Validación pre-emisión (DGII compliance)
    # ─────────────────────────────────────────────────────────────────────────

    def _validar_pre_emision(self):
        """Valida todos los campos requeridos por DGII antes de enviar al SaaS."""
        self.ensure_one()

        # Bloqueado si la factura está en borrador
        if self.state == 'draft':
            raise UserError(_(
                'No se puede emitir un e-CF para una factura en borrador. '
                'Confirme la factura primero.'
            ))

        # Bloqueado si está diferido y no está listo
        if self.ecf_pendiente_conciliacion and not self.ecf_listo_para_emitir:
            raise UserError(_(
                'Esta factura está en modo diferido y aún no ha sido pagada completamente. '
                'Concilie el pago primero.'
            ))

        # Guard anti re-emisión: si ya tiene NCF y no fue rechazado/anulado,
        # volver a emitir generaría un NCF duplicado ante la DGII.
        if self.ecf_ncf and self.ecf_estado not in ('rechazado', 'anulado', 'error', False):
            raise UserError(_(
                'Esta factura ya tiene el NCF %(ncf)s en estado "%(estado)s". '
                'No se puede volver a emitir. Use "Consultar Estado" o anule el e-CF primero.',
                ncf=self.ecf_ncf, estado=self.ecf_estado,
            ))

        if not self.ecf_tipo_id:
            raise UserError(_('Debe seleccionar un Tipo e-CF antes de emitir'))

        # Tipos distintos de E32 requieren RNC/Cédula del comprador
        if self.ecf_tipo_id.codigo != 32:
            vat = ''.join(filter(str.isdigit, (self.partner_id.vat or '').strip()))
            if len(vat) not in (9, 11):
                raise UserError(_(
                    'El tipo E%(tipo)s requiere el RNC o Cédula del comprador. '
                    'Configure el campo "NIF/RNC" del cliente (9 u 11 dígitos).',
                    tipo=self.ecf_tipo_id.codigo,
                ))
            if self.ecf_tipo_id.codigo == 31 and not _validar_rnc_o_cedula(vat):
                raise UserError(_(
                    'El RNC o Cédula del cliente "%s" no pasa la validación oficial DGII '
                    '(dígito verificador mod-11 incorrecto). Verifique el dato en el partner.',
                    vat,
                ))

        # Fecha emisión no puede ser futura
        if self.invoice_date and self.invoice_date > date.today():
            raise UserError(_('La fecha de emisión no puede ser futura'))

        # Debe tener al menos una línea de producto
        product_lines = self.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        if not product_lines:
            raise UserError(_('La factura debe tener al menos una línea de producto'))

        # Configuración del SaaS
        company = self.company_id
        if not company.ecf_saas_url or not company.ecf_api_key:
            raise UserError(_('Configure la URL y API Key del Renace e-CF en Ajustes → e-CF DGII'))

        # E33/E34 requieren NCF de referencia de la factura original
        if self.ecf_tipo_id.codigo in (33, 34):
            ref = self.reversed_entry_id
            if not ref or not ref.ecf_ncf:
                raise UserError(_(
                    'El tipo E%(tipo)s requiere la factura original con e-CF emitido '
                    '(NCF de referencia). Vincule la factura rectificada.',
                    tipo=self.ecf_tipo_id.codigo,
                ))

    def _dgii_campos_emision(self):
        """Campos normativos DGII para el payload de emisión (Norma 06-2018)."""
        self.ensure_one()
        partner = self.partner_id
        direccion = ', '.join(
            p for p in (
                partner.street,
                partner.street2,
                partner.city,
                partner.state_id.name if partner.state_id else None,
            ) if p
        )[:255] or None

        campos = {
            'tipo_pago': (
                '2' if self.payment_state in ('not_paid', 'partial', 'in_payment') else '1'
            ),
            'tipo_ingresos': '01',
            'indicador_envio_diferido': 0,
        }
        if campos['tipo_pago'] == '2' and self.invoice_date_due:
            campos['fecha_limite_pago'] = self.invoice_date_due.isoformat()
        if direccion:
            campos['direccion_comprador'] = direccion
        if self.ecf_tipo_id.codigo in (33, 34):
            campos['codigo_modificacion'] = '2' if self.move_type == 'out_refund' else '4'
        return campos

    # ─────────────────────────────────────────────────────────────────────────
    #  Emisión del e-CF
    # ─────────────────────────────────────────────────────────────────────────

    def action_emitir_ecf(self):
        """Acción manual: valida y emite el e-CF."""
        for record in self:
            record._validar_pre_emision()
            record._emitir_ecf()

        if len(self) == 1:
            title = _('e-CF Enviado')
            msg = _('El e-CF fue enviado al SaaS. Espere el callback de la DGII.')
        else:
            title = _('Lote e-CF Enviado')
            msg = _('Se enviaron %s e-CFs al SaaS. Espere el callback.', len(self))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': msg,
                'type': 'info',
                'sticky': False,
            },
        }

    def _emitir_ecf(self):
        """Construye el payload DGII-compliant y lo envía al Renace e-CF."""
        self.ensure_one()

        company  = self.company_id
        api_url  = company.ecf_saas_url or ''
        api_key  = company.ecf_api_key or ''

        product_lines = self.invoice_line_ids.filtered(lambda l: l.display_type == 'product')

        # Construir items del payload (líneas negativas = descuento global del Excel DGII)
        items = []
        descuento_global = 0.0
        for line in product_lines:
            if line.price_unit < 0:
                descuento_global += abs(float(line.price_unit) * float(line.quantity or 1.0))
                continue
            if line.price_unit <= 0 or line.quantity <= 0:
                continue

            price_unit = line.price_unit
            discount   = line.discount / 100.0 * price_unit * line.quantity

            # Detectar tasa ITBIS (E43, E45, E46, E47 generalmente no llevan ITBIS)
            itbis_tasa = 0
            tipos_sin_itbis = {43, 45, 46, 47}
            if self.ecf_tipo_id.codigo not in tipos_sin_itbis:
                for tax in line.tax_ids:
                    if 'itbis' in tax.name.lower() or tax.amount in (16, 18):
                        itbis_tasa = tax.amount
                        break

            # Indicador bien/servicio: 1=Bien, 2=Servicio
            indicador = 2
            if line.product_id and line.product_id.type in ('consu', 'product', 'storable'):
                indicador = 1

            items.append({
                'descripcion':             (line.name or '')[:200],
                'cantidad':                str(line.quantity),
                'precio_unitario':         str(price_unit),
                'descuento':               str(discount),
                'itbis_tasa':              str(itbis_tasa),
                'unidad':                  _uom_to_dgii_code(line.product_uom_id.name),
                'indicador_bien_servicio': indicador,
            })

        if not items:
            raise UserError(_('No hay líneas positivas para emitir el e-CF'))
        if descuento_global > 0:
            items[0]['descuento'] = str(float(items[0]['descuento']) + descuento_global)

        # Detectar tipo de identificación del comprador
        partner_vat = ''.join(c for c in (self.partner_id.vat or '') if c.isdigit())
        if len(partner_vat) == 9:
            tipo_rnc = '1'   # RNC
        elif len(partner_vat) == 11:
            tipo_rnc = '2'   # Cédula
        else:
            tipo_rnc = '3'   # Pasaporte / sin documento

        # Tipo de cambio: Odoo rate → DGII espera DOP/1_moneda
        if self.currency_id.name != 'DOP' and self.currency_id.rate:
            tipo_cambio = round(1.0 / self.currency_id.rate, 4)
        else:
            tipo_cambio = 1.0

        payload = {
            'tipo_ecf':           self.ecf_tipo_id.codigo,
            'rnc_comprador':      partner_vat or None,
            'nombre_comprador':   self.partner_id.name,
            'tipo_rnc_comprador': tipo_rnc,
            'fecha_emision':      (
                self.invoice_date.isoformat() if self.invoice_date
                else date.today().isoformat()
            ),
            'items':              items,
            'moneda':             self.currency_id.name,
            'tipo_cambio':        tipo_cambio,
            'odoo_move_id':       str(self.id),
            'odoo_move_name':     self.name,
            'ambiente_emision':   company.ecf_ambiente,
        }
        payload.update(self._dgii_campos_emision())

        # E33/E34: NCF de referencia (no solo out_refund)
        if self.ecf_tipo_id.codigo in (33, 34) and self.reversed_entry_id and self.reversed_entry_id.ecf_ncf:
            payload['ncf_referencia'] = self.reversed_entry_id.ecf_ncf
            payload['fecha_ncf_referencia'] = (
                self.reversed_entry_id.invoice_date.isoformat()
                if self.reversed_entry_id.invoice_date else None
            )

        try:
            # Idempotencia: un doble clic o retry de red no debe asignar 2 NCF
            idem_seq = self.env['ecf.log'].sudo().search_count([('move_id', '=', self.id)])
            response = requests.post(
                f"{api_url}/v1/ecf/emitir",
                json=payload,
                headers={
                    'X-API-Key':    api_key,
                    'Content-Type': 'application/json',
                    'Idempotency-Key': f'odoo-{self.company_id.id}-{self.id}-{idem_seq}',
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            # Persistir NCF y estado inicial
            self.sudo().write({
                'ecf_ncf':    data['ncf'],
                'ecf_estado': 'pendiente',
                # Limpiar banderas de diferido
                'ecf_pendiente_conciliacion': False,
                'ecf_listo_para_emitir':      False,
            })

            # Crear log con ambiente
            self.env['ecf.log'].sudo().create({
                'move_id':  self.id,
                'ncf':      data['ncf'],
                'ecf_id':   data.get('ecf_id'),
                'tipo_ecf': self.ecf_tipo_id.codigo,
                'estado':   'pendiente',
                'ambiente': company.ecf_ambiente or 'certificacion',
            })

            self.message_post(
                body=_('📤 e-CF enviado al SaaS. NCF asignado: <strong>%s</strong>', data['ncf']),
                message_type='comment',
            )
            _logger.info('e-CF emitido para %s. NCF=%s', self.name, data['ncf'])

        except requests.RequestException as e:
            raise UserError(_('Error de conexión con el Renace e-CF: %s', str(e)))

    # ─────────────────────────────────────────────────────────────────────────
    #  Flujo POS diferido: conciliación de pago
    # ─────────────────────────────────────────────────────────────────────────

    def _compute_payment_state(self):
        """Override para detectar pago completo en facturas diferidas."""
        res = super()._compute_payment_state()
        for move in self:
            if (
                move.ecf_pendiente_conciliacion
                and move.payment_state == 'paid'
                and move.ecf_tipo_id
                and move.ecf_modo == 'diferido'
            ):
                move.sudo().write({
                    'ecf_listo_para_emitir':      True,
                    'ecf_pendiente_conciliacion': False,
                })
                move.message_post(
                    body=_(
                        '✅ Pago conciliado. La factura está <strong>lista para emitir e-CF</strong>. '
                        'Use el botón "Emitir e-CF" para generar el comprobante ante la DGII.'
                    ),
                    message_type='comment',
                )
        return res

    # ─────────────────────────────────────────────────────────────────────────
    #  Acciones
    # ─────────────────────────────────────────────────────────────────────────

    def action_anular_ecf(self):
        """Abre el wizard de anulación."""
        return {
            'type':      'ir.actions.act_window',
            'res_model': 'ecf.anular.wizard',
            'view_mode': 'form',
            'target':    'new',
            'context':   {'default_move_id': self.id},
        }

    def action_consultar_estado_ecf(self):
        """Consulta el estado actual del e-CF en el SaaS."""
        self.ensure_one()
        if not self.ecf_ncf:
            raise UserError(_('Esta factura no tiene NCF asignado'))

        company = self.company_id
        api_url = company.ecf_saas_url or ''
        api_key = company.ecf_api_key or ''

        try:
            response = requests.get(
                f"{api_url}/v1/ecf/{self.ecf_ncf}/estado",
                headers={'X-API-Key': api_key},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            raise UserError(_('Error de conexión con el Renace e-CF: %s', str(e)))

        vals = {'ecf_estado': data.get('estado')}
        codigo = data.get('codigo_seguridad') or data.get('security_code') or data.get('cufe')
        if codigo:
            vals['ecf_codigo_seguridad'] = codigo
        if data.get('track_id'):
            vals['ecf_track_id'] = data['track_id']
        qr = data.get('qr_url') or data.get('qr_code')
        if qr:
            vals['ecf_qr'] = qr
        self.sudo().write(vals)

        log = self.env['ecf.log'].sudo().search(
            [('move_id', '=', self.id), ('ncf', '=', self.ecf_ncf)],
            limit=1, order='create_date desc',
        )
        if log:
            log_vals = {'estado': data.get('estado')}
            if codigo:
                log_vals['codigo_seguridad'] = codigo
            if qr:
                log_vals['qr_code'] = qr
            if data.get('estado') == 'aprobado' and not log.approved_at:
                log_vals['approved_at'] = fields.Datetime.now()
            log.write(log_vals)

        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _('Estado e-CF'),
                'message': _('NCF %s: %s%s', self.ecf_ncf, (data.get('estado') or '').upper(),
                             f' — Cód. {codigo}' if codigo else ''),
                'type':    'success' if data.get('estado') == 'aprobado' else 'info',
            },
        }

    # ─────────────────────────────────────────────────────────────────────────
    #  Cron: detecta facturas diferidas listas para emitir
    # ─────────────────────────────────────────────────────────────────────────

    @api.model
    def _cron_detectar_ecf_listos(self):
        """
        Cron que revisa facturas POS diferidas con pago conciliado y las
        marca como listas para emitir. Notifica al manager por chatter.
        Ejecutar cada hora.
        """
        facturas_diferidas = self.search([
            ('ecf_pendiente_conciliacion', '=', True),
            ('ecf_modo', '=', 'diferido'),
            ('ecf_tipo_id', '!=', False),
            ('payment_state', '=', 'paid'),
        ])

        for move in facturas_diferidas:
            move.sudo().write({
                'ecf_listo_para_emitir':      True,
                'ecf_pendiente_conciliacion': False,
            })
            move.message_post(
                body=_(
                    '🤖 [Cron] Pago detectado y conciliado. '
                    'Factura lista para emitir e-CF. '
                    'Vaya a e-CF DGII → Pendientes para gestionar el envío.'
                ),
                message_type='comment',
            )
            _logger.info(
                'Cron ECF: factura %s marcada como lista para emitir', move.name
            )

        if facturas_diferidas:
            _logger.info(
                'Cron ECF: %d facturas diferidas listas para emitir detectadas',
                len(facturas_diferidas)
            )

# ─────────────────────────────────────────────────────────────────────────────
#  Punto de Venta (POS)
# ─────────────────────────────────────────────────────────────────────────────

class PosOrder(models.Model):
    _inherit = 'pos.order'

    ecf_tipo_id = fields.Many2one(
        'ecf.tipo', 
        string='Tipo e-CF',
        help='Tipo de comprobante seleccionado en el POS'
    )
    ecf_ncf = fields.Char(
        string='NCF',
        related='account_move.ecf_ncf',
        store=True,
    )
    ecf_codigo_seguridad = fields.Char(
        string='Código de Seguridad',
        related='account_move.ecf_codigo_seguridad',
        store=True,
    )
    ecf_qr = fields.Text(
        string='QR Code',
        related='account_move.ecf_qr',
        store=True,
    )
    ecf_estado = fields.Selection(
        related='account_move.ecf_estado',
        string='Estado e-CF',
        store=True,
    )

    @api.model
    def _load_pos_data_fields(self, config_id):
        fields = super()._load_pos_data_fields(config_id)
        return fields + [
            'ecf_tipo_id', 'ecf_ncf', 'ecf_codigo_seguridad', 'ecf_qr',
        ]

    def _order_fields(self, ui_order):
        fields = super()._order_fields(ui_order)
        fields['ecf_tipo_id'] = ui_order.get('ecf_tipo_id')
        return fields

    def _prepare_invoice_vals(self):
        vals = super()._prepare_invoice_vals()
        if not self.ecf_tipo_id:
            return vals

        vals['ecf_tipo_id'] = self.ecf_tipo_id.id

        partner_vat = (self.partner_id.vat or '').strip()
        partner_tiene_rnc_valido = bool(partner_vat) and _validar_rnc_o_cedula(partner_vat)

        es_credito = (self.amount_total > self.amount_paid)
        es_e31_sin_rnc = (self.ecf_tipo_id.codigo == 31 and not partner_tiene_rnc_valido)

        vals['ecf_modo'] = 'diferido' if (es_credito or es_e31_sin_rnc) else 'inmediato'
        return vals

    def export_for_ui(self):
        result = super().export_for_ui()
        if self.account_move:
            result['ecf_ncf'] = self.account_move.ecf_ncf
            result['ecf_codigo_seguridad'] = self.account_move.ecf_codigo_seguridad
            result['ecf_qr'] = self.account_move.ecf_qr
            result['ecf_ambiente'] = self.company_id.ecf_ambiente
        return result

    def action_pos_order_invoice(self):
        """Emite e-CF tras facturar en POS, respetando ``ecf_emision_automatica``."""
        res = super().action_pos_order_invoice()
        for order in self:
            move = order.account_move
            if not (move and move.ecf_modo == 'inmediato' and move.ecf_tipo_id):
                continue
            if not move.company_id.ecf_emision_automatica:
                continue
            try:
                move._emitir_ecf()
            except Exception as e:
                _logger.error("Error emitiendo e-CF desde POS: %s", e)
        return res

class PosSession(models.Model):
    _inherit = 'pos.session'

    @api.model
    def _load_pos_data_models(self, config_id):
        data = super()._load_pos_data_models(config_id)
        data += ['ecf.tipo']
        return data


class ResCompanyPos(models.Model):
    _inherit = 'res.company'

    @api.model
    def _load_pos_data_fields(self, config_id):
        params = super()._load_pos_data_fields(config_id)
        # ecf_api_key NO se expone al POS frontend por seguridad.
        return params + ['ecf_saas_url', 'ecf_ambiente']



