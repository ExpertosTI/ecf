/** @odoo-module **/
/**
 * PSM - Diálogo de selección de envíos para liquidar desde el POS.
 */
import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class ShipmentSettleDialog extends Component {
    static template = "pos_shipment_manager.ShipmentSettleDialog";
    static components = { Dialog };
    static props = {
        shipments: Array,
        onSettle: Function,
        onRefresh: Function,
        onShare: Function,
        close: Function,
    };

    setup() {
        const checked = {};
        for (const s of this.props.shipments) {
            checked[s.id] = true;
        }
        this.state = useState({ 
            checked,
            payMessenger: true // Por defecto sugerir pagar al mensajero
        });
        this.notification = useService("notification");
        // Bind methods to ensure context is never lost
        this.toggle = this.toggle.bind(this);
        this.confirm = this.confirm.bind(this);
        this.cancel = this.cancel.bind(this);
        this.onShare = this.onShare.bind(this);
        this.copyToClipboard = this.copyToClipboard.bind(this);
    }

    async copyToClipboard(url, label = "") {
        if (!url) return;
        try {
            await navigator.clipboard.writeText(url);
            this.notification.add(_t(`📋 Link ${label} copiado`), { type: "success" });
        } catch (err) {
            console.error("Error al copiar:", err);
        }
    }

    toggle(id) {
        this.state.checked[id] = !this.state.checked[id];
    }

    onShare(shipment) {
        console.log("[PSM] Triggering share from settle list for shipment:", shipment.id);
        this.props.onShare(shipment);
    }

    get selectedIds() {
        return Object.entries(this.state.checked)
            .filter(([, v]) => v)
            .map(([k]) => parseInt(k));
    }

    get totalSelected() {
        return this.props.shipments
            .filter((s) => this.state.checked[s.id])
            .reduce((acc, s) => acc + (s.is_cod ? s.total_order : s.charge), 0)
            .toFixed(2);
    }

    async confirm() {
        const ids = this.selectedIds;
        if (!ids.length) return;
        this.props.close();
        await this.props.onSettle(ids, this.state.payMessenger);
    }

    cancel() {
        this.props.close();
    }
}
