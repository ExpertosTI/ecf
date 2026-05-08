# -*- coding: utf-8 -*-
"""
Controller para exportar e-CFs a XLSX.
Ruta: GET /ecf/export/xlsx?ids=1,2,3
"""
import io
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class ECFXlsxController(http.Controller):

    @http.route('/ecf/export/xlsx', type='http', auth='user', methods=['GET'])
    def export_xlsx(self, ids='', **kwargs):
        """Exporta los account.move indicados en formato XLSX."""
        try:
            move_ids = [int(i) for i in ids.split(',') if i.strip().isdigit()]
        except (ValueError, AttributeError):
            return request.make_response('IDs inválidos', status=400)

        if not move_ids:
            return request.make_response('No se indicaron IDs', status=400)

        moves = request.env['account.move'].browse(move_ids)
        if not moves.exists():
            return request.make_response('Registros no encontrados', status=404)

        try:
            import xlsxwriter  # noqa: PLC0415
        except ImportError:
            return request.make_response(
                'xlsxwriter no instalado en este servidor', status=501
            )

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        ws = workbook.add_worksheet('e-CF')

        bold = workbook.add_format({'bold': True})
        date_fmt = workbook.add_format({'num_format': 'yyyy-mm-dd'})

        headers = [
            'NCF', 'Tipo e-CF', 'Estado', 'Fecha Emisión',
            'RNC/Cédula Comprador', 'Nombre Comprador',
            'Subtotal', 'ITBIS', 'Total', 'Moneda',
            'Cód. Seguridad', 'TrackId DGII',
        ]
        for col, h in enumerate(headers):
            ws.write(0, col, h, bold)

        for row, move in enumerate(moves, start=1):
            ws.write(row, 0, move.ecf_ncf or '')
            ws.write(row, 1, str(move.ecf_tipo_id.name) if move.ecf_tipo_id else '')
            ws.write(row, 2, move.ecf_estado or '')
            if move.invoice_date:
                ws.write_datetime(row, 3, move.invoice_date, date_fmt)
            else:
                ws.write(row, 3, '')
            ws.write(row, 4, move.partner_id.vat or '')
            ws.write(row, 5, move.partner_id.name or '')
            ws.write(row, 6, float(move.amount_untaxed))
            ws.write(row, 7, float(move.amount_tax))
            ws.write(row, 8, float(move.amount_total))
            ws.write(row, 9, move.currency_id.name or 'DOP')
            ws.write(row, 10, move.ecf_codigo_seguridad or '')
            ws.write(row, 11, move.ecf_track_id or '')

        workbook.close()
        xlsx_data = output.getvalue()

        headers_resp = [
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', 'attachment; filename="ecf_export.xlsx"'),
            ('Content-Length', str(len(xlsx_data))),
        ]
        return request.make_response(xlsx_data, headers=headers_resp)
