/** @odoo-module **/
import { Component } from "@odoo/owl";

export class ShipmentSettleReceipt extends Component {
    static template = "pos_shipment_manager.ShipmentSettleReceipt";
    static props = {
        shipments: Array,
        total: Number,
        cashier: String,
        date: String,
        settleRef: String,
    };
}
