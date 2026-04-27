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
        
        const today = new Date();
        const firstDay = new Date(today.getFullYear(), today.getMonth(), 1);
        
        this.state = useState({
            stats: {},
            fiscal: {},
            compliance: {},
            loading: true,
            saas_status: 'checking', 
            date_from: firstDay.toISOString().split('T')[0],
            date_to: today.toISOString().split('T')[0],
            show_report_viewer: false,
            current_report_type: '',
            report_details: [],
        });

        this.chartStatusRef = useRef("chartStatus");
        this.chartTypeRef = useRef("chartType");
        this.chartVolumeRef = useRef("chartVolume");

        onWillStart(async () => {
            await this.loadData();
            await this.checkSaasStatus();
        });

        onMounted(() => {
            this.renderCharts();
        });
    }

    async loadData() {
        this.state.loading = true;
        try {
            const context = { 
                date_from: this.state.date_from, 
                date_to: this.state.date_to 
            };
            
            const stats = await this.orm.call("ecf.log", "get_dashboard_stats", [[]], { context });
            this.state.stats = stats;
            
            const fiscal = await this.orm.call("ecf.log", "get_fiscal_summary", [[]], { context });
            this.state.fiscal = fiscal;

            const compliance = await this.orm.call("ecf.log", "check_dgii_compliance", [[]]);
            this.state.compliance = compliance;
        } catch (err) {
            console.error("Error loading dashboard data", err);
        } finally {
            this.state.loading = false;
        }
    }

    async openReportDetail(type) {
        this.state.current_report_type = type;
        this.state.loading = true;
        try {
            const domain = [
                ['invoice_date', '>=', this.state.date_from],
                ['invoice_date', '<=', this.state.date_to],
                ['move_type', '=', type === '606' ? 'in_invoice' : 'out_invoice'],
                ['state', '=', 'posted']
            ];
            
            const fields = ['name', 'invoice_date', 'partner_id', 'amount_untaxed', 'amount_tax', 'amount_total'];
            const data = await this.orm.searchRead("account.move", domain, fields);
            
            this.state.report_details = data.map(m => ({
                id: m.id,
                ncf: m.name,
                date: m.invoice_date,
                rnc: m.partner_id[1].match(/\(([^)]+)\)/)?.[1] || '---',
                partner: m.partner_id[1].split('(')[0],
                base: m.amount_untaxed,
                tax: m.amount_tax,
                total: m.amount_total
            }));
            
            this.state.show_report_viewer = true;
        } catch (err) {
            this.notification.add(_t("Error al cargar detalles del reporte"), { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    async checkSaasStatus() {
        try {
            const configs = await this.orm.searchRead("res.company", [["id", "=", 1]], ["ecf_saas_url", "ecf_api_key"]);
            if (configs.length && configs[0].ecf_saas_url) {
                this.state.saas_status = 'online';
            } else {
                this.state.saas_status = 'offline';
            }
        } catch (err) {
            this.state.saas_status = 'offline';
        }
    }

    renderCharts() {
        if (this.state.loading || !this.state.stats.daily_volume) return;
        const stats = this.state.stats;

        // Chart Estado
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
            options: { responsive: true, maintainAspectRatio: false }
        });

        // Chart Volumen Diario
        new Chart(this.chartVolumeRef.el, {
            type: 'line',
            data: {
                labels: stats.daily_volume.map(d => d.day),
                datasets: [{
                    label: 'Comprobantes',
                    data: stats.daily_volume.map(d => d.count),
                    borderColor: '#0087ff',
                    borderWidth: 3,
                    tension: 0.4,
                    fill: true,
                    backgroundColor: 'rgba(0, 135, 255, 0.05)',
                }]
            },
            options: { responsive: true, maintainAspectRatio: false }
        });
    }

    async exportReport(reportType, format) {
        this.notification.add(_t(`Exportando Reporte ${reportType} (${format.toUpperCase()})...`), { type: "info" });
        if (format === 'excel') {
            this.actionService.doAction(reportType === '606' ? 'ecf_connector.ecf_compras_action' : 'ecf_connector.ecf_ventas_action');
        } else if (format === 'pdf') {
            window.print(); 
        } else if (format === 'txt') {
            const content = `Reporte ${reportType} | Periodo: ${this.state.date_from} a ${this.state.date_to}\n` + 
                            this.state.report_details.map(r => `${r.ncf}|${r.rnc}|${r.total}`).join('\n');
            const blob = new Blob([content], { type: 'text/plain' });
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `Reporte_${reportType}_DGII.txt`;
            a.click();
        }
    }

    printReport608() {
        this.actionService.doAction("ecf_connector.ecf_log_action", {
            additional_context: { 'search_default_anulados': 1 }
        });
    }

    openHistory() {
        this.actionService.doAction("ecf_connector.ecf_log_action");
    }

    openMove(moveId) {
        if (!moveId) return;
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
