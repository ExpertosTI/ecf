import base64
import io
import openpyxl
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class EcfHomologacionWizard(models.TransientModel):
    _name = 'ecf.homologacion.wizard'
    _description = 'Asistente de Homologación DGII'

    xlsx_file = fields.Binary(string='Archivo Excel de Pruebas (.xlsx)', required=True)
    filename = fields.Char(string='Nombre de Archivo')

    def action_importar_set_pruebas(self):
        self.ensure_one()
        if not self.xlsx_file:
            raise UserError(_("Por favor, sube el archivo Excel downloaded desde la DGII."))

        # 1. Load Excel file
        file_data = base64.b64decode(self.xlsx_file)
        try:
            wb = openpyxl.load_workbook(filename=io.BytesIO(file_data), data_only=True)
        except Exception as e:
            raise UserError(_("Error al leer el archivo Excel: %s") % str(e))

        if 'ECF' not in wb.sheetnames:
            raise UserError(_("El archivo subido no contiene la pestaña 'ECF' requerida por la DGII."))

        # 2. Process ECF Sheet
        ws = wb['ECF']
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise UserError(_("La pestaña 'ECF' está vacía."))

        headers = [str(h) for h in rows[0]]
        
        # Get column indices
        tipo_idx = headers.index('TipoeCF') if 'TipoeCF' in headers else -1
        ncf_idx = headers.index('ENCF') if 'ENCF' in headers else -1
        rnc_comp_idx = headers.index('RNCComprador') if 'RNCComprador' in headers else -1
        rs_comp_idx = headers.index('RazonSocialComprador') if 'RazonSocialComprador' in headers else -1
        total_idx = headers.index('MontoTotal') if 'MontoTotal' in headers else -1
        discount_idx = headers.index('MontoDescuentooRecargo[1]') if 'MontoDescuentooRecargo[1]' in headers else -1

        if tipo_idx == -1 or rnc_comp_idx == -1:
            raise UserError(_("No se encontraron las columnas 'TipoeCF' y 'RNCComprador' en el Excel."))

        invoices_created = 0

        # Try to find standard DO country
        country_do = self.env['res.country'].search([('code', '=', 'DO')], limit=1)

        # Process each row
        for r_idx, r in enumerate(rows[1:], start=2):
            if not any(r):
                continue
            
            tipo = r[tipo_idx]
            ncf_esperado = r[ncf_idx] if ncf_idx != -1 else ""
            rnc_comprador = str(r[rnc_comp_idx]).strip() if r[rnc_comp_idx] is not None and r[rnc_comp_idx] != '#e' else False
            rs_comprador = r[rs_comp_idx] if rs_comp_idx != -1 and r[rs_comp_idx] != '#e' else False
            
            if not tipo or tipo == '#e':
                continue

            tipo = int(tipo)

            # 1. Partner (Cliente / Proveedor)
            partner = False
            if rnc_comprador:
                # Normalizar RNC (quitar guiones)
                rnc_clean = rnc_comprador.replace('-', '').replace(' ', '')
                partner = self.env['res.partner'].search([('vat', '=', rnc_clean)], limit=1)
                if not partner:
                    partner = self.env['res.partner'].create({
                        'name': rs_comprador or f'DGII Test Cliente {rnc_clean}',
                        'vat': rnc_clean,
                        'country_id': country_do.id if country_do else False,
                    })

            # 2. Determine move type and journal
            if tipo == 34:
                move_type = 'out_refund'
            elif tipo in (41, 43, 47):
                move_type = 'in_invoice'
            else:
                move_type = 'out_invoice'

            # Find matching ecf.tipo
            ecf_tipo = self.env['ecf.tipo'].search([('codigo', '=', tipo)], limit=1)

            # Build invoice lines
            line_ids = []
            
            # Loop standard item columns (1 to 15)
            for item_num in range(1, 15):
                desc_col = None
                qty_col = None
                price_col = None
                tax_ind_col = None
                
                for idx, h in enumerate(headers):
                    if f'NombreItem[{item_num}]' in h:
                        desc_col = idx
                    elif f'CantidadItem[{item_num}]' in h:
                        qty_col = idx
                    elif f'PrecioUnitarioItem[{item_num}]' in h:
                        price_col = idx
                    elif f'IndicadorFacturacion[{item_num}]' in h:
                        tax_ind_col = idx

                if desc_col is not None and r[desc_col] is not None and r[desc_col] != '#e':
                    item_name = str(r[desc_col])
                    qty = float(r[qty_col]) if qty_col is not None and r[qty_col] is not None and r[qty_col] != '#e' else 1.0
                    price = float(r[price_col]) if price_col is not None and r[price_col] is not None and r[price_col] != '#e' else 0.0
                    tax_ind = int(r[tax_ind_col]) if tax_ind_col is not None and r[tax_ind_col] is not None and r[tax_ind_col] != '#e' else 4

                    # Find Odoo tax
                    # Type: sale for out_invoice/out_refund, purchase for in_invoice
                    tax_type = 'sale' if move_type in ('out_invoice', 'out_refund') else 'purchase'
                    
                    tax = False
                    if tax_ind == 1: # 18%
                        tax = self.env['account.tax'].search([
                            ('amount', '=', 18.0),
                            ('type_tax_use', '=', tax_type),
                            ('company_id', '=', self.env.company.id)
                        ], limit=1)
                    elif tax_ind == 2: # 16%
                        tax = self.env['account.tax'].search([
                            ('amount', '=', 16.0),
                            ('type_tax_use', '=', tax_type),
                            ('company_id', '=', self.env.company.id)
                        ], limit=1)

                    line_ids.append((0, 0, {
                        'name': item_name,
                        'quantity': qty,
                        'price_unit': price,
                        'tax_ids': [(6, 0, [tax.id])] if tax else False,
                    }))

            # Apply general discount if any
            discount_val = float(r[discount_idx]) if discount_idx != -1 and r[discount_idx] is not None and r[discount_idx] != '#e' else 0.0
            if discount_val > 0.0:
                line_ids.append((0, 0, {
                    'name': 'Descuento General de Prueba',
                    'quantity': 1.0,
                    'price_unit': -discount_val,
                    'tax_ids': False,
                }))

            if not line_ids:
                # Add default fallback line if no lines were parsed
                total_val = float(r[total_idx]) if total_idx != -1 and r[total_idx] is not None and r[total_idx] != '#e' else 100.0
                line_ids.append((0, 0, {
                    'name': f'Servicio de Homologación DGII {ncf_esperado}',
                    'quantity': 1.0,
                    'price_unit': total_val,
                    'tax_ids': False,
                }))
            else:
                positive = sum(
                    float(cmd[2].get('quantity') or 0) * float(cmd[2].get('price_unit') or 0)
                    for cmd in line_ids if cmd[0] == 0 and float(cmd[2].get('price_unit') or 0) > 0
                )
                if positive <= 0 and total_idx != -1 and r[total_idx] not in (None, '#e'):
                    total_val = float(r[total_idx])
                    if total_val > 0:
                        line_ids = [(0, 0, {
                            'name': f'Servicio de Homologación DGII {ncf_esperado}',
                            'quantity': 1.0,
                            'price_unit': total_val,
                            'tax_ids': False,
                        })]

            # Create Draft Invoice
            invoice_vals = {
                'move_type': move_type,
                'partner_id': partner.id if partner else self.env.company.partner_id.id,
                'invoice_date': fields.Date.context_today(self),
                'ecf_tipo_id': ecf_tipo.id if ecf_tipo else False,
                'invoice_line_ids': line_ids,
                'ref': f'Caso DGII {ncf_esperado}',
            }
            
            try:
                self.env['account.move'].create(invoice_vals)
                invoices_created += 1
            except Exception as e:
                import logging
                logging.getLogger(__name__).exception(
                    'Homologación DGII: error creando fila %s (NCF %s): %s',
                    r_idx, ncf_esperado, e,
                )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Importación Exitosa'),
                'message': _('Se crearon %s facturas de prueba en estado borrador.') % invoices_created,
                'type': 'success',
                'sticky': False,
            }
        }
