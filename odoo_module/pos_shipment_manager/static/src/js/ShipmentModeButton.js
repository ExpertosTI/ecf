/** @odoo-module **/

import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useService } from "@web/core/utils/hooks";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { ShipmentConfigDialog } from "@pos_shipment_manager/js/shipment_config_dialog";

// 1. Patch PosOrder to support shipment_mode
patch(PosOrder.prototype, {
    setup() {
        super.setup(...arguments);
        this.shipment_mode = this.shipment_mode || 'none';
        this.messenger_id = this.messenger_id || false;
        this.messenger_name = this.messenger_name || "";
        this.manual_location_link = this.manual_location_link || "";
    },
    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        json.shipment_mode = this.shipment_mode;
        json.messenger_id = this.messenger_id;
        json.manual_location_link = this.manual_location_link;
        return json;
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.shipment_mode = json.shipment_mode || 'none';
        this.messenger_id = json.messenger_id || false;
        this.manual_location_link = json.manual_location_link || "";
    },
    get_shipment_mode() {
        return this.shipment_mode;
    },
    export_for_printing() {
        const result = super.export_for_printing(...arguments);
        result.shipment_mode = this.shipment_mode;
        result.messenger_name = this.messenger_name;
        result.manual_location_link = this.manual_location_link;
        return result;
    }
});

// 2. Create the Button Component
export class ShipmentModeButton extends Component {
    static template = "pos_shipment_manager.ShipmentModeButton";

    setup() {
        this.pos = usePos();
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
    }

    get currentMode() {
        const order = this.pos.get_order();
        return order ? order.shipment_mode : 'none';
    }

    get buttonLabel() {
        const mode = this.currentMode;
        const order = this.pos.get_order();
        let label = _t('Sin Envío');
        if (mode === 'paid') label = _t('Pago al Instante');
        if (mode === 'cod') label = _t('Contra Entrega');
        
        if (order && order.messenger_name && mode !== 'none') {
            label += ` | 🛵 ${order.messenger_name}`;
        }
        return label;
    }

    get buttonColor() {
        return this.currentMode === 'none' ? 'text-muted' : 'text-warning';
    }

    async onClick() {
        const order = this.pos.get_order();
        if (!order) return;

        // Recuperación Quirúrgica vía RPC (Evita fallos de carga inicial)
        this.notification.add(_t("Cargando mensajeros..."), { type: "info", sticky: false });
        const messengers = await this.orm.searchRead(
            "res.users", 
            [["is_messenger", "=", true]], 
            ["id", "name"]
        );

        if (messengers.length === 0) {
            this.notification.add(_t("⚠️ No hay mensajeros cargados. Revisa el backend."), { type: "warning" });
        }

        try {
            const payload = await makeAwaitable(this.dialog, ShipmentConfigDialog, {
                messengers: messengers,
                initialMode: order.shipment_mode !== 'none' ? order.shipment_mode : (order.get_total_paid() >= order.get_total_with_tax() ? 'paid' : 'cod'),
                initialMessengerId: order.messenger_id,
                initialLocationLink: order.manual_location_link,
                isAlreadyPaid: order.get_total_paid() >= order.get_total_with_tax(),
            });

            if (payload) {
                order.shipment_mode = payload.mode;
                order.messenger_id = payload.messengerId;
                order.manual_location_link = payload.locationLink;
                
                const m = messengers.find(u => u.id === payload.messengerId);
                order.messenger_name = m ? m.name : "";
            }
        } catch (error) {
            // Cancelado
        }
    }
}

// 3. Register the component so it can be used in ProductScreen's template
ProductScreen.components = { ...ProductScreen.components, ShipmentModeButton };
