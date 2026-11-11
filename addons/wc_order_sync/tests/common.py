# -*- coding: utf-8 -*-
"""Shared fixtures for the wc_security tests.

Black-box: nothing here relies on names introduced by the security fix, so the
tests can also be run against the unfixed module (where they are expected to fail).
Every value is fake; no test reaches the network (requests.get is always mocked).
"""
import base64
import hashlib
import hmac
from unittest.mock import MagicMock

from odoo import Command

WEBHOOK_SECRET_PARAM = 'wc_order_sync.wc_webhook_secret'
FAKE_WEBHOOK_SECRET = 'fake-webhook-secret-123'
FAKE_PARAMS = {
    'wc_base_connector.wc_url': 'https://fake-shop.wcsec.test',
    'wc_base_connector.wc_username': 'fake-user-123',
    'wc_base_connector.wc_password': 'fake-secret-123',
    WEBHOOK_SECRET_PARAM: FAKE_WEBHOOK_SECRET,
}
SECRET_FIELDS = ['wc_url', 'wc_username', 'wc_password', 'wc_webhook_secret']


def create_user(env, login, groups):
    return env['res.users'].with_context(no_reset_password=True).create({
        'name': login,
        'login': login,
        'email': '%s@wcsec.test' % login,
        'password': login,
        'groups_id': [Command.set([env.ref(xmlid).id for xmlid in groups])],
    })


def create_users(cls, prefix):
    """Users of every access level the tests compare, as attributes of ``cls``."""
    env = cls.env
    cls.user_portal = create_user(env, prefix + '_portal', ['base.group_portal'])
    cls.user_internal = create_user(env, prefix + '_internal', ['base.group_user'])
    cls.user_wc = create_user(env, prefix + '_wc_user', ['wc_base_connector.group_wc_user'])
    cls.user_wc_manager = create_user(env, prefix + '_wc_manager', ['wc_base_connector.group_wc_manager'])
    cls.user_system = create_user(env, prefix + '_system', ['base.group_system'])
    # No WooCommerce role at all, or only the read-only WooCommerce User role.
    cls.unprivileged = cls.user_portal | cls.user_internal | cls.user_wc


def store_fake_params(env):
    ICP = env['ir.config_parameter'].sudo()
    for key, value in FAKE_PARAMS.items():
        ICP.set_param(key, value)


def fake_wc_response(orders=(), total_pages=1, status_code=200):
    """A stand-in for the requests.Response of GET /wp-json/wc/v3/orders."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {'X-WP-TotalPages': str(total_pages), 'X-WP-Total': str(len(orders))}
    resp.json.return_value = list(orders)
    resp.text = ''
    return resp


def wc_signature(secret, raw_body):
    """X-WC-Webhook-Signature as WooCommerce computes it."""
    return base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()).decode()
