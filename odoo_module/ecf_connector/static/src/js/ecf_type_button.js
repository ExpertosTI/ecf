/** @odoo-module **/
import { Component, useState, onWillStart } from "@odoo/owl";
import { usePos } from "@point_of_sale/app/store/pos_hook";
import { SelectionPopup } from "@point_of_sale/app/utils/input_popups/selection_popup";
import { ProductScreen } from "@point_of_sale/app/screens/product_screen/product_screen";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class EcfTypeButton extends Component {
    static template = "ecf_connector.EcfTypeButton";

    setup() {
        this.pos = usePos();
        this.orm = useService("orm");
        this.state = useState({
            types: [],
        });

        onWillStart(async () => {
            // Carga quirúrgica vía RPC (Evita fallos si el modelo no carga en el POS boot)
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
        if (!order || !order.ecf_tipo_id) return "Consumidor Final";
        const type = this.state.types.find(t => t.id === order.ecf_tipo_id);
        return type ? type.prefijo : "Consumidor Final";
    }

    async onClick() {
        const order = this.pos.get_order();
        if (!order) return;

        // Health Check con el SaaS Renace
        try {
            const response = await fetch(`${this.pos.company.ecf_saas_url}/v1/health`, {
                headers: { "X-API-Key": this.pos.company.ecf_api_key },
                signal: AbortSignal.timeout(3000)
            });
            if (!response.ok) throw new Error("SaaS Offline");
        } catch (err) {
            this.pos.popup.add(SelectionPopup, {
                title: "⚠️ SaaS Desconectado",
                list: [{ id: 1, label: "Verifique conexión con Renace.tech", item: true }],
            });
        }

        if (this.state.types.length === 0) {
            // Reintentar carga si falló al inicio
            this.state.types = await this.orm.searchRead("ecf.tipo", [["activo", "=", true]], ["id", "nombre", "codigo", "prefijo"]);
        }

        const selectionList = this.state.types.map(t => ({
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

ProductScreen.components = { ...ProductScreen.components, EcfTypeButton };
