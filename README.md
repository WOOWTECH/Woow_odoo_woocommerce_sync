# Woow Odoo WooCommerce Sync

Odoo 18 modules for syncing WooCommerce orders, customers, and products into Odoo.

## Modules

| Module | Purpose |
|---|---|
| `wc_base_connector` | Connection settings (URL, consumer key/secret, WordPress App Password) and access groups shared by all WooCommerce integrations. |
| `wc_order_sync` | Order/customer/product sync engine: REST polling cron, webhook receiver, back-sync of order status, product/partner mapping, shipping/tax/coupon capture. |

`wc_order_sync` depends on `wc_base_connector` — install both together.

## Requirements

- Odoo 18
- Python: `requests`
- Odoo deps: `sale_management`, `stock`, `mail`

## Layout

```
addons/
├── wc_base_connector/
└── wc_order_sync/
```

## Deployment

Clone into your Odoo addons path (e.g. `/mnt/extra-addons/`) via a symlink for each module, or add the repo's `addons/` directly to `addons_path`.

Example symlink pattern (used in WOOWTECH k3s clone-addons Job):

```sh
git clone https://github.com/WOOWTECH/Woow_odoo_woocommerce_sync.git
for mod in wc_base_connector wc_order_sync; do
  ln -s "$PWD/Woow_odoo_woocommerce_sync/addons/$mod" "/mnt/extra-addons/$mod"
done
```

## wc_order_sync 18.0.3.0.0

Behaviour changes, all verified against a copy of the production database:

- **Fetching** filters on WooCommerce *modified* time (`modified_after`, `dates_are_gmt`)
  from the last successful fetch, so an order paid after later orders were created is
  still picked up. A failed fetch is logged as an error instead of stopping silently.
- **Watchdog** (hourly cron): alerts once in the Discuss channel *WooCommerce 同步警示*
  when no successful fetch for 2 h, no new order for 24 h, or queue errors; announces
  recovery. Thresholds: `wc_order_sync.watchdog_fetch_hours` / `_order_hours`.
  `/wc_sync/health?token=<wc_order_sync.health_token>` returns the same numbers (503 when degraded).
- **Customers**: a registered account is its account; anyone else is identified by
  e-mail, then phone (normalised, `+886 912…` = `0912…`). Never by name alone. An order
  with neither goes to the contact *一般消費者*. Guests get their own mapping.
- **Delivery address**: CVS pickup creates a delivery address for the recipient at the
  store (keyed on the store ID); a different home address creates one too. The order
  records `wc_shipping_method`, `wc_cvs_store_id`, `wc_cvs_store_name`.
- **Products** are matched by SKU against the internal reference, stripping the `-N`
  suffixes WooCommerce adds when a product is duplicated per promoter. A bundle whose
  SKU points at a single item is not trusted. Unmatched lines go to the placeholder
  *待確認網店商品* and the mapping gets a review activity; nothing is guessed or auto-created.
- **Channel**: the promoter tag in the line names becomes the order's UTM source; WC's
  own traffic attribution becomes the UTM medium.
- **Stock**: deliveries are validated when WC reports any shipped status (`completed`,
  `wmp-shipped`, `wmp-in-transit`, `ry-at-cvs`, `ry-out-cvs`) and
  `wc_order_sync.wc_auto_stock` is on. Keep it off until the opening inventory count.

Data repairs for orders imported before this version live in `models/wc_maintenance.py`
(`wc_repair_customers`, `wc_repair_shipping`, `wc_repair_channels`,
`wc_create_bundle_products`, `wc_repair_product_maps`, `wc_create_bundle_boms`,
`wc_close_deliveries_before_count`). Each defaults to `dry_run=True` and returns a report.

## License

LGPL-3.0
