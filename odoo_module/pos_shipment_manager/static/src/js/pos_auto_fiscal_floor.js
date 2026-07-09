/** @odoo-module **/
import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

patch(PosOrder.prototype, {
    setup() {
        super.setup(...arguments);
        // Bind an event listener or check periodically?
        // Actually, we can intercept set_table or wait for tableId to be set.
    },
    set_table(table) {
        super.set_table(...arguments);
        this._applyAutoFiscalPosition(table);
    },
    _applyAutoFiscalPosition(table) {
        if (!table || !this.pos) return;
        const floor = this.pos.models['restaurant.floor'].find(f => f.id === table.floor_id[0] || f.id === table.floor_id);
        if (floor && floor.name && floor.name.toUpperCase().includes("ENVIO")) {
            // Find Fiscal Position containing "Delivery" or "Envío"
            const fps = this.pos.models['account.fiscal.position'].getAll();
            const targetFp = fps.find(fp => 
                (fp.name || "").toUpperCase().includes("DELIVERY") || 
                (fp.name || "").toUpperCase().includes("ENVIO") ||
                (fp.name || "").toUpperCase().includes("ENVÍO")
            );
            if (targetFp) {
                this.update({ fiscal_position_id: targetFp });
            }
        } else {
            // Revert to default if needed? Probably better to just let the user change it back if needed,
            // or reset to the POS default fiscal position.
            if (this.pos.config.default_fiscal_position_id) {
                const defaultFp = this.pos.models['account.fiscal.position'].get(this.pos.config.default_fiscal_position_id[0]);
                this.update({ fiscal_position_id: defaultFp });
            } else {
                this.update({ fiscal_position_id: false });
            }
        }
    }
});
