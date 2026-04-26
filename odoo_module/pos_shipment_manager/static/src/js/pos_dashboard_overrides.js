/** @odoo-module **/
import { PosDashboard } from "@dashboard_pos/js/pos_dashboard";
import { patch } from "@web/core/utils/patch";
import { useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

patch(PosDashboard.prototype, {
    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.isShipmentRef = useRef('is_shipment_check');
    },

    async settleShipmentFromPos(sale) {
        if (!sale.order_id) return;

        try {
            const shipments = await this.orm.searchRead('pos.shipment', [['order_id', '=', sale.order_id]], ['id']);
            if (shipments.length > 0) {
                await this.orm.call("pos.shipment", "action_settle_cash", [shipments[0].id]);
                this.notification.add(_t("Dinero recibido correctamente"), { type: "success" });
                await this.applyFilters();
            }
        } catch (error) {
            console.error("Error settling shipment from POS:", error);
        }
    },

    getFilters() {
        const filters = super.getFilters();
        if (this.isShipmentRef.el) {
            filters.is_shipment = this.isShipmentRef.el.checked;
        }
        return filters;
    }
});
