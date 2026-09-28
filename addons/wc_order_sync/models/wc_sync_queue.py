# -*- coding: utf-8 -*-
import json
import logging
import re
from datetime import timedelta

import requests
from odoo import api, fields, models

_logger = logging.getLogger(__name__)

STUCK_PROCESSING_MINUTES = 30
BACK_SYNC_PARAM = 'wc_order_sync.last_back_sync'
# Last time a fetch of new orders fully succeeded (UTC, '%Y-%m-%dT%H:%M:%S').
FETCH_OK_PARAM = 'wc_order_sync.last_fetch_ok'
# How far each fetch reaches back before the last success, so an order that
# changed while the previous run was in flight is still seen.
FETCH_OVERLAP_MINUTES = 10
WC_STATUSES_CANCEL_ODOO = ('cancelled', 'refunded', 'failed')
# Every WooCommerce status that means the goods have left the shop. The last
# four come from the store's logistics plugins (home delivery and CVS pickup).
WC_STATUSES_SHIPPED = ('completed', 'wmp-shipped', 'wmp-in-transit', 'ry-at-cvs', 'ry-out-cvs')
WC_STATUSES_IMPORT = 'processing,on-hold,completed'

# A channel tag is the short trailing "(...)" on a WC product name, e.g.
# "迷你香功能系列 – 十方清淨 (下班女子)". Longer phrases in the same position are
# product descriptions ("給想要沈澱及靜心的您"), not channels.
CHANNEL_TAG_RE = re.compile(r'[（(]([^（()）]{1,14})[)）]\s*$')


def _normalize_email(value):
    return (value or '').strip().lower()


def _normalize_phone(value):
    """Reduce a Taiwanese phone number to its local digits.

    '+886 912-345-678', '886912345678' and '0912345678' all become '0912345678',
    so the same customer typing their number differently is still one customer.
    """
    digits = re.sub(r'\D', '', value or '')
    if digits.startswith('886') and len(digits) >= 11:
        digits = '0' + digits[3:]
    return digits


# Parenthesised words in product names that are not promoters: deity aliases
# ("福德正神（土地公）"), ingredients, and WordPress duplication leftovers.
NOT_CHANNEL_TAGS = {'土地公', '媽祖', '三太子', '釋迦摩尼佛', '濟公', '呂洞賓', '啤酒花', '複製'}


def _channel_tag(name):
    match = CHANNEL_TAG_RE.search(name or '')
    if not match:
        return ''
    tag = match.group(1).strip()
    if (tag.startswith('給') or tag.endswith('的您') or tag in NOT_CHANNEL_TAGS
            or tag.isdigit() or re.search(r'[，,、。]', tag)):
        return ''
    return tag


BUNDLE_RE = re.compile(r'(\+|＋|組|組合|x\s?\d)')
LEADING_NO_RE = re.compile(r'^\s*\d+\s*[.．]\s*')


def _is_bundle_name(name):
    return bool(BUNDLE_RE.search(name or ''))


def _bundle_name(name):
    """A WC bundle name without its catalogue number and promoter tag:
    '08.【迷你香_淨化守護組】除障香+... (下班女子)' -> '【迷你香_淨化守護組】除障香+...'."""
    name = LEADING_NO_RE.sub('', name or '')
    tag = _channel_tag(name)
    if tag:
        name = re.sub(r'\s*[（(]%s[)）]\s*$' % re.escape(tag), '', name)
    return name.strip()


def _meta(data):
    return {m.get('key'): m.get('value') for m in (data.get('meta_data') or []) if m.get('key')}


