# -*- coding: utf-8 -*-
from odoo import _, api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    wc_webhook_secret = fields.Char(
        string="Webhook Secret",
        config_parameter='wc_order_sync.wc_webhook_secret',
        groups='base.group_system',
        help="Required: webhook deliveries are rejected while it is empty.",
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
        help="Automatically validate delivery orders for completed WC orders to deduct stock",
    )
    wc_default_product_id = fields.Many2one(
        'product.product',
        string="Default Product",
        config_parameter='wc_order_sync.wc_default_product_id',
        help="Fallback product when WC product cannot be matched",
    )

    @api.model
    def _wc_restricted_settings(self):
        return super()._wc_restricted_settings() + ['wc_webhook_secret']

    def action_manual_sync(self):
        self.env['wc.connection.mixin']._wc_check_access(allow_wc_manager=False)
        self.env['wc.sync.queue'].sudo()._cron_process_queue()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'WooCommerce',
                'message': _("Manual sync completed"),
                'type': 'success',
                'sticky': False,
            }
        }
