/** @odoo-module **/
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { ShipmentSettleDialog } from "@pos_shipment_manager/js/shipment_settle_dialog";
import { ShipmentSettleReceipt } from "@pos_shipment_manager/js/shipment_settle_receipt";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { useState } from "@odoo/owl";

patch(ProductScreen.prototype, {
    setup() {
        super.setup(...arguments);
        this.pos = usePos();
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.notification = useService("notification");
        this.action = useService("action");
        this.bus = useService("bus_service");
        this.shipmentState = useState({ pendingCount: 0, pending: [] });

        // Sincronización en tiempo real
        this.bus.addChannel("pos_shipment_update");
        this.bus.addEventListener("notification", ({ detail: notifications }) => {
            for (const { type } of notifications) {
                if (type === "pos_shipment_update") {
                    this._refreshPending();
                }
            }
        });

        // Carga inicial sin bloquear la UI
        this._refreshPending();
    },

    async _refreshPending(showNotify = false) {
        try {
            // FUENTE ÚNICA DE VERDAD: Consumir directamente del Dashboard (SST)
            const data = await this.orm.call("pos.shipment", "get_dashboard_data", [], { date_filter: 'today' });
            
            // Combinamos 'street' y 'delivered' que son los liquidables
            const active = [...(data.street || []), ...(data.delivered || [])];

            if (showNotify) {
                this.notification.add(_t(`🔄 Sincronizado: ${active.length} envíos activos.`), { type: "info" });
            }

            this.shipmentState.pending = active.map((s) => ({
                id: s.id,
                name: s.name,
                date: s.date_formatted,
                seller: s.seller_name,
                partner_name: s.partner_name,
                messenger_name: s.messenger_name,
                charge: s.charge || 0,
                cost: s.cost || 0,
                total_order: s.total_order || 0,
                is_cod: s.is_cod,
                amount: s.amount || 0,
                state: s.state,
                state_label: s.state_label,
                customer_url: s.customer_portal_url,
                messenger_url: s.messenger_portal_url,
                products: s.products || [], // Soportar Detalle de Productos
            }));
            this.shipmentState.pendingCount = this.shipmentState.pending.length;
        } catch (e) {
            console.warn("[PSM] Error sincronizando con SST:", e);
            this.shipmentState.pendingCount = 0;
            this.shipmentState.pending = [];
        }
    },

    async onClickSettleShipments() {
        await this._refreshPending();
        const pending = this.shipmentState.pending;

        if (!pending.length) {
            this.notification.add(
                _t("✅ Sin envíos pendientes de liquidación"),
                { type: "info" }
            );
            return;
        }

        this.dialog.add(ShipmentSettleDialog, {
            shipments: pending,
            onRefresh: async () => {
                await this._refreshPending(true);
                this.dialog.closeAll();
                this.onClickSettleShipments();
            },
            onShare: async (shipment) => {
                await this._openShareDialog(shipment.id);
            },
            onSettle: async (ids, payMessenger) => {
                try {
                    // 1. Recolectar datos para el ticket ANTES de la llamada al servidor
                    const settledData = this.shipmentState.pending
                        .filter(s => ids.includes(s.id))
                        .map(s => ({
                            ...s,
                            // Aseguramos que amount sea el correcto para el ticket
                            amount: s.is_cod ? s.total_order : s.charge
                        }));
                    
                    const totalAmount = settledData.reduce((acc, s) => acc + s.amount, 0);
                    
                    // 2. Ejecutar liquidación en el servidor
                    const result = await this.orm.call(
                        "pos.shipment",
                        "action_settle_cash_bulk",
                        [ids, payMessenger]
                    );

                    // 3. Imprimir Recibo Térmico OWL
                    try {
                        const date = new Date();
                        const dateStr = date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                        const settleRef = `SET-${date.getTime().toString().slice(-6)}`;

                        await this.pos.printer.print(ShipmentSettleReceipt, {
                            shipments: settledData,
                            total: totalAmount,
                            cashier: this.pos.get_cashier().name,
                            date: dateStr,
                            settleRef: settleRef,
                        });
                    } catch (printError) {
                        console.warn("[PSM] Error al imprimir recibo OWL:", printError);
                        await this.action.doAction({
                            type: "ir.actions.report",
                            report_name: "pos_shipment_manager.report_settlement_receipt",
                            report_type: "qweb-pdf",
                            res_ids: ids,
                        });
                    }

                    await this._refreshPending();
                    const n = (result && result.settled) ? result.settled.length : ids.length;
                    this.notification.add(
                        _t(`✅ ${n} envío(s) liquidados correctamente.`),
                        { type: "success" }
                    );
                } catch (e) {
                    console.error("[PSM] Error liquidando:", e);
                    this.notification.add(
                        _t("Error al liquidar. Verifica el tablero."),
                        { type: "danger" }
                    );
                }
            },
        });
    },
    async onClickShareCurrent() {
        const order = this.pos.get_order();
        if (!order) return;

        // Intentar obtener el ID del envío desde el pedido o la cotización vinculada
        let shipmentId = order.shipment_id ? order.shipment_id.id : null;
        
        // Si no hay ID directo, buscar por el nombre de la orden o SO
        if (!shipmentId) {
            const domain = order.sale_order_id 
                ? [['sale_order_id', '=', order.sale_order_id.id]] 
                : [['order_id.pos_reference', '=', order.name]];
            
            const shipmentRecs = await this.orm.searchRead("pos.shipment", domain, ["id"], { limit: 1 });
            if (shipmentRecs.length > 0) {
                shipmentId = shipmentRecs[0].id;
            }
        }

        if (!shipmentId) {
            this.notification.add(_t("Este pedido no tiene un envío generado todavía. Valida el pago primero."), {
                type: "warning",
            });
            return;
        }

        await this._openShareDialog(shipmentId);
    },

    async _openShareDialog(shipmentId) {
        try {
            const shipmentData = await this.orm.read("pos.shipment", [shipmentId], [
                "id", "name", "customer_portal_url", "messenger_portal_url", "total_order", "is_cod", "partner_id", "messenger_id"
            ]);
            
            if (shipmentData && shipmentData.length > 0) {
                const s = shipmentData[0];
                
                // Obtener teléfonos
                const partner = await this.orm.read("res.partner", [s.partner_id[0]], ["phone", "mobile", "name"]);
                const messenger = s.messenger_id ? await this.orm.read("res.users", [s.messenger_id[0]], ["messenger_whatsapp", "name"]) : null;

                const { ShipmentShareDialog } = await import("@pos_shipment_manager/js/shipment_share_dialog");
                
                this.dialog.add(ShipmentShareDialog, {
                    shipment: {
                        id: s.id,
                        name: s.name,
                        total_order: s.total_order,
                        is_cod: s.is_cod,
                        partner_name: partner[0].name
                    },
                    customer_url: s.customer_portal_url,
                    customer_phone: partner[0].phone || partner[0].mobile,
                    messenger_url: s.messenger_portal_url,
                    messenger_phone: messenger ? messenger[0].messenger_whatsapp : null,
                });
            }
        } catch (error) {
            console.error("[PSM] Error al abrir compartido:", error);
        }
    },
});

