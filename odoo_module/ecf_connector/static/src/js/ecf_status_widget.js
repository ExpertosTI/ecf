/** @odoo-module **/
/**
 * ECF Status Widget — OWL Component para Odoo 18
 * Muestra el estado del e-CF como un badge coloreado con ícono
 * Renace.tech | Facturación Electrónica DGII
 */
import { registry } from "@web/core/registry";
import { Component, xml } from "@odoo/owl";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const STATE_CONFIG = {
    pendiente: {
        color: "secondary",
        icon: "fa-clock-o",
        label: "Pendiente",
    },
    enviado: {
        color: "info",
        icon: "fa-paper-plane",
        label: "Enviado",
    },
    aprobado: {
        color: "success",
        icon: "fa-check-circle",
        label: "Aprobado DGII",
    },
    rechazado: {
        color: "danger",
        icon: "fa-times-circle",
        label: "Rechazado",
    },
    condicionado: {
        color: "warning",
        icon: "fa-exclamation-triangle",
        label: "Condicionado",
    },
    anulacion_pendiente: {
        color: "warning",
        icon: "fa-hourglass-half",
        label: "Anulación Pendiente",
    },
    anulado: {
        color: "dark",
        icon: "fa-ban",
        label: "Anulado",
    },
    anulacion_fallida: {
        color: "danger",
        icon: "fa-exclamation-circle",
        label: "Anulación Fallida",
    },
};

class ECFStatusWidget extends Component {
    static template = xml`
        <span t-if="props.record.data[props.name]" t-att-class="badgeClass">
            <i t-att-class="iconClass"/>
            <t t-esc="displayValue"/>
        </span>
        <span t-else="" class="text-muted small">—</span>
    `;
    static props = { ...standardFieldProps };

    get estado() {
        return this.props.record.data[this.props.name];
    }

    get config() {
        return STATE_CONFIG[this.estado] || { color: "secondary", icon: "fa-question-circle" };
    }

    get badgeClass() {
        return `badge text-bg-${this.config.color} d-inline-flex align-items-center gap-1`;
    }

    get iconClass() {
        return `fa ${this.config.icon}`;
    }

    get displayValue() {
        if (!this.estado) return "";
        // Use selection label if available, else use config label, else raw value
        const selection = this.props.record.fields[this.props.name]?.selection || [];
        const match = selection.find(([val]) => val === this.estado);
        return match ? match[1] : (this.config.label || this.estado);
    }
}

registry.category("fields").add("ecf_status", {
    component: ECFStatusWidget,
    supportedTypes: ["selection"],
});
