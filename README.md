# Woow Odoo WooCommerce Sync

Odoo 18 modules that import WooCommerce orders into Odoo as confirmed sales orders,
together with their customers, delivery addresses, products, promoters and stock moves.

繁體中文說明：[README_zh-TW.md](README_zh-TW.md)

## Modules

| Module | Purpose |
|---|---|
| `wc_base_connector` | Connection settings (store URL, WordPress application password) and the *WooCommerce / User* and *Manager* access groups. |
| `wc_order_sync` | The sync engine: polling cron, webhook receiver, status back-sync, customer / product mapping, delivery addresses, promoter tracking, watchdog, and one-off data repairs. |

`wc_order_sync` depends on `wc_base_connector`; install both.

## Requirements

- Odoo 18
- Python: `requests`
- Odoo modules: `sale_management`, `stock`, `mail`, `utm`
- Optional: `mrp` + `sale_mrp`, to stock bundles through kit bills of materials

## How an order flows

```
WooCommerce ──(every 15 min: orders modified since last success)──┐
WooCommerce ──(webhook: order created / updated)──────────────────┤
                                                                  ▼
                                                     WooCommerce › Sync Queue
                                                                  │  one row per WC order, raw JSON kept
                                                                  ▼
            customer ─ delivery address ─ products ─ promoter ─ sales order (confirmed)
                                                                  │
WooCommerce ──(status changes)── back-sync ──► cancel the order / validate its delivery
```

- **Polling** asks WooCommerce for orders *modified* since the last successful fetch
  (minus a 10-minute overlap). Filtering on modification rather than creation is what
  catches an order created while awaiting payment (ATM transfer, CVS payment code) and
  paid later. Orders already queued or imported are skipped, so the overlap is harmless.
- **The webhook** (`POST /wc_sync/webhook`) queues an order the moment it is created or
  paid. It verifies `X-WC-Webhook-Signature` against the webhook secret.
- **Back-sync** follows status changes of imported orders: cancelled / refunded / failed
  cancels the Odoo order; a shipped status validates its delivery when automatic stock
  deduction is on.
- The queue keeps each order's raw JSON, so history can be re-derived at any time
  (see *Data repairs*).

## Matching rules

### Customers

1. A registered WooCommerce account is always the same customer.
2. Anyone else is identified by **e-mail**, then **phone**. Phones are normalised, so
   `+886 912-345-678`, `886912345678` and `0912345678` are one number.
3. **Never by name.** Two people often share a name; wrongly merging them is far harder
   to undo than splitting one person in two (Odoo can merge contacts).
4. An order with **neither e-mail nor phone** goes to the shared contact **一般消費者**.

Each identity gets its own row in *WooCommerce › Partner Mapping*.

### Delivery address

- **CVS pickup** (7-ELEVEN, FamilyMart, Hi-Life): a delivery address named
  `recipient｜method store（store ID）` with the store's address, keyed on the store ID so
  a customer reusing a store reuses the address.
- **Home delivery to another address or recipient**: a delivery address from WooCommerce's
  shipping block.
- Otherwise the customer itself.

The order also records `wc_shipping_method`, `wc_cvs_store_name` and `wc_cvs_store_id`
(tab *WooCommerce* on the sales order; filter and group by shipping method).

### Products

Each order line is resolved in this order:

1. An existing row in *WooCommerce › Product Mapping* for that WC product.
2. The line's **SKU** against Odoo's **internal reference**. WooCommerce appends `-N` each
   time a product is duplicated for another promoter (`MIE009` → `MIE009-2` →
   `MIE009-1-1-2-1-2`), so trailing segments are stripped one at a time until a
   reference matches. References that contain a hyphen themselves (`INZ-219`) survive.
3. **Bundles** (names with `+`, `組`, `x2`…) are only matched to a product that is itself a
   bundle. Many bundles were made by duplicating a single item without changing its SKU;
   trusting that SKU would record a five-item set as one stick.
