import { PosOrder } from "@point_of_sale/app/models/pos_order";
import { patch } from "@web/core/utils/patch";

// Parchear PosOrder para tipo e-CF (Odoo 18+: campos cargados + serialize)
patch(PosOrder.prototype, {
    setup() {
        super.setup(...arguments);
        if (!this.ecf_tipo_id && this.models && this.models["ecf.tipo"]) {
            try {
                const types = this.models["ecf.tipo"].getAll();
                const defaultType = types.find(t => t.codigo === 32) || types[0];
                this.ecf_tipo_id = defaultType ? defaultType.id : false;
            } catch (err) {
                console.warn("Could not set default ECF type", err);
            }
        }
    },
    // Compat sesión antigua (Odoo ≤17)
    export_as_JSON() {
        const json = super.export_as_JSON(...arguments);
        json.ecf_tipo_id = this.ecf_tipo_id;
        json.ecf_ncf = this.ecf_ncf;
        json.ecf_codigo_seguridad = this.ecf_codigo_seguridad;
        json.ecf_qr = this.ecf_qr;
        return json;
    },
    init_from_JSON(json) {
        super.init_from_JSON(...arguments);
        this.ecf_tipo_id = json.ecf_tipo_id || null;
        this.ecf_ncf = json.ecf_ncf || null;
        this.ecf_codigo_seguridad = json.ecf_codigo_seguridad || json.ecf_cufe || null;
        this.ecf_qr = json.ecf_qr || null;
    },
    // Odoo 18+: el sync al backend usa serialize()
    serialize() {
        const data = super.serialize(...arguments);
        data.ecf_tipo_id = this.ecf_tipo_id || false;
        return data;
    },
});
