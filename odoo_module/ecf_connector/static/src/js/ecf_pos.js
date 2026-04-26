import { patch } from "@web/core/utils/patch";
import { PosStore } from "@point_of_sale/app/store/pos_store";
import { Order } from "@point_of_sale/app/store/models";

// Parchear la clase Order para manejar el tipo de e-CF
patch(Order.prototype, {
    setup() {
        super.setup(...arguments);
        // Por defecto: Consumidor Final (E32 / Código 32)
        if (!this.ecf_tipo) {
            const types = this.pos.models["ecf.tipo"].getAll();
            this.ecf_tipo = types.find(t => t.codigo === 32) || types[0];
        }
    },
    //@override
    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        json.ecf_tipo_id = this.ecf_tipo ? this.ecf_tipo.id : null;
        return json;
    },
    set_ecf_tipo(tipo) {
        this.ecf_tipo = tipo;
    },
    get_ecf_tipo() {
        return this.ecf_tipo;
    }
});
