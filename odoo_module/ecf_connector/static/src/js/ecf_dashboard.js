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
        this.notification = useService("notification");
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
        try {
            const stats = await this.orm.call("ecf.log", "get_dashboard_stats", [[]]);
            this.state.stats = stats;
        } catch (err) {
            console.error("Error loading dashboard data", err);
        } finally {
            this.state.loading = false;
        }
    }

    renderCharts() {
        if (this.state.loading || !this.state.stats.daily_volume) return;

        const stats = this.state.stats;

        // 1. Chart Estado
        new Chart(this.chartStatusRef.el, {
            type: 'doughnut',
            data: {
                labels: Object.keys(stats.stats_estado),
                datasets: [{
                    data: Object.values(stats.stats_estado),
                    backgroundColor: ['#10b981', '#ef4444', '#f59e0b', '#3b82f6'],
                    borderWidth: 0,
                }]
            },
            options: { 
                responsive: true, 
                maintainAspectRatio: false,
                plugins: { legend: { position: 'bottom' } }
            }
        });

        // 2. Chart Tipo
        new Chart(this.chartTypeRef.el, {
            type: 'bar',
            data: {
                labels: Object.keys(stats.stats_tipo),
                datasets: [{
                    label: 'Cantidad',
                    data: Object.values(stats.stats_tipo),
                    backgroundColor: 'rgba(0, 135, 255, 0.8)',
                    borderRadius: 8,
                }]
            },
            options: { 
                responsive: true, 
                maintainAspectRatio: false,
                plugins: { legend: { display: false } }
            }
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
                    borderWidth: 3,
                    pointBackgroundColor: '#0087ff',
                    tension: 0.4,
                    fill: true,
                    backgroundColor: 'rgba(0, 135, 255, 0.05)',
                }]
            },
            options: { 
                responsive: true, 
                maintainAspectRatio: false,
                scales: {
                    y: { beginAtZero: true, grid: { display: false } },
                    x: { grid: { display: false } }
                }
            }
        });
    }

    // Acciones de Reportes
    printReport606() {
        this.actionService.doAction("ecf_connector.ecf_compras_action");
        this.notification.add(_t("Abriendo reporte 606 (Compras)"), { type: "info" });
    }

    printReport607() {
        this.actionService.doAction("account.action_move_out_invoice_type");
        this.notification.add(_t("Abriendo Ventas (Filtre por 607)"), { type: "info" });
    }

    printReport608() {
        this.actionService.doAction("ecf_connector.ecf_log_action", {
            additional_context: { 'search_default_anulados': 1 }
        });
        this.notification.add(_t("Abriendo Anulaciones (608)"), { type: "info" });
    }

    openHistory() {
        this.actionService.doAction("ecf_connector.ecf_log_action");
    }

    openMove(moveId) {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "account.move",
            res_id: moveId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("ecf_dashboard_client_action", EcfDashboard);
