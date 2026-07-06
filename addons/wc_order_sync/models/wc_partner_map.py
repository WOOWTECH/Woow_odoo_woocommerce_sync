# -*- coding: utf-8 -*-
from odoo import fields, models


class PartnerWcMap(models.Model):
    _name = 'partner.wc.map'
    _description = 'WooCommerce Partner Mapping'
    _order = 'wc_customer_name'

    wc_customer_name = fields.Char(string="WC 客戶名稱", required=True, index=True)
    wc_customer_id = fields.Integer(string="WC 客戶 ID", index=True)
    wc_email = fields.Char(string="WC Email")
    wc_phone = fields.Char(string="WC 電話")
    partner_id = fields.Many2one('res.partner', string="Odoo 聯絡人",
                                 ondelete='set null')
    auto_matched = fields.Boolean(string="自動匹配", default=False)
    last_order_date = fields.Datetime(string="最近訂單日期")
    order_count = fields.Integer(string="訂單數", compute='_compute_order_count')

    def _compute_order_count(self):
        for rec in self:
            if rec.partner_id:
                rec.order_count = self.env['sale.order'].sudo().search_count([
                    ('partner_id', '=', rec.partner_id.id),
                    ('wc_order_id', '>', 0),
                ])
            else:
                rec.order_count = 0
