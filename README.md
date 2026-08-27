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

## License

LGPL-3.0
