/** @odoo-module **/
import { Component } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { _t } from "@web/core/l10n/translation";

export class EcfTypeButton extends Component {
    static template = "ecf_connector.EcfTypeButton";

    setup() {
        this.pos = usePos();
    }

    get currentType() {
        const order = this.pos.get_order();
        if (!order || !order.ecf_tipo_id) return null;
        return this.pos.models["ecf.tipo"].get(order.ecf_tipo_id);
    }

    async onClick() {
        const order = this.pos.get_order();
        if (!order) return;

        // Chequeo rápido de conexión al SaaS para demostrar "funciones que conecten"
        try {
            const response = await fetch(`${this.pos.company.ecf_saas_url}/v1/health`, {
                headers: { "X-API-Key": this.pos.company.ecf_api_key },
                signal: AbortSignal.timeout(3000)
            });
            if (!response.ok) throw new Error("SaaS Offline");
            console.log("e-CF SaaS Online");
        } catch (err) {
            this.pos.popup.add(SelectionPopup, {
                title: "⚠️ SaaS Desconectado",
                list: [{ id: 1, label: "Verifique conexión con Renace.tech", item: true }],
            });
        }

        const types = this.pos.models["ecf.tipo"].getAll();
        const selectionList = types.map(t => ({
            id: t.id,
            item: t,
            label: `${t.prefijo} - ${t.nombre}`,
            isSelected: order.ecf_tipo_id === t.id,
        }));

        const { confirmed, payload: selectedType } = await this.pos.popup.add(SelectionPopup, {
            title: "Seleccionar Tipo de Comprobante",
            list: selectionList,
        });

        if (confirmed) {
            order.ecf_tipo_id = selectedType.id;
            
            // Si selecciona Crédito Fiscal (31), sugerir elegir cliente
            if (selectedType.codigo === 31 && !order.get_partner()) {
                this.pos.popup.add(SelectionPopup, {
                    title: "Atención: Crédito Fiscal",
                    list: [{ id: 1, label: "Seleccionar Cliente (RNC requerido)", item: true }],
                }).then(({ confirmed }) => {
                    if (confirmed) {
                        this.pos.selectPartner();
                    }
                });
            }
        }
    }
}

// Registrar el componente en la ProductScreen para que pueda ser inyectado vía XML
ProductScreen.components = { ...ProductScreen.components, EcfTypeButton };
