/** @odoo-module **/
import { Dialog } from "@web/core/dialog/dialog";
import { Component } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";

export class ShipmentShareDialog extends Component {
    static template = "pos_shipment_manager.ShipmentShareDialog";
    static components = { Dialog };

    setup() {
        this.notification = useService("notification");
    }

    async copyToClipboard(text) {
        try {
            await navigator.clipboard.writeText(text);
            this.notification.add(_t("Enlace copiado al portapapeles"), {
                type: "success",
            });
        } catch (err) {
            this.notification.add(_t("Error al copiar enlace"), {
                type: "danger",
            });
        }
    }

    shareWhatsApp(type) {
        const shipment = this.props.shipment;
        const url = type === 'customer' ? this.props.customer_url : this.props.messenger_url;
        const phone = type === 'customer' ? this.props.customer_phone : this.props.messenger_phone;
        
        // Limpiar teléfono y asegurar formato internacional (Dominicana +1)
        let cleanPhone = phone ? phone.toString().replace(/\D/g, '') : '';
        if (cleanPhone.length === 10) cleanPhone = '1' + cleanPhone;

        const msg = type === 'customer' 
            ? `Hola ${shipment.partner_name || ''}, tu pedido *${shipment.name}* está en camino. Síguelo aquí: ${url}`
            : `🛵 *NUEVO ENVÍO: ${shipment.name}*\n👤 Cliente: ${shipment.partner_name}\n📦 Cobrar: RD$ ${shipment.total_order.toFixed(2)}${shipment.is_cod ? ' (CONTRA ENTREGA)' : ''}\n✅ Link Hoja de Ruta: ${url}`;
        
        const waUrl = cleanPhone 
            ? `https://wa.me/${cleanPhone}?text=${encodeURIComponent(msg)}`
            : `https://wa.me/?text=${encodeURIComponent(msg)}`;
            
        window.open(waUrl, '_blank');
    }

    cancel() {
        this.props.close();
    }
}