4. Otherwise the line goes to the placeholder product **待確認網店商品** (`WC-UNMATCHED`) and
   the mapping gets a *To Do* activity for a WooCommerce manager. Nothing is guessed from
   the name and no product is created automatically.

Set the Odoo product on the mapping row; later orders use it.

### Promoter (channel)

The short trailing `(…)` of a line name, e.g. `迷你香功能系列 – 十方清淨 (下班女子)`, is the
promoter. It becomes the order's **UTM source**; WooCommerce's own traffic attribution
(facebook, ig, google, direct) becomes the **UTM medium**. Sales › Reporting grouped by
*Source* is then sales per promoter. Descriptions, deity aliases (`土地公`), ingredients
and numbers in the same position are ignored.

## Configuration

*Settings › WooCommerce*:

| Setting | Meaning |
|---|---|
| WooCommerce URL, API Username, API Password | Store and WordPress application password used by the REST API. |
| Webhook Secret | Secret of the WooCommerce webhook pointing at `/wc_sync/webhook`. |
| Auto-confirm Orders | Confirm imported orders (creates their deliveries). |
| Auto-deduct Stock | Validate the delivery once WooCommerce reports the order shipped. **Keep off until the opening inventory count** (see below). |

System parameters (*Settings › Technical › System Parameters*):

| Key | Default | Meaning |
|---|---|---|
| `wc_order_sync.wc_auto_stock` | `False` | Same as *Auto-deduct Stock*. |
| `wc_order_sync.last_fetch_ok` | — | UTC time of the last successful fetch; the next fetch starts here. |
| `wc_order_sync.last_back_sync` | — | UTC time of the last status back-sync. |
| `wc_order_sync.watchdog_fetch_hours` | `2` | Alert when no successful fetch for this long. |
| `wc_order_sync.watchdog_order_hours` | `24` | Alert when no new order for this long. |
| `wc_order_sync.health_token` | generated | Token for the health endpoint. |

Shipped statuses (validate the delivery): `completed`, `wmp-shipped`, `wmp-in-transit`,
`ry-at-cvs`, `ry-out-cvs`. Cancelling statuses: `cancelled`, `refunded`, `failed`.

### WooCommerce side

Configure **one** webhook per topic (order created, order updated) with delivery URL
`https://<odoo>/wc_sync/webhook` and the same secret as in Odoo. A second webhook with an
old secret is rejected on every delivery; the log names it
(`Invalid signature (webhook id=…, topic=…)`) so it can be deleted in WooCommerce.

## Monitoring

- **Watchdog** (cron *WooCommerce: Sync Watchdog*, hourly) posts once to the Discuss
  channel **WooCommerce 同步警示** and notifies the WooCommerce managers when fetching has
  failed for 2 h, no order has arrived for 24 h, or queue rows are in error — and once more
  when it recovers.
- **Health endpoint** for an external monitor:
  `GET /wc_sync/health?token=<wc_order_sync.health_token>` returns fetch age, last order
  age, pending and error counts; HTTP 200 when healthy, 503 when degraded, 403 without the
  token.
- Fetch and back-sync failures are logged at `ERROR` level with the HTTP status and body.

## Stock go-live (opening inventory count)

Deliveries that were open before the count belong to goods already gone; the count
already reflects them. Validating them afterwards deducts them twice. On count day:

1. Count stock and enter it (*Inventory › Physical Inventory*).
2. In an Odoo shell, cancel the pre-count deliveries of orders WooCommerce already shipped:
   ```python
   env['wc.sync.queue'].wc_close_deliveries_before_count('2026-10-01 00:00:00', dry_run=True)   # review
   env['wc.sync.queue'].wc_close_deliveries_before_count('2026-10-01 00:00:00', dry_run=False)
   env.cr.commit()
   ```
3. Turn on *Auto-deduct Stock*.

## Bundles

A bundle is a product of its own whose stock moves through its components. With `mrp` and
`sale_mrp` installed, give it a **kit** bill of materials (type *Kit*); selling it then puts
each component on the delivery. `wc_create_bundle_boms` creates kits for bundles whose
name lists components that each resolve to exactly one base product
(`迷你香 – 除障香`); the rest are reported and need their contents entered by hand.

