/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class EcfTypeButton extends Component {
    static template = "ecf_connector_v19.EcfTypeButton";

    setup() {
        this.pos = usePos();
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.popup = useService("popup");
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

        // Health Check simplificado para máxima velocidad
        try {
            const configs = await this.orm.searchRead("res.company", [["id", "=", this.pos.company.id]], ["ecf_saas_url", "ecf_api_key"]);
            if (configs.length && configs[0].ecf_saas_url) {
                fetch(`${configs[0].ecf_saas_url}/v1/health`, {
                    headers: { "X-API-Key": configs[0].ecf_api_key },
                    signal: AbortSignal.timeout(1500)
                }).catch(() => console.warn("SaaS Offline check ignored"));
            }
        } catch (e) {}

        if (this.state.types.length === 0) {
            this.state.types = await this.orm.searchRead("ecf.tipo", [["activo", "=", true]], ["id", "nombre", "codigo", "prefijo"]);
        }

        const selectionList = this.state.types.map(t => ({
            id: t.id,
            item: t,
            label: `${t.prefijo} - ${t.nombre}`,
            isSelected: order.ecf_tipo_id === t.id,
        }));

        const { confirmed, payload: selectedType } = await this.popup.add(SelectionPopup, {
            title: "Seleccionar Tipo de Comprobante",
            list: selectionList,
        });

        if (confirmed) {
            order.ecf_tipo_id = selectedType.id;
            
            if (selectedType.codigo === 31 && !order.get_partner()) {
                this.popup.add(SelectionPopup, {
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

ProductScreen.components = { ...ProductScreen.components, EcfTypeButton };
