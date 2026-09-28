# -*- coding: utf-8 -*-
import logging

from markupsafe import Markup
from odoo import api, fields, models

from .wc_sync_queue import FETCH_OK_PARAM

_logger = logging.getLogger(__name__)

WATCHDOG_STATE_PARAM = 'wc_order_sync.watchdog_alert'
WATCHDOG_CHANNEL_PARAM = 'wc_order_sync.watchdog_channel_id'


class WcSyncQueue(models.Model):
    _inherit = 'wc.sync.queue'

    @api.model
    def _wc_health(self):
        """Numbers describing whether the sync is alive. Ages are in hours, None if unknown."""
        ICP = self.env['ir.config_parameter'].sudo()
        now = fields.Datetime.now()
        last_ok = ICP.get_param(FETCH_OK_PARAM, '')
        fetch_age = None
        if last_ok:
            fetch_age = (now - fields.Datetime.to_datetime(last_ok.replace('T', ' '))).total_seconds() / 3600
        last_item = self.sudo().search([], order='create_date desc', limit=1)
        order_age = (now - last_item.create_date).total_seconds() / 3600 if last_item else None
        return {
            'last_fetch_ok': last_ok or None,
            'fetch_age_hours': round(fetch_age, 2) if fetch_age is not None else None,
            'last_order_age_hours': round(order_age, 2) if order_age is not None else None,
            'pending': self.sudo().search_count([('state', '=', 'pending')]),
            'errors': self.sudo().search_count([('state', '=', 'error')]),
        }

    @api.model
    def _cron_watchdog(self):
        """Alert once when the sync stops, and once when it recovers.

        The shop takes 8-16 orders a day, so a sync that has not fetched
        successfully for hours, or a day without a single new order, means
        something is broken - as it was for 17 days in August 2026 without
        anyone noticing.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        health = self._wc_health()
        max_fetch = float(ICP.get_param('wc_order_sync.watchdog_fetch_hours', '2'))
        max_order = float(ICP.get_param('wc_order_sync.watchdog_order_hours', '24'))
        problems = {}
        if health['fetch_age_hours'] is None or health['fetch_age_hours'] > max_fetch:
            problems['fetch'] = "超過 %s 小時沒有成功從網店抓單（上次成功：%s UTC）" % (
                int(max_fetch), health['last_fetch_ok'] or '從未')
        if health['last_order_age_hours'] is not None and health['last_order_age_hours'] > max_order:
            problems['orders'] = "超過 %s 小時沒有任何新訂單進入 Odoo（最後一張在 %.0f 小時前）" % (
                int(max_order), health['last_order_age_hours'])
        if health['errors']:
            problems['errors'] = "同步佇列有 %d 筆錯誤等待處理" % health['errors']
        key = ','.join(sorted(problems))
        previous = ICP.get_param(WATCHDOG_STATE_PARAM, '')
        if key == previous:
            return health
        if problems:
            body = Markup("<p><b>⚠ WooCommerce 同步異常</b></p><ul>%s</ul>"
                          "<p>請到 WooCommerce → 同步佇列 查看。</p>") % Markup('').join(
                Markup("<li>%s</li>") % text for text in problems.values())
            _logger.error("WC Watchdog: %s", '; '.join(problems.values()))
        else:
            body = Markup("<p><b>✅ WooCommerce 同步已恢復正常</b></p>")
            _logger.info("WC Watchdog: sync recovered")
        self._watchdog_post(body)
        ICP.set_param(WATCHDOG_STATE_PARAM, key)
        return health

    @api.model
    def _watchdog_post(self, body):
        """Post to the alert channel, creating it (with the WooCommerce managers) on first use."""
        ICP = self.env['ir.config_parameter'].sudo()
        Channel = self.env['discuss.channel'].sudo()
        managers = self.env.ref('wc_base_connector.group_wc_manager').sudo().users
        channel = Channel.browse(int(ICP.get_param(WATCHDOG_CHANNEL_PARAM, '0') or 0)).exists()
        if not channel:
            channel = Channel.create({'name': 'WooCommerce 同步警示', 'channel_type': 'channel',
                                      'description': 'WooCommerce 訂單同步的自動警示'})
            channel.add_members(partner_ids=managers.partner_id.ids)
            ICP.set_param(WATCHDOG_CHANNEL_PARAM, str(channel.id))
        channel.message_post(body=body, message_type='comment', subtype_xmlid='mail.mt_comment',
                             partner_ids=managers.partner_id.ids)
