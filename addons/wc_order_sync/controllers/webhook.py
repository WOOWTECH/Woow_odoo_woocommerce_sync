# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WcWebhookController(http.Controller):

    @http.route('/wc_sync/webhook', type='http', auth='none',
                methods=['POST'], csrf=False, save_session=False)
    def receive_webhook(self, **_kwargs):
        """Receive WooCommerce webhook and queue for processing.

        Uses type='http' (not 'json') because WooCommerce sends raw JSON,
        not JSON-RPC 2.0 envelopes that Odoo's type='json' expects.
        """
        def _resp(payload, status=200):
            return request.make_response(
                json.dumps(payload),
                headers=[('Content-Type', 'application/json')],
                status=status,
            )
        try:
            body = request.httprequest.get_data(as_text=True)
            headers = request.httprequest.headers

            # Verify signature if secret is configured
            secret = request.env['ir.config_parameter'].sudo().get_param(
                'wc_order_sync.wc_webhook_secret', '')
            if secret:
                signature = headers.get('X-WC-Webhook-Signature', '')
                expected = hmac.new(
                    secret.encode('utf-8'),
                    body.encode('utf-8'),
                    hashlib.sha256,
                ).digest()
                import base64
                expected_b64 = base64.b64encode(expected).decode('utf-8')
                if not hmac.compare_digest(signature, expected_b64):
                    # Name the webhook, so a stale duplicate configured in WooCommerce
                    # with an old secret can be found and removed there.
                    _logger.warning("WC Webhook: Invalid signature (webhook id=%s, topic=%s, source=%s, delivery=%s)",
                                    headers.get('X-WC-Webhook-ID', '?'), headers.get('X-WC-Webhook-Topic', '?'),
                                    headers.get('X-WC-Webhook-Source', '?'), headers.get('X-WC-Webhook-Delivery-ID', '?'))
                    return _resp({'status': 'error', 'message': 'invalid signature'}, 401)

            data = json.loads(body)

            # Skip ping/test webhooks
            topic = headers.get('X-WC-Webhook-Topic', '')
            if not data.get('id') or topic == 'action.woocommerce_webhook_delivery':
                _logger.info("WC Webhook: Ping received, topic=%s", topic)
                return _resp({'status': 'ok', 'message': 'ping acknowledged'})

            wc_order_id = data.get('id')
            wc_status = data.get('status', '')

            # Only process completed/processing/on-hold orders
            if wc_status not in ('completed', 'processing', 'on-hold', ''):
                _logger.info("WC Webhook: Skipping order #%s status=%s",
                             wc_order_id, wc_status)
                return _resp({'status': 'skipped', 'message': f'status {wc_status}'})

            # Check if already queued
            Queue = request.env['wc.sync.queue'].sudo()
            existing = Queue.search([
                ('wc_order_id', '=', wc_order_id),
                ('state', 'in', ('pending', 'processing', 'done')),
            ], limit=1)
            if existing:
                _logger.info("WC Webhook: Order #%s already in queue (%s)",
                             wc_order_id, existing.state)
                return _resp({'status': 'duplicate', 'queue_id': existing.id})

            # Create queue item
            queue_item = Queue.create({
                'wc_order_id': wc_order_id,
                'wc_order_number': str(data.get('number', wc_order_id)),
                'payload': body,
                'state': 'pending',
                'wc_total': float(data.get('total', 0)),
                'wc_date': data.get('date_created', ''),
                'wc_status': wc_status,
            })

            _logger.info("WC Webhook: Queued order #%s (queue_id=%d)",
                         wc_order_id, queue_item.id)
            return _resp({'status': 'queued', 'queue_id': queue_item.id})

        except Exception as e:
            _logger.exception("WC Webhook: Error processing webhook")
            return _resp({'status': 'error', 'message': str(e)[:200]}, 500)

    @http.route('/wc_sync/health', type='http', auth='none',
                methods=['GET'], csrf=False, save_session=False)
    def health_check(self, token=None, **_kwargs):
        """Health check for an external monitor: /wc_sync/health?token=<health token>.

        The token is the ir.config_parameter wc_order_sync.health_token (generated
        on first use). Without it the endpoint only says it exists.
        """
        ICP = request.env['ir.config_parameter'].sudo()
        expected = ICP.get_param('wc_order_sync.health_token', '')
        if not expected:
            import secrets
            expected = secrets.token_urlsafe(24)
            ICP.set_param('wc_order_sync.health_token', expected)
        headers = [('Content-Type', 'application/json')]
        if not token or not hmac.compare_digest(str(token), expected):
            return request.make_response(json.dumps({'status': 'forbidden'}), headers=headers, status=403)
        health = request.env['wc.sync.queue'].sudo()._wc_health()
        max_fetch = float(ICP.get_param('wc_order_sync.watchdog_fetch_hours', '2'))
        max_order = float(ICP.get_param('wc_order_sync.watchdog_order_hours', '24'))
        healthy = (health['fetch_age_hours'] is not None and health['fetch_age_hours'] <= max_fetch
                   and (health['last_order_age_hours'] is None or health['last_order_age_hours'] <= max_order)
                   and not health['errors'])
        return request.make_response(json.dumps(dict(health, status='ok' if healthy else 'degraded')),
                                     headers=headers, status=200 if healthy else 503)
