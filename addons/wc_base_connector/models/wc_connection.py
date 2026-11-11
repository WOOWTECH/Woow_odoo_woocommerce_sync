# -*- coding: utf-8 -*-
from odoo import _, api, models
from odoo.exceptions import AccessError


class WcConnectionMixin(models.AbstractModel):
    _name = 'wc.connection.mixin'
    _description = 'WooCommerce Connection Helper'

    def _get_wc_params(self):
        ICP = self.env['ir.config_parameter'].sudo()
        return {
            'url': ICP.get_param('wc_base_connector.wc_url', ''),
            'username': ICP.get_param('wc_base_connector.wc_username', ''),
            'password': ICP.get_param('wc_base_connector.wc_password', ''),
        }

    def _get_wc_auth(self):
        p = self._get_wc_params()
        return p['url'], (p['username'], p['password'].replace(' ', ''))

    @api.model
    def _wc_check_access(self, allow_wc_manager=True):
        """Refuse a privileged WooCommerce operation to anyone not entitled to it.

        Every such operation reads the store credentials or writes orders,
        products and stock through sudo(), so it must check its caller itself:
        menu, view and ACL restrictions do not apply to an RPC call on an empty
        recordset or to an @api.model method.

        Allowed: superuser mode (Odoo shell, crons running as OdooBot, code that
        already called sudo()), Settings administrators (base.group_system) and,
        unless ``allow_wc_manager`` is False, WooCommerce managers.

        Call it on the caller's environment, never on a sudo() one, e.g.
        ``self.env['wc.connection.mixin']._wc_check_access()``.
        """
        env = self.env
        if env.su or env.is_system():
            return True
        if allow_wc_manager and env.user.has_group('wc_base_connector.group_wc_manager'):
            return True
        if allow_wc_manager:
            raise AccessError(_("Only WooCommerce managers and Settings administrators "
                                "can run this WooCommerce operation."))
        raise AccessError(_("Only Settings administrators can run this WooCommerce operation."))
