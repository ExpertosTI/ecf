/** @odoo-module **/
import { Dialog } from "@web/core/dialog/dialog";
import { Component, useState } from "@odoo/owl";

export class ShipmentConfigDialog extends Component {
    static template = "pos_shipment_manager.ShipmentConfigDialog";
    static components = { Dialog };

    setup() {
        this.state = useState({
            mode: this.props.initialMode || '',
            messengerId: this.props.initialMessengerId || '',
            locationLink: this.props.initialLocationLink || '',
            isAlreadyPaid: this.props.isAlreadyPaid || false,
        });
    }

    setMode(mode) {
        this.state.mode = mode;
    }

    confirm() {
        this.props.getPayload({
            mode: this.state.mode,
            messengerId: parseInt(this.state.messengerId),
            locationLink: this.state.locationLink,
        });
        this.props.close();
    }

    cancel() {
        this.props.close();
    }
}
