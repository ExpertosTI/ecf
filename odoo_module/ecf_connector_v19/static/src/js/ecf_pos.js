import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

// Parchear la clase PosOrder para manejar el tipo de e-CF en Odoo 19
patch(PosOrder.prototype, {
    setup() {
        super.setup(...arguments);
        // Evitar el crash de 'undefined this.models' usando una referencia segura a this.pos.models
        if (!this.ecf_tipo_id && this.pos && this.pos.models && this.pos.models["ecf.tipo"]) {
            try {
                const types = this.pos.models["ecf.tipo"].getAll();
                const defaultType = types.find(t => t.codigo === 32) || types[0];
                this.ecf_tipo_id = defaultType ? defaultType.id : null;
            } catch (err) {
                console.warn("Could not set default ECF type", err);
            }
        }
    },
    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        json.ecf_tipo_id = this.ecf_tipo_id;
        json.ecf_ncf = this.ecf_ncf;
        json.ecf_cufe = this.ecf_cufe;
        json.ecf_qr = this.ecf_qr;
        return json;
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.ecf_tipo_id = json.ecf_tipo_id || null;
        this.ecf_ncf = json.ecf_ncf || null;
        this.ecf_cufe = json.ecf_cufe || null;
        this.ecf_qr = json.ecf_qr || null;
    }
});
