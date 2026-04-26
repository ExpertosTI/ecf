/** @odoo-module **/
/*
 * PSM Robustness Patch:
 * Evita el crash "TypeError: product.taxes_id is undefined" cuando se carga
 * una Sale Order que contiene productos no habilitados para el POS.
 */
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { patch } from "@web/core/utils/patch";

patch(PosStore.prototype, {
    async addLineToCurrentOrder(product, options = {}) {
        // Si el producto no existe en el POS (caché local), evitamos el crash
        if (!product || typeof product !== 'object') {
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
    }
});
