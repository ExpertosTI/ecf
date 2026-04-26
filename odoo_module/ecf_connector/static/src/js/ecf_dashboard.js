/** @odoo-module **/
import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, useRef, onMounted } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class EcfDashboard extends Component {
    static template = "ecf_connector.EcfDashboard";

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.state = useState({
            stats: {},
            loading: true,
        });

        this.chartStatusRef = useRef("chartStatus");
        this.chartTypeRef = useRef("chartType");
        this.chartVolumeRef = useRef("chartVolume");

        onWillStart(async () => {
            await this.loadData();
        });

        onMounted(() => {
            this.renderCharts();
        });
    }

    async loadData() {
        this.state.loading = true;
        const stats = await this.orm.call("ecf.log", "get_dashboard_stats", [[]]);
        this.state.stats = stats;
        this.state.loading = false;
    }

    renderCharts() {
        if (this.state.loading) return;

        const stats = this.state.stats;

        // 1. Chart Estado
        new Chart(this.chartStatusRef.el, {
            type: 'doughnut',
            data: {
                labels: Object.keys(stats.stats_estado),
                datasets: [{
                    data: Object.values(stats.stats_estado),
                    backgroundColor: ['#10b981', '#ef4444', '#f59e0b', '#3b82f6'],
                }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });

        // 2. Chart Tipo
        new Chart(this.chartTypeRef.el, {
            type: 'bar',
            data: {
                labels: Object.keys(stats.stats_tipo),
                datasets: [{
                    label: 'Cantidad',
                    data: Object.values(stats.stats_tipo),
                    backgroundColor: '#0087ff',
                }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });

        // 3. Chart Volumen Diario
        new Chart(this.chartVolumeRef.el, {
            type: 'line',
            data: {
                labels: stats.daily_volume.map(d => d.day),
                datasets: [{
                    label: 'Comprobantes',
                    data: stats.daily_volume.map(d => d.count),
                    borderColor: '#0087ff',
                    fill: true,
                    backgroundColor: 'rgba(0, 135, 255, 0.1)',
                }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    openHistory() {
        this.actionService.doAction("ecf_connector.ecf_log_action");
    }
}

registry.category("actions").add("ecf_dashboard_client_action", EcfDashboard);
