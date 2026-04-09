# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

import hashlib
import hmac
import json
import logging
import requests
from datetime import date

_logger = logging.getLogger(__name__)


# Configuración ECF por compañía (aislamiento multi-empresa)

class ResCompany(models.Model):
    _inherit = 'res.company'

    ecf_saas_url = fields.Char(
        string='URL del SaaS ECF',
        default='https://api.tu-saas-ecf.do',
        help='URL base del API Gateway del SaaS de facturación electrónica',
    )
    ecf_api_key = fields.Char(
        string='API Key del Tenant',
        help='API Key asignada a esta empresa en el SaaS ECF',
    )
    ecf_webhook_secret = fields.Char(
        string='Webhook Secret',
        help='Secret para verificar los callbacks del SaaS ECF (HMAC-SHA256)',
    )
    ecf_ambiente = fields.Selection(
        selection=[('certificacion', 'Certificación'), ('produccion', 'Producción')],
        string='Ambiente DGII',
        default='certificacion',
    )
    ecf_emision_automatica = fields.Boolean(
        string='Emisión automática al confirmar',
        default=True,
        help='Si está activo, el e-CF se emite automáticamente al confirmar la factura',
    )


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Campos related a res.company (aislados por empresa)
    ecf_saas_url = fields.Char(
        related='company_id.ecf_saas_url',
        readonly=False,
        string='URL del SaaS ECF',
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
    # RNC de la empresa (para validaciones)
    ecf_rnc_empresa = fields.Char(
        related='company_id.vat',
        readonly=False,
        string='RNC de la empresa',
    )

    def set_values(self):
        super().set_values()
        # Validar que la API key tenga formato esperado (sk_xxxx_...)
        api_key = self.company_id.ecf_api_key
        if api_key and not api_key.startswith('sk_'):
            raise ValidationError(_('La API Key debe tener formato sk_xxxx_... (proporcionada al crear el tenant)'))


# Tipos de e-CF

class ECFTipo(models.Model):
    _name = 'ecf.tipo'
    _description = 'Tipos de Comprobante Fiscal Electrónico'
    _order = 'codigo'

    codigo      = fields.Integer(string='Código', required=True)
    nombre      = fields.Char(string='Nombre', required=True)
    prefijo     = fields.Char(string='Prefijo', required=True)  # E31, E32...
    activo      = fields.Boolean(default=True)


# Log de e-CF

class ECFLog(models.Model):
    _name = 'ecf.log'
    _description = 'Registro de e-CF emitidos'
    _order = 'create_date desc'
    _rec_name = 'ncf'

    move_id     = fields.Many2one('account.move', string='Factura', ondelete='cascade')
    ncf         = fields.Char(string='NCF', index=True)
    ecf_id      = fields.Char(string='ID en SaaS')
    tipo_ecf    = fields.Integer(string='Tipo e-CF')
    estado      = fields.Selection([
        ('pendiente',   'Pendiente'),
        ('enviado',     'Enviado'),
        ('aprobado',    'Aprobado'),
        ('rechazado',   'Rechazado'),
        ('condicionado','Condicionado'),
        ('anulacion_pendiente', 'Anulación Pendiente'),
        ('anulado',     'Anulado'),
        ('anulacion_fallida', 'Anulación Fallida'),
    ], string='Estado', default='pendiente', index=True)
    cufe        = fields.Char(string='CUFE')
    qr_code     = fields.Text(string='Código QR')
    error_msg   = fields.Text(string='Error')
    raw_response= fields.Text(string='Respuesta raw DGII')
    create_date = fields.Datetime(string='Fecha emisión', readonly=True)
    approved_at = fields.Datetime(string='Fecha aprobación')


# Extensión de account.move (factura)

class AccountMove(models.Model):
    _inherit = 'account.move'

    # Campos ECF visibles en la factura
    ecf_tipo_id = fields.Many2one(
        'ecf.tipo',
        string='Tipo e-CF',
        ondelete='restrict',
        help='Tipo de comprobante fiscal electrónico',
    )
    ecf_ncf = fields.Char(
        string='NCF',
        readonly=True,
        copy=False,
        help='Número de Comprobante Fiscal asignado por el SaaS',
    )
    ecf_estado = fields.Selection([
        ('pendiente',   'Pendiente'),
        ('enviado',     'Enviado'),
        ('aprobado',    'Aprobado'),
        ('rechazado',   'Rechazado'),
        ('condicionado','Condicionado'),
        ('anulacion_pendiente', 'Anulación Pendiente'),
        ('anulado',     'Anulado'),
        ('anulacion_fallida', 'Anulación Fallida'),
    ], string='Estado e-CF', readonly=True, copy=False)
    ecf_cufe = fields.Char(
        string='CUFE',
        readonly=True,
        copy=False,
        help='Código Único de Factura Electrónica (hash SHA-384)',
    )
    ecf_qr   = fields.Text(string='QR Code', readonly=True, copy=False)
    ecf_log_ids = fields.One2many('ecf.log', 'move_id', string='Historial e-CF')

    # RNC del cliente para e-CF tipo 31 (Crédito Fiscal)
    partner_rnc = fields.Char(
        related='partner_id.vat',
        string='RNC/Cédula del cliente',
        readonly=True,
    )

    # --------------------------------------------------------
    # Override de action_post: emitir e-CF al confirmar
    # --------------------------------------------------------

    def action_post(self):
        res = super().action_post()

        # Solo facturas de cliente en empresas con ECF configurado
        for move in self.filtered(
            lambda m: m.move_type in ('out_invoice', 'out_refund')
            and m.ecf_tipo_id
        ):
            emision_auto = move.company_id.ecf_emision_automatica
            if emision_auto:
                try:
                    move._emitir_ecf()
                except Exception as e:
                    _logger.exception("Error emitiendo e-CF para %s: %s", move.name, e)
                    move.message_post(
                        body=_(f"⚠️ Error al emitir e-CF: {e}"),
                        message_type='comment',
                    )

        return res

    def _emitir_ecf(self):
        """Construye el payload y lo envía al SaaS ECF."""
        self.ensure_one()

        company = self.company_id
        api_url = company.ecf_saas_url or ''
        api_key = company.ecf_api_key or ''

        if not api_url or not api_key:
            raise UserError(_('Configure la URL y API Key del SaaS ECF en Ajustes'))

        product_lines = self.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
        if not product_lines:
            raise UserError(_('La factura no tiene líneas de producto para emitir e-CF'))

        # Construir items del payload
        items = []
        for idx, line in enumerate(product_lines, 1):
            price_unit = line.price_unit
            discount   = line.discount / 100 * price_unit * line.quantity

            # Determinar tasa ITBIS
            itbis_tasa = 0
            for tax in line.tax_ids:
                if 'itbis' in tax.name.lower() or tax.amount in (16, 18):
                    itbis_tasa = tax.amount
                    break

            # Indicador: 1=Bien, 2=Servicio (Odoo 18 uses 'consu' and 'product' for goods)
            indicador = 2  # default Servicio
            if line.product_id and line.product_id.type in ('consu', 'product', 'storable'):
                indicador = 1

            items.append({
                "descripcion":             line.name[:200],
                "cantidad":                str(line.quantity),
                "precio_unitario":         str(price_unit),
                "descuento":               str(discount),
                "itbis_tasa":              str(itbis_tasa),
                "unidad":                  line.product_uom_id.name or "Unidad",
                "indicador_bien_servicio": indicador,
            })

        # Detectar tipo de identificación del comprador
        partner_vat = self.partner_id.vat or ''
        if len(partner_vat) == 9:
            tipo_rnc = "1"   # RNC
        elif len(partner_vat) == 11:
            tipo_rnc = "2"   # Cédula
        else:
            tipo_rnc = "3"   # Pasaporte u otro

        # Tipo de cambio: Odoo rate es unidades_moneda / 1_DOP → DGII espera DOP / 1_moneda
        if self.currency_id.name != 'DOP' and self.currency_id.rate:
            tipo_cambio = round(1.0 / self.currency_id.rate, 4)
        else:
            tipo_cambio = 1.0

        payload = {
            "tipo_ecf":          self.ecf_tipo_id.codigo,
            "rnc_comprador":     self.partner_id.vat or None,
            "nombre_comprador":  self.partner_id.name,
            "tipo_rnc_comprador": tipo_rnc,
            "fecha_emision":     self.invoice_date.isoformat() if self.invoice_date else date.today().isoformat(),
            "items":             items,
            "moneda":            self.currency_id.name,
            "tipo_cambio":       tipo_cambio,
            "odoo_move_id":      str(self.id),
            "odoo_move_name":    self.name,
        }

        # Nota de crédito: incluir NCF de referencia
        if self.move_type == 'out_refund' and self.reversed_entry_id:
            payload["ncf_referencia"] = self.reversed_entry_id.ecf_ncf
            payload["fecha_ncf_referencia"] = (
                self.reversed_entry_id.invoice_date.isoformat()
                if self.reversed_entry_id.invoice_date else None
            )

        try:
            response = requests.post(
                f"{api_url}/v1/ecf/emitir",
                json=payload,
                headers={
                    "X-API-Key":    api_key,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            # Persistir NCF y estado inicial
            self.sudo().write({
                'ecf_ncf':    data['ncf'],
                'ecf_estado': 'pendiente',
            })

            # Crear log
            self.env['ecf.log'].sudo().create({
                'move_id':  self.id,
                'ncf':      data['ncf'],
                'ecf_id':   data.get('ecf_id'),
                'tipo_ecf': self.ecf_tipo_id.codigo,
                'estado':   'pendiente',
            })

            self.message_post(
                body=_(f"e-CF enviado al SaaS. NCF asignado: <strong>{data['ncf']}</strong>"),
                message_type='comment',
            )
            _logger.info("e-CF emitido para %s. NCF=%s", self.name, data['ncf'])

        except requests.RequestException as e:
            raise UserError(_(f"Error de conexión con el SaaS ECF: {e}"))

    def action_anular_ecf(self):
        """Abre el wizard de anulación."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'ecf.anular.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_move_id': self.id},
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
                headers={"X-API-Key": api_key},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            raise UserError(_(f"Error de conexión con el SaaS ECF: {e}"))

        self.sudo().write({'ecf_estado': data['estado']})

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Estado e-CF'),
                'message': _(f"NCF {self.ecf_ncf}: {data['estado'].upper()}"),
                'type': 'info',
            }
        }
