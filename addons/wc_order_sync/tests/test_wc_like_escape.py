# -*- coding: utf-8 -*-
"""E-mails and names from a WooCommerce payload are matched literally.

`_` and `%` are SQL LIKE wildcards; an `=ilike` built from an unescaped buyer
e-mail can attach the order to somebody else's customer record. Matching stays
case-insensitive and exact.
"""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'wc_security')
class TestWcLikeEscape(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        # New customers are created in Traditional Chinese.
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.Queue = cls.env['wc.sync.queue']
        Partner = cls.env['res.partner']
        cls.partner_axb = Partner.create({'name': 'WCSEC aXb', 'email': 'aXb@x.test', 'type': 'contact'})
        cls.partner_exact = Partner.create({'name': 'WCSEC exact', 'email': 'Exact.Case@X.test',
                                            'type': 'contact'})
        cls.env['partner.wc.map'].create({
            'wc_customer_name': 'WCSEC aXb', 'wc_customer_id': 0, 'wc_email': 'axb@x.test',
            'partner_id': cls.partner_axb.id, 'auto_matched': True,
        })

    def _order(self, email):
        return {'id': 660001, 'customer_id': 0,
                'billing': {'email': email, 'first_name': 'B', 'last_name': 'WCSEC '}}

    def test_find_partner_underscore_is_not_a_wildcard(self):
        self.assertFalse(self.Queue._find_partner_by_contact('a_b@x.test', ''))

    def test_find_partner_percent_is_not_a_wildcard(self):
        self.assertFalse(self.Queue._find_partner_by_contact('%@x.test', ''))
        self.assertFalse(self.Queue._find_partner_by_contact('%', ''))

    def test_find_partner_exact_email_any_case(self):
        self.assertEqual(self.Queue._find_partner_by_contact('exact.case@x.test', ''), self.partner_exact)
        self.assertEqual(self.Queue._find_partner_by_contact('axb@x.test', ''), self.partner_axb)

    def test_order_with_wildcard_email_gets_its_own_customer(self):
        for email in ('a_b@x.test', '%@x.test'):
            with self.subTest(email=email):
                partner = self.Queue._find_or_create_partner(self._order(email))
                self.assertNotIn(partner, self.partner_axb | self.partner_exact)
                self.assertEqual(partner.email, email)

    def test_order_with_known_email_in_other_case_keeps_its_customer(self):
        self.assertEqual(self.Queue._find_or_create_partner(self._order('AXB@x.TEST')), self.partner_axb)
        self.assertEqual(self.Queue._find_or_create_partner(self._order('EXACT.case@x.test')),
                         self.partner_exact)

    def test_promoter_name_is_matched_literally(self):
        source = self.env['utm.source'].create({'name': 'wcsecXpromoter'})
        self.assertEqual(self.Queue._utm_record('utm.source', 'WCSECXPROMOTER'), source)
        other = self.Queue._utm_record('utm.source', 'wcsec_promoter')
        self.assertNotEqual(other, source)
        self.assertEqual(other.name, 'wcsec_promoter')
