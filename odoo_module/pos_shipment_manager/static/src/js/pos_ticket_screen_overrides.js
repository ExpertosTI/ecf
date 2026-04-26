/** @odoo-module **/
/**
 * PSM - Parche a TicketScreen y SaleOrderManagementScreen.
 * 1. TicketScreen: sin confirm(), liquidación directa.
 * 2. SaleOrderManagementScreen: auto-click "Cerrar la orden" sin preguntar.
 */
import { TicketScreen } from "@point_of_sale/app/screens/ticket_screen/ticket_screen";
// import { SaleOrderManagementScreen } from "@pos_sale/app/sale_order_management_screen/sale_order_management_screen";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

// ── TicketScreen ──────────────────────────────────────────────
patch(TicketScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
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

/* Comentado para estabilidad en Odoo 18 hasta confirmar ruta de pos_sale
// ── SaleOrderManagementScreen: auto-cerrar el diálogo ────────
patch(SaleOrderManagementScreen.prototype, {
    async onClickSaleOrder(clickedOrder) {
        const currentOrder = this.pos.get_order();
        if (currentOrder && currentOrder.get_orderlines().length === 0) {
            try {
                this.pos.removeOrder(currentOrder, { silent: true });
            } catch (_) {}
        }
        const resultPromise = super.onClickSaleOrder(clickedOrder);
        const tryAutoClick = () => {
            const allBtns = document.querySelectorAll(
                '.o_dialog button, .o_dialog .list-group-item, .modal button, .modal .list-group-item'
            );
            for (const btn of allBtns) {
                if (/cerrar.la.orden/i.test(btn.textContent || btn.innerText || "")) {
                    btn.click();
                    return true;
                }
            }
            return false;
        };
        requestAnimationFrame(() => {
            if (!tryAutoClick()) {
                requestAnimationFrame(tryAutoClick);
            }
        });
        return resultPromise;
    },
});
*/
