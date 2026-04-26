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
            this.notification.add(_t("📋 Enlace copiado al portapapeles"), {
                type: "success",
                sticky: false,
            });
        } catch (err) {
            this.notification.add(_t("Error al copiar enlace"), {
                type: "danger",
            });
        }
    }

    shareWhatsApp(type) {
        const url = type === 'customer' ? this.props.customer_url : this.props.messenger_url;
        const phone = type === 'customer' ? this.props.customer_phone : this.props.messenger_phone;
        
        if (!phone) {
            this.notification.add(_t("El contacto no tiene teléfono asignado."), {
                type: "warning",
            });
            return;
        }

        const cleanPhone = phone.replace(/\D/g, '');
        const message = encodeURIComponent(`Hola, aquí tienes el enlace de seguimiento de tu pedido: ${url}`);
        window.open(`https://wa.me/${cleanPhone}?text=${message}`, '_blank');
    }

    cancel() {
        this.props.close();
    }
}
