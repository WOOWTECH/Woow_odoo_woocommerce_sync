# -*- coding: utf-8 -*-
"""An imported order only gets a pricelist its own company may use.

A second company with a TWD pricelist sorted first used to make every import fail
with "incompatible companies". Black-box: only _process_wc_order (present before the
fix) is called, so on the unfixed module these tests fail the same way production did.
"""
import json

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'wc_security', 'wc_pricelist')
class TestWcOrderPricelist(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        # New customers are created in Traditional Chinese.
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.Queue = cls.env['wc.sync.queue']
        cls.company = cls.env.company
        cls.twd = cls.env.ref('base.TWD')
        cls.twd.active = True
        cls.other_company = cls.env['res.company'].create({'name': 'WCSEC other company'})
        Pricelist = cls.env['product.pricelist']
        # Lowest sequence: the first TWD pricelist a company-blind search returns.
        cls.other_pricelist = Pricelist.create({
            'name': 'WCSEC other company TWD', 'currency_id': cls.twd.id,
            'company_id': cls.other_company.id, 'sequence': 1,
        })
        cls.own_pricelist = Pricelist.create({
            'name': 'WCSEC own company TWD', 'currency_id': cls.twd.id,
            'company_id': cls.company.id, 'sequence': 2,
        })
        cls.product = cls.env['product.product'].create({'name': 'WCSEC pricelist product', 'list_price': 100.0})
        cls.env['product.wc.map'].create({
            'wc_product_name': 'WCSEC pricelist product', 'wc_product_id': 770101,
            'product_id': cls.product.id,
        })

    def _import(self, wc_id):
        payload = {
            'id': wc_id, 'number': str(wc_id), 'status': 'processing', 'customer_id': 0,
            'date_created': '2026-11-11T10:00:00', 'total': '100.00', 'currency': 'TWD',
            'billing': {'first_name': 'B', 'last_name': 'WCSEC', 'email': 'wcsec-pl-%s@x.test' % wc_id,
                        'phone': ''},
            'shipping': {'first_name': 'B', 'last_name': 'WCSEC'},
            'line_items': [{'name': 'WCSEC pricelist product', 'product_id': 770101, 'sku': '',
                            'quantity': 1, 'total': '100.00'}],
            'shipping_lines': [], 'fee_lines': [], 'coupon_lines': [], 'meta_data': [],
        }
        item = self.Queue.create({'wc_order_id': wc_id, 'payload': json.dumps(payload), 'state': 'pending'})
        return self.Queue._process_wc_order(payload, item)

    def test_order_gets_its_own_company_pricelist(self):
        order = self._import(770001)
        self.assertEqual(order.company_id, self.company)
        self.assertEqual(order.pricelist_id, self.own_pricelist)

    def test_other_company_pricelist_is_never_used(self):
        self.own_pricelist.action_archive()
        order = self._import(770002)
        self.assertEqual(order.company_id, self.company)
        self.assertNotEqual(order.pricelist_id, self.other_pricelist)
        self.assertFalse(order.pricelist_id.company_id - self.company)
