/** @odoo-module **/
import { registry } from "@web/core/registry";
import { Component, xml } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const STATE_COLORS = {
    pendiente: "secondary",
    enviado: "info",
    aprobado: "success",
    rechazado: "danger",
    condicionado: "warning",
    anulacion_pendiente: "warning",
    anulado: "dark",
    anulacion_fallida: "danger",
};

const STATE_ICONS = {
    pendiente: "fa-clock-o",
    enviado: "fa-paper-plane",
    aprobado: "fa-check-circle",
    rechazado: "fa-times-circle",
    condicionado: "fa-exclamation-triangle",
    anulacion_pendiente: "fa-hourglass-half",
    anulado: "fa-ban",
    anulacion_fallida: "fa-exclamation-circle",
};

class ECFStatusWidget extends Component {
    static template = xml`
        <span t-if="props.record.data[props.name]" t-att-class="badgeClass">
            <i t-att-class="iconClass"/>
            <t t-esc="displayValue"/>
        </span>
    `;
    static props = { ...standardFieldProps };

    get badgeClass() {
        const estado = this.props.record.data[this.props.name];
        return `badge text-bg-${STATE_COLORS[estado] || "secondary"}`;
    }

    get iconClass() {
        const estado = this.props.record.data[this.props.name];
        return `fa ${STATE_ICONS[estado] || "fa-question-circle"} me-1`;
    }

    get displayValue() {
        const estado = this.props.record.data[this.props.name];
        if (!estado) return "";
        const selection = this.props.record.fields[this.props.name].selection || [];
        const match = selection.find(([val]) => val === estado);
        return match ? match[1] : estado;
    }
}

registry.category("fields").add("ecf_status", {
    component: ECFStatusWidget,
    supportedTypes: ["selection"],
});
