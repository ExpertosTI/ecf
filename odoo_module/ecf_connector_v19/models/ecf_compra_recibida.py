# -*- coding: utf-8 -*-
"""
ecf_compra_recibida.py — Modelo para e-CF Recibidas en Odoo 18

Representa las facturas de proveedor recibidas automáticamente desde la DGII
vía el SaaS ECF. Al procesarlas, se crea un account.move tipo in_invoice.
"""
from __future__ import annotations

import logging
from datetime import date

import requests

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ECFCompraRecibida(models.Model):
    _name        = 'ecf.compra.recibida'
    _description = 'e-CF Recibida (Compra desde DGII)'
    _order       = 'fecha_comprobante desc, ncf'
    _rec_name    = 'ncf'

    # ─────────────────────────────────────────────────────────────────────────
    # Campos de identificación
    # ─────────────────────────────────────────────────────────────────────────
    ncf              = fields.Char('NCF', required=True, index=True, size=13)
    cufe             = fields.Char('CUFE', size=128)
    tipo_ecf         = fields.Integer('Tipo e-CF')
    tipo_ecf_nombre  = fields.Char('Tipo', compute='_compute_tipo_nombre', store=False)
    ambiente         = fields.Selection([
        ('produccion',    'Producción'),
        ('certificacion', 'Certificación'),
        ('pruebas',       'Pruebas'),
    ], string='Ambiente', default='produccion')

    # ─────────────────────────────────────────────────────────────────────────
    # Proveedor
    # ─────────────────────────────────────────────────────────────────────────
    rnc_proveedor   = fields.Char('RNC Proveedor', size=11, index=True)
    nombre_proveedor = fields.Char('Proveedor', size=255)
    partner_id       = fields.Many2one(
        'res.partner', string='Partner Odoo',
        compute='_compute_partner', store=True,
        help='Partner de Odoo encontrado por RNC/VAT',
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Montos
    # ─────────────────────────────────────────────────────────────────────────
    fecha_comprobante = fields.Date('Fecha Comprobante', required=True)
    fecha_pago        = fields.Date('Fecha de Pago')
    monto_servicios   = fields.Monetary('Monto Servicios', currency_field='currency_id', default=0)
    monto_bienes      = fields.Monetary('Monto Bienes',    currency_field='currency_id', default=0)
    total_monto       = fields.Monetary('Total',           currency_field='currency_id')
    itbis_facturado   = fields.Monetary('ITBIS',           currency_field='currency_id', default=0)
    itbis_retenido    = fields.Monetary('ITBIS Retenido',  currency_field='currency_id', default=0)
    isr_retencion     = fields.Monetary('ISR Retención',   currency_field='currency_id', default=0)
    currency_id       = fields.Many2one(
        'res.currency', string='Moneda',
        default=lambda self: self.env.company.currency_id,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Estado de procesamiento
    # ─────────────────────────────────────────────────────────────────────────
    estado_odoo = fields.Selection([
        ('nueva',     'Nueva'),
        ('procesando','Procesando'),
        ('procesada', 'Procesada — Factura creada'),
        ('error',     'Error'),
    ], string='Estado', default='nueva', required=True, index=True)

    move_id = fields.Many2one(
        'account.move', string='Factura de Proveedor',
        readonly=True, ondelete='set null',
        help='Factura de proveedor creada automáticamente en Odoo',
    )
    error_mensaje = fields.Text('Error')

    # ─────────────────────────────────────────────────────────────────────────
    # Computed
    # ─────────────────────────────────────────────────────────────────────────

    _TIPOS_ECF = {
        31: 'Crédito Fiscal', 32: 'Consumo', 33: 'Nota de Débito',
        34: 'Nota de Crédito', 41: 'Compras', 43: 'Gastos Menores',
        44: 'Reg. Especiales', 45: 'Gubernamental', 46: 'Exportaciones', 47: 'Pagos Exterior',
    }

    @api.depends('tipo_ecf')
    def _compute_tipo_nombre(self):
        for rec in self:
            rec.tipo_ecf_nombre = self._TIPOS_ECF.get(rec.tipo_ecf, str(rec.tipo_ecf or ''))

    @api.depends('rnc_proveedor')
    def _compute_partner(self):
        """Busca el partner en Odoo por VAT/RNC."""
        for rec in self:
            if rec.rnc_proveedor:
                partner = self.env['res.partner'].search(
                    [('vat', '=', rec.rnc_proveedor), ('is_company', '=', True)],
                    limit=1,
                )
                rec.partner_id = partner or False
            else:
                rec.partner_id = False

    # ─────────────────────────────────────────────────────────────────────────
    # Acciones
    # ─────────────────────────────────────────────────────────────────────────

    def action_crear_factura(self):
        """
        Crea una factura de proveedor (account.move tipo in_invoice)
        a partir de los datos del e-CF recibido.
        """
        self.ensure_one()
        if self.move_id:
            raise UserError(_('Ya existe una factura para este e-CF: %s', self.move_id.name))
        if self.estado_odoo == 'procesada':
            raise UserError(_('Este e-CF ya fue procesado.'))

        # Buscar o crear partner
        partner = self.partner_id
        if not partner:
            # Crear partner con los datos del proveedor
            partner = self.env['res.partner'].create({
                'name':       self.nombre_proveedor or f'Proveedor RNC {self.rnc_proveedor}',
                'vat':        self.rnc_proveedor,
                'is_company': True,
                'country_id': self.env.ref('base.do', raise_if_not_found=False).id if self.env.ref('base.do', raise_if_not_found=False) else False,
                'comment':    _('Creado automáticamente desde e-CF Recibida NCF %s', self.ncf),
            })
            _logger.info('Partner creado automáticamente: %s (RNC: %s)', partner.name, self.rnc_proveedor)

        # Cuenta de gastos por defecto
        expense_account = self.env['account.account'].search([
            ('account_type', 'in', ['expense', 'expense_direct_cost']),
            ('company_id', '=', self.env.company.id),
            ('deprecated', '=', False),
        ], limit=1)

        # Cuenta de ITBIS (IVA soportado)
        tax_account = self.env['account.tax'].search([
            ('type_tax_use', '=', 'purchase'),
            ('amount', 'in', [16, 18]),
            ('company_id', '=', self.env.company.id),
            ('active', '=', True),
        ], limit=1)

        # Monto base (sin ITBIS)
        monto_base = self.total_monto - self.itbis_facturado

        invoice_line_vals = [{
            'name':       _('e-CF Recibido NCF: %s — %s', self.ncf, self.nombre_proveedor or ''),
            'quantity':   1.0,
            'price_unit': float(monto_base),
            'account_id': expense_account.id if expense_account else False,
            'tax_ids':    [(6, 0, [tax_account.id])] if tax_account and self.itbis_facturado else [],
        }]

        move_vals = {
            'move_type':        'in_invoice',
            'partner_id':       partner.id,
            'invoice_date':     self.fecha_comprobante,
            'ref':              self.ncf,
            'narration':        _(
                'e-CF Recibido automáticamente desde DGII.\n'
                'NCF: %s | CUFE: %s | Ambiente: %s',
                self.ncf, self.cufe or 'N/D', self.ambiente,
            ),
            'invoice_line_ids': [(0, 0, line) for line in invoice_line_vals],
        }

        move = self.env['account.move'].create(move_vals)
        self.write({
            'move_id':     move.id,
            'estado_odoo': 'procesada',
            'error_mensaje': False,
        })

        # Notificar al SaaS que fue procesada
        self._notificar_saas_estado('procesada', str(move.id))

        _logger.info('Factura in_invoice creada: %s ← e-CF %s', move.name, self.ncf)

        return {
            'type':      'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id':    move.id,
            'view_mode': 'form',
        }

    def action_ver_factura(self):
        """Abre la factura de proveedor relacionada."""
        self.ensure_one()
        if not self.move_id:
            raise UserError(_('No hay factura asociada a este e-CF.'))
        return {
            'type':      'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id':    self.move_id.id,
            'view_mode': 'form',
        }

    def action_registrar_pago(self):
        """Registra la fecha de pago en el SaaS (para el 606 DGII)."""
        self.ensure_one()
        if not self.fecha_pago:
            raise UserError(_('Debe establecer la fecha de pago primero.'))
        self._notificar_saas_pago()
        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _('Pago Registrado'),
                'message': _('Fecha de pago %s notificada al SaaS ECF.', self.fecha_pago),
                'type':    'success',
                'sticky':  False,
            },
        }

    def action_sincronizar_dgii(self):
        """Dispara sincronización manual con la DGII desde Odoo."""
        company = self.env.company
        if not company.ecf_saas_url or not company.ecf_api_key:
            raise UserError(_('Configure la URL y API Key del SaaS ECF en Ajustes → e-CF DGII'))
        try:
            resp = requests.post(
                f"{company.ecf_saas_url}/v1/compras/sincronizar",
                headers={'X-API-Key': company.ecf_api_key},
                timeout=15,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            raise UserError(_('Error conectando con el SaaS: %s', str(e)))
        return {
            'type': 'ir.actions.client',
            'tag':  'display_notification',
            'params': {
                'title':   _('Sincronización Iniciada'),
                'message': _('La DGII está siendo consultada. Los e-CF recibidos aparecerán en minutos.'),
                'type':    'info',
                'sticky':  False,
            },
        }

    @api.model
    def action_sync_from_saas(self):
        """
        Consulta al SaaS Renace por nuevos e-CF recibidos para el RNC de la compañía.
        """
        company = self.env.company
        if not company.ecf_saas_url or not company.ecf_api_key:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Configuración incompleta'),
                    'message': _('Debe configurar la URL y API Key del SaaS en Ajustes.'),
                    'type': 'warning',
                }
            }

        try:
            # Consulta al SaaS por e-CFs recibidos (Simulación en ambiente de pruebas)
            url = f"{company.ecf_saas_url}/v1/ecf/received?rnc={company.vat}"
            response = requests.get(url, headers={'X-API-Key': company.ecf_api_key}, timeout=15)
            response.raise_for_status()
            data = response.json()

            created_count = 0
            for ecf in data.get('received', []):
                # Evitar duplicados por NCF/CUFE
                if not self.search([('ncf', '=', ecf.get('ncf')), ('company_id', '=', company.id)]):
                    self.create({
                        'ncf': ecf.get('ncf'),
                        'cufe': ecf.get('cufe'),
                        'rnc_proveedor': ecf.get('rnc_proveedor'),
                        'nombre_proveedor': ecf.get('nombre_proveedor'),
                        'fecha_comprobante': ecf.get('fecha_comprobante'),
                        'total_monto': float(ecf.get('total_monto', 0)),
                        'itbis_facturado': float(ecf.get('itbis_facturado', 0)),
                        'ambiente': company.ecf_ambiente or 'certificacion',
                        'estado_odoo': 'nueva',
                        'company_id': company.id,
                    })
                    created_count += 1
            
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Sincronización Exitosa'),
                    'message': _('Se encontraron %s nuevos e-CF recibidos.', created_count),
                    'type': 'success',
                }
            }
        except Exception as e:
            _logger.error("Error sincronizando e-CF recibidos: %s", e)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Error de Sincronización'),
                    'message': _('No se pudo conectar con el SaaS para recibir e-CF.'),
                    'type': 'danger',
                }
            }

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers internos
    # ─────────────────────────────────────────────────────────────────────────

    def _notificar_saas_estado(self, estado_odoo: str, odoo_bill_id: str | None = None):
        """Notifica al SaaS el estado de procesamiento del e-CF."""
        company = self.env.company
        if not company.ecf_saas_url or not company.ecf_api_key:
            return
        try:
            requests.patch(
                f"{company.ecf_saas_url}/v1/compras/{self.ncf}/estado-odoo",
                params={'estado_odoo': estado_odoo, 'odoo_bill_id': odoo_bill_id},
                headers={'X-API-Key': company.ecf_api_key},
                timeout=10,
            )
        except requests.RequestException as e:
            _logger.warning('Error notificando estado al SaaS: %s', e)

    def _notificar_saas_pago(self):
        """Notifica al SaaS la fecha de pago (para 606 DGII)."""
        company = self.env.company
        if not company.ecf_saas_url or not company.ecf_api_key:
            return
        try:
            requests.patch(
                f"{company.ecf_saas_url}/v1/compras/{self.ncf}/pagar",
                params={'fecha_pago': self.fecha_pago.isoformat()},
                headers={'X-API-Key': company.ecf_api_key},
                timeout=10,
            )
        except requests.RequestException as e:
            _logger.warning('Error notificando pago al SaaS: %s', e)

    # ─────────────────────────────────────────────────────────────────────────
    # SQL Constraints
    # ─────────────────────────────────────────────────────────────────────────
    _sql_constraints = [
        ('ncf_unique', 'UNIQUE(ncf, company_id)', 'El NCF ya existe en este sistema.'),
    ]

    company_id = fields.Many2one(
        'res.company', string='Compañía',
        required=True, default=lambda self: self.env.company,
        index=True,
    )
