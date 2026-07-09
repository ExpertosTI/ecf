/** @odoo-module **/
/**
 * PSM - Diálogo de selección de envíos para liquidar desde el POS.
 */
import { Component, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

export class ShipmentSettleDialog extends Component {
    static template = "pos_shipment_manager.ShipmentSettleDialog";
    static components = { Dialog };
    static props = {
        shipments: Array,
        onSettle: Function,
        onRefresh: Function,
        close: Function,
    };

    setup() {
        this.notification = useService("notification");
        this.orm = useService("orm");
        
        // Inicializar estado con los envíos recibidos
        this.state = useState({ 
            shipments: this.props.shipments,
            checked: {},
            payMessenger: true,
            messengerFilter: "",
            messengers: [],
            loading: false
        });

        this._updateInitialData(this.props.shipments);
        
        this.toggle = this.toggle.bind(this);
        this.confirm = this.confirm.bind(this);
        this.cancel = this.cancel.bind(this);
        this.refresh = this.refresh.bind(this);
    }

    _updateInitialData(shipments) {
        const checked = {};
        for (const s of shipments) {
            checked[s.id] = (s.state === 'delivered');
        }
        this.state.checked = checked;
        this.state.messengers = [...new Set(shipments.map(s => s.messenger_name))].sort();
    }

    async refresh() {
        this.state.loading = true;
        try {
            const data = await this.orm.call("pos.shipment", "get_dashboard_data", [], { date_filter: 'today' });
            const active = [...(data.street || []), ...(data.delivered || [])];
            
            const mappedActive = active.map((s) => ({
                id: s.id,
                name: s.name,
                date: s.date_formatted,
                seller: s.seller_name,
                partner_name: s.partner_name,
                partner_phone: s.partner_phone,
                messenger_name: s.messenger_name,
                charge: s.charge || 0,
                cost: s.cost || 0,
                total_order: s.total_order || 0,
                is_cod: s.is_cod,
                amount: s.amount || 0,
                state: s.state,
                state_label: s.state_label,
                customer_url: s.customer_portal_url,
                messenger_url: s.messenger_portal_url,
                products: s.products || [],
            }));
            
            this.state.shipments = mappedActive;
            this._updateInitialData(mappedActive);
            
            this.notification.add(_t("🔄 Sincronizado correctamente"), { type: "info" });
        } catch (e) {
            this.notification.add(_t("Error al sincronizar"), { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    get filteredShipments() {
        if (!this.state.messengerFilter) return this.state.shipments;
        return this.state.shipments.filter(s => s.messenger_name === this.state.messengerFilter);
    }

    async copyToClipboard(text, type) {
        try {
            await navigator.clipboard.writeText(text);
            this.notification.add(_t(`Enlace de ${type} copiado`), {
                type: "success",
            });
        } catch (err) {
            this.notification.add(_t("Error al copiar enlace"), {
                type: "danger",
            });
        }
    }

    toggle(id) {
        this.state.checked[id] = !this.state.checked[id];
    }

    get selectedIds() {
        return Object.entries(this.state.checked)
            .filter(([, v]) => v)
            .map(([k]) => parseInt(k));
    }

    get totalSelected() {
        return this.state.shipments
            .filter((s) => this.state.checked[s.id])
            .reduce((acc, s) => acc + (s.amount || 0), 0)
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
