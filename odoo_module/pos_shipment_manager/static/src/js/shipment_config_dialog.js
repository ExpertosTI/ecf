/** @odoo-module **/
import { Dialog } from "@web/core/dialog/dialog";
import { Component, useState } from "@odoo/owl";

export class ShipmentConfigDialog extends Component {
    static template = "pos_shipment_manager.ShipmentConfigDialog";
    static components = { Dialog };

    setup() {
        this.state = useState({
            mode: this.props.initialMode || '',
            messengerId: this.props.initialMessengerId || '',
            locationLink: this.props.initialLocationLink || '',
            isAlreadyPaid: this.props.isAlreadyPaid || false,
            distance: this.props.initialDistance || 0,
            calculatedPrice: this.props.initialPrice || 0,

            // Cliente: Teléfono es el campo primario
            phoneQuery: (this.props.initialPartner && this.props.initialPartner.id)
                ? (this.props.initialPartner.phone || this.props.initialPartner.mobile || '')
                : '',
            customerName: (this.props.initialPartner && this.props.initialPartner.id)
                ? (this.props.initialPartner.name || this.props.initialPartner.display_name || '')
                : '',
            searchResults: [],
            selectedPartner: (this.props.initialPartner && this.props.initialPartner.id)
                ? this.props.initialPartner
                : null,
            isSearching: false,
            showNameInput: false, // Mostrar campo nombre cuando no se encuentra partner

            // Mensajero
            showQuickMessenger: false,
            quickMessengerName: '',
            quickMessengerPhone: '',
            messengersList: [...(this.props.messengers || [])],
        });

        this._searchTimeout = null;
    }

    // ── Búsqueda de Cliente por Teléfono (RPC) ──

    onPhoneInput(ev) {
        const phone = ev.target.value;
        this.state.phoneQuery = phone;

        // Limpiar selección previa si cambia el teléfono
        if (this.state.selectedPartner) {
            this.state.selectedPartner = null;
            this.state.customerName = '';
            this.state.showNameInput = false;
        }

        // Debounce de 400ms para no saturar el servidor
        clearTimeout(this._searchTimeout);
        const digits = phone.replace(/\D/g, '');
        if (digits.length < 7) {
            this.state.searchResults = [];
            this.state.showNameInput = false;
            return;
        }

        this._searchTimeout = setTimeout(() => this._searchByPhone(phone), 400);
    }

    async _searchByPhone(phone) {
        this.state.isSearching = true;
        try {
            const results = await this.env.services.orm.call(
                "res.partner", "search_by_phone_pos", [phone]
            );
            this.state.searchResults = results || [];
            // Si no hay resultados, mostrar campo de nombre para crear
            this.state.showNameInput = results.length === 0;
        } catch (e) {
            console.warn("[PSM] Error buscando por teléfono:", e);
            this.state.searchResults = [];
            this.state.showNameInput = true;
        }
        this.state.isSearching = false;
    }

    selectPartner(partner) {
        this.state.selectedPartner = partner;
        this.state.phoneQuery = partner.phone || partner.mobile || '';
        this.state.customerName = partner.name || partner.display_name || '';
        this.state.searchResults = [];
        this.state.showNameInput = false;

        this.env.services.notification.add(
            `✅ ${partner.name} vinculado`,
            { type: "success" }
        );
    }

    deselectPartner() {
        this.state.selectedPartner = null;
        this.state.phoneQuery = '';
        this.state.customerName = '';
        this.state.searchResults = [];
        this.state.showNameInput = false;
    }

    // ── Modos y Cálculos ──

    setMode(mode) {
        this.state.mode = mode;
    }

    onDistanceChange(ev) {
        const d = parseFloat(ev.target.value) || 0;
        this.state.distance = d;
        this.state.calculatedPrice = this._calculatePrice(d);
    }

    _calculatePrice(d) {
        if (d <= 0) return 0;
        let rawCharge = 0;
        if (d <= 5) rawCharge = 150.0;
        else if (d <= 10) rawCharge = 150.0 + ((d - 5) * 30.0);
        else if (d <= 20) rawCharge = 300.0 + ((d - 10) * 15.0);
        else rawCharge = 500.0 + ((d - 20) * 15.0);
        return Math.round(rawCharge / 10.0) * 10.0;
    }

    // ── Confirmar ──

    async confirm() {
        // Si hay teléfono pero no partner seleccionado, crear uno nuevo
        let partner = this.state.selectedPartner;
        const phone = this.state.phoneQuery.trim();
        const name = this.state.customerName.trim();

        if (!partner && phone && name) {
            try {
                const result = await this.env.services.orm.call(
                    "res.partner", "quick_create_from_phone_pos", [phone, name]
                );
                if (result.error) {
                    this.env.services.notification.add(result.error, { type: "danger" });
                    return;
                }
                partner = { id: result.id, name: result.name, phone: result.phone || phone };
                if (result.existing) {
                    this.env.services.notification.add(`Cliente existente: ${result.name}`, { type: "info" });
                } else {
                    this.env.services.notification.add(`Cliente creado: ${result.name}`, { type: "success" });
                }

                // Notificar al padre para registrar en la orden
                if (this.props.onCustomerCreated) {
                    this.props.onCustomerCreated({ id: partner.id, name: partner.name });
                }
            } catch (error) {
                const msg = error.message ? (error.message.message || error.message) : String(error);
                this.env.services.notification.add("Error: " + msg, { type: "danger" });
                return;
            }
        }

        this.props.getPayload({
            mode: this.state.mode,
            messengerId: parseInt(this.state.messengerId),
            locationLink: this.state.locationLink,
            distance: this.state.distance,
            price: this.state.calculatedPrice,
            selectedPartner: partner,
        });
        this.props.close();
    }

    // ── Crear Mensajero Rápido ──

    async quickCreateMessenger() {
        if (!this.state.quickMessengerName || !this.state.quickMessengerPhone) {
            this.env.services.notification.add("Nombre y teléfono son requeridos", { type: "danger" });
            return;
        }
        try {
            const res = await this.env.services.orm.call(
                "res.partner", "action_create_pos_messenger",
                [this.state.quickMessengerName, this.state.quickMessengerPhone]
            );
            if (res.error) {
                this.env.services.notification.add(res.error, { type: "danger" });
            } else {
                this.env.services.notification.add("Mensajero creado", { type: "success" });
                this.state.messengersList.push({ id: res.id, name: res.name });
                this.state.messengerId = res.id;
                this.state.showQuickMessenger = false;
                this.state.quickMessengerName = '';
                this.state.quickMessengerPhone = '';
            }
        } catch (error) {
            const msg = error.message ? (error.message.message || error.message) : String(error);
            this.env.services.notification.add("Error: " + msg, { type: "danger" });
        }
    }

    cancel() {
        this.props.close();
    }
}
