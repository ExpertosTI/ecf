/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { EcfSelectionDialog } from "@ecf_connector_v19/js/EcfSelectionDialog";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class EcfTypeButton extends Component {
    static template = "ecf_connector_v19.EcfTypeButton";

    setup() {
        this.pos = usePos();
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.dialog = useService("dialog");
        this.state = useState({
            types: [],
        });

        onWillStart(async () => {
            try {
                const types = await this.orm.searchRead(
                    "ecf.tipo",
                    [["activo", "=", true]],
                    ["id", "nombre", "codigo", "prefijo"]
                );
                this.state.types = types;
            } catch (err) {
                console.error("Error loading ECF types via RPC", err);
            }
        });
    }

    get currentTypeName() {
        const order = this.pos.get_order();
        if (!order || !order.ecf_tipo_id) return "e-CF: Consumidor";
        const type = this.state.types.find(t => t.id === order.ecf_tipo_id);
        return type ? `e-CF: ${type.prefijo}` : "e-CF: Consumidor";
    }

    async onClick() {
        const order = this.pos.get_order();
        if (!order) return;

        // Health ping ejecutado en el servidor (ecf_api_key nunca sale al browser)
        this.orm.call("res.company", "pos_check_ecf_health", [[]]).catch(() => {});

        if (this.state.types.length === 0) {
            this.state.types = await this.orm.searchRead("ecf.tipo", [["activo", "=", true]], ["id", "nombre", "codigo", "prefijo"]);
        }

        this.dialog.add(EcfSelectionDialog, {
            title: "Seleccionar Tipo de Comprobante",
            types: this.state.types,
            onSelect: async (selectedType) => {
                order.ecf_tipo_id = selectedType.id;

                if (selectedType.codigo === 31 && !order.get_partner()) {
                    this.notification.add("Atención: Crédito Fiscal requiere seleccionar un cliente con RNC.", { type: "warning" });
                    this.pos.selectPartner();
                }
            }
        });
    }
}

ProductScreen.components = { ...ProductScreen.components, EcfTypeButton };