Bundle products created from WooCommerce use the bundle's base SKU as internal reference
and barcode (`CM002`); bundles duplicated from a single item have no usable SKU and get
`BND-001`, `BND-002`, …

## Data repairs

`models/wc_maintenance.py` re-derives data imported before 18.0.3 from the queue's raw
JSON, with the same rules as the live sync. Every method defaults to `dry_run=True` and
returns a report; run it in an Odoo shell and `env.cr.commit()` after a real run.

| Method | Repairs |
|---|---|
| `wc_create_bundle_products` | Creates the missing bundle products. Run before the product mappings. |
| `wc_repair_product_maps` | Re-points every product mapping by SKU; clears bundles mapped to a single item. |
| `wc_repair_customers` | Moves orders of different people off a shared customer; orders without contact to 一般消費者; one mapping per identity. Orders with invoices are skipped. |
| `wc_repair_shipping` | Records method / store on every order; gives open deliveries their CVS or shipping address. |
| `wc_repair_channels` | Sets promoter (UTM source) and traffic (UTM medium) on every order. |
| `wc_create_bundle_boms` | Kit bills of materials for bundles whose contents resolve (needs `mrp`). |
| `wc_close_deliveries_before_count` | See *Stock go-live*. |

Moving an order pins its pricelist, salesperson, team, payment terms and fiscal position,
so only the customer and addresses change.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Alert *超過 N 小時沒有成功從網店抓單* | Server log `WC Sync: fetch failed with HTTP …`; check the store URL and application password (*Settings › WooCommerce › Test Connection*). |
| Order lines on **待確認網店商品** | *WooCommerce › Product Mapping*, rows highlighted; set the Odoo product. |
| Log `WC Webhook: Invalid signature` | A webhook in WooCommerce has a different secret; the log names its id. |
| Order on **一般消費者** | The WC order carried neither e-mail nor phone. |
| Queue row in *Error* | Open it for the message; *Retry* after fixing. |

## Layout

```
addons/
├── wc_base_connector/
└── wc_order_sync/
    ├── controllers/webhook.py      webhook receiver, health endpoint
    ├── models/wc_sync_queue.py     fetch, import, matching rules
    ├── models/wc_watchdog.py       outage alerts
    ├── models/wc_maintenance.py    dry-run-first data repairs
    └── data/                       crons, 一般消費者
```

## Deployment

Clone into your Odoo addons path (e.g. `/mnt/extra-addons/`) via a symlink for each module,
or add the repo's `addons/` directly to `addons_path`.

Example symlink pattern (used in WOOWTECH k3s clone-addons Job):

```sh
git clone https://github.com/WOOWTECH/Woow_odoo_woocommerce_sync.git
for mod in wc_base_connector wc_order_sync; do
  ln -s "$PWD/Woow_odoo_woocommerce_sync/addons/$mod" "/mnt/extra-addons/$mod"
done
```

Upgrading to 18.0.3.0.0: update the module (`-u wc_order_sync`), then run the data repairs
above (dry run first). Keep *Auto-deduct Stock* off until the opening inventory count.

## Changelog

### wc_order_sync 18.0.3.0.0

- Fetch on modified time since the last success; failures logged as errors.
- Hourly watchdog with Discuss alerts; token-protected health endpoint.
- Customers by account → e-mail → phone, never name; no contact → 一般消費者.
- CVS store and separate shipping address become the delivery address.
- Products by SKU; bundles guarded; unmatched lines parked for review, never auto-created.
- Promoter → UTM source, WC traffic attribution → UTM medium.
- Deliveries validated for every shipped WC status, behind *Auto-deduct Stock*.
- App icon; webhook secret masked; raw payload visible to managers only; the rejecting
  webhook named in the log.
- `wc_maintenance.py` data repairs.

## License

LGPL-3.0. See [LICENSE](LICENSE).
