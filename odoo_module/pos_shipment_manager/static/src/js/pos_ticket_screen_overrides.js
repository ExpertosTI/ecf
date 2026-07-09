/** @odoo-module **/
/**
 * PSM - Parche a TicketScreen.
 * 1. TicketScreen: sin confirm(), liquidación directa.
 */
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

// ── TicketScreen ──────────────────────────────────────────────
patch(TicketScreen.prototype, {setup() {
        super.setup(...arguments);
        this.orm          = useService("orm");
        this.notification = useService("notification");
    },

    async settleCashFromTicket(order) {
        if (!order || !order.id) return;
        try {
            const shipments = await this.orm.searchRead(
                "pos.shipment",
                [["order_id", "=", order.id]],
                ["id"]
            );
            if (shipments.length > 0) {
                await this.orm.call("pos.shipment", "action_settle_cash", [shipments[0].id]);
                this.notification.add(_t("✓ Envío liquidado"), { type: "success" });
            }
        } catch (e) {
            console.error("[PSM] settleCashFromTicket:", e);
        }
    },
});
