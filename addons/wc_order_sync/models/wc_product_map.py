# -*- coding: utf-8 -*-
from odoo import _, fields, models


class ProductWcMap(models.Model):
    _name = 'product.wc.map'
    _description = 'WooCommerce Product Mapping'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'wc_product_name'

    wc_product_name = fields.Char(string="WC Product Name", required=True, index=True, tracking=True)
    wc_product_id = fields.Integer(string="WC Product ID", index=True, tracking=True)
    wc_sku = fields.Char(string="WC SKU", index=True, tracking=True)
    product_id = fields.Many2one('product.product', string="Odoo Product",
                                 ondelete='set null', tracking=True)
    auto_matched = fields.Boolean(string="Auto Matched", default=False, tracking=True)

    def _schedule_review(self):
        """Ask the WooCommerce managers to pick the Odoo product for an unmatched WC product."""
        managers = self.env.ref('wc_base_connector.group_wc_manager').sudo().users
        activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        for mapping in self:
            for user in managers[:1] or self.env.user:
                mapping.sudo().activity_schedule(
                    activity_type_id=activity_type.id if activity_type else False,
                    summary=_("Map this WooCommerce product"),
                    note=_("WC product %(name)s (SKU %(sku)s) matches no Odoo internal reference. "
                           "Orders containing it are parked on the placeholder product until an "
                           "Odoo product is set here.",
                           name=mapping.wc_product_name, sku=mapping.wc_sku or '-'),
                    user_id=user.id,
                )
