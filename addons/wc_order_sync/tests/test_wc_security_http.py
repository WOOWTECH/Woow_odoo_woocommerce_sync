# -*- coding: utf-8 -*-
"""The same protections, exercised through real HTTP requests.

- /web/dataset/call_kw as a logged-in portal or internal user (what any account
  on the site can do from a browser console);
- /wc_sync/webhook without any session, as WooCommerce (or anyone else) calls it.
"""
import json
from unittest.mock import patch

import requests

from odoo.tests import HttpCase, tagged

from .common import (FAKE_PARAMS, FAKE_WEBHOOK_SECRET, SECRET_FIELDS, WEBHOOK_SECRET_PARAM,
                     create_users, store_fake_params, wc_signature)

ACCESS_ERROR = 'odoo.exceptions.AccessError'


@tagged('post_install', '-at_install', 'wc_security')
class TestWcSecurityHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        create_users(cls, 'wcsec_http')
        store_fake_params(cls.env)
        cls.service = cls.env['product.product'].create({
            'name': 'WCSEC HTTP service product', 'type': 'service', 'default_code': 'WCSEC-HTTP-SRV'})

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _jsonrpc(self, route, params):
        payload = {'jsonrpc': '2.0', 'method': 'call', 'id': 1, 'params': params}
        response = self.url_open(route, data=json.dumps(payload),
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _call_kw(self, model, method, args, kwargs=None, route=None):
        route = route or '/web/dataset/call_kw/%s/%s' % (model, method)
        return self._jsonrpc(route, {'model': model, 'method': method, 'args': args, 'kwargs': kwargs or {}})

    def _assert_access_error(self, rpc_response):
        error = rpc_response.get('error') or {}
        self.assertEqual((error.get('data') or {}).get('name'), ACCESS_ERROR, rpc_response)

    def _set_secret(self, value):
        self.env['ir.config_parameter'].sudo().set_param(WEBHOOK_SECRET_PARAM, value)

    def _queued(self, wc_order_id):
        return self.env['wc.sync.queue'].sudo().search([('wc_order_id', '=', wc_order_id)])

    def _order_body(self, wc_order_id, **extra):
        order = {'id': wc_order_id, 'number': str(wc_order_id), 'status': 'processing', 'total': '100',
                 'date_created': '2026-11-11T10:00:00',
                 'billing': {'first_name': '小明', 'last_name': '王', 'email': 'buyer@wcsec.test'},
                 'line_items': []}
        order.update(extra)
        return json.dumps(order, ensure_ascii=False).encode('utf-8')

    def _post_webhook(self, raw_body, signature=None, extra_headers=None):
        headers = {'Content-Type': 'application/json', 'X-WC-Webhook-Topic': 'order.created',
                   'X-WC-Webhook-ID': '9', 'X-WC-Webhook-Delivery-ID': '1', **(extra_headers or {})}
        if signature is not None:
            headers['X-WC-Webhook-Signature'] = signature
        return self.url_open('/wc_sync/webhook', data=raw_body, headers=headers, timeout=60)

    # ------------------------------------------------------------------
    # /web/dataset/call_kw
    # ------------------------------------------------------------------

    def test_call_kw_default_get_returns_no_secrets(self):
        for user in (self.user_portal, self.user_internal, self.user_wc):
            self._login(user)
            for route in ('/web/dataset/call_kw', '/web/dataset/call_kw/res.config.settings/default_get'):
                with self.subTest(user=user.login, route=route):
                    response = self._call_kw('res.config.settings', 'default_get', [SECRET_FIELDS], route=route)
                    text = json.dumps(response)
                    for secret in FAKE_PARAMS.values():
                        self.assertNotIn(secret, text)

    def test_call_kw_maintenance_method_refused(self):
        for user in (self.user_portal, self.user_internal, self.user_wc):
            self._login(user)
            with self.subTest(user=user.login):
                response = self._call_kw('wc.sync.queue', 'bulk_retype_service_to_storable', [],
                                         {'product_ids': [self.service.id], 'dry_run': False})
                self._assert_access_error(response)
                response = self._call_kw('wc.sync.queue', 'wc_repair_channels', [], {'dry_run': True})
                self._assert_access_error(response)
        self.service.invalidate_recordset()
        self.assertEqual(self.service.type, 'service')

    def test_call_kw_settings_buttons_refused(self):
        with patch('requests.get') as get, \
                patch.object(type(self.env['wc.sync.queue']), '_cron_process_queue') as process:
            for user in (self.user_portal, self.user_internal, self.user_wc_manager):
                self._login(user)
                with self.subTest(user=user.login):
                    for method in ('action_test_wc_connection', 'action_manual_sync'):
                        self._assert_access_error(self._call_kw('res.config.settings', method, [[]]))
                        self._assert_access_error(self._call_kw(
                            'res.config.settings', method, [[]],
                            route='/web/dataset/call_button/res.config.settings/%s' % method))
            get.assert_not_called()
            process.assert_not_called()

    def test_call_kw_import_wizard_refused(self):
        with patch('requests.get') as get:
            for user in (self.user_portal, self.user_internal, self.user_wc):
                self._login(user)
                with self.subTest(user=user.login):
                    self._assert_access_error(self._call_kw('wc.import.wizard', 'action_import', [[]]))
            get.assert_not_called()

    # ------------------------------------------------------------------
    # /wc_sync/webhook
    # ------------------------------------------------------------------

    def test_webhook_rejected_without_configured_secret(self):
        self._set_secret(False)
        body = self._order_body(990001)
        for signature in (None, '', wc_signature('', body), wc_signature(FAKE_WEBHOOK_SECRET, body)):
            with self.subTest(signature=signature):
                response = self._post_webhook(body, signature)
                self.assertGreaterEqual(response.status_code, 400)
        self.assertFalse(self._queued(990001))

    def test_webhook_rejects_wrong_or_missing_signature(self):
        self._set_secret(FAKE_WEBHOOK_SECRET)
        body = self._order_body(990002)
        for signature in (None, '', 'not-a-signature', wc_signature('fake-old-secret-456', body)):
            with self.subTest(signature=signature):
                response = self._post_webhook(body, signature)
                self.assertEqual(response.status_code, 401)
        self.assertFalse(self._queued(990002))

    def test_webhook_signature_covers_the_exact_body(self):
        self._set_secret(FAKE_WEBHOOK_SECRET)
        body = self._order_body(990003)
        tampered = self._order_body(990003, total='1')
        response = self._post_webhook(tampered, wc_signature(FAKE_WEBHOOK_SECRET, body))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self._queued(990003))

    def test_webhook_queues_correctly_signed_order(self):
        self._set_secret(FAKE_WEBHOOK_SECRET)
        body = self._order_body(990004)
        response = self._post_webhook(body, wc_signature(FAKE_WEBHOOK_SECRET, body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get('status'), 'queued')
        item = self._queued(990004)
        self.assertEqual(len(item), 1)
        self.assertEqual(item.state, 'pending')
        self.assertEqual(json.loads(item.payload)['billing']['first_name'], '小明')

        # The same delivery again is a duplicate, not a second row.
        response = self._post_webhook(body, wc_signature(FAKE_WEBHOOK_SECRET, body))
        self.assertEqual(response.json().get('status'), 'duplicate')
        self.assertEqual(len(self._queued(990004)), 1)

    def test_webhook_rejects_oversized_body(self):
        self._set_secret(FAKE_WEBHOOK_SECRET)
        body = self._order_body(990005, customer_note='x' * (3 * 1024 * 1024))
        try:
            # Connection: close, so a body the server refuses to read is never parsed
            # as the next request on a kept-alive connection.
            response = self._post_webhook(body, wc_signature(FAKE_WEBHOOK_SECRET, body),
                                          extra_headers={'Connection': 'close'})
        except requests.exceptions.RequestException:
            response = None  # the server may close the connection without reading the body
        if response is not None:
            self.assertGreaterEqual(response.status_code, 400)
        self.assertFalse(self._queued(990005))
