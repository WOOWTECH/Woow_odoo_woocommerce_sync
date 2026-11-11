# -*- coding: utf-8 -*-
"""Who may read the WooCommerce credentials and test the connection.

Black-box: only public behaviour and names that already existed before the
security fix are used, so the file can also be run against the unfixed module
to show the problems (it is expected to fail there).
"""
from unittest.mock import MagicMock, patch

import requests

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

FAKE_PARAMS = {
    'wc_base_connector.wc_url': 'https://fake-shop.wcsec.test',
    'wc_base_connector.wc_username': 'fake-user-123',
    'wc_base_connector.wc_password': 'fake-secret-123',
}
CREDENTIAL_FIELDS = ['wc_url', 'wc_username', 'wc_password']


def _create_user(env, login, groups):
    return env['res.users'].with_context(no_reset_password=True).create({
        'name': login,
        'login': login,
        'email': '%s@wcsec.test' % login,
        'password': login,
        'groups_id': [Command.set([env.ref(xmlid).id for xmlid in groups])],
    })


def _fake_response(status_code=200, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = ''
    return resp


@tagged('post_install', '-at_install', 'wc_security')
class TestWcBaseConnectorSecurity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_internal = _create_user(cls.env, 'wcsec_base_internal', ['base.group_user'])
        cls.user_wc = _create_user(cls.env, 'wcsec_base_wc_user', ['wc_base_connector.group_wc_user'])
        cls.user_wc_manager = _create_user(cls.env, 'wcsec_base_wc_manager',
                                           ['wc_base_connector.group_wc_manager'])
        cls.user_portal = _create_user(cls.env, 'wcsec_base_portal', ['base.group_portal'])
        cls.user_system = _create_user(cls.env, 'wcsec_base_system', ['base.group_system'])
        cls.not_system = cls.user_portal | cls.user_internal | cls.user_wc | cls.user_wc_manager
        ICP = cls.env['ir.config_parameter'].sudo()
        for key, value in FAKE_PARAMS.items():
            ICP.set_param(key, value)

    def _settings(self, user):
        return self.env['res.config.settings'].with_user(user)

    def _read_as(self, user, method, *args):
        """What ``user`` gets back from a settings read; refusing the read is fine too."""
        try:
            return getattr(self._settings(user), method)(*args)
        except AccessError:
            return {}

    # ------------------------------------------------------------------
    # credentials in res.config.settings
    # ------------------------------------------------------------------

    def test_default_get_hides_credentials_from_non_administrators(self):
        for user in self.not_system:
            with self.subTest(user=user.login):
                values = self._read_as(user, 'default_get', CREDENTIAL_FIELDS)
                for secret in FAKE_PARAMS.values():
                    self.assertNotIn(secret, values.values())

    def test_get_values_hides_credentials_from_non_administrators(self):
        for user in self.not_system:
            with self.subTest(user=user.login):
                values = self._read_as(user, 'get_values')
                for secret in FAKE_PARAMS.values():
                    self.assertNotIn(secret, values.values())

    def test_default_get_shows_credentials_to_administrators(self):
        for settings in (self._settings(self.user_system), self.env['res.config.settings'].sudo()):
            values = settings.default_get(CREDENTIAL_FIELDS)
            self.assertEqual(values.get('wc_url'), FAKE_PARAMS['wc_base_connector.wc_url'])
            self.assertEqual(values.get('wc_username'), FAKE_PARAMS['wc_base_connector.wc_username'])
            self.assertEqual(values.get('wc_password'), FAKE_PARAMS['wc_base_connector.wc_password'])

    def test_administrator_saving_settings_keeps_credentials(self):
        admin = self.env.ref('base.user_admin')
        settings = self._settings(admin).create({})
        self.assertEqual(settings.wc_password, FAKE_PARAMS['wc_base_connector.wc_password'])
        settings.execute()
        ICP = self.env['ir.config_parameter'].sudo()
        for key, value in FAKE_PARAMS.items():
            self.assertEqual(ICP.get_param(key), value)

    def test_administrator_can_change_credentials(self):
        admin = self.env.ref('base.user_admin')
        self._settings(admin).create({'wc_username': 'fake-user-456'}).execute()
        ICP = self.env['ir.config_parameter'].sudo()
        self.assertEqual(ICP.get_param('wc_base_connector.wc_username'), 'fake-user-456')
        self.assertEqual(ICP.get_param('wc_base_connector.wc_password'),
                         FAKE_PARAMS['wc_base_connector.wc_password'])

    def test_settings_cannot_be_written_by_non_administrators(self):
        # Core set_values() clears every config parameter through sudo() when called on an
        # empty recordset; a non-administrator must be refused before anything is written.
        IrConfig = type(self.env['ir.config_parameter'])
        with patch.object(IrConfig, 'set_param', autospec=True, side_effect=IrConfig.set_param) as spy:
            for user in self.not_system:
                with self.subTest(user=user.login):
                    with self.assertRaises(AccessError):
                        self._settings(user).browse().set_values()
            spy.assert_not_called()
        ICP = self.env['ir.config_parameter'].sudo()
        for key, value in FAKE_PARAMS.items():
            self.assertEqual(ICP.get_param(key), value)

    # ------------------------------------------------------------------
    # Test Connection button
    # ------------------------------------------------------------------

    def test_test_connection_refused_to_non_administrators(self):
        with patch('requests.get', return_value=_fake_response(200, {'X-WP-Total': '7'})) as get:
            for user in self.not_system:
                with self.subTest(user=user.login):
                    with self.assertRaises(AccessError):
                        self._settings(user).browse().action_test_wc_connection()
            get.assert_not_called()

    def test_test_connection_works_for_administrators(self):
        with patch('requests.get', return_value=_fake_response(200, {'X-WP-Total': '42'})) as get:
            action = self._settings(self.user_system).browse().action_test_wc_connection()
        get.assert_called_once()
        self.assertEqual(action['params']['type'], 'success')
        self.assertIn('42', action['params']['message'])

    def test_test_connection_does_not_return_exception_text(self):
        error = requests.ConnectionError('fake-internal-host.wcsec.test refused the connection')
        with patch('requests.get', side_effect=error):
            action = self._settings(self.user_system).browse().action_test_wc_connection()
        self.assertEqual(action['params']['type'], 'danger')
        self.assertNotIn('fake-internal-host', action['params']['message'])
