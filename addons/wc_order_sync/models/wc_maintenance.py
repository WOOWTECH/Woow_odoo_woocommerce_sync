# -*- coding: utf-8 -*-
"""One-off repairs for data imported before the 18.0.3 sync rules.

Every method takes dry_run=True by default and returns a report dict, so it
can be reviewed before anything is written. They reuse the same rules the
sync now applies to new orders, which keeps history and future consistent.
Run from an Odoo shell, e.g.:

    env['wc.sync.queue'].wc_repair_customers(dry_run=True)
"""
import json
import logging
import re
from collections import Counter, defaultdict

from odoo import api, models

from .wc_sync_queue import (BUNDLE_RE, _bundle_name, _is_bundle_name, _meta,
                            _normalize_email, _normalize_phone)

_logger = logging.getLogger(__name__)



def _without_promoter_tag(name):
    """The WC name with only its promoter tag removed (used to prefer untagged names)."""
    from .wc_sync_queue import _channel_tag
    tag = _channel_tag(name)
    return re.sub(r'\s*[（(]%s[)）]\s*$' % re.escape(tag), '', name).strip() if tag else (name or '').strip()


class WcSyncQueue(models.Model):
    _inherit = 'wc.sync.queue'

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @api.model
    def _wc_imported_items(self):
        """Done queue items with their order and parsed payload, oldest first."""
        items = self.sudo().search([('state', '=', 'done'), ('sale_order_id', '!=', False)],
                                   order='create_date asc')
        for item in items:
            try:
                data = json.loads(item.payload or '{}')
            except ValueError:
                continue
            yield item, data

    @api.model
    def _wc_identity(self, data):
        """Stable identity of the buyer of one order, following the shop's rule:
        a registered account is its account; otherwise e-mail, then phone;
        an order with neither belongs to the generic consumer."""
        email, phone = self._wc_contact_keys(data)
        if not email and not phone:
            return 'generic'
        if data.get('customer_id'):
            return 'cid:%s' % data['customer_id']
        if email:
            return 'em:%s' % email
        return 'ph:%s' % phone

    # ------------------------------------------------------------------
    # 1. customers merged by name
    # ------------------------------------------------------------------

    @api.model
    def wc_repair_customers(self, dry_run=True):
        """Give every buyer identity its own customer and move its orders there.

        For each Odoo customer currently holding WC orders from several
        identities, the identity matching the customer's own e-mail/phone stays
        (or, if none matches, the one with the most orders); every other
        identity is moved to the existing customer with that e-mail/phone, or a
        new one. Orders without any e-mail or phone go to 一般消費者.
        Only orders without invoices are moved.
        """
        generic = self._generic_consumer()

        by_partner = defaultdict(lambda: defaultdict(list))   # partner -> identity -> [(item, data)]
        for item, data in self._wc_imported_items():
            so = item.sale_order_id
            by_partner[so.partner_id.id][self._wc_identity(data)].append((item, data))

        moves = []            # (item, data, identity, from_partner, to_partner_or_None)
        for partner_id, identities in by_partner.items():
            partner = self.env['res.partner'].sudo().browse(partner_id)
            own_email = _normalize_email(partner.email)
            own_phone = _normalize_phone(partner.phone) or _normalize_phone(partner.mobile)

            def owns(identity):
                # Whether this identity is the customer record's own person: any of its
                # orders carries the customer's e-mail or phone. Applies to registered
                # accounts too, so two accounts sharing a name are still told apart.
                for _item, data in identities[identity]:
                    email, phone = self._wc_contact_keys(data)
                    if (email and email == own_email) or (phone and phone == own_phone):
                        return True
                return False

            keep = set()
            if partner.id != generic.id:
                real = [i for i in identities if i != 'generic']
                owned = [i for i in real if owns(i)]
                if owned:
                    keep = set(owned)
                elif real:
                    keep = {max(real, key=lambda i: len(identities[i]))}
            else:
                keep = {'generic'}
            for identity, entries in identities.items():
                if identity in keep:
                    continue
                for item, data in entries:
                    moves.append((item, data, identity, partner))

        report = Counter()
        samples = []
        target_cache = {}
        for item, data, identity, from_partner in moves:
            so = item.sale_order_id
            if so.invoice_ids:
                report['skipped_has_invoice'] += 1
                continue
            if identity == 'generic':
                target = generic
            elif identity in target_cache:
                target = target_cache[identity]
            else:
                email, phone = self._wc_contact_keys(data)
                target = self._find_partner_by_contact(email, phone)
                if target and target.id == from_partner.id:
                    target = self.env['res.partner']
                target_cache[identity] = target
            report['orders_to_move'] += 1
            report['to_generic' if identity == 'generic' else
                   ('to_existing' if target else 'to_new_customer')] += 1
            if len(samples) < 15:
                samples.append({'order': so.name, 'from': from_partner.name, 'identity': identity.split(':')[0],
                                'to': target.name if target else '(new) ' + (
                                    '%s%s' % ((data.get('billing') or {}).get('last_name') or '',
                                              (data.get('billing') or {}).get('first_name') or ''))})
            if dry_run:
                continue
            if not target:
                target = self._find_or_create_partner(dict(data, customer_id=0), so.date_order)
                target_cache[identity] = target
            shipping = self._find_or_create_delivery_address(target, data)
            # Pin the fields Odoo would recompute from the customer, so moving an
            # order changes who bought it and nothing else. (A confirmed order
            # refuses any write to its pricelist, which therefore cannot change.)
            so.write({
                'partner_id': target.id,
                'partner_invoice_id': target.id,
                'partner_shipping_id': shipping.id,
                'fiscal_position_id': so.fiscal_position_id.id,
                'payment_term_id': so.payment_term_id.id,
                'user_id': so.user_id.id,
                'team_id': so.team_id.id,
            })
            so.picking_ids.filtered(lambda p: p.partner_id != shipping).write({'partner_id': shipping.id})
            item.write({'partner_id': target.id})
            report['moved'] += 1

        report['partners_involved'] = len({m[3].id for m in moves})
        if not dry_run:
            report.update(self._wc_rebuild_partner_maps())
        return {'dry_run': dry_run, 'counts': dict(report), 'samples': samples}

    @api.model
    def _wc_rebuild_partner_maps(self):
        """One mapping per buyer identity, pointing at the customer that now holds its orders."""
        PartnerMap = self.env['partner.wc.map'].sudo()
        latest = {}
        for item, data in self._wc_imported_items():
            identity = self._wc_identity(data)
            if identity != 'generic':
                latest[identity] = (item, data)
        created = updated = 0
        for identity, (item, data) in latest.items():
            partner = item.sale_order_id.partner_id
            email, phone = self._wc_contact_keys(data)
            billing = data.get('billing') or {}
            name = ('%s%s' % (billing.get('last_name') or '', billing.get('first_name') or '')).strip() or email or phone
            kind, _, value = identity.partition(':')
            if kind == 'cid':
                domain = [('wc_customer_id', '=', int(value))]
            elif kind == 'em':
                domain = [('wc_customer_id', '=', 0), ('wc_email', '=ilike', value)]
            else:
                domain = [('wc_customer_id', '=', 0), ('wc_email', 'in', ('', False)), ('wc_phone', '=', value)]
            mapping = PartnerMap.search(domain, limit=1)
            vals = {'partner_id': partner.id, 'wc_email': email, 'wc_phone': phone}
            if mapping:
                if mapping.partner_id != partner or mapping.wc_phone != phone:
                    mapping.write(vals)
                    updated += 1
            else:
                PartnerMap.create(dict(vals, wc_customer_name=name,
                                       wc_customer_id=int(value) if kind == 'cid' else 0,
                                       auto_matched=True, last_order_date=item.sale_order_id.date_order))
                created += 1
        # Duplicate mappings of one identity: keep the first, drop the rest, so a
        # lookup can never land on a stale duplicate pointing at someone else.
        groups = {}
        for mapping in PartnerMap.search([], order='id'):
            if mapping.wc_customer_id:
                key = 'cid:%s' % mapping.wc_customer_id
            elif mapping.wc_email:
                key = 'em:%s' % _normalize_email(mapping.wc_email)
            elif mapping.wc_phone:
                key = 'ph:%s' % _normalize_phone(mapping.wc_phone)
            else:
                continue
            groups.setdefault(key, []).append(mapping.id)
        duplicate_ids = [i for ids in groups.values() for i in ids[1:]]
        PartnerMap.browse(duplicate_ids).unlink()
        # Guest mappings no order belongs to any more (the one the old code kept rewriting).
        stale = PartnerMap.search([('wc_customer_id', '=', 0)]).filtered(
            lambda m: ('em:%s' % _normalize_email(m.wc_email)) not in latest
            and ('ph:%s' % _normalize_phone(m.wc_phone)) not in latest)
        stale_count = len(stale)
        stale.unlink()
        return {'maps_created': created, 'maps_updated': updated, 'maps_removed_duplicate': len(duplicate_ids),
                'maps_removed_stale': stale_count}

    # ------------------------------------------------------------------
    # 2. delivery addresses
    # ------------------------------------------------------------------

    @api.model
    def wc_repair_shipping(self, dry_run=True):
        """Record the shipping method / CVS store on every WC order, and give orders
        whose delivery is still open their real delivery address (CVS store or
        the separate recipient address), so the picking goes to the right place."""
        report = Counter()
        samples = []
        for item, data in self._wc_imported_items():
            so = item.sale_order_id
            vals = {k: v for k, v in self._wc_shipping_vals(data).items() if so[k] != v}
            open_pickings = so.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel'))
            if vals:
                report['orders_shipping_fields'] += 1
            if open_pickings and so.state != 'cancel':
                report['orders_with_open_delivery'] += 1
                if dry_run:
                    if _meta(data).get('_shipping_cvs_store_ID'):
                        report['open_cvs'] += 1
                        if len(samples) < 8:
                            samples.append({'order': so.name, 'store': '%s %s' % (
                                vals.get('wc_shipping_method') or so.wc_shipping_method,
                                vals.get('wc_cvs_store_name') or so.wc_cvs_store_name)})
                else:
                    shipping = self._find_or_create_delivery_address(so.partner_id, data)
                    if shipping != so.partner_shipping_id:
                        vals['partner_shipping_id'] = shipping.id
                        report['delivery_address_set'] += 1
            if vals and not dry_run:
                so.write(vals)
                if 'partner_shipping_id' in vals:
                    open_pickings.write({'partner_id': vals['partner_shipping_id']})
        return {'dry_run': dry_run, 'counts': dict(report), 'samples': samples}

    # ------------------------------------------------------------------
    # 3. channel
    # ------------------------------------------------------------------

    @api.model
    def wc_repair_channels(self, dry_run=True):
        """Set promoter (utm source) and traffic source (utm medium) on every WC order."""
        report = Counter()
        revenue = Counter()
        for item, data in self._wc_imported_items():
            so = item.sale_order_id
            channel = self._wc_channel(data)
            if channel:
                revenue[channel] += so.amount_total if so.state != 'cancel' else 0
            vals = self._wc_utm_vals(data) if not dry_run else {}
            if dry_run:
                report['orders_with_channel' if channel else 'orders_without_channel'] += 1
                continue
            vals = {k: v for k, v in vals.items() if so[k].id != v}
            if vals:
                so.write(vals)
                report['orders_updated'] += 1
        return {'dry_run': dry_run, 'counts': dict(report),
                'top_channels': [(c, round(v)) for c, v in revenue.most_common(12)]}

    # ------------------------------------------------------------------
    # 4. products: bundles and SKU-based mappings
    # ------------------------------------------------------------------

    @api.model
    def _wc_sku_history(self):
        """For every WC product seen in orders: its latest SKU, name and unit price."""
        seen = {}
        for item, data in self._wc_imported_items():
            for line in data.get('line_items') or []:
                key = line.get('product_id') or line.get('name')
                qty = line.get('quantity') or 1
                seen[key] = {
                    'wc_product_id': line.get('product_id') or 0,
                    'name': line.get('name') or '',
                    'sku': (line.get('sku') or '').strip(),
                    'price': float(line.get('total') or 0) / qty,
                }
        return seen

    @api.model
    def _bundle_base_sku(self, sku):
        base = (sku or '').strip().rstrip('-')
        while re.search(r'-\d+$', base):
            base = re.sub(r'-\d+$', '', base).rstrip('-')
        return base

    @api.model
    def wc_create_bundle_products(self, dry_run=True):
        """Create an Odoo product for every WC bundle that has none.

        A bundle whose own SKU matches nothing gets that base SKU as internal
        reference and barcode (CM002-1 -> CM002), following the shop's
        barcode = internal reference convention. A bundle that was made by
        duplicating a single product - so its SKU points at that single item -
        gets a new reference BND-001, BND-002, ... Bundles are goods but not
        stocked themselves; their stock moves through their components once a
        kit bill of materials exists.
        """
        Product = self.env['product.product'].sudo()
        bundles = {}
        for entry in self._wc_sku_history().values():
            if not _is_bundle_name(entry['name']):
                continue
            if self._resolve_line_product(entry['name'], entry['sku']):
                continue
            name = _bundle_name(entry['name'])
            own = self._resolve_product_by_sku(entry['sku'])
            code = None if own else (self._bundle_base_sku(entry['sku']) or None)
            key = code or name
            tagged = entry['name'].strip() != _without_promoter_tag(entry['name'])
            current = bundles.get(key)
            if not current or (current['tagged'] and not tagged):
                bundles[key] = {'code': code, 'name': name, 'price': entry['price'], 'tagged': tagged}
        existing_bnd = Product.with_context(active_test=False).search([('default_code', '=like', 'BND-%')])
        seq = max([int(p.default_code[4:]) for p in existing_bnd if p.default_code[4:].isdigit()] or [0])
        created = []
        for key, info in sorted(bundles.items(), key=lambda kv: (kv[1]['code'] is None, kv[0])):
            code = info['code']
            if not code:
                seq += 1
                code = 'BND-%03d' % seq
            created.append({'default_code': code, 'name': info['name'], 'list_price': round(info['price'])})
            if dry_run:
                continue
            product = Product.create({
                'name': info['name'], 'default_code': code, 'barcode': code,
                'type': 'consu', 'is_storable': False, 'sale_ok': True,
                'list_price': round(info['price']), 'invoice_policy': 'order',
            })
            if 'product.barcode' in self.env:
                self.env['product.barcode'].sudo().create({
                    'name': code, 'barcode_type': 'qr', 'product_id': product.id, 'sequence': 10})
        return {'dry_run': dry_run, 'bundles': created}

    @api.model
    def wc_repair_product_maps(self, dry_run=True):
        """Re-derive every WC product mapping from its SKU instead of its name.

        A mapping whose SKU resolves is pointed at that product. A mapping whose
        SKU does not resolve keeps its current product unless it is a bundle
        mapped to a single item - the old name-substring guess - in which case it
        is cleared and sent for review, so its orders stop deducting the wrong item.
        """
        ProductMap = self.env['product.wc.map'].sudo()
        history = self._wc_sku_history()
        report = Counter()
        changes = []
        for mapping in ProductMap.search([]):
            entry = history.get(mapping.wc_product_id) or history.get(mapping.wc_product_name) or {}
            sku = entry.get('sku') or mapping.wc_sku or ''
            vals = {}
            if sku and sku != mapping.wc_sku:
                vals['wc_sku'] = sku
            product = self._resolve_line_product(mapping.wc_product_name, sku)
            if product:
                if product != mapping.product_id:
                    vals.update(product_id=product.id, auto_matched=True)
                    report['repointed'] += 1
                    if len(changes) < 20:
                        changes.append({'wc': mapping.wc_product_name[:40], 'from': mapping.product_id.display_name,
                                        'to': product.display_name})
                else:
                    report['already_right'] += 1
            else:
                current = mapping.product_id
                if current and _is_bundle_name(mapping.wc_product_name) and not _is_bundle_name(current.name):
                    vals.update(product_id=False, auto_matched=False)
                    report['bundle_to_single_cleared'] += 1
                    if len(changes) < 20:
                        changes.append({'wc': mapping.wc_product_name[:40], 'from': current.display_name,
                                        'to': '(review)'})
                else:
                    report['unresolved_kept' if current else 'unresolved_empty'] += 1
            if vals and not dry_run:
                mapping.write(vals)
                if 'product_id' in vals and not vals['product_id']:
                    mapping._schedule_review()
        return {'dry_run': dry_run, 'counts': dict(report), 'changes': changes}

    # ------------------------------------------------------------------
    # 5. stock go-live (run on the day of the opening inventory count)
    # ------------------------------------------------------------------

    @api.model
    def wc_close_deliveries_before_count(self, cutoff, dry_run=True):
        """Cancel open WC deliveries of orders the shop already shipped before the count.

        The opening inventory count measures what is physically on the shelf,
        so goods that left before it are already accounted for. Validating their
        still-open deliveries afterwards would deduct them a second time. Orders
        WooCommerce still reports as processing keep their delivery open.
        """
        from .wc_sync_queue import WC_STATUSES_CANCEL_ODOO, WC_STATUSES_SHIPPED
        pickings = self.env['stock.picking'].sudo().search([
            ('sale_id.wc_order_id', '>', 0),
            ('sale_id.date_order', '<', cutoff),
            ('sale_id.wc_order_status', 'in', list(WC_STATUSES_SHIPPED + WC_STATUSES_CANCEL_ODOO)),
            ('state', 'not in', ('done', 'cancel')),
        ])
        report = Counter(p.sale_id.wc_order_status for p in pickings)
        if not dry_run:
            for picking in pickings:
                picking.action_cancel()
                picking.message_post(body="已在期初盤點（%s）前出貨，盤點數量已反映，故取消此出貨單以免重複扣庫存。" % cutoff)
        return {'dry_run': dry_run, 'deliveries': len(pickings), 'by_wc_status': dict(report)}

    # ------------------------------------------------------------------
    # 6. kit bills of materials for bundles (needs the mrp module)
    # ------------------------------------------------------------------

    @api.model
    def _bundle_components(self, name):
        """Parse '【淨化守護組】除障香 + 正龍沉香 + 壇城樂土' into (family, [(token, qty)]).

        Returns (None, []) when the name does not list its contents or mixes
        mini and stick incense, so no bill of materials is guessed."""
        has_mini, has_stick = '迷你香' in name, ('長線香' in name or '線香' in name.replace('迷你香', ''))
        if has_mini == has_stick:
            return None, []
        family = '迷你香' if has_mini else '長線香'
        cut = max(name.rfind(sep) for sep in (':', '：', '|', '｜', '】'))
        body = name[cut + 1:] if cut >= 0 else name
        tokens = []
        for raw in re.split(r'[+＋、,，]', body):
            token = raw.strip().strip('-–').strip()
            if not token:
                continue
            qty = 1
            m = re.search(r'\s*[xX×]\s*(\d+)\s*$', token)
            if m:
                qty, token = int(m.group(1)), token[:m.start()].strip()
            token = token.replace(family, '').strip(' -–:')
            if token:
                tokens.append((token, qty))
        return (family, tokens) if len(tokens) >= 2 else (None, [])

    @api.model
    def wc_create_bundle_boms(self, dry_run=True):
        """Create a kit bill of materials for every bundle whose contents resolve.

        A component resolves only when exactly one active base product of the
        right family carries its name ("迷你香 – 除障香"), so nothing is guessed.
        Bundles that do not resolve are listed for the shop to define by hand.
        """
        if 'mrp.bom' not in self.env:
            return {'error': 'mrp is not installed'}
        Product = self.env['product.product'].sudo()
        Bom = self.env['mrp.bom'].sudo()
        bundles = Product.search([('type', '=', 'consu'), ('sale_ok', '=', True)]).filtered(
            lambda p: _is_bundle_name(p.name) and not (p.default_code or '').startswith('INZ-'))
        built, unresolved = [], []
        for bundle in bundles:
            if Bom.search_count([('product_tmpl_id', '=', bundle.product_tmpl_id.id)]):
                continue
            family, tokens = self._bundle_components(bundle.name)
            if not family:
                unresolved.append({'bundle': bundle.display_name, 'reason': '名稱未列出成分或混合迷你香與長線香'})
                continue
            lines, missing = [], []
            for token, qty in tokens:
                # Components are single items: never another bundle, never a promoter duplicate.
                candidates = Product.search([('name', 'ilike', family), ('name', 'ilike', token),
                                             ('default_code', 'not like', 'INZ-%'), ('type', '=', 'consu')]
                                            ).filtered(lambda p: not _is_bundle_name(p.name))
                canonical = candidates.filtered(
                    lambda p: re.sub(r'\s', '', p.name) == re.sub(r'\s', '', '%s–%s' % (family, token)))
                pick = canonical if len(canonical) == 1 else (candidates if len(candidates) == 1 else Product)
                if len(pick) == 1:
                    lines.append((pick, qty))
                else:
                    missing.append('%s（%d 個候選）' % (token, len(canonical or candidates)))
            if missing:
                unresolved.append({'bundle': bundle.display_name, 'reason': '對不到：' + '、'.join(missing)})
                continue
            built.append({'bundle': bundle.display_name,
                          'components': ['%s x%d' % (p.display_name, q) for p, q in lines]})
            if not dry_run:
                Bom.create({
                    'product_tmpl_id': bundle.product_tmpl_id.id, 'type': 'phantom', 'product_qty': 1,
                    'bom_line_ids': [(0, 0, {'product_id': p.id, 'product_qty': q}) for p, q in lines],
                })
        return {'dry_run': dry_run, 'built': built, 'unresolved': unresolved}
