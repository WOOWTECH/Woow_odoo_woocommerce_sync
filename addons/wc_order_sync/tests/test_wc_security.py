# -*- coding: utf-8 -*-
"""Privileged WooCommerce operations refuse callers without the right role.

Each guarded operation runs through sudo() internally, so menu, view and ACL
restrictions do not protect it; these tests call them the way an RPC caller
would (as the user, on an empty recordset or as @api.model) and check both the
AccessError and that nothing was changed.
"""
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged

from .common import (FAKE_PARAMS, FAKE_WEBHOOK_SECRET, SECRET_FIELDS, create_users,
                     fake_wc_response, store_fake_params)

CUTOFF_AFTER_ORDER = '2021-01-01 00:00:00'


@tagged('post_install', '-at_install', 'wc_security')
class TestWcOrderSyncSecurity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        create_users(cls, 'wcsec_rpc')
        store_fake_params(cls.env)
        cls.Queue = cls.env['wc.sync.queue']
        cls.Settings = cls.env['res.config.settings']
        cls.Wizard = cls.env['wc.import.wizard']

        Product = cls.env['product.product']
        cls.service = Product.create({
            'name': 'WCSEC service product', 'type': 'service', 'default_code': 'WCSEC-SRV'})
        goods = Product.create({
            'name': 'WCSEC goods', 'type': 'consu', 'is_storable': True, 'default_code': 'WCSEC-GDS'})
        customer = cls.env['res.partner'].create({'name': 'WCSEC customer'})
        order = cls.env['sale.order'].create({
            'partner_id': customer.id,
            'wc_order_id': 770001,
            'wc_order_status': 'completed',
            'order_line': [Command.create({'product_id': goods.id, 'product_uom_qty': 1, 'price_unit': 10})],
        })
        order.action_confirm()
        order.write({'date_order': '2020-01-01 00:00:00'})
        cls.picking = order.picking_ids

    def _maintenance_calls(self):
        """Every one-off maintenance method, with arguments that would write."""
        return [
            ('wc_repair_customers', (), {'dry_run': False}),
            ('wc_repair_shipping', (), {'dry_run': False}),
            ('wc_repair_channels', (), {'dry_run': False}),
            ('wc_create_bundle_products', (), {'dry_run': False}),
            ('wc_repair_product_maps', (), {'dry_run': False}),
            ('wc_close_deliveries_before_count', (CUTOFF_AFTER_ORDER,), {'dry_run': False}),
            ('wc_create_bundle_boms', (), {'dry_run': False}),
            ('bulk_retype_service_to_storable', (), {'product_ids': [self.service.id], 'dry_run': False}),
        ]

    def _assert_untouched(self):
        self.env.invalidate_all()
        self.assertEqual(self.service.type, 'service')
        self.assertTrue(self.picking)
        self.assertNotIn('cancel', self.picking.mapped('state'))

    # ------------------------------------------------------------------
    # maintenance methods
    # ------------------------------------------------------------------

    def test_maintenance_refused_to_unprivileged_users(self):
        for user in self.unprivileged:
            for name, args, kwargs in self._maintenance_calls():
                with self.subTest(user=user.login, method=name):
                    with self.assertRaises(AccessError):
                        getattr(self.Queue.with_user(user), name)(*args, **kwargs)
        self._assert_untouched()

    def test_maintenance_dry_runs_allowed_for_admin_manager_and_superuser(self):
        for queue in (self.Queue.with_user(self.user_system), self.Queue.with_user(self.user_wc_manager),
                      self.Queue.sudo()):
            for name, args, kwargs in self._maintenance_calls():
                with self.subTest(user=queue.env.user.login, method=name):
                    result = getattr(queue, name)(*args, **dict(kwargs, dry_run=True))
                    self.assertIsInstance(result, dict)
        self._assert_untouched()

    def test_maintenance_real_runs_for_admin(self):
        queue = self.Queue.with_user(self.user_system)
        report = queue.wc_close_deliveries_before_count(CUTOFF_AFTER_ORDER, dry_run=False)
        self.assertGreaterEqual(report['deliveries'], 1)
        report = queue.bulk_retype_service_to_storable(product_ids=[self.service.id], dry_run=False)
        self.assertEqual(report['safe'], [self.service.id])
        self.env.invalidate_all()
        self.assertEqual(set(self.picking.mapped('state')), {'cancel'})
        self.assertEqual(self.service.type, 'consu')

    # ------------------------------------------------------------------
    # manual sync (queue model and Settings button)
    # ------------------------------------------------------------------

    def test_queue_manual_sync_refused_to_unprivileged_users(self):
        QueueClass = type(self.Queue)
        with patch.object(QueueClass, '_fetch_new_wc_orders', return_value=0) as fetch, \
                patch.object(QueueClass, '_cron_process_queue') as process:
            for user in self.unprivileged:
                with self.subTest(user=user.login):
                    with self.assertRaises(AccessError):
                        self.Queue.with_user(user).action_manual_sync()
            fetch.assert_not_called()
            process.assert_not_called()

            action = self.Queue.with_user(self.user_system).action_manual_sync()
            self.assertEqual(action['params']['type'], 'success')
            fetch.assert_called_once()
            process.assert_called_once()

    def test_settings_manual_sync_only_for_settings_administrators(self):
        with patch.object(type(self.Queue), '_cron_process_queue') as process:
            for user in self.unprivileged | self.user_wc_manager:
                with self.subTest(user=user.login):
                    with self.assertRaises(AccessError):
                        self.Settings.with_user(user).browse().action_manual_sync()
            process.assert_not_called()

            action = self.Settings.with_user(self.user_system).browse().action_manual_sync()
            self.assertEqual(action['params']['type'], 'success')
            process.assert_called_once()

    # ------------------------------------------------------------------
    # historical import wizard
    # ------------------------------------------------------------------

    def _wc_order(self, wc_id):
        return {'id': wc_id, 'number': str(wc_id), 'status': 'completed', 'total': '10',
                'date_created': '2026-01-01T00:00:00', 'billing': {}, 'line_items': []}

    def test_import_refused_to_unprivileged_users(self):
        response = fake_wc_response([self._wc_order(880001)])
        with patch('requests.get', return_value=response) as get:
            for user in self.unprivileged:
                with self.subTest(user=user.login):
                    with self.assertRaises(AccessError):
                        self.Wizard.with_user(user).browse().action_import()
            get.assert_not_called()
        self.assertFalse(self.Queue.sudo().search([('wc_order_id', '=', 880001)]))

    def test_import_needs_a_wizard_record(self):
        with patch('requests.get', return_value=fake_wc_response([self._wc_order(880002)])) as get:
            with self.assertRaises(ValueError):
                self.Wizard.with_user(self.user_wc_manager).browse().action_import()
            get.assert_not_called()
        self.assertFalse(self.Queue.sudo().search([('wc_order_id', '=', 880002)]))

    def test_import_works_for_wc_manager(self):
        wizard = self.Wizard.with_user(self.user_wc_manager).create({'import_limit': 1})
        with patch('requests.get', return_value=fake_wc_response([self._wc_order(880003)])) as get:
            wizard.action_import()
        get.assert_called_once()
        self.assertEqual(wizard.imported_count, 1)
        self.assertTrue(self.Queue.sudo().search([('wc_order_id', '=', 880003)]))

    # ------------------------------------------------------------------
    # secrets in res.config.settings
    # ------------------------------------------------------------------

    def _read_settings_as(self, user, method, *args):
        try:
            return getattr(self.Settings.with_user(user), method)(*args)
        except AccessError:
            return {}

    def test_settings_secrets_hidden_from_non_administrators(self):
        for user in self.unprivileged | self.user_wc_manager:
            with self.subTest(user=user.login):
                for values in (self._read_settings_as(user, 'default_get', SECRET_FIELDS),
                               self._read_settings_as(user, 'get_values')):
                    for secret in FAKE_PARAMS.values():
                        self.assertNotIn(secret, values.values())

    def test_settings_form_onchange_hides_secrets(self):
        """The Settings form loads through onchange(), which calls default_get()."""
        spec = {name: {} for name in SECRET_FIELDS}
        for user in self.unprivileged | self.user_wc_manager:
            with self.subTest(user=user.login):
                try:
                    result = self.Settings.with_user(user).onchange({}, [], spec)
                except AccessError:
                    result = {}
                for secret in FAKE_PARAMS.values():
                    self.assertNotIn(secret, repr(result))

    def test_settings_secrets_visible_to_administrators(self):
        values = self.Settings.with_user(self.user_system).default_get(SECRET_FIELDS)
        self.assertEqual(values.get('wc_webhook_secret'), FAKE_WEBHOOK_SECRET)
        self.assertEqual(values.get('wc_password'), FAKE_PARAMS['wc_base_connector.wc_password'])
        result = self.Settings.with_user(self.env.ref('base.user_admin')).onchange(
            {}, [], {name: {} for name in SECRET_FIELDS})
        self.assertEqual(result['value'].get('wc_webhook_secret'), FAKE_WEBHOOK_SECRET)

    def test_administrator_saving_settings_keeps_webhook_secret(self):
        admin = self.env.ref('base.user_admin')
        self.Settings.with_user(admin).create({'wc_auto_confirm': True}).execute()
        ICP = self.env['ir.config_parameter'].sudo()
        for key, value in FAKE_PARAMS.items():
            self.assertEqual(ICP.get_param(key), value)
