/** @odoo-module **/
import { Printer } from "@point_of_sale/app/printer/printer";
import { patch } from "@web/core/utils/patch";

/**
 * PSM - Parche de ultra-resiliencia para el servicio de impresora del POS.
 * Evita el crash 'null is not an object' cuando el contenedor de recibos no existe.
 */
patch(Printer.prototype, {async printHtml(html) {
        if (!this.container) {
            this.container = document.querySelector(".pos-receipt-container");
            if (!this.container) {
                // Crear el contenedor si falta totalmente
                this.container = document.createElement("div");
                this.container.className = "pos-receipt-container";
                this.container.style.display = "none";
                document.body.appendChild(this.container);
            }
        }
        return super.printHtml(...arguments);
    }
});
