/** @odoo-module **/
/* Comentado para estabilidad en Odoo 18 hasta confirmar ruta de Printer
import { Printer } from "@point_of_sale/app/printer/printer";
import { patch } from "@web/core/utils/patch";

patch(Printer.prototype, {
    async printHtml(html) {
        if (!this.container) {
            this.container = document.querySelector(".pos-receipt-container");
            if (!this.container) {
                this.container = document.createElement("div");
                this.container.className = "pos-receipt-container";
                this.container.style.display = "none";
                document.body.appendChild(this.container);
            }
        }
        return super.printHtml(...arguments);
    }
});
*/
