# -*- coding: utf-8 -*-
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    wc_url = fields.Char(
        string="WooCommerce URL",
        config_parameter='wc_order_sync.wc_url',
    )
    wc_username = fields.Char(
        string="WC API Username",
        config_parameter='wc_order_sync.wc_username',
    )
    wc_password = fields.Char(
        string="WC API Password",
        config_parameter='wc_order_sync.wc_password',
    )
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
        string="Auto-deduct Stock (completed orders)",
        config_parameter='wc_order_sync.wc_auto_stock',
        default=True,
        help="Automatically validate delivery orders for WC completed orders to deduct stock",
    )
    wc_default_product_id = fields.Many2one(
        'product.product',
        string="Default Product",
        config_parameter='wc_order_sync.wc_default_product_id',
    )

    def action_test_wc_connection(self):
        import requests
        url = self.env['ir.config_parameter'].sudo().get_param('wc_order_sync.wc_url', '')
        username = self.env['ir.config_parameter'].sudo().get_param('wc_order_sync.wc_username', '')
        password = self.env['ir.config_parameter'].sudo().get_param('wc_order_sync.wc_password', '')
        if not all([url, username, password]):
            return self._wc_notify("請先填寫 WooCommerce 連線資訊", 'danger')
        try:
            resp = requests.get(
                f"{url.rstrip('/')}/wp-json/wc/v3/orders",
                auth=(username, password.replace(' ', '')),
                params={'per_page': 1}, timeout=15,
            )
            if resp.status_code == 200:
                total = resp.headers.get('X-WP-Total', '?')
                return self._wc_notify(f"連線成功！WooCommerce 共有 {total} 筆訂單", 'success')
            else:
                return self._wc_notify(f"連線失敗：HTTP {resp.status_code}", 'danger')
        except Exception as e:
            return self._wc_notify(f"連線錯誤：{str(e)[:100]}", 'danger')

    def _wc_notify(self, message, notif_type='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'WooCommerce', 'message': message, 'type': notif_type, 'sticky': False}
        }
