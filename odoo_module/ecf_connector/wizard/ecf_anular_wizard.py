# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError

import logging
import requests

_logger = logging.getLogger(__name__)


class ECFAnularWizard(models.TransientModel):
    _name = 'ecf.anular.wizard'
    _description = 'Asistente de anulación de e-CF'

    move_id = fields.Many2one('account.move', string='Factura', required=True)
    ncf = fields.Char(related='move_id.ecf_ncf', string='NCF', readonly=True)
    motivo = fields.Selection([
        ('01', 'Deterioro de factura pre-impresa'),
        ('02', 'Errores de impresión (factura pre-impresa)'),
        ('03', 'Impresión defectuosa'),
        ('04', 'Duplicidad de factura'),
        ('05', 'Corrección de la información'),
        ('06', 'Cambio de productos'),
        ('07', 'Devolución de productos'),
        ('08', 'Omisión de productos'),
        ('09', 'Errores en secuencia de NCF'),
    ], string='Motivo de anulación', required=True, default='05')
    nota = fields.Text(string='Observaciones')

    def action_anular(self):
        self.ensure_one()
        move = self.move_id

        if not move.ecf_ncf:
            raise UserError(_('Esta factura no tiene NCF asignado'))

        if move.ecf_estado not in ('aprobado', 'condicionado'):
            raise UserError(_('Solo se pueden anular e-CF en estado Aprobado o Condicionado'))

        params = move.company_id
        api_url = params.ecf_saas_url or ''
        api_key = params.ecf_api_key or ''

        if not api_url or not api_key:
            raise UserError(_('Configure la URL y API Key del SaaS ECF en Ajustes'))

        payload = {
            'ncf': move.ecf_ncf,
            'motivo': self.motivo,
            'nota': self.nota or '',
        }

        try:
            response = requests.post(
                f"{api_url}/v1/ecf/anular",
                json=payload,
                headers={
                    'X-API-Key': api_key,
                    'Content-Type': 'application/json',
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()

            move.sudo().write({'ecf_estado': 'anulacion_pendiente'})

            # Actualizar log
            log = self.env['ecf.log'].sudo().search(
                [('move_id', '=', move.id), ('ncf', '=', move.ecf_ncf)],
                limit=1, order='create_date desc',
            )
            if log:
                log.write({'estado': 'anulacion_pendiente'})

            move.message_post(
                body=_(
                    "Anulación e-CF solicitada. NCF: %s. Motivo: %s. Esperando confirmación DGII.",
                    move.ecf_ncf,
                    dict(self._fields['motivo'].selection).get(self.motivo, self.motivo),
                ),
                message_type='comment',
            )

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Anulación Solicitada'),
                    'message': _('La anulación del e-CF %s fue enviada a la DGII. Recibirás confirmación por webhook.', move.ecf_ncf),
                    'type': 'info',
                },
            }

        except requests.RequestException as e:
            raise UserError(_('Error al anular e-CF: %s', str(e)))
