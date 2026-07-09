/** @odoo-module **/
// usePos deprecado en Odoo 18
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { ShipmentSettleDialog } from "@pos_shipment_manager/js/shipment_settle_dialog";
import { ShipmentSettleReceipt } from "@pos_shipment_manager/js/shipment_settle_receipt";
import { ShipmentShareDialog } from "@pos_shipment_manager/js/shipment_share_dialog";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { useState } from "@odoo/owl";

patch(ProductScreen.prototype, {setup() {
        super.setup(...arguments);
        // this.pos ya está inyectado nativamente en ProductScreen
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
                partner_phone: s.partner_phone,
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
        let shipmentId = order.shipment_id ? (typeof order.shipment_id === 'object' ? order.shipment_id.id : order.shipment_id) : null;
        
        // Si no hay ID directo, buscar por el nombre de la orden o SO
        const saleOrderId = order.sale_order_id ? (typeof order.sale_order_id === 'object' ? order.sale_order_id.id : order.sale_order_id) : null;
        if (!shipmentId) {
            const domain = saleOrderId 
                ? [['sale_order_id', '=', saleOrderId]] 
                : [['order_id.pos_reference', '=', order.name]];
            
            try {
                const shipmentRecs = await this.orm.searchRead("pos.shipment", domain, ["id"], { limit: 1 });
                if (shipmentRecs.length > 0) {
                    shipmentId = shipmentRecs[0].id;
                }
            } catch (err) {
                console.warn("[PSM] Error searching shipment:", err);
            }
        }

        // Si aún no hay un envío generado, lo creamos al vuelo como cotización
        if (!shipmentId) {
            const partner = order.get_partner();
            if (!partner) {
                this.notification.add(_t("Debe seleccionar un cliente antes de generar el envío."), {
                    type: "danger",
                });
                return;
            }
            if (order.shipment_mode === 'none' || !order.shipment_mode) {
                this.notification.add(_t("Debe configurar el modo de envío primero."), {
                    type: "danger",
                });
                return;
            }

            this.notification.add(_t("Generando cotización y envío..."), { type: "info", sticky: false });

            // Mapear líneas del carrito del POS
            const orderLines = order.get_orderlines().map(line => ({
                product_id: line.get_product().id,
                qty: line.get_quantity(),
                price_unit: line.get_unit_price(),
            }));

            try {
                const result = await this.orm.call(
                    "pos.shipment",
                    "action_create_shipment_from_draft_pos",
                    [{
                        partner_id: partner.id,
                        shipment_mode: order.shipment_mode,
                        messenger_id: order.messenger_id,
                        manual_location_link: order.manual_location_link,
                        distance_km: order.distance_km || 0.0,
                        shipping_charge: order.shipping_charge || 0.0,
                        lines: orderLines,
                    }]
                );

                if (result && result.shipment_id) {
                    shipmentId = result.shipment_id;
                    order.sale_order_id = result.sale_order_id;
                    order.shipment_id = result.shipment_id;
                    
                    // Actualizar contador y lista local de envíos pendientes
                    await this._refreshPending();
                    this.notification.add(_t("Envío generado con éxito."), { type: "success" });
                }
            } catch (error) {
                this.notification.add(_t("Error al generar envío: ") + (error.message ? error.message.message : error.toString()), {
                    type: "danger",
                });
                return;
            }
        }

        if (shipmentId) {
            await this._openShareDialog(shipmentId);
        }
    },

    async _openShareDialog(shipmentId) {
        try {
            const data = await this.orm.call("pos.shipment", "get_share_data", [[shipmentId]]);
            if (data) {
                this.dialog.add(ShipmentShareDialog, {
                    shipment: {
                        id: data.id,
                        name: data.name,
                        total_order: data.total_order,
                        is_cod: data.is_cod,
                        partner_name: data.partner_name
                    },
                    customer_url: data.customer_url,
                    customer_phone: data.customer_phone,
                    messenger_url: data.messenger_url,
                    messenger_phone: data.messenger_phone,
                });
            }
        } catch (error) {
            console.error("[PSM] Error al abrir compartido:", error);
        }
    },
});
