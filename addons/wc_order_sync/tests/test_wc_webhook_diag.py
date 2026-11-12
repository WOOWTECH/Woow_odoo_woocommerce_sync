# -*- coding: utf-8 -*-
"""A rejected webhook delivery is logged with enough to find its sender, and nothing more.

Header names identify the plugin or code that sends it. Apart from the four WooCommerce
webhook identifiers and the media type, header values (signatures, cookies, tokens) and the
body (customer data) must never reach the log, and logged request values are capped.
"""
import json

from odoo.tests import HttpCase, tagged

from .common import FAKE_WEBHOOK_SECRET, WEBHOOK_SECRET_PARAM

LOGGER = 'odoo.addons.wc_order_sync.controllers.webhook'


@tagged('post_install', '-at_install', 'wc_security', 'wc_webhook_diag')
class TestWcWebhookDiagnostics(HttpCase):
    # See TestWcSecurityHttp: the HttpCase cursor cannot retry auth='none' routes read/write.
    readonly_enabled = False

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(WEBHOOK_SECRET_PARAM, FAKE_WEBHOOK_SECRET)

    def _rejected(self, **kwargs):
        with self.assertLogs(LOGGER, level='WARNING') as logs:
            response = self.url_open('/wc_sync/webhook', timeout=60, **kwargs)
        self.assertEqual(response.status_code, 401)
        text = '\n'.join(logs.output)
        self.assertIn('Invalid signature', text)
        return text

    def test_rejected_json_delivery_logs_header_names_not_values(self):
        body = json.dumps({'id': 990101, 'status': 'processing',
                           'billing': {'email': 'diag-body@wcsec.test'}}).encode('utf-8')
        text = self._rejected(data=body, headers={
            'Content-Type': 'application/json; charset=utf-8',
            'X-Diag-Plugin-Probe': 'diag-header-value-7c1e',
            'X-WC-Webhook-Signature': 'not-a-valid-signature',
            'X-WC-Webhook-Source': 'https://shop.wcsec.test/' + 'a' * 200,
        })
        self.assertIn('x-diag-plugin-probe', text)
        self.assertIn('x-wc-webhook-signature', text)
        self.assertIn('headers(', text)
        self.assertIn('content_type=application/json, declared_bytes=%d, read_bytes=%d)'
                      % (len(body), len(body)), text)
        self.assertNotIn('a' * 100, text)  # logged header values are capped
        for value in ('diag-header-value-7c1e', 'not-a-valid-signature', 'diag-body@wcsec.test',
                      FAKE_WEBHOOK_SECRET):
            self.assertNotIn(value, text)
        self.assertFalse(self.env['wc.sync.queue'].sudo().search([('wc_order_id', '=', 990101)]))

    def test_rejected_form_delivery_logs_declared_size(self):
        # WordPress posts array bodies form-encoded; Odoo parses the form before the
        # controller runs, so only the declared length tells the size.
        text = self._rejected(data={'webhook_id': '7735'})
        self.assertIn('content_type=application/x-www-form-urlencoded, declared_bytes=%d, read_bytes=0)'
                      % len('webhook_id=7735'), text)
        self.assertIn('webhook id=?, topic=?, source=?, delivery=?', text)
        self.assertNotIn('7735', text)
