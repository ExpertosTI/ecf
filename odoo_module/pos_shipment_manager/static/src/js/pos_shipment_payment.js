/** @odoo-module **/
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { ShipmentConfigDialog } from "@pos_shipment_manager/js/shipment_config_dialog";

function _isShipmentMethod(pm) {
    if (!pm) return false;
    // Chequeo primario por flag
    if (pm.is_messenger_method === true) return true;
    // Fallback
    const name = (pm.name || "").toLowerCase();
    return name.includes("envío") || name.includes("envio") || name.includes("shipment");
}

function _orderHasShipmentLine(order) {
    try {
        const lines = typeof order.get_paymentlines === "function"
            ? order.get_paymentlines()
            : (Array.isArray(order.paymentlines) ? order.paymentlines : []);
        return lines.some((l) => _isShipmentMethod(l.payment_method || l.payment_method_id));
    } catch (e) {
        return false;
    }
}

patch(PaymentScreen.prototype, {async addNewPaymentLine(paymentMethod) {
        try {
            if (_isShipmentMethod(paymentMethod)) {
                const order = this.currentOrder || (this.pos && this.pos.get_order && this.pos.get_order());
                if (!order || !order.get_partner()) {
                    this.env.services.notification.add(
                        _t("Debe seleccionar un cliente antes de usar el método de pago por Envío."),
                        { type: "warning", title: _t("Cliente requerido"), sticky: false }
                    );
                    return false;
                }
                
                // Forzar configuración del modo de envío
                const orm = this.env.services.orm;
                const dialog = this.env.services.dialog;
                
                // Verificar mensajeros
                this.env.services.notification.add(_t("Cargando mensajeros..."), { type: "info", sticky: false });
                const messengers = await orm.searchRead(
                    "res.partner", 
                    [["is_messenger", "=", true]], 
                    ["id", "name"]
                );

                let initialMessengerId = order.messenger_id;
                if (Array.isArray(initialMessengerId)) {
                    initialMessengerId = initialMessengerId[0];
                } else if (initialMessengerId && typeof initialMessengerId === 'object') {
                    initialMessengerId = initialMessengerId.id;
                }

                const payload = await makeAwaitable(dialog, ShipmentConfigDialog, {
                    messengers: messengers,
                    initialMode: order.shipment_mode !== 'none' ? order.shipment_mode : 'cod',
                    initialMessengerId: initialMessengerId,
                    initialLocationLink: order.manual_location_link,
                    initialPrice: order.shipping_charge || 0,
                    initialDistance: order.distance_km || 0,
                    isAlreadyPaid: order.get_total_paid() >= order.get_total_with_tax(),
                    initialPartner: order.get_partner(),
                });

                if (payload) {
                    order.shipment_mode = payload.mode;
                    order.messenger_id = payload.messengerId;
                    order.manual_location_link = payload.locationLink;
                    order.distance_km = payload.distance;
                    order.shipping_charge = payload.price; 
                    order.shipping_cost = payload.price;   
                    
                    if (payload.selectedPartner) {
                        order.set_partner(payload.selectedPartner);
                    }

                    const m = messengers.find(u => u.id === payload.messengerId);
                    order.messenger_name = m ? m.name : "";
                    
                    const label = payload.mode === 'cod' ? 'Contra Entrega' : 'Pago al Instante';
                    this.env.services.notification.add(_t(`🛵 Envío configurado: ${label}.`), { type: "info" });
                } else {
                    // Si el usuario cancela el modal, evitamos que agregue el pago
                    return false;
                }

                // Forzar factura
                if (order && typeof order.set_to_invoice === 'function') {
                    order.set_to_invoice(true);
                }
            }
        } catch (e) {
            console.warn("[pos_shipment_manager] addNewPaymentLine check:", e);
        }
        return super.addNewPaymentLine(...arguments);
    },

    async validateOrder(isForceValidate) {
        try {
            const order = this.currentOrder || (this.pos && this.pos.get_order && this.pos.get_order());
            if (order && _orderHasShipmentLine(order)) {
                if (!order.get_partner()) {
                    this.env.services.notification.add(
                        _t("Se requiere un cliente para procesar el pago por envío. Seleccione el cliente e intente de nuevo."),
                        { type: "danger", title: _t("Cliente requerido"), sticky: true }
                    );
                    return;
                }
                if (order.shipment_mode === 'none') {
                     this.env.services.notification.add(
                        _t("El método de pago es de envío pero no se ha configurado la modalidad de envío. Elimine la línea de pago y vuelva a intentarlo."),
                        { type: "danger", title: _t("Configuración requerida"), sticky: true }
                    );
                    return;
                }
                if (typeof order.set_to_invoice === 'function') {
                    order.set_to_invoice(true);
                }
            }
        } catch (e) {
            console.warn("[pos_shipment_manager] validateOrder check:", e);
        }
        return super.validateOrder(...arguments);
    },
});
