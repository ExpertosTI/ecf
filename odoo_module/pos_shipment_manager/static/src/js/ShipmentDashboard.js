/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

export class ShipmentDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.bus = useService("bus_service");
        this.notification = useService("notification");
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
            this.bus.addChannel("pos_shipment_update");
            this.bus.addEventListener("notification", ({ detail: notifications }) => {
                for (const { type } of notifications) {
                    if (type === "pos_shipment_update") this._fetchData();
                }
            });
        });
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
                // Mapeo defensivo profundo para evitar errores de OWL (Array.from)
                const stats = result.stats || {};
                this.state.columns = {
                    draft: result.draft || [],
                    street: result.street || [],
                    delivered: result.delivered || [],
                    cancelled: result.cancelled || [],
                    all_delivered_count: result.all_delivered_count || 0,
                    stats: {
                        messenger_perf: stats.messenger_perf || [],
                        seller_perf: stats.seller_perf || [],
                        avg_time: stats.avg_time || 0,
                        avg_rating: stats.avg_rating || 0,
                        total_count: stats.total_count || 0
                    },
                    reconciliation: result.reconciliation || { in_transit: '0.00' }
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

    async onRefresh() {
        await this._fetchData();
        this.notification.add("🔄 Sincronizado con el Centro de Control", {
            type: "success",
            sticky: false,
        });
    }

    openShipment(id) {
        this.action.doAction({
            type: 'ir.actions.act_window',
            res_model: 'pos.shipment',
            res_id: id,
            views: [[false, 'form']],
            target: 'current',
        });
    }

    openOrder(shipment) {
        if (shipment.pos_order_id) {
            this.action.doAction({
                type: 'ir.actions.act_window',
                res_model: 'pos.order',
                res_id: shipment.pos_order_id,
                views: [[false, 'form']],
                target: 'current',
            });
        } else if (shipment.sale_order_id) {
            this.action.doAction({
                type: 'ir.actions.act_window',
                res_model: 'sale.order',
                res_id: shipment.sale_order_id,
                views: [[false, 'form']],
                target: 'current',
            });
        }
    }

    async settleShipment(id) {
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
