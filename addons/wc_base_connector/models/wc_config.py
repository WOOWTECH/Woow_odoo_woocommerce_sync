# -*- coding: utf-8 -*-
import logging

import requests

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    wc_url = fields.Char(
        string="WooCommerce URL",
        config_parameter='wc_base_connector.wc_url',
        help="e.g. https://www.inzense.com.tw",
        groups='base.group_system',
    )
    wc_username = fields.Char(
        string="WC API Username",
        config_parameter='wc_base_connector.wc_username',
        groups='base.group_system',
    )
    wc_password = fields.Char(
        string="WC API Password",
        config_parameter='wc_base_connector.wc_password',
        groups='base.group_system',
    )

    # ------------------------------------------------------------------
    # Credentials are for Settings administrators only
    # ------------------------------------------------------------------

    @api.model
    def _wc_restricted_settings(self):
        """Settings fields holding credentials or secrets.

        Core's res.config.settings.default_get() reads every config_parameter
        field through sudo() and ignores field groups, and any logged-in user
        can call it over RPC, so these are removed from its result for anyone
        but Settings administrators. Modules adding a secret extend this list.
        """
        return ['wc_url', 'wc_username', 'wc_password']

    @api.model
    def _wc_hide_restricted_settings(self, values):
        if self.env.su or self.env.is_system():
            return values
        for name in self._wc_restricted_settings():
            values.pop(name, None)
        return values

    @api.model
    def default_get(self, fields_list):
        return self._wc_hide_restricted_settings(super().default_get(fields_list))

    def get_values(self):
        return self._wc_hide_restricted_settings(super().get_values())

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    def action_test_wc_connection(self):
        self.env['wc.connection.mixin']._wc_check_access(allow_wc_manager=False)
        ICP = self.env['ir.config_parameter'].sudo()
        url = ICP.get_param('wc_base_connector.wc_url', '')
        username = ICP.get_param('wc_base_connector.wc_username', '')
        password = ICP.get_param('wc_base_connector.wc_password', '')
        if not all([url, username, password]):
            return self._wc_notify(_("Please fill in WooCommerce connection information first"), 'danger')
        try:
            resp = requests.get(
                f"{url.rstrip('/')}/wp-json/wc/v3/orders",
                auth=(username, password.replace(' ', '')),
                params={'per_page': 1}, timeout=15,
            )
            if resp.status_code == 200:
                total = resp.headers.get('X-WP-Total', '?')
                return self._wc_notify(_("Connection successful! WooCommerce has %s orders", total), 'success')
            else:
                return self._wc_notify(_("Connection failed: HTTP %s", resp.status_code), 'danger')
        except Exception:
            # The exception text can name hosts and internals; keep it in the server log.
            _logger.warning("WooCommerce connection test failed", exc_info=True)
            return self._wc_notify(_("Connection error. See the server log for details."), 'danger')

    def _wc_notify(self, message, notif_type='info'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': 'WooCommerce', 'message': message, 'type': notif_type, 'sticky': False},
        }
