# -*- coding: utf-8 -*-
from odoo import fields, models


class ProductWcMap(models.Model):
    _name = 'product.wc.map'
    _description = 'WooCommerce Product Mapping'
    _order = 'wc_product_name'

    wc_product_name = fields.Char(string="WC 商品名稱", required=True, index=True)
    wc_product_id = fields.Integer(string="WC 商品 ID", index=True)
    product_id = fields.Many2one('product.product', string="Odoo 商品",
                                 ondelete='set null')
    auto_matched = fields.Boolean(string="自動匹配", default=False)
