/** @odoo-module **/

import { registry } from "@web/core/registry";
import { X2ManyField, x2ManyField } from "@web/views/fields/x2many/x2many_field";
import { onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class PosProductGridSales extends X2ManyField {
    static props = {
        ...X2ManyField.props,
    };

    setup() {
        super.setup();
        this.orm = useService("orm");
        this.state = useState({
            searchQuery: "",
            selectedCategory: "all",
            products: [],
            categories: [],
            loading: false,
        });

        onWillStart(async () => {
            await this._loadProductsAndCategories();
        });
    }

    async _loadProductsAndCategories() {
        this.state.loading = true;
        try {
            // 1. Cargar categorías del Punto de Venta (POS)
            const categories = await this.orm.searchRead(
                "pos.category",
                [],
                ["id", "name"]
            );
            this.state.categories = categories;

            // 2. Cargar productos que se pueden vender y están disponibles en el POS
            const products = await this.orm.searchRead(
                "product.product",
                [
                    ["sale_ok", "=", true],
                    ["available_in_pos", "=", true]
                ],
                ["id", "display_name", "lst_price", "pos_categ_ids", "has_configurable_attributes", "product_tmpl_id"]
            );
            this.state.products = products;
        } catch (error) {
            console.error("Error loading products/categories:", error);
        } finally {
            this.state.loading = false;
        }
    }

    get filteredProducts() {
        let prods = this.state.products;
        if (this.state.selectedCategory !== "all") {
            const catId = parseInt(this.state.selectedCategory);
            prods = prods.filter(p => {
                if (p.pos_categ_ids && Array.isArray(p.pos_categ_ids)) {
                    return p.pos_categ_ids.includes(catId);
                }
                if (p.pos_categ_id && p.pos_categ_id[0] === catId) {
                    return true;
                }
                return false;
            });
        }
        if (this.state.searchQuery) {
            const query = this.state.searchQuery.toLowerCase();
            prods = prods.filter(p => p.display_name.toLowerCase().includes(query));
        }
        return prods;
    }

    _findLineByProductId(productId) {
        return this.list.records.find(
            r => r.data.product_id && r.data.product_id[0] === productId
        );
    }

    async addProduct(product) {
        const existingLine = this._findLineByProductId(product.id);
        if (existingLine) {
            const currentQty = existingLine.data.product_uom_qty || 0;
            await existingLine.update({ product_uom_qty: currentQty + 1 });
        } else {
            await this.list.addNewRecord({
                context: {
                    default_product_id: product.id,
                    default_product_uom_qty: 1.0,
                },
            });
        }
    }

    async updateQty(line, delta) {
        const currentQty = line.data.product_uom_qty || 0;
        const newQty = currentQty + delta;
        if (newQty <= 0) {
            await this.removeLine(line);
        } else {
            await line.update({ product_uom_qty: newQty });
        }
    }

    async removeLine(line) {
        await this.list.delete(line);
    }

    async editLine(line) {
        // Disabled to avoid activeFields crash.
    }
}

PosProductGridSales.template = "pos_shipment_manager.PosProductGridSales";

registry.category("fields").add("pos_product_grid_sales", {
    ...x2ManyField,
    component: PosProductGridSales,
});


