# -*- coding: utf-8 -*-
import base64
import hashlib
import hmac
import json
import logging

from werkzeug.exceptions import RequestEntityTooLarge

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# A WooCommerce order payload is a few KiB; anything near this is not an order.
WEBHOOK_MAX_CONTENT_LENGTH = 2 * 1024 * 1024


def _webhook_signature(secret, raw_body):
    """WooCommerce's X-WC-Webhook-Signature: base64(HMAC-SHA256(secret, raw body bytes))."""
    digest = hmac.new(secret.encode('utf-8'), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest)


class WcWebhookController(http.Controller):

    @http.route('/wc_sync/webhook', type='http', auth='none',
                methods=['POST'], csrf=False, save_session=False,
                max_content_length=WEBHOOK_MAX_CONTENT_LENGTH)
    def receive_webhook(self, **_kwargs):
        """Receive WooCommerce webhook and queue for processing.

        Uses type='http' (not 'json') because WooCommerce sends raw JSON,
        not JSON-RPC 2.0 envelopes that Odoo's type='json' expects.

        Fails closed: without a configured webhook secret every delivery is
        rejected, and a delivery is only queued when its signature matches the
        HMAC of the exact request body bytes.
        """
        def _resp(payload, status=200):
            return request.make_response(
                json.dumps(payload),
                headers=[('Content-Type', 'application/json')],
                status=status,
            )
        headers = request.httprequest.headers

        def _describe():
            # Name the webhook, so a stale duplicate configured in WooCommerce
            # with an old secret can be found and removed there.
            return "webhook id=%s, topic=%s, source=%s, delivery=%s" % (
                headers.get('X-WC-Webhook-ID', '?'), headers.get('X-WC-Webhook-Topic', '?'),
                headers.get('X-WC-Webhook-Source', '?'), headers.get('X-WC-Webhook-Delivery-ID', '?'))

        def _too_large():
            _logger.warning("WC Webhook: Rejected, body larger than %d bytes (%s)",
                            WEBHOOK_MAX_CONTENT_LENGTH, _describe())
            return _resp({'status': 'error', 'message': 'payload too large'}, 413)

        try:
            # The route's max_content_length is enforced by werkzeug when the body is
            # read; the explicit checks also cover werkzeug versions that do not.
            if (request.httprequest.content_length or 0) > WEBHOOK_MAX_CONTENT_LENGTH:
                return _too_large()
            try:
                raw_body = request.httprequest.get_data()
            except RequestEntityTooLarge:
                return _too_large()
            if len(raw_body) > WEBHOOK_MAX_CONTENT_LENGTH:
                return _too_large()

            secret = request.env['ir.config_parameter'].sudo().get_param(
                'wc_order_sync.wc_webhook_secret', '')
            if not secret:
                _logger.warning("WC Webhook: Rejected, no webhook secret is configured in Odoo "
                                "(Settings > WooCommerce > Webhook Secret) (%s)", _describe())
                return _resp({'status': 'error', 'message': 'webhook secret not configured'}, 503)

            signature = headers.get('X-WC-Webhook-Signature', '').strip().encode('utf-8')
            if not hmac.compare_digest(signature, _webhook_signature(secret, raw_body)):
                _logger.warning("WC Webhook: Invalid signature (%s)", _describe())
                return _resp({'status': 'error', 'message': 'invalid signature'}, 401)

            body = raw_body.decode('utf-8', errors='replace')
            data = json.loads(body)
            if not isinstance(data, dict):
                return _resp({'status': 'error', 'message': 'unexpected payload'}, 400)

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

        except Exception:
            _logger.exception("WC Webhook: Error processing webhook")
            return _resp({'status': 'error', 'message': 'internal error'}, 500)

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
        if not token or not hmac.compare_digest(str(token).encode('utf-8'), expected.encode('utf-8')):
            return request.make_response(json.dumps({'status': 'forbidden'}), headers=headers, status=403)
        health = request.env['wc.sync.queue'].sudo()._wc_health()
        max_fetch = float(ICP.get_param('wc_order_sync.watchdog_fetch_hours', '2'))
        max_order = float(ICP.get_param('wc_order_sync.watchdog_order_hours', '24'))
        healthy = (health['fetch_age_hours'] is not None and health['fetch_age_hours'] <= max_fetch
                   and (health['last_order_age_hours'] is None or health['last_order_age_hours'] <= max_order)
                   and not health['errors'])
        return request.make_response(json.dumps(dict(health, status='ok' if healthy else 'degraded')),
                                     headers=headers, status=200 if healthy else 503)
