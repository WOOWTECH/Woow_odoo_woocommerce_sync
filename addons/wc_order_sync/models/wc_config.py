# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    wc_webhook_secret = fields.Char(
        string="Webhook Secret",
        config_parameter='wc_order_sync.wc_webhook_secret',
    )
    wc_auto_confirm = fields.Boolean(
        string="Auto-confirm Orders",
        config_parameter='wc_order_sync.wc_auto_confirm',
        default=True,
    )
    wc_auto_stock = fields.Boolean(
        string="Auto-deduct Stock",
        config_parameter='wc_order_sync.wc_auto_stock',
        default=True,
    )
    wc_default_product_id = fields.Many2one(
        'product.product',
        string="Default Product",
        config_parameter='wc_order_sync.wc_default_product_id',
    )
