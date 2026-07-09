/** @odoo-module **/
/*
 * PSM Robustness Patch:
 * Evita el crash "TypeError: product.taxes_id is undefined" cuando se carga
 * una Sale Order que contiene productos no habilitados para el POS.
 */
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { patch } from "@web/core/utils/patch";

patch(PosStore.prototype, {async addLineToCurrentOrder(product, options = {}) {
        // Si el producto no existe en el POS (caché local), evitamos el crash
        const realProduct = product && (product.product_id || product);
        if (!realProduct || typeof realProduct !== 'object') {
            console.warn("[PSM] Producto no encontrado en la caché del POS. Ignorando línea para evitar error visual.");
            // Opcional: Podrías mostrar una notificación aquí
            return; 
        }
        
        // Si llegamos aquí, el producto existe y tiene sus propiedades
        try {
            return await super.addLineToCurrentOrder(...arguments);
        } catch (e) {
            console.error("[PSM] Error al añadir línea al pedido:", e);
        }
    },

    // Odoo 18: _processData ya no existe; los datos auxiliares de envío se
    // cargan vía RPC dedicado tras procesar los datos del servidor.
    async processServerData() {
        await super.processServerData(...arguments);
        this.pos_messengers = [];
        this.pos_shipment_product_id = false;
        try {
            const sessionId = this.session?.id || odoo.pos_session_id;
            if (sessionId) {
                const data = await this.data.call("pos.session", "load_shipment_data", [[sessionId]]);
                if (data) {
                    this.pos_messengers = data.pos_messengers || [];
                    this.pos_shipment_product_id = data.pos_shipment_product_id || false;
                }
            }
        } catch (e) {
            console.warn("[PSM] No se pudieron cargar los datos de envío:", e);
        }
    },

    async settleSO(sale_order) {
        const result = await super.settleSO(...arguments);
        const order = this.get_order();
        if (order && sale_order) {
            order.shipment_mode = sale_order.shipment_mode || 'none';
            
            let rawId = sale_order.messenger_id;
            let mName = "";
            let mId = null;
            if (Array.isArray(rawId)) {
                mId = rawId[0];
                mName = rawId[1];
            } else if (rawId && typeof rawId === 'object') {
                mId = rawId.id;
                mName = rawId.name;
            } else {
                mId = parseInt(rawId, 10) || null;
            }
            order.messenger_id = mId;
            
            if (order.messenger_id && !mName) {
                const posMessengers = this.pos_messengers || [];
                const found = posMessengers.find(m => m.id === order.messenger_id);
                if (found) {
                    mName = found.name;
                }
            }
            order.messenger_name = mName || "";
            
            order.manual_location_link = sale_order.manual_location_link || '';
            order.distance_km = sale_order.distance_km || 0;
            order.shipping_charge = sale_order.shipping_fee || 0;
            order.shipping_cost = sale_order.shipping_fee || 0;
        }
        return result;
    }
});