class WcSyncQueue(models.Model):
    _name = 'wc.sync.queue'
    _description = 'WooCommerce Sync Queue'
    _order = 'create_date desc'

    wc_order_id = fields.Integer(string="WC Order ID", index=True)
    wc_order_number = fields.Char(string="WC Order Number")
    payload = fields.Text(string="JSON Payload", groups='wc_base_connector.group_wc_manager')
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

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    @api.model
    def _fetch_new_wc_orders(self):
        """Fetch orders modified since the last successful fetch and enqueue new ones.

        Filtering on *modified* rather than *created* time is what catches an
        order that was created while awaiting payment (ATM, CVS code, card
        authorisation) and only became payable later: its creation time is
        already older than orders imported since, but paying it modifies it.
        Orders already queued or imported are skipped, so the overlap is safe.

        Any failure is logged as an error and leaves the success timestamp
        untouched, which is what the watchdog cron alerts on.
        """
        mixin = self.env['wc.connection.mixin'].sudo()
        wc_url, auth = mixin._get_wc_auth()
        if not wc_url or not auth[0]:
            _logger.error("WC Sync: fetch skipped - WooCommerce connection is not configured")
            return 0
        ICP = self.env['ir.config_parameter'].sudo()
        run_started = fields.Datetime.now()
        last_ok = ICP.get_param(FETCH_OK_PARAM, '')
        if last_ok:
            since = fields.Datetime.to_datetime(last_ok.replace('T', ' ')) - timedelta(minutes=FETCH_OVERLAP_MINUTES)
        else:
            since = run_started - timedelta(days=2)
        api_url = f"{wc_url.rstrip('/')}/wp-json/wc/v3/orders"
        fetched = 0
        page = 1
        try:
            while True:
                params = {
                    'per_page': 100, 'page': page,
                    'orderby': 'modified', 'order': 'asc',
                    'modified_after': since.strftime('%Y-%m-%dT%H:%M:%S'),
                    'dates_are_gmt': 'true',
                    'status': WC_STATUSES_IMPORT,
                }
                resp = requests.get(api_url, auth=auth, params=params, timeout=30)
                if resp.status_code != 200:
                    _logger.error("WC Sync: fetch failed with HTTP %s on page %d: %s",
                                  resp.status_code, page, (resp.text or '')[:300])
                    return fetched
                orders = resp.json()
                if not orders:
                    break
                for order_data in orders:
                    if self._enqueue_order(order_data):
                        fetched += 1
                page += 1
                total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
                if page > total_pages:
                    break
        except Exception as e:
            _logger.error("WC Sync: fetch failed: %s", str(e)[:300])
            return fetched
        ICP.set_param(FETCH_OK_PARAM, run_started.strftime('%Y-%m-%dT%H:%M:%S'))
        _logger.info("WC Sync: Fetched %d new orders from WooCommerce (modified since %s UTC)",
                     fetched, since.strftime('%Y-%m-%d %H:%M:%S'))
        return fetched

    @api.model
    def _enqueue_order(self, order_data, body=None):
        """Queue one WC order unless it is already queued or imported. Returns the new item or False."""
        wc_id = order_data.get('id')
        if not wc_id:
            return False
        if self.search_count([('wc_order_id', '=', wc_id)]):
            return False
        if self.env['sale.order'].sudo().search_count([('wc_order_id', '=', wc_id)]):
            return False
        return self.create({
            'wc_order_id': wc_id,
            'wc_order_number': str(order_data.get('number', wc_id)),
            'payload': body or json.dumps(order_data),
            'state': 'pending',
            'wc_total': float(order_data.get('total', 0) or 0),
            'wc_date': order_data.get('date_created', ''),
            'wc_status': order_data.get('status', ''),
        })

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
                order_data = json.loads(item.sudo().payload)
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
        """Follow status changes of already-imported orders.

        Cancels the Odoo order when WC cancels/refunds/fails it, and - when
        automatic stock deduction is enabled - validates its delivery once WC
        reports the goods as shipped.
        """
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
            auto_stock = self._auto_stock_enabled()
            api_url = f"{wc_url.rstrip('/')}/wp-json/wc/v3/orders"
            updated = cancelled = validated = 0
            page = 1
            new_last = fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
            while True:
                params = {
                    'per_page': 100, 'page': page,
                    'orderby': 'modified', 'order': 'asc',
                    'modified_after': last_run,
                    'dates_are_gmt': 'true',
                    # Include all statuses so we can detect cancellations
                    'status': 'any',
                }
                resp = requests.get(api_url, auth=auth, params=params, timeout=30)
                if resp.status_code != 200:
                    _logger.error("WC Back-sync: HTTP %s on page %d: %s",
                                  resp.status_code, page, (resp.text or '')[:300])
                    return updated
                orders = resp.json()
                if not orders:
                    break
                for order_data in orders:
                    wc_id = order_data.get('id')
                    wc_status = order_data.get('status', '')
                    so = SaleOrder.search([('wc_order_id', '=', wc_id)], limit=1)
                    if not so:
                        continue
                    if so.wc_order_status != wc_status:
                        so.write({'wc_order_status': wc_status,
                                  'wc_last_synced': fields.Datetime.now()})
                        updated += 1
                    if wc_status in WC_STATUSES_CANCEL_ODOO and so.state != 'cancel':
                        try:
                            so._action_cancel() if hasattr(so, '_action_cancel') else so.action_cancel()
                            cancelled += 1
                            _logger.info("WC Back-sync: Cancelled %s (WC #%s status=%s)",
                                         so.name, wc_id, wc_status)
                        except Exception as e:
                            _logger.warning("WC Back-sync: Cannot cancel %s: %s",
                                            so.name, str(e)[:120])
                    elif wc_status in WC_STATUSES_SHIPPED and auto_stock and so.state == 'sale':
                        validated += self._auto_validate_pickings(so)
                page += 1
                total_pages = int(resp.headers.get('X-WP-TotalPages', 1))
                if page > total_pages:
                    break
            ICP.set_param(BACK_SYNC_PARAM, new_last)
            _logger.info("WC Back-sync: updated %d SOs (cancelled %d, deliveries validated %d) since %s",
                         updated, cancelled, validated, last_run)
            return updated
        except Exception as e:
            _logger.error("WC Back-sync: Failed: %s", str(e)[:300])
            return 0

    # ------------------------------------------------------------------
    # Import one order
    # ------------------------------------------------------------------

    @api.model
    def _auto_stock_enabled(self):
        value = self.env['ir.config_parameter'].sudo().get_param('wc_order_sync.wc_auto_stock', 'False')
        return value in ('True', '1', 'true')

    def _process_wc_order(self, data, queue_item):
        wc_order_id = data.get('id')
        existing = self.env['sale.order'].sudo().search([('wc_order_id', '=', wc_order_id)], limit=1)
        if existing:
            return existing
        wc_date = self._parse_wc_date(data.get('date_created', ''))
        partner = self._find_or_create_partner(data, wc_date)
        shipping_partner = self._find_or_create_delivery_address(partner, data)
        order_lines = self._build_order_lines(data)
        order_lines += self._build_shipping_lines(data)
        order_lines += self._build_fee_lines(data)
        coupon_codes = ','.join(c.get('code', '') for c in data.get('coupon_lines', []) if c.get('code'))
        order_vals = {
            'partner_id': partner.id,
            'partner_shipping_id': shipping_partner.id,
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
        order_vals.update(self._wc_shipping_vals(data))
        order_vals.update(self._wc_utm_vals(data))
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
        if data.get('status', '') in WC_STATUSES_SHIPPED and self._auto_stock_enabled():
            self._auto_validate_pickings(sale_order)
        return sale_order

    def _auto_validate_pickings(self, sale_order):
        """Validate open deliveries of a shipped order. Returns how many were validated."""
        count = 0
        for picking in sale_order.picking_ids:
            if picking.state in ('confirmed', 'assigned', 'waiting'):
                try:
                    for move in picking.move_ids:
                        move.quantity = move.product_uom_qty
                    picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
                    count += 1
                except Exception as e:
                    _logger.warning("WC Sync: Auto-validate picking failed for %s: %s", sale_order.name, str(e)[:100])
        return count

    # ------------------------------------------------------------------
    # Customers
    # ------------------------------------------------------------------

    @api.model
    def _generic_consumer(self):
        """The shared contact for orders that carry neither an e-mail nor a phone."""
        partner = self.env.ref('wc_order_sync.partner_generic_consumer', raise_if_not_found=False)
        if partner:
            return partner.sudo()
        return self.env['res.partner'].sudo().create({'name': '一般消費者', 'customer_rank': 1})

    @api.model
    def _wc_contact_keys(self, data):
        billing = data.get('billing') or {}
        return _normalize_email(billing.get('email')), _normalize_phone(billing.get('phone'))

    @api.model
    def _find_partner_by_contact(self, email, phone):
        """Find an existing contact by e-mail, then by phone. Never by name alone:
        two different people often share a name, and merging them is far harder
        to undo than splitting one person in two."""
        Partner = self.env['res.partner'].sudo()
        if email:
            partner = Partner.search([('email', '=ilike', email), ('type', '=', 'contact')], limit=1)
            if partner:
                return partner
        if phone:
            variants = list({phone, '+886' + phone[1:] if phone.startswith('0') else phone,
                             '886' + phone[1:] if phone.startswith('0') else phone})
            partner = Partner.search(['&', ('type', '=', 'contact'),
                                      '|', ('phone', 'in', variants), ('mobile', 'in', variants)], limit=1)
            if partner:
                return partner
        return Partner

    def _find_or_create_partner(self, data, wc_date=None):
        """Resolve the Odoo customer of a WC order.

        Identity rule: a registered WC customer is its account; anyone else is
        identified by e-mail, then phone. An order with neither is attributed
        to the shared "一般消費者" contact rather than guessed from the name.
        """
        Partner = self.env['res.partner'].sudo()
        PartnerMap = self.env['partner.wc.map'].sudo()
        billing = data.get('billing') or {}
        email, phone = self._wc_contact_keys(data)
        last_name = (billing.get('last_name') or '').strip()
        first_name = (billing.get('first_name') or '').strip()
        name = f"{last_name}{first_name}".strip() or email or phone
        wc_customer_id = data.get('customer_id') or 0
        order_date = wc_date or fields.Datetime.now()

        if not email and not phone:
            return self._generic_consumer()

        mapping = PartnerMap
        if wc_customer_id:
            mapping = PartnerMap.search([('wc_customer_id', '=', wc_customer_id)], limit=1)
        if not mapping and email:
            mapping = PartnerMap.search([('wc_customer_id', '=', 0), ('wc_email', '=ilike', email)], limit=1)
        if not mapping and phone and not email:
            mapping = PartnerMap.search([('wc_customer_id', '=', 0), ('wc_email', 'in', ('', False)),
                                         ('wc_phone', '=', phone)], limit=1)
        if mapping and mapping.partner_id:
            if not mapping.last_order_date or str(order_date) > str(mapping.last_order_date):
                mapping.write({'last_order_date': order_date})
            return mapping.partner_id

        partner = self._find_partner_by_contact(email, phone)
        if not partner:
            country_tw = self.env['res.country'].sudo().search([('code', '=', 'TW')], limit=1)
            customer_tag = self.env['res.partner.category'].sudo().search([('name', '=', 'Customer')], limit=1)
            if not customer_tag:
                customer_tag = self.env['res.partner.category'].sudo().search([('name', 'ilike', 'customer')], limit=1)
            vals = {
                'name': name, 'email': email or False, 'phone': billing.get('phone') or False,
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

        map_vals = {'partner_id': partner.id, 'last_order_date': order_date}
        if mapping:
            mapping.write(map_vals)
        else:
            PartnerMap.create(dict(map_vals, wc_customer_name=name, wc_customer_id=wc_customer_id,
                                   wc_email=email, wc_phone=phone, auto_matched=True))
        return partner

    # ------------------------------------------------------------------
    # Delivery address
    # ------------------------------------------------------------------

    @api.model
    def _wc_shipping_method(self, data):
        for line in data.get('shipping_lines') or []:
            title = line.get('method_title') or line.get('method_id')
            if title:
                return title
        return ''

    @api.model
    def _wc_shipping_vals(self, data):
        meta = _meta(data)
        return {
            'wc_shipping_method': self._wc_shipping_method(data) or False,
            'wc_cvs_store_id': meta.get('_shipping_cvs_store_ID') or False,
            'wc_cvs_store_name': meta.get('_shipping_cvs_store_name') or False,
        }

    def _find_or_create_delivery_address(self, partner, data):
        """Return the contact the goods are shipped to.

        CVS pickup: a delivery address named after the recipient and the store,
        keyed on the store ID so a customer reusing a store reuses the address.
        Home delivery to another address or recipient: a delivery address built
        from WC's shipping block. Otherwise the customer itself.
        """
        Partner = self.env['res.partner'].sudo()
        meta = _meta(data)
        billing = data.get('billing') or {}
        shipping = data.get('shipping') or {}
        recipient = f"{(shipping.get('last_name') or '').strip()}{(shipping.get('first_name') or '').strip()}" \
            or partner.name
        recipient_phone = shipping.get('phone') or billing.get('phone') or False
        country_tw = self.env['res.country'].sudo().search([('code', '=', 'TW')], limit=1)
        store_id = (meta.get('_shipping_cvs_store_ID') or '').strip()
        if store_id:
            method = self._wc_shipping_method(data) or 'CVS'
            store_name = (meta.get('_shipping_cvs_store_name') or '').strip()
            ref = f"CVS-{store_id}"
            vals = {
                'name': f"{recipient}｜{method} {store_name}（{store_id}）",
                'street': meta.get('_shipping_cvs_store_address') or False,
                'phone': recipient_phone,
                'comment': f"超商取貨 {method} {store_name}，門市代號 {store_id}，"
                           f"門市電話 {meta.get('_shipping_cvs_store_telephone') or '-'}",
            }
            address = Partner.search([('parent_id', '=', partner.id), ('type', '=', 'delivery'),
                                      ('ref', '=', ref)], limit=1)
            if address:
                changes = {k: v for k, v in vals.items() if address[k] != v}
                if changes:
                    address.write(changes)
                return address
            return Partner.create(dict(vals, parent_id=partner.id, type='delivery', ref=ref,
                                       country_id=country_tw.id if country_tw else False))

        street = ' '.join(s for s in (shipping.get('address_1'), shipping.get('address_2')) if s).strip()
        billing_street = ' '.join(s for s in (billing.get('address_1'), billing.get('address_2')) if s).strip()
        billing_name = f"{(billing.get('last_name') or '').strip()}{(billing.get('first_name') or '').strip()}"
        if not street or (street == billing_street and recipient in (billing_name, partner.name)):
            return partner
        address = Partner.search([('parent_id', '=', partner.id), ('type', '=', 'delivery'),
                                  ('name', '=', recipient), ('street', '=', street)], limit=1)
        if address:
            return address
        return Partner.create({
            'parent_id': partner.id, 'type': 'delivery', 'name': recipient,
            'street': street, 'city': shipping.get('city') or False,
            'zip': shipping.get('postcode') or False, 'phone': recipient_phone,
            'country_id': country_tw.id if country_tw else False,
        })

    # ------------------------------------------------------------------
    # Channel (UTM)
    # ------------------------------------------------------------------

    @api.model
    def _utm_record(self, model, name):
        if not name:
            return self.env[model]
        # Case-insensitive, so "Cherry" and "cherry" are one promoter.
        record = self.env[model].sudo().search([('name', '=ilike', name)], limit=1)
        return record or self.env[model].sudo().create({'name': name})

    @api.model
    def _wc_channel(self, data):
        """The promoter the order came through, read from its line names."""
        tags = [_channel_tag(item.get('name')) for item in data.get('line_items') or []]
        tags = [t for t in tags if t]
        if not tags:
            return ''
        return max(set(tags), key=tags.count)

    @api.model
    def _wc_utm_vals(self, data):
        """Promoter -> utm source; WC's own traffic attribution -> utm medium."""
        meta = _meta(data)
        traffic = (meta.get('_wc_order_attribution_utm_source') or '').strip()
        if traffic == '(direct)':
            traffic = '直接輸入'
        vals = {}
        channel = self._wc_channel(data)
        if channel:
            vals['source_id'] = self._utm_record('utm.source', channel).id
        if traffic:
            vals['medium_id'] = self._utm_record('utm.medium', traffic).id
        return vals

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    @api.model
    def _resolve_product_by_sku(self, sku):
        """Match a WC SKU to an Odoo internal reference.

        WooCommerce appends '-N' to the SKU each time a product is duplicated
        for another promoter (MIE009 -> MIE009-2 -> MIE009-1-1-2-1-2), so trailing
        segments are stripped one at a time until an internal reference matches.
        Stripping one at a time keeps references that contain a hyphen themselves
        (INZ-219) intact.
        """
        Product = self.env['product.product'].sudo()
        candidate = (sku or '').strip().rstrip('-')
        while candidate:
            product = Product.search([('default_code', '=', candidate)], limit=1)
            if product:
                return product
            if '-' not in candidate:
                break
            candidate = candidate.rsplit('-', 1)[0].rstrip('-')
        return Product

    @api.model
    def _resolve_line_product(self, wc_name, sku):
        """The Odoo product for one WC order line, or an empty recordset.

        SKU first. But the shop created many bundles by duplicating a single
        product without changing its SKU (37 bundles carry LIG007-..., LIC004-...,
        MIE014-...), so a bundle name resolving to a non-bundle product is not
        trusted; the bundle is then looked up by its exact normalised name.
        """
        Product = self.env['product.product'].sudo()
        product = self._resolve_product_by_sku(sku)
        if _is_bundle_name(wc_name):
            if product and _is_bundle_name(product.name):
                return product
            return Product.search([('name', '=', _bundle_name(wc_name))], limit=1)
        return product

    @api.model
    def _unmatched_product(self):
        """Placeholder for lines whose WC product matches no Odoo product.

        Created at runtime rather than as module data: product templates carry
        NOT NULL columns owned by modules loaded after this one (website_sale's
        base_unit_count), which XML data loaded during this module's install
        cannot fill.
        """
        Product = self.env['product.product'].sudo()
        product = Product.with_context(active_test=False).search([('default_code', '=', 'WC-UNMATCHED')], limit=1)
        if product:
            return product
        return Product.create({
            'name': '待確認網店商品', 'default_code': 'WC-UNMATCHED',
            'type': 'consu', 'is_storable': False, 'sale_ok': True, 'purchase_ok': False,
            'list_price': 0.0, 'invoice_policy': 'order',
        })

    def _build_order_lines(self, data):
        lines = []
        ProductMap = self.env['product.wc.map'].sudo()
        for item in data.get('line_items', []):
            wc_name = item.get('name', '')
            wc_product_id = item.get('product_id', 0)
            sku = (item.get('sku') or '').strip()
            qty = item.get('quantity', 1)
            total = float(item.get('total', 0))
            price_unit = total / qty if qty else total
            mapping = ProductMap
            if wc_product_id:
                mapping = ProductMap.search([('wc_product_id', '=', wc_product_id)], limit=1)
            if not mapping:
                mapping = ProductMap.search([('wc_product_name', '=', wc_name)], limit=1)
            product = mapping.product_id
            if not mapping:
                product = self._resolve_line_product(wc_name, sku)
                mapping = ProductMap.create({
                    'wc_product_name': wc_name, 'wc_product_id': wc_product_id, 'wc_sku': sku or False,
                    'product_id': product.id or False, 'auto_matched': bool(product),
                })
                if not product:
                    mapping._schedule_review()
            if not product:
                # Never guess: park the line on a placeholder a manager must resolve.
                product = self._unmatched_product()
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
