import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

// Parchear la clase PosOrder para manejar el tipo de e-CF en Odoo 18
patch(PosOrder.prototype, {
    setup() {
        super.setup(...arguments);
        // Por defecto: Consumidor Final (E32 / Código 32)
        if (!this.ecf_tipo_id) {
            const types = this.models["ecf.tipo"].getAll();
            const defaultType = types.find(t => t.codigo === 32) || types[0];
            this.ecf_tipo_id = defaultType ? defaultType.id : null;
            this.ecf_tipo_prefijo = defaultType ? defaultType.prefijo : "";
        }
    },
    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        json.ecf_tipo_id = this.ecf_tipo_id;
        return json;
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.ecf_tipo_id = json.ecf_tipo_id || null;
    }
});
