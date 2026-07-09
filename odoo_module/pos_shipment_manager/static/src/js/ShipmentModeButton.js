/** @odoo-module **/

import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useService } from "@web/core/utils/hooks";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { makeAwaitable } from "@point_of_sale/app/store/make_awaitable_dialog";
import { Component } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { ShipmentConfigDialog } from "@pos_shipment_manager/js/shipment_config_dialog";

patch(PosOrder.prototype, {setup() {
        super.setup(...arguments);
        // Odoo 18: No forzar inicialización de propiedades en setup() 
        // para evitar que el Proxy aborte la cadena de relaciones nativas.
    },
    init_from_JSON(json) {
        if (super.init_from_JSON) {
            super.init_from_JSON(...arguments);
        }
        this.shipment_mode = json.shipment_mode || 'none';
        this.manual_location_link = json.manual_location_link || '';
        this.distance_km = json.distance_km || 0;
        this.shipping_charge = json.shipping_charge || 0;
        this.shipping_cost = json.shipping_cost || 0;

        let rawId = json.messenger_id;
        if (Array.isArray(rawId)) {
            rawId = rawId[0];
        } else if (rawId && typeof rawId === 'object') {
            rawId = rawId.id;
        }
        this.messenger_id = (rawId && typeof rawId === 'number' && rawId > 0) ? rawId : parseInt(rawId, 10) || null;

        const posStore = this.pos || this.models?.pos || this.env?.services?.pos;
        if (this.messenger_id && posStore) {
            const messengers = posStore.pos_messengers || [];
            const found = messengers.find(m => m.id === this.messenger_id);
            if (found) {
                this.messenger_name = found.name;
            }
        }
    },
    serialize(options) {
        const data = super.serialize ? super.serialize(options) : {};
        data.shipment_mode = this.shipment_mode || 'none';
        
        let rawId = this.messenger_id;
        if (Array.isArray(rawId)) {
            rawId = rawId[0];
        } else if (rawId && typeof rawId === 'object') {
            rawId = rawId.id;
        }
        data.messenger_id = (rawId && typeof rawId === 'number' && rawId > 0) ? rawId : parseInt(rawId, 10) || false;

        data.manual_location_link = this.manual_location_link || '';
        data.distance_km = this.distance_km || 0;
        data.shipping_charge = this.shipping_charge || 0;
        data.shipping_cost = this.shipping_cost || 0;
        return data;
    },
    get_shipment_mode() {
        return this.shipment_mode;
    },
    export_for_printing() {
        const result = super.export_for_printing ? super.export_for_printing(...arguments) : {};
        
        let mName = this.messenger_name;
        if (!mName && this.messenger_id) {
            const posStore = this.pos || this.models?.pos || this.env?.services?.pos;
            if (posStore) {
                const messengers = posStore.pos_messengers || [];
                const found = messengers.find(m => m.id === this.messenger_id);
                if (found) {
                    mName = found.name;
                    this.messenger_name = found.name;
                }
            }
        }

        result.shipment_mode = this.shipment_mode;
        result.messenger_name = mName;
        result.manual_location_link = this.manual_location_link;
        result.shipping_charge = this.shipping_charge;
        
        result.headerData = result.headerData || {};
        result.headerData.shipment_mode = this.shipment_mode;
        result.headerData.messenger_name = mName;
        result.headerData.manual_location_link = this.manual_location_link;
        result.headerData.shipping_charge = this.shipping_charge;
        
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
        
        if (order && mode !== 'none') {
            let mName = order.messenger_name;
            if (!mName && order.messenger_id && this.pos) {
                const messengers = this.pos.pos_messengers || [];
                const found = messengers.find(m => m.id === order.messenger_id);
                if (found) {
                    mName = found.name;
                    order.messenger_name = found.name;
                }
            }
            if (mName) {
                label += ` | 🛵 ${mName}`;
            }
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
            "res.partner", 
            [["is_messenger", "=", true]], 
            ["id", "name"]
        );

        if (messengers.length === 0) {
            this.notification.add(_t("⚠️ No hay mensajeros cargados. Revisa el backend."), { type: "warning" });
        }

        try {
            let initialMessengerId = order.messenger_id;
            if (Array.isArray(initialMessengerId)) {
                initialMessengerId = initialMessengerId[0];
            } else if (initialMessengerId && typeof initialMessengerId === 'object') {
                initialMessengerId = initialMessengerId.id;
            }

            const payload = await makeAwaitable(this.dialog, ShipmentConfigDialog, {
                messengers: messengers,
                initialMode: order.shipment_mode !== 'none' ? order.shipment_mode : (order.get_total_paid() >= order.get_total_with_tax() ? 'paid' : 'cod'),
                initialMessengerId: initialMessengerId,
                initialLocationLink: order.manual_location_link,
                initialPrice: order.shipping_charge || 0,
                initialDistance: order.distance_km || 0,
                isAlreadyPaid: order.get_total_paid() >= order.get_total_with_tax(),
                initialPartner: order.get_partner(),
                onCustomerCreated: (customerData) => {
                    // Deprecated: El partner se asigna directamente al retornar el payload
                }
            });

            if (payload) {
                order.shipment_mode = payload.mode;
                order.messenger_id = payload.messengerId;
                order.manual_location_link = payload.locationLink;
                order.distance_km = payload.distance;
                order.shipping_charge = payload.price; // Guardar cargo cliente
                order.shipping_cost = payload.price;   // Guardar costo (igual al cargo por defecto)
                
                // Actualizar el cliente nativo de la orden de forma limpia
                if (payload.selectedPartner && payload.selectedPartner.id) {
                    try {
                        const partner_arr = await this.pos.data.read(
                            "res.partner", 
                            [payload.selectedPartner.id]
                        );
                        if (partner_arr && partner_arr.length > 0) {
                            order.set_partner(partner_arr[0]);
                        } else {
                            const localPartner = this.pos.models["res.partner"].get(payload.selectedPartner.id);
                            order.set_partner(localPartner || payload.selectedPartner);
                        }
                    } catch (e) {
                        console.error("[PSM] Error setting partner:", e);
                        const localPartner = this.pos.models["res.partner"].get(payload.selectedPartner.id);
                        order.set_partner(localPartner || payload.selectedPartner);
                    }
                } else {
                    order.set_partner(null);
                }

                const m = messengers.find(u => u.id === payload.messengerId);
                order.messenger_name = m ? m.name : "";

                // ── Update shipping line on the order ────────────────
                const lines = order.get_orderlines();
                let shippingLine = lines.find(l => {
                    const p = l.get_product();
                    return p && (p.id === this.pos.pos_shipment_product_id || p.display_name.toLowerCase().includes("envío") || p.display_name.toLowerCase().includes("envio") || p.display_name.toLowerCase().includes("delivery"));
                });

                if (payload.mode !== 'none' && payload.price > 0) {
                    if (shippingLine) {
                        shippingLine.set_unit_price(payload.price);
                    } else {
                        // Find the shipping product in local cache (Odoo 18)
                        let shippingProduct = null;
                        if (this.pos.pos_shipment_product_id) {
                            if (this.pos.models && this.pos.models["product.product"]) {
                                shippingProduct = this.pos.models["product.product"].get(this.pos.pos_shipment_product_id);
                            }
                            if (!shippingProduct && this.pos.db) {
                                shippingProduct = this.pos.db.get_product_by_id(this.pos.pos_shipment_product_id);
                            }
                        }
                        if (!shippingProduct) {
                            if (this.pos.models && this.pos.models["product.product"]) {
                                const products = this.pos.models["product.product"].getAll ? this.pos.models["product.product"].getAll() : (this.pos.models["product.product"].records || []);
                                shippingProduct = products.find(p => p.display_name && (p.display_name.toLowerCase().includes("envío") || p.display_name.toLowerCase().includes("envio") || p.display_name.toLowerCase().includes("delivery")));
                            }
                            if (!shippingProduct && this.pos.db && this.pos.db.product_by_id) {
                                const products = Object.values(this.pos.db.product_by_id || {});
                                shippingProduct = products.find(p => p.display_name && (p.display_name.toLowerCase().includes("envío") || p.display_name.toLowerCase().includes("envio") || p.display_name.toLowerCase().includes("delivery")));
                            }
                        }
                        if (shippingProduct) {
                            const newLine = await this.pos.addLineToCurrentOrder({ product_id: shippingProduct });
                            if (newLine) {
                                newLine.set_unit_price(payload.price);
                            }
                        } else {
                            console.warn("[PSM] No shipping product found in POS to add to the order lines.");
                        }
                    }
                    this.notification.add(_t(`🛵 Envío configurado: RD$ ${payload.price}. (Se agregó al total del pedido)`), { type: "info" });
                } else {
                    if (shippingLine) {
                        order.removeOrderline(shippingLine);
                    }
                }
                // Force reactivity/IndexedDB save trigger in Odoo 18
                if (this.pos.models && this.pos.models["pos.order"]) {
                    this.pos.models["pos.order"].trigger("change", order);
                }
                if (order.lines) {
                    order.lines.trigger("change");
                }

                // Sincronización Directa de Emergencia al Servidor para evitar pérdida de datos en F5
                if (order.id && typeof order.id === 'number') {
                    this.orm.write("pos.order", [order.id], {
                        shipment_mode: payload.mode,
                        messenger_id: payload.messengerId,
                        manual_location_link: payload.locationLink,
                        distance_km: payload.distance,
                        shipping_charge: payload.price,
                        shipping_cost: payload.price,
                    }).catch(err => console.error("[PSM] Error escribiendo datos de envío en base de datos:", err));
                }
            }
        } catch (error) {
            // Cancelado
        }
    }
}

// 3. Register the component so it can be used in ProductScreen's template
ProductScreen.components = { ...ProductScreen.components, ShipmentModeButton };
