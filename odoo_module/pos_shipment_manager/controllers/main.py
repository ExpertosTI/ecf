# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request


class ShipmentController(http.Controller):

    def _get_shipment_by_token(self, token, token_types=None):
        """Helper to find shipment by any valid token."""
        # Sin token no se busca: un domain con token vacío/None haría match
        # con registros cuyo campo token esté en NULL (acceso sin credencial).
        if not token or not str(token).strip():
            return request.env['pos.shipment'].sudo().browse()
        if not token_types:
            token_types = ['customer_token', 'access_token', 'secure_token']
        
        domain = ['|' for _ in range(len(token_types) - 1)]
        for t_type in token_types:
            domain.append((t_type, '=', token))
            
        return request.env['pos.shipment'].sudo().search(domain, limit=1)

    # --- RUTA PARA MENSAJEROS ---
    @http.route(['/reportar-entrega/<string:token>',
                  '/reportar-entrega/<string:token>/<string:slug>',
                  '/pos/shipment/confirm/<string:token>'], 
                  type='http', auth="public", website=True)
    def shipment_confirm_portal(self, token, slug=None, **kwargs):
        shipment = self._get_shipment_by_token(token, ['secure_token', 'access_token'])
        if not shipment:
            return request.render('pos_shipment_manager.shipment_not_found')
        
        # 1. Si ya se entregó, el mensajero ve su pantalla de ÉXITO (no el mapa del cliente)
        if shipment.state in ('delivered', 'settled'):
            return request.render('pos_shipment_manager.shipment_messenger_success', {
                'shipment': shipment,
                'already_confirmed': True
            })

        # Pre-calcular el monto que debe cobrar el mensajero
        sale = shipment.sale_order_id
        pos_order = shipment.order_id
        mode = shipment.shipment_mode or (sale.shipment_mode if sale else 'cod')
        is_cod = mode == 'cod'
        
        if is_cod:
            order_total = (pos_order.amount_total if pos_order else (sale.amount_total if sale else 0.0))
            amount_to_collect = order_total + (shipment.shipping_charge or 0.0)
        else:
            amount_to_collect = 0.0 if mode == 'paid' else (shipment.shipping_charge or 0.0)

        return request.render('pos_shipment_manager.shipment_confirmation_portal', {
            'shipment': shipment,
            'partner': shipment.partner_id,
            'order': pos_order,
            'sale': sale,
            'is_cod': is_cod,
            'amount_to_collect': amount_to_collect,
            'today': fields.Date.today(),
        })

    # --- RUTA PARA CLIENTES (Flujo: Confirmación -> Seguimiento -> Calificación) ---
    @http.route(['/confirmar-pedido/<string:token>',
                  '/confirmar-pedido/<string:token>/<string:slug>',
                  '/pos/shipment/customer/<string:token>',
                  '/shipment/customer/<string:token>'],
                 type='http', auth="public", website=True)
    def shipment_customer_portal(self, token, slug=None, **kwargs):
        shipment = self._get_shipment_by_token(token, ['customer_token', 'access_token'])
        if not shipment:
            return request.render('pos_shipment_manager.shipment_not_found')

        # ETAPA 3: CALIFICACIÓN (Si ya se entregó)
        if shipment.state in ('delivered', 'settled'):
            if shipment.customer_rating > 0:
                # Ya calificó -> Pantalla de Gracias
                return request.render('pos_shipment_manager.shipment_customer_success', {
                    'shipment': shipment,
                    'rating_success': True,
                    'is_delivered': True
                })
            # No ha calificado -> Mostrar Estrellas
            return request.render('pos_shipment_manager.shipment_customer_rating_template', {
                'shipment': shipment,
                'token': token
            })

        # ETAPA 2: SEGUIMIENTO (Si ya confirmó pero no se ha entregado)
        if shipment.customer_confirmed:
            return request.render('pos_shipment_manager.shipment_customer_success', {
                'shipment': shipment,
                'order': shipment.order_id,
                'partner': shipment.partner_id,
            })

        # ETAPA 1: CONFIRMACIÓN (Si no ha confirmado)
        return request.render('pos_shipment_manager.shipment_customer_portal_template', {
            'shipment': shipment,
            'order': shipment.order_id,
            'partner': shipment.partner_id,
        })

    @http.route('/pos/shipment/customer/<string:token>/confirm', type='http', auth="public", website=True, csrf=True)
    def shipment_customer_confirm(self, token, **post):
        shipment = self._get_shipment_by_token(token, ['customer_token'])
        if not shipment:
            return request.not_found()

        signature_name = post.get('signature_name') or shipment.partner_id.name
        shipment.write({
            'customer_confirmed': True,
            'customer_note': post.get('notes'),
            'customer_signature_name': signature_name,
        })

        # Auto-confirmar cotización si aplica
        sale = shipment.sale_order_id
        if sale and sale.state in ('draft', 'sent'):
            try:
                sale.sudo().action_confirm()
                sale.sudo().message_post(body=f"✅ {signature_name} confirmó la orden desde el portal.")
            except Exception:
                pass

        # Tras confirmar, enviamos al Mapa de Seguimiento
        return request.render('pos_shipment_manager.shipment_customer_success', {
            'shipment': shipment,
            'order': shipment.order_id,
            'partner': shipment.partner_id,
        })

    @http.route('/pos/shipment/customer/<string:token>/rate', type='http', auth="public", methods=['POST'], website=True, csrf=True)
    def shipment_customer_rate(self, token, **post):
        shipment = self._get_shipment_by_token(token, ['customer_token', 'access_token'])
        if not shipment:
            return request.not_found()

        shipment.action_submit_rating(
            customer_rating=post.get('rating', 0),
            customer_rating_note=post.get('comment'),
            vendor_rating=post.get('vendor_rating', 0),
            vendor_rating_note=post.get('vendor_comment')
        )
        return request.render('pos_shipment_manager.shipment_customer_success', {
            'shipment': shipment,
            'rating_success': True
        })
    @http.route('/pos/shipment/status_json/<string:token>', type='json', auth="public")
    def shipment_status_json(self, token, **kwargs):
        """Endpoint para polling en tiempo real del portal del cliente."""
        shipment = self._get_shipment_by_token(token, ['customer_token', 'access_token'])
        if not shipment:
            return {'error': 'not_found'}
        return {
            'state': shipment.state,
            'is_delivered': shipment.state in ('delivered', 'settled'),
            'eta': shipment.dynamic_eta,
            'status_title': shipment.dynamic_status_title,
            'status_message': shipment.dynamic_status_message,
        }

    # --- ACCIONES COMPARTIDAS ---
    @http.route('/pos/shipment/submit_confirmation', type='http', auth="public", methods=['POST'], website=True, csrf=False)
    def shipment_submit_confirmation(self, **post):
        token = post.get('token')
        shipment = self._get_shipment_by_token(token, ['secure_token', 'access_token'])
        if not shipment:
            return "Shipment not found"

        action = post.get('action')
        note = post.get('messenger_note') or post.get('note')
        cancel_reason = post.get('cancel_reason')
        payment_method = post.get('payment_method', 'cash')

        if action == 'confirm':
            try:
                shipment.action_confirm_delivery(payment_method, note)
                # El mensajero ve su éxito, el cliente (al recargar) verá las estrellas
                return request.render('pos_shipment_manager.shipment_messenger_success', {'shipment': shipment})
            except Exception as e:
                return request.render('pos_shipment_manager.shipment_confirmation_portal', {
                    'shipment': shipment,
                    'error_message': str(e),
                    'partner': shipment.partner_id,
                    'order': shipment.order_id,
                    'sale': shipment.sale_order_id,
                    'today': fields.Date.today(),
                })
        elif action == 'cancel':
            full_note = f"[{cancel_reason}] {note}" if cancel_reason else note
            shipment.action_cancel_delivery(full_note)
            return request.render('pos_shipment_manager.shipment_cancelled_success', {'shipment': shipment})
        

    @http.route('/thermal_print/pos.shipment/<int:record_id>', type='http', auth='user')
    def thermal_print_shipment(self, record_id):
        shipment = request.env['pos.shipment'].browse(record_id)
        if not shipment.exists():
            return request.not_found()
        # Verificar que el usuario tiene acceso real al registro antes de
        # renderizar con sudo (evita fuga entre compañías / sin permisos).
        try:
            shipment.check_access('read')
        except Exception:
            return request.not_found()

        report = request.env.ref('pos_shipment_manager.action_report_settlement_receipt')
        pdf_content, _ = report.sudo()._render_qweb_pdf([record_id])
        
        filename = f"Liquidacion-{shipment.name}.pdf"
        return request.make_response(pdf_content, headers=[
            ('Content-Type', 'application/pdf'),
            ('Content-Disposition', f'inline; filename="{filename}"'),
        ])
