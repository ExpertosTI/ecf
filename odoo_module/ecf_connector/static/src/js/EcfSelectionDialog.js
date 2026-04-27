/** @odoo-module **/
import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";

export class EcfSelectionDialog extends Component {
    static template = "ecf_connector.EcfSelectionDialog";
    static components = { Dialog };
    static props = {
        title: { type: String, optional: true },
        types: Array,
        onSelect: Function,
        close: Function,
    };

    static defaultProps = {
        title: "Seleccionar Tipo de Comprobante",
    };

    setup() {}

    async select(type) {
        await this.props.onSelect(type);
        this.props.close();
    }

    cancel() {
        this.props.close();
    }
}
