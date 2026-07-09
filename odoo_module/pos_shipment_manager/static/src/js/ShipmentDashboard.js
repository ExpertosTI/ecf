/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService, useBus } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

export class ShipmentDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.bus = useService("bus_service");
        this.state = useState({
            dateFilter: 'today',
            searchQuery: '',
            loading: false,
            selectedMonth: new Date().getMonth() + 1,
            selectedYear: new Date().getFullYear(),
            columns: {
                draft: [], street: [], delivered: [], cancelled: [],
                all_delivered_count: 0,
                stats: { messenger_perf: [], seller_perf: [], avg_time: 0, avg_rating: 0, total_count: 0 },
                reconciliation: { in_transit: '0.00' }
            }
        });

        // Debounce para búsqueda Elite
        this.searchTimer = null;

        onWillStart(async () => {
            await this._fetchData();
        });

        useBus(this.bus, "notification", ({ detail: notifications }) => {
            for (const { type } of notifications) {
                if (type === "pos_shipment_update") this._fetchData();
            }
        });
        this.bus.addChannel("pos_shipment_update");
    }

    async _fetchData() {
        this.state.loading = true;
        try {
            const result = await this.orm.call("pos.shipment", "get_dashboard_data", [], {
                date_filter: this.state.dateFilter,
                search_query: this.state.searchQuery,
                month: this.state.dateFilter === 'month' ? this.state.selectedMonth : null,
                year: this.state.dateFilter === 'month' ? this.state.selectedYear : null,
            });
            
            if (result) {
                // Mapeo super-defensivo (Renquitec Shield) para evitar TypeError: Array.from
                const safeStats = result.stats || {};
                const safeRecon = result.reconciliation || {};
                
                this.state.columns = {
                    draft: Array.isArray(result.draft) ? result.draft : [],
                    street: Array.isArray(result.street) ? result.street : [],
                    delivered: Array.isArray(result.delivered) ? result.delivered : [],
                    cancelled: Array.isArray(result.cancelled) ? result.cancelled : [],
                    all_delivered_count: result.all_delivered_count || 0,
                    stats: {
                        messenger_perf: Array.isArray(safeStats.messenger_perf) ? safeStats.messenger_perf : [],
                        seller_perf: Array.isArray(safeStats.seller_perf) ? safeStats.seller_perf : [],
                        avg_time: safeStats.avg_time || 0,
                        avg_rating: safeStats.avg_rating || 0,
                        total_count: safeStats.total_count || 0
                    },
                    reconciliation: {
                        in_transit: safeRecon.in_transit || '0.00'
                    }
                };
            }
        } catch (error) {
            console.error("Dashboard Fetch Error:", error);
        } finally {
            this.state.loading = false;
        }
    }

    async openShipmentList(stateType) {
        let domain = [];
        let title = "Envíos";
        
        if (stateType === 'draft') {
            domain = [['state', '=', 'draft']];
            title = "Por Asignar";
        } else if (stateType === 'street') {
            domain = [['state', '=', 'street']];
            title = "En la Calle";
        } else if (stateType === 'delivered') {
            domain = [['state', '=', 'delivered']];
            title = "Entregados";
        } else if (stateType === 'cancel') {
            domain = [['state', '=', 'cancelled']];
            title = "No Entregados";
        } else if (stateType === 'liquidate') {
            domain = [['state', '=', 'delivered'], ['is_liquidated', '=', false]];
            title = "Por Liquidar";
        }

        this.action.doAction({
            type: 'ir.actions.act_window',
            name: title,
            res_model: 'pos.shipment',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: domain,
            target: 'current',
        });
    }

    async setDateFilter(filter) {
        this.state.dateFilter = filter;
        if (filter !== 'month') {
            // Reset month/year if choosing a standard filter
            this.state.selectedMonth = new Date().getMonth() + 1;
            this.state.selectedYear = new Date().getFullYear();
        }
        await this._fetchData();
    }

    onSearch(ev) {
        this.state.searchQuery = ev.target.value;
        if (this.searchTimer) clearTimeout(this.searchTimer);
        this.searchTimer = setTimeout(() => this._fetchData(), 400); // 400ms debounce
    }

    async onMonthChange(ev) {
        this.state.selectedMonth = parseInt(ev.target.value);
        this.state.dateFilter = 'month';
        await this._fetchData();
    }

    async onYearChange(ev) {
        this.state.selectedYear = parseInt(ev.target.value);
        this.state.dateFilter = 'month';
        await this._fetchData();
    }

    async onRefresh() {
        await this._fetchData();
    }

    openShipment(id) {
        if (!id) return;
        this.action.doAction({
            type: 'ir.actions.act_window',
            res_model: 'pos.shipment',
            res_id: id,
            views: [[false, 'form']],
            target: 'current',
        });
    }

    openOrder(shipment) {
        if (!shipment) return;
        const resModel = shipment.pos_order_id ? 'pos.order' : (shipment.sale_order_id ? 'sale.order' : 'pos.shipment');
        const resId = shipment.pos_order_id || shipment.sale_order_id || shipment.id;
        
        this.action.doAction({
            type: 'ir.actions.act_window',
            res_model: resModel,
            res_id: resId,
            views: [[false, 'form']],
            target: 'current',
        });
    }

    async settleShipment(id) {
        if (!id) return;
        this.state.loading = true;
        try {
            const action = await this.orm.call("pos.shipment", "action_settle_cash", [id]);
            if (action && typeof action === 'object') {
                this.action.doAction(action);
                if (action.params && action.params.next && action.params.next.print_thermal) {
                    const next = action.params.next;
                    this._printDirect(next.model, next.shipment_id);
                }
            }
        } finally {
            await this._fetchData();
        }
    }

    scrollToPanel(state) {
        const el = document.getElementById(`panel-${state}`);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    _printDirect(model, id) {
        const url = `/thermal_print/${model}/${id}`;
        const iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        iframe.src = url;
        document.body.appendChild(iframe);
        iframe.onload = () => {
            setTimeout(() => {
                iframe.contentWindow.focus();
                iframe.contentWindow.print();
                setTimeout(() => document.body.removeChild(iframe), 2000);
            }, 500);
        };
    }
}

ShipmentDashboard.template = "pos_shipment_manager.ShipmentDashboard";
registry.category("actions").add("pos_shipment_dashboard", ShipmentDashboard);
