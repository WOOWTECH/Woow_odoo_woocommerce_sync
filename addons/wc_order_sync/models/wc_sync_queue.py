# -*- coding: utf-8 -*-
import json
import logging
from datetime import timedelta

import requests
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

STUCK_PROCESSING_MINUTES = 30
BACK_SYNC_PARAM = 'wc_order_sync.last_back_sync'
WC_STATUSES_CANCEL_ODOO = ('cancelled', 'refunded', 'failed')


class WcSyncQueue(models.Model):
    _name = 'wc.sync.queue'
    _description = 'WooCommerce Sync Queue'
    _order = 'create_date desc'

    wc_order_id = fields.Integer(string="WC Order ID", index=True)
    wc_order_number = fields.Char(string="WC Order Number")
    payload = fields.Text(string="JSON Payload")
    state = fields.Selection([
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], default='pending', string="State", index=True)
    error_message = fields.Text(string="Error Message")
    sale_order_id = fields.Many2one('sale.order', string="Sale Order")
    partner_id = fields.Many2one('res.partner', string="Customer")
    attempts = fields.Integer(default=0, string="Attempts")
    wc_total = fields.Float(string="WC Order Amount")
    wc_date = fields.Char(string="WC Order Date")
    wc_status = fields.Char(string="WC Order Status")

    def action_retry(self):
        self.write({'state': 'pending', 'error_message': False, 'attempts': 0})

    @api.model
    def action_manual_sync(self):
        fetched = self._fetch_new_wc_orders()
        self._cron_process_queue()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'WooCommerce Sync',
                'message': f'Fetched {fetched} new orders, sync completed',
                'type': 'success',
                'sticky': False,
            }
        }

    @api.model
    def _fetch_new_wc_orders(self):
        """Fetch new orders from WooCommerce API and enqueue them."""
        try:
            mixin = self.env['wc.connection.mixin'].sudo()
            wc_url, auth = mixin._get_wc_auth()
            if not wc_url or not auth[0]:
                return 0
            # Find the latest synced WC order ID
            latest = self.search([], order='wc_order_id desc', limit=1)
            last_id = latest.wc_order_id if latest else 0
            api_url = f"{wc_url.rstrip('/')}/wp-json/wc/v3/orders"
            fetched = 0
            page = 1
            while True:
                params = {
                    'per_page': 100, 'page': page,
                    'orderby': 'date', 'order': 'asc',
                    'status': 'completed,processing,on-hold',
                }
                if last_id:
                    # Only fetch orders newer than the last synced
                    last_queue = self.search(
                        [('wc_order_id', '=', last_id)], limit=1)
                    if last_queue and last_queue.wc_date:
                        params['after'] = last_queue.wc_date
                resp = requests.get(api_url, auth=auth, params=params,
                                    timeout=30)
                if resp.status_code != 200:
                    break
                orders = resp.json()
                if not orders:
                    break
                for order_data in orders:
                    wc_id = order_data.get('id')
                    if self.search([('wc_order_id', '=', wc_id)], limit=1):
                        continue
                    if self.env['sale.order'].sudo().search(
                            [('wc_order_id', '=', wc_id)], limit=1):
                        continue
                    self.create({
                        'wc_order_id': wc_id,
                        'wc_order_number': str(
                            order_data.get('number', wc_id)),
                        'payload': json.dumps(order_data),
                        'state': 'pending',
                        'wc_total': float(order_data.get('total', 0)),
                        'wc_date': order_data.get('date_created', ''),
                        'wc_status': order_data.get('status', ''),
                    })
                    fetched += 1
                page += 1
                total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
                if page > total_pages:
                    break
            _logger.info("WC Sync: Fetched %d new orders from WooCommerce",
                         fetched)
            return fetched
        except Exception as e:
            _logger.warning("WC Sync: Failed to fetch new orders: %s",
                            str(e)[:200])
            return 0

    @api.model
    def _cron_process_queue(self):
        self._reset_stuck_processing()
        self._fetch_new_wc_orders()
        self._back_sync_wc_status()
        pending = self.search([
            ('state', '=', 'pending'),
            ('attempts', '<', 5),
        ], limit=50, order='create_date asc')
        _logger.info("WC Sync: Processing %d pending queue items", len(pending))
        for item in pending:
            try:
                item.write({'state': 'processing', 'attempts': item.attempts + 1})
                self.env.cr.commit()
                order_data = json.loads(item.payload)
                sale_order = self._process_wc_order(order_data, item)
                item.write({
                    'state': 'done',
                    'sale_order_id': sale_order.id if sale_order else False,
                    'partner_id': sale_order.partner_id.id if sale_order else False,
                    'error_message': False,
                })
                self.env.cr.commit()
            except Exception as e:
                self.env.cr.rollback()
                _logger.exception("WC Sync: Error processing queue item %d", item.id)
                item.write({'state': 'error', 'error_message': str(e)[:500]})
                self.env.cr.commit()

    @api.model
    def _reset_stuck_processing(self):
        """Reset queue items stuck in 'processing' beyond threshold back to 'pending'."""
        threshold = fields.Datetime.now() - timedelta(minutes=STUCK_PROCESSING_MINUTES)
        stuck = self.search([
            ('state', '=', 'processing'),
            ('write_date', '<', threshold),
        ])
        if stuck:
            _logger.warning("WC Sync: Resetting %d stuck 'processing' items", len(stuck))
            stuck.write({'state': 'pending', 'error_message': 'auto-reset from stuck processing'})

    @api.model
    def _back_sync_wc_status(self):
        """Fetch WC orders modified since last back-sync and update existing SO status
        (cancel Odoo SO when WC becomes cancelled/refunded/failed)."""
        try:
            mixin = self.env['wc.connection.mixin'].sudo()
            wc_url, auth = mixin._get_wc_auth()
            if not wc_url or not auth[0]:
                return 0
            ICP = self.env['ir.config_parameter'].sudo()
            SaleOrder = self.env['sale.order'].sudo()
            last_run = ICP.get_param(BACK_SYNC_PARAM, '')
            if not last_run:
                # First run: only look back 7 days to avoid a huge fetch
                last_run = (fields.Datetime.now() - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S')
            api_url = f"{wc_url.rstrip('/')}/wp-json/wc/v3/orders"
            updated = 0
            cancelled = 0
            page = 1
            new_last = fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            while True:
                params = {
                    'per_page': 100, 'page': page,
                    'orderby': 'modified', 'order': 'asc',
                    'modified_after': last_run,
                    # Include all statuses so we can detect cancellations
                    'status': 'any',
                }
                resp = requests.get(api_url, auth=auth, params=params, timeout=30)
                if resp.status_code != 200:
                    _logger.warning("WC Back-sync: HTTP %s on page %d", resp.status_code, page)
                    break
                orders = resp.json()
                if not orders:
                    break
                for order_data in orders:
                    wc_id = order_data.get('id')
                    wc_status = order_data.get('status', '')
                    so = SaleOrder.search([('wc_order_id', '=', wc_id)], limit=1)
                    if not so:
                        continue
                    changed = False
                    if so.wc_order_status != wc_status:
                        so.write({'wc_order_status': wc_status,
                                  'wc_last_synced': fields.Datetime.now()})
                        changed = True
                    if wc_status in WC_STATUSES_CANCEL_ODOO and so.state != 'cancel':
                        try:
                            so._action_cancel() if hasattr(so, '_action_cancel') else so.action_cancel()
                            cancelled += 1
                            _logger.info("WC Back-sync: Cancelled %s (WC #%s status=%s)",
                                         so.name, wc_id, wc_status)
                        except Exception as e:
                            _logger.warning("WC Back-sync: Cannot cancel %s: %s",
                                            so.name, str(e)[:120])
                    if changed:
                        updated += 1
                page += 1
                total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
                if page > total_pages:
                    break
            ICP.set_param(BACK_SYNC_PARAM, new_last)
            _logger.info("WC Back-sync: updated %d SOs (cancelled %d) since %s",
                         updated, cancelled, last_run)
            return updated
        except Exception as e:
            _logger.warning("WC Back-sync: Failed: %s", str(e)[:200])
            return 0

    def _process_wc_order(self, data, queue_item):
        wc_order_id = data.get('id')
        existing = self.env['sale.order'].sudo().search([('wc_order_id', '=', wc_order_id)], limit=1)
        if existing:
            return existing
        wc_date = self._parse_wc_date(data.get('date_created', ''))
        partner = self._find_or_create_partner(data, wc_date)
        order_lines = self._build_order_lines(data)
        order_lines += self._build_shipping_lines(data)
        order_lines += self._build_fee_lines(data)
        coupon_codes = ','.join(c.get('code', '') for c in data.get('coupon_lines', []) if c.get('code'))
        order_vals = {
            'partner_id': partner.id,
            'wc_order_id': wc_order_id,
            'wc_order_status': data.get('status', ''),
            'wc_payment_method': data.get('payment_method_title', ''),
            'wc_shipping_total': float(data.get('shipping_total') or 0),
            'wc_tax_total': float(data.get('total_tax') or 0),
            'wc_discount_total': float(data.get('discount_total') or 0),
            'wc_coupon_codes': coupon_codes or False,
            'wc_last_synced': fields.Datetime.now(),
            'date_order': wc_date,
            'order_line': order_lines,
            'note': self._build_note(data),
        }
        pricelist = self.env['product.pricelist'].sudo().search([('currency_id.name', '=', 'TWD')], limit=1)
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id
        sale_order = self.env['sale.order'].sudo().create(order_vals)
        ICP = self.env['ir.config_parameter'].sudo()
        auto_confirm = ICP.get_param('wc_order_sync.wc_auto_confirm', 'True')
        if auto_confirm in ('True', '1', 'true'):
            try:
                sale_order.action_confirm()
                # Restore the original WC order date (action_confirm resets it)
                sale_order.write({'date_order': wc_date})
            except Exception as e:
                _logger.warning("WC Sync: Auto-confirm failed for %s: %s", sale_order.name, str(e)[:100])
        wc_status = data.get('status', '')
        auto_stock = ICP.get_param('wc_order_sync.wc_auto_stock', 'True')
        if wc_status == 'completed' and auto_stock in ('True', '1', 'true'):
            self._auto_validate_pickings(sale_order)
        return sale_order

    def _auto_validate_pickings(self, sale_order):
        for picking in sale_order.picking_ids:
            if picking.state in ('confirmed', 'assigned', 'waiting'):
                try:
                    for move in picking.move_ids:
                        move.quantity = move.product_uom_qty
                    picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
                except Exception as e:
                    _logger.warning("WC Sync: Auto-validate picking failed for %s: %s", sale_order.name, str(e)[:100])

    def _find_or_create_partner(self, data, wc_date=None):
        Partner = self.env['res.partner'].sudo()
        PartnerMap = self.env['partner.wc.map'].sudo()
        billing = data.get('billing', {})
        email = billing.get('email', '').strip()
        phone = billing.get('phone', '').strip()
        last_name = billing.get('last_name', '').strip()
        first_name = billing.get('first_name', '').strip()
        name = f"{last_name}{first_name}".strip() or email or 'Unknown Customer'
        wc_customer_id = data.get('customer_id', 0)
        order_date = wc_date or fields.Datetime.now()
        if wc_customer_id:
            mapping = PartnerMap.search([('wc_customer_id', '=', wc_customer_id)], limit=1)
            if mapping and mapping.partner_id:
                if not mapping.last_order_date or str(order_date) > str(mapping.last_order_date):
                    mapping.write({'last_order_date': order_date})
                return mapping.partner_id
        if email:
            mapping = PartnerMap.search([('wc_email', '=', email)], limit=1)
            if mapping and mapping.partner_id:
                if not mapping.last_order_date or str(order_date) > str(mapping.last_order_date):
                    mapping.write({'last_order_date': order_date})
                return mapping.partner_id
        partner = False
        if email:
            partner = Partner.search([('email', '=', email)], limit=1)
        if not partner and phone:
            partner = Partner.search(['|', ('phone', '=', phone), ('mobile', '=', phone)], limit=1)
        if not partner and name and name != 'Unknown Customer':
            partner = Partner.search([('name', '=', name), ('customer_rank', '>', 0)], limit=1)
        if not partner:
            country_tw = self.env['res.country'].sudo().search([('code', '=', 'TW')], limit=1)
            customer_tag = self.env['res.partner.category'].sudo().search([('name', '=', 'Customer')], limit=1)
            if not customer_tag:
                customer_tag = self.env['res.partner.category'].sudo().search([('name', 'ilike', 'customer')], limit=1)
            vals = {
                'name': name, 'email': email or False, 'phone': phone or False,
                'customer_rank': 1, 'lang': 'zh_TW', 'tz': 'Asia/Taipei',
                'country_id': country_tw.id if country_tw else False,
            }
            if customer_tag:
                vals['category_id'] = [(4, customer_tag.id)]
            street_parts = [billing.get(k) for k in ('address_1', 'address_2') if billing.get(k)]
            if street_parts:
                vals['street'] = ' '.join(street_parts)
            if billing.get('city'):
                vals['city'] = billing['city']
            if billing.get('postcode'):
                vals['zip'] = billing['postcode']
            partner = Partner.create(vals)
        existing_map = PartnerMap.search([
            '|', ('wc_customer_id', '=', wc_customer_id), ('wc_email', '=', email),
        ], limit=1) if (wc_customer_id or email) else False
        if existing_map:
            existing_map.write({'partner_id': partner.id, 'last_order_date': order_date})
        else:
            PartnerMap.create({
                'wc_customer_name': name, 'wc_customer_id': wc_customer_id,
                'wc_email': email, 'wc_phone': phone, 'partner_id': partner.id,
                'auto_matched': True, 'last_order_date': order_date,
            })
        return partner

    def _build_order_lines(self, data):
        lines = []
        ProductMap = self.env['product.wc.map'].sudo()
        ICP = self.env['ir.config_parameter'].sudo()
        default_product_id = int(ICP.get_param('wc_order_sync.wc_default_product_id', '0'))
        for item in data.get('line_items', []):
            wc_name = item.get('name', '')
            wc_product_id = item.get('product_id', 0)
            qty = item.get('quantity', 1)
            total = float(item.get('total', 0))
            price_unit = total / qty if qty else total
            product = False
            mapping = ProductMap.search([
                '|', ('wc_product_id', '=', wc_product_id), ('wc_product_name', '=', wc_name),
            ], limit=1)
            if mapping and mapping.product_id:
                product = mapping.product_id
            else:
                product = self._fuzzy_match_product(wc_name)
                if product:
                    ProductMap.create({
                        'wc_product_name': wc_name, 'wc_product_id': wc_product_id,
                        'product_id': product.id, 'auto_matched': True,
                    })
            if not product and default_product_id:
                product = self.env['product.product'].sudo().browse(default_product_id)
                if not product.exists():
                    product = False
            if not product:
                product = self.env['product.product'].sudo().create({
                    'name': wc_name[:100],
                    'type': 'consu', 'is_storable': True,
                    'sale_ok': True, 'list_price': price_unit,
                })
                ProductMap.create({
                    'wc_product_name': wc_name, 'wc_product_id': wc_product_id,
                    'product_id': product.id, 'auto_matched': True,
                })
            lines.append((0, 0, {
                'product_id': product.id, 'product_uom_qty': qty,
                'price_unit': price_unit, 'name': wc_name,
            }))
        return lines

    def _build_shipping_lines(self, data):
        lines = []
        for sl in data.get('shipping_lines', []) or []:
            method_title = sl.get('method_title') or sl.get('method_id') or 'Shipping'
            total = float(sl.get('total') or 0)
            if total <= 0:
                continue
            product = self._get_or_create_service_product(
                name=method_title, code='WC-SHIPPING',
                default_name='WooCommerce Shipping')
            lines.append((0, 0, {
                'product_id': product.id, 'product_uom_qty': 1,
                'price_unit': total, 'name': f"[Shipping] {method_title}",
            }))
        return lines

    def _build_fee_lines(self, data):
        lines = []
        for fl in data.get('fee_lines', []) or []:
            fee_name = fl.get('name') or 'Fee'
            total = float(fl.get('total') or 0)
            if total == 0:
                continue
            product = self._get_or_create_service_product(
                name=fee_name, code='WC-FEE',
                default_name='WooCommerce Fee')
            lines.append((0, 0, {
                'product_id': product.id, 'product_uom_qty': 1,
                'price_unit': total, 'name': f"[Fee] {fee_name}",
            }))
        return lines

    def _get_or_create_service_product(self, name, code, default_name):
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', code)], limit=1)
        if product:
            return product
        return Product.create({
            'name': default_name, 'default_code': code,
            'type': 'service', 'sale_ok': True, 'purchase_ok': False,
            'list_price': 0.0, 'invoice_policy': 'order',
        })

    def _fuzzy_match_product(self, wc_name):
        Product = self.env['product.product'].sudo()
        if not wc_name:
            return False
        # Prefer storable/consu goods over service so stock actually moves
        for extra_domain in ([('type', '=', 'consu')], []):
            product = Product.search([('name', '=', wc_name)] + extra_domain, limit=1)
            if product:
                return product
        for extra_domain in ([('type', '=', 'consu')], []):
            products = Product.search([('sale_ok', '=', True)] + extra_domain, limit=500)
            for p in products:
                if p.name and len(p.name) > 3 and p.name in wc_name:
                    return p
        return False

    @api.model
    def bulk_retype_service_to_storable(self, product_ids=None, dry_run=True):
        """Retype auto-matched service products (with no stock history) to storable goods.

        Call from Odoo shell with a scoped list, e.g.:
            env['wc.sync.queue'].bulk_retype_service_to_storable([497, 501], dry_run=False)

        With dry_run=True, only reports what would change.
        """
        Product = self.env['product.product'].sudo()
        domain = [('type', '=', 'service')]
        if product_ids:
            domain.append(('id', 'in', product_ids))
        candidates = Product.search(domain)
        safe = []
        for p in candidates:
            if self.env['stock.move'].sudo().search_count([('product_id', '=', p.id)]):
                continue
            safe.append(p.id)
        _logger.info("WC bulk_retype: %d safe candidates (of %d requested), dry_run=%s",
                     len(safe), len(candidates), dry_run)
        if not dry_run and safe:
            Product.browse(safe).write({'type': 'consu', 'is_storable': True})
        return {'candidates': candidates.ids, 'safe': safe, 'applied': not dry_run}

    def _parse_wc_date(self, date_str):
        if not date_str:
            return fields.Datetime.now()
        try:
            return date_str.replace('T', ' ')[:19]
        except Exception:
            return fields.Datetime.now()

    def _build_note(self, data):
        parts = []
        if data.get('payment_method_title'):
            parts.append(f"Payment: {data['payment_method_title']}")
        if data.get('id'):
            parts.append(f"WC Order #{data['id']}")
        if data.get('customer_note'):
            parts.append(f"Note: {data['customer_note']}")
        for coupon in data.get('coupon_lines', []):
            parts.append(f"Coupon: {coupon.get('code', '')}")
        return '\n'.join(parts) if parts else ''
