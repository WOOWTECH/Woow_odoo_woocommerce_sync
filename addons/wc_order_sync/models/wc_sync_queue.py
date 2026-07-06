# -*- coding: utf-8 -*-
import json
import logging
from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class WcSyncQueue(models.Model):
    _name = 'wc.sync.queue'
    _description = 'WooCommerce Sync Queue'
    _order = 'create_date desc'

    wc_order_id = fields.Integer(string="WC Order ID", index=True)
    wc_order_number = fields.Char(string="WC Order Number")
    payload = fields.Text(string="JSON Payload")
    state = fields.Selection([
        ('pending', '待處理'),
        ('processing', '處理中'),
        ('done', '完成'),
        ('error', '錯誤'),
    ], default='pending', string="狀態", index=True)
    error_message = fields.Text(string="錯誤訊息")
    sale_order_id = fields.Many2one('sale.order', string="銷售訂單")
    partner_id = fields.Many2one('res.partner', string="客戶")
    attempts = fields.Integer(default=0, string="嘗試次數")
    wc_total = fields.Float(string="WC 訂單金額")
    wc_date = fields.Char(string="WC 訂單日期")
    wc_status = fields.Char(string="WC 訂單狀態")

    def action_retry(self):
        """Reset to pending for retry."""
        self.write({'state': 'pending', 'error_message': False, 'attempts': 0})

    @api.model
    def _cron_process_queue(self):
        """Cron job: process pending queue items."""
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
                item.write({
                    'state': 'error',
                    'error_message': str(e)[:500],
                })
                self.env.cr.commit()

    def _process_wc_order(self, data, queue_item):
        """Process a single WC order and create sale.order."""
        wc_order_id = data.get('id')

        # 1. Check duplicate
        existing = self.env['sale.order'].sudo().search([
            ('wc_order_id', '=', wc_order_id),
        ], limit=1)
        if existing:
            _logger.info("WC Sync: Order #%s already exists as %s, skipping",
                         wc_order_id, existing.name)
            return existing

        # 2. Find or create customer
        partner = self._find_or_create_partner(data)

        # 3. Build order lines
        order_lines = self._build_order_lines(data)

        # 4. Create sale order
        order_vals = {
            'partner_id': partner.id,
            'wc_order_id': wc_order_id,
            'wc_order_status': data.get('status', ''),
            'wc_payment_method': data.get('payment_method_title', ''),
            'date_order': self._parse_wc_date(data.get('date_created', '')),
            'order_line': order_lines,
            'note': self._build_note(data),
        }

        pricelist = self.env['product.pricelist'].sudo().search([
            ('currency_id.name', '=', 'TWD'),
        ], limit=1)
        if pricelist:
            order_vals['pricelist_id'] = pricelist.id

        sale_order = self.env['sale.order'].sudo().create(order_vals)
        _logger.info("WC Sync: Created sale order %s for WC #%s",
                     sale_order.name, wc_order_id)

        # 5. Auto-confirm if enabled
        auto_confirm = self.env['ir.config_parameter'].sudo().get_param(
            'wc_order_sync.wc_auto_confirm', 'True')
        if auto_confirm in ('True', '1', 'true'):
            try:
                sale_order.action_confirm()
                _logger.info("WC Sync: Order %s auto-confirmed", sale_order.name)
            except Exception as e:
                _logger.warning("WC Sync: Auto-confirm failed for %s: %s",
                                sale_order.name, str(e)[:100])

        return sale_order

    def _find_or_create_partner(self, data):
        """Find existing partner by email/phone/name or create new one."""
        Partner = self.env['res.partner'].sudo()
        billing = data.get('billing', {})
        email = billing.get('email', '').strip()
        phone = billing.get('phone', '').strip()
        last_name = billing.get('last_name', '').strip()
        first_name = billing.get('first_name', '').strip()
        name = f"{last_name}{first_name}".strip() or email or '未知客戶'

        # Search by email (most reliable)
        if email:
            partner = Partner.search([('email', '=', email)], limit=1)
            if partner:
                return partner

        # Search by phone
        if phone:
            partner = Partner.search([
                '|', ('phone', '=', phone), ('mobile', '=', phone),
            ], limit=1)
            if partner:
                return partner

        # Search by name
        if name and name != '未知客戶':
            partner = Partner.search([
                ('name', '=', name),
                ('customer_rank', '>', 0),
            ], limit=1)
            if partner:
                return partner

        # Create new partner
        country_tw = self.env['res.country'].sudo().search(
            [('code', '=', 'TW')], limit=1)
        customer_tag = self.env['res.partner.category'].sudo().search(
            [('name', '=', '客戶')], limit=1)

        vals = {
            'name': name,
            'email': email or False,
            'phone': phone or False,
            'customer_rank': 1,
            'lang': 'zh_TW',
            'tz': 'Asia/Taipei',
            'country_id': country_tw.id if country_tw else False,
        }
        if customer_tag:
            vals['category_id'] = [(4, customer_tag.id)]

        # Address
        street_parts = []
        for key in ('address_1', 'address_2'):
            if billing.get(key):
                street_parts.append(billing[key])
        if street_parts:
            vals['street'] = ' '.join(street_parts)
        if billing.get('city'):
            vals['city'] = billing['city']
        if billing.get('postcode'):
            vals['zip'] = billing['postcode']

        partner = Partner.create(vals)
        _logger.info("WC Sync: Created partner '%s' (id=%d)", name, partner.id)
        return partner

    def _build_order_lines(self, data):
        """Build sale.order.line vals from WC line_items."""
        lines = []
        ProductMap = self.env['product.wc.map'].sudo()
        ICP = self.env['ir.config_parameter'].sudo()
        default_product_id = int(ICP.get_param(
            'wc_order_sync.wc_default_product_id', '0'))

        for item in data.get('line_items', []):
            wc_name = item.get('name', '')
            wc_product_id = item.get('product_id', 0)
            qty = item.get('quantity', 1)
            total = float(item.get('total', 0))
            price_unit = total / qty if qty else total

            # Try product map
            product = False
            mapping = ProductMap.search([
                '|',
                ('wc_product_id', '=', wc_product_id),
                ('wc_product_name', '=', wc_name),
            ], limit=1)

            if mapping and mapping.product_id:
                product = mapping.product_id
            else:
                # Try fuzzy match by name
                product = self._fuzzy_match_product(wc_name)
                if product:
                    # Save mapping for future use
                    ProductMap.create({
                        'wc_product_name': wc_name,
                        'wc_product_id': wc_product_id,
                        'product_id': product.id,
                        'auto_matched': True,
                    })

            if not product and default_product_id:
                product = self.env['product.product'].sudo().browse(
                    default_product_id)
                if not product.exists():
                    product = False

            if not product:
                # Create a generic service product for unmatched items
                product = self.env['product.product'].sudo().create({
                    'name': wc_name[:100],
                    'type': 'service',
                    'sale_ok': True,
                    'list_price': price_unit,
                })
                ProductMap.create({
                    'wc_product_name': wc_name,
                    'wc_product_id': wc_product_id,
                    'product_id': product.id,
                    'auto_matched': True,
                })

            lines.append((0, 0, {
                'product_id': product.id,
                'product_uom_qty': qty,
                'price_unit': price_unit,
                'name': wc_name,
            }))

        return lines

    def _fuzzy_match_product(self, wc_name):
        """Try to match WC product name to Odoo product."""
        Product = self.env['product.product'].sudo()
        if not wc_name:
            return False

        # Exact match
        product = Product.search([('name', '=', wc_name)], limit=1)
        if product:
            return product

        # Partial match — check if any Odoo product name is contained in WC name
        products = Product.search([('sale_ok', '=', True)], limit=500)
        for p in products:
            if p.name and len(p.name) > 3 and p.name in wc_name:
                return p

        return False

    def _parse_wc_date(self, date_str):
        """Parse WC date format to Odoo datetime."""
        if not date_str:
            return fields.Datetime.now()
        # WC format: "2026-07-06T14:22:00"
        try:
            return date_str.replace('T', ' ')[:19]
        except Exception:
            return fields.Datetime.now()

    def _build_note(self, data):
        """Build order note from WC data."""
        parts = []
        if data.get('payment_method_title'):
            parts.append(f"付款方式：{data['payment_method_title']}")
        if data.get('id'):
            parts.append(f"WC 訂單 #{data['id']}")
        if data.get('customer_note'):
            parts.append(f"客戶備註：{data['customer_note']}")
        # Coupon info
        for coupon in data.get('coupon_lines', []):
            parts.append(f"優惠券：{coupon.get('code', '')}")
        return '\n'.join(parts) if parts else ''
