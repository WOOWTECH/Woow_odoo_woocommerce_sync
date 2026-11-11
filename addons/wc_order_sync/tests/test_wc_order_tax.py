# -*- coding: utf-8 -*-
"""Imported order lines are taxed the way WooCommerce taxed them.

WooCommerce decides what the customer pays. A line it charged no tax on must not pick up
the product's Odoo sale tax on import, or the Odoo order total exceeds the amount paid.
Black-box: only methods present before the fix are called, so on the unfixed module the
no-tax tests fail the way production did (products created in Odoo carry the company's
default sale tax).
"""
import json

from odoo import Command
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'wc_security', 'wc_tax')
class TestWcOrderTax(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        # New customers are created in Traditional Chinese.
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.Queue = cls.env['wc.sync.queue']
        company = cls.env.company
        cls.env.ref('base.TWD').active = True
        country = company.account_fiscal_country_id or company.country_id or cls.env.ref('base.tw')
        group = cls.env['account.tax.group'].create({
            'name': 'WCTAX group', 'company_id': company.id, 'country_id': country.id,
        })
        cls.tax = cls.env['account.tax'].create({
            'name': 'WCTAX 5%', 'amount': 5.0, 'amount_type': 'percent', 'type_tax_use': 'sale',
            'price_include_override': 'tax_excluded', 'company_id': company.id,
            'country_id': country.id, 'tax_group_id': group.id,
        })
        taxed = {'taxes_id': [Command.set(cls.tax.ids)]}
        cls.product = cls.env['product.product'].create(dict(taxed, name='WCTAX product', list_price=100.0))
        cls.env['product.wc.map'].create({
            'wc_product_name': 'WCTAX product', 'wc_product_id': 780101, 'product_id': cls.product.id,
        })
        # The products the import itself uses for shipping, fees and unmatched lines.
        cls.shipping_product = cls.Queue._get_or_create_service_product(
            name='Shipping', code='WC-SHIPPING', default_name='WooCommerce Shipping')
        cls.fee_product = cls.Queue._get_or_create_service_product(
            name='Fee', code='WC-FEE', default_name='WooCommerce Fee')
        cls.unmatched_product = cls.Queue._unmatched_product()
        (cls.shipping_product | cls.fee_product | cls.unmatched_product).write(taxed)

    def _import(self, wc_id, line_items, total, total_tax='0.00', ship_tax='0.00', fee_tax='0.00'):
        payload = {
            'id': wc_id, 'number': str(wc_id), 'status': 'processing', 'customer_id': 0,
            'date_created': '2026-11-11T10:00:00', 'currency': 'TWD',
            'total': total, 'total_tax': total_tax, 'shipping_total': '60.00', 'discount_total': '0.00',
            'billing': {'first_name': 'T', 'last_name': 'WCTAX', 'email': 'wctax-%s@x.test' % wc_id,
                        'phone': ''},
            'shipping': {'first_name': 'T', 'last_name': 'WCTAX'},
            'line_items': line_items,
            'shipping_lines': [{'method_id': 'flat_rate', 'method_title': '宅配', 'total': '60.00',
                                'total_tax': ship_tax}],
            'fee_lines': [{'name': '手續費', 'total': '30.00', 'total_tax': fee_tax}],
            'coupon_lines': [], 'meta_data': [],
        }
        item = self.Queue.create({'wc_order_id': wc_id, 'payload': json.dumps(payload), 'state': 'pending'})
        return self.Queue._process_wc_order(payload, item)

    def _line(self, order, product):
        line = order.order_line.filtered(lambda l: l.product_id == product)
        self.assertEqual(len(line), 1, product.display_name)
        return line

    def test_untaxed_wc_order_gets_no_odoo_tax(self):
        order = self._import(780001, [{'name': 'WCTAX product', 'product_id': 780101, 'sku': '',
                                       'quantity': 2, 'total': '200.00', 'total_tax': '0.00'}],
                             total='290.00')
        for product in (self.product, self.shipping_product, self.fee_product):
            self.assertFalse(self._line(order, product).tax_id, product.display_name)
        self.assertEqual(order.amount_tax, 0.0)
        self.assertEqual(order.amount_total, 290.0)

    def test_unmatched_line_gets_no_odoo_tax(self):
        order = self._import(780002, [{'name': 'WCTAX item nobody mapped', 'product_id': 780199, 'sku': '',
                                       'quantity': 1, 'total': '150.00', 'total_tax': '0.00'}],
                             total='240.00')
        self.assertFalse(self._line(order, self.unmatched_product).tax_id)
        self.assertEqual(order.amount_total, 240.0)

    def test_line_wc_taxed_keeps_product_tax(self):
        order = self._import(780003, [{'name': 'WCTAX product', 'product_id': 780101, 'sku': '',
                                       'quantity': 2, 'total': '200.00', 'total_tax': '10.00'}],
                             total='300.00', total_tax='10.00')
        self.assertEqual(self._line(order, self.product).tax_id, self.tax)
        self.assertFalse(self._line(order, self.shipping_product).tax_id)
        self.assertFalse(self._line(order, self.fee_product).tax_id)
        self.assertEqual(order.amount_tax, 10.0)
        self.assertEqual(order.amount_total, 300.0)
