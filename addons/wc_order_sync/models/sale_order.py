# -*- coding: utf-8 -*-
from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    wc_order_id = fields.Integer(string="WC Order ID", index=True, copy=False)
    wc_order_status = fields.Char(string="WC Order Status", copy=False)
    wc_payment_method = fields.Char(string="WC Payment Method", copy=False)
    wc_shipping_total = fields.Float(string="WC Shipping Total", copy=False)
    wc_tax_total = fields.Float(string="WC Tax Total", copy=False)
    wc_discount_total = fields.Float(string="WC Discount Total", copy=False)
    wc_coupon_codes = fields.Char(string="WC Coupon Codes", copy=False)
    wc_last_synced = fields.Datetime(string="WC Last Synced", copy=False)
