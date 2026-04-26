import { Component } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { registry } from "@web/core/registry";

export class EcfTypeButton extends Component {
    static template = "ecf_connector.EcfTypeButton";

    setup() {
        this.pos = usePos();
    }

    get currentType() {
        return this.pos.get_order().get_ecf_tipo();
    }

    async onClick() {
        const types = this.pos.models["ecf.tipo"].getAll();
        const selectionList = types.map(t => ({
            id: t.id,
            item: t,
            label: `${t.prefijo} - ${t.nombre}`,
            isSelected: this.currentType && t.id === this.currentType.id,
        }));

        const { confirmed, payload: selectedType } = await this.pos.popup.add(SelectionPopup, {
            title: "Seleccionar Tipo de Comprobante",
            list: selectionList,
        });

        if (confirmed) {
            this.pos.get_order().set_ecf_tipo(selectedType);
            // Si selecciona Crédito Fiscal (31), sugerir elegir cliente
            if (selectedType.codigo === 31 && !this.pos.get_order().get_partner()) {
                this.pos.popup.add(SelectionPopup, {
                    title: "Atención",
                    list: [{ id: 1, label: "Seleccionar Cliente ahora", item: true }],
                }).then(({ confirmed }) => {
                    if (confirmed) {
                        this.pos.selectPartner();
                    }
                });
            }
        }
    }
}

// Registrar el componente en la ProductScreen (área de botones de control)
ProductScreen.addControlButton({
    component: EcfTypeButton,
    condition: function () {
        return true;
    },
});
