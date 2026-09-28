# Woow Odoo WooCommerce 同步

把 WooCommerce 網店訂單匯入 Odoo 18 成為已確認的銷售訂單，連同客戶、出貨地址、商品、推廣者與庫存異動一起處理。

English: [README.md](README.md)

## 模組

| 模組 | 用途 |
|---|---|
| `wc_base_connector` | 連線設定（網店網址、WordPress 應用程式密碼），以及「WooCommerce／使用者」「管理者」兩個權限群組。 |
| `wc_order_sync` | 同步主體：定時抓單、webhook 接收、狀態回頭同步、客戶與商品對應、出貨地址、推廣者追蹤、看門狗、歷史資料修復。 |

`wc_order_sync` 依賴 `wc_base_connector`，兩個要一起安裝。

## 需求

- Odoo 18
- Python：`requests`
- Odoo 模組：`sale_management`、`stock`、`mail`、`utm`
- 選配：`mrp` + `sale_mrp`，用「套組」物料清單讓組合商品扣到各成分的庫存

## 一張訂單怎麼進 Odoo

```
網店 ──（每 15 分鐘：上次成功之後有修改過的訂單）──┐
網店 ──（webhook：訂單建立／更新）─────────────────┤
                                                   ▼
                                    WooCommerce › 同步佇列
                                                   │  每張網店訂單一筆，保留原始資料
                                                   ▼
            客戶 ─ 出貨地址 ─ 商品 ─ 推廣者 ─ 銷售訂單（已確認）
                                                   │
網店 ──（狀態改變）── 回頭同步 ──► 取消訂單／完成出貨單
```

- **定時抓單**：向網店要「上次成功抓單之後**修改過**」的訂單（往前多抓 10 分鐘重疊）。用「修改時間」而不是「建立時間」，才抓得到先建立、晚一點才付款的單（ATM 轉帳、超商代碼）。已經在佇列或已匯入的單會自動略過，重疊不會重複建單。
- **webhook**（`POST /wc_sync/webhook`）：訂單一建立或一付款就進佇列，會驗證 `X-WC-Webhook-Signature` 簽章。
- **回頭同步**：追蹤已匯入訂單的狀態。網店取消／退款／失敗 → 取消 Odoo 訂單；網店已出貨 → 在開啟自動扣庫存時完成出貨單。
- 佇列保留每張單的原始資料，歷史資料隨時可以重新推導（見「資料修復」）。

## 對應規則

### 客戶

1. 網店的**會員帳號**永遠是同一位客戶。
2. 其他人依 **email**，再依**電話**辨識。電話會統一格式：`+886 912-345-678`、`886912345678`、`0912345678` 視為同一支。
3. **絕不只憑姓名。** 同名的人很多；把不同的人併在一起非常難拆，把同一人拆成兩位則可以用 Odoo 內建功能合併。
4. **沒有 email 也沒有電話**的訂單，歸到共用客戶「**一般消費者**」。

每個身分在「WooCommerce › 聯絡人對應表」各有一筆。

### 出貨地址

- **超商取貨**（7-ELEVEN、全家、萊爾富）：建立「收件人｜物流 門市（門市代號）」的送貨地址，地址是門市地址；以門市代號辨識，同一位客人選同一家門市會沿用。
- **宅配到別的地址或收件人**：用網店的收件資料建立送貨地址。
- 其他情況：客戶本身。

訂單也會記錄物流方式、門市名稱、門市代號（銷售訂單的「WooCommerce」分頁；可依物流方式篩選、分組）。

### 商品

每一行依序判斷：

1. 「WooCommerce › 商品對應表」裡這個網店商品已有的對應。
2. 用訂單行的 **SKU** 對 Odoo 的**內部參考**。網店每替一位推廣者複製一次商品，就在 SKU 後面加 `-數字`（`MIE009` → `MIE009-2` → `MIE009-1-1-2-1-2`），所以會一段一段去掉尾巴直到對上。本身就有橫線的編號（`INZ-219`）不受影響。
3. **組合商品**（名稱含 `+`、`組`、`x2` 等）只會對到本身也是組合的商品。很多組合是複製單品做的、SKU 沒改，照 SKU 對會把五樣一組記成一支單品。
4. 都對不到 → 先記在暫代商品「**待確認網店商品**」（`WC-UNMATCHED`），並在對應表上指派待辦給 WooCommerce 管理者。**不會用名稱猜，也不會自動建立商品。**

在對應表上指定 Odoo 商品後，之後的訂單就會用它。

### 推廣者（通路）

訂單行名稱最後括號裡的短字，例如「迷你香功能系列 – 十方清淨 (下班女子)」，就是推廣者，記在訂單的 **UTM 來源**；網店自己記錄的流量來源（facebook、ig、google、直接輸入）記在 **UTM 媒介**。在「銷售 › 報表」依「來源」分組，就是各推廣者的業績。同一位置的描述文字、神明別名（`土地公`）、成分、數字會自動排除。

## 設定

「設定 › WooCommerce」：

| 設定 | 說明 |
|---|---|
| 網址、API 帳號、API 密碼 | 網店與 WordPress 應用程式密碼，給 REST API 用。 |
| Webhook 密鑰 | 網店中指向 `/wc_sync/webhook` 的 webhook 所用的密鑰。 |
| 自動確認訂單 | 匯入後自動確認（會產生出貨單）。 |
| 自動扣庫存 | 網店回報已出貨時自動完成出貨單。**期初盤點完成前請保持關閉**（見下方）。 |

系統參數（「設定 › 技術 › 系統參數」）：

| 參數 | 預設 | 說明 |
|---|---|---|
| `wc_order_sync.wc_auto_stock` | `False` | 同「自動扣庫存」。 |
| `wc_order_sync.last_fetch_ok` | — | 上次成功抓單時間（UTC），下次從這裡開始抓。 |
| `wc_order_sync.last_back_sync` | — | 上次回頭同步時間（UTC）。 |
| `wc_order_sync.watchdog_fetch_hours` | `2` | 超過幾小時沒成功抓單就警示。 |
| `wc_order_sync.watchdog_order_hours` | `24` | 超過幾小時沒有新訂單就警示。 |
| `wc_order_sync.health_token` | 自動產生 | 健康檢查網址的權杖。 |

視為「已出貨」的網店狀態（會完成出貨單）：`completed`、`wmp-shipped`、`wmp-in-transit`、`ry-at-cvs`、`ry-out-cvs`。視為「取消」的：`cancelled`、`refunded`、`failed`。

### 網店端設定

每個主題（訂單建立、訂單更新）**只設一組** webhook，送達網址 `https://<odoo 網址>/wc_sync/webhook`，密鑰與 Odoo 相同。若另有一組用舊密鑰的 webhook，每次都會被拒收；日誌會寫出是哪一組（`Invalid signature (webhook id=…, topic=…)`），到網店後台刪掉即可。

## 監控

- **看門狗**（排程「WooCommerce：同步看門狗」，每小時）：超過 2 小時沒成功抓單、超過 24 小時沒有新訂單、或佇列有錯誤時，在討論頻道「**WooCommerce 同步警示**」發一則並通知 WooCommerce 管理者；恢復正常時再發一則。同一個問題不會重複通知。
- **健康檢查網址**（給外部監控用）：`GET /wc_sync/health?token=<wc_order_sync.health_token>`，回傳抓單間隔、最新訂單距今、待處理與錯誤數量；正常 200、異常 503、沒帶權杖 403。
- 抓單與回頭同步失敗會以 `ERROR` 等級寫入日誌，附上 HTTP 狀態與內容。

## 開啟庫存扣帳（期初盤點當天）

盤點前就還開著的出貨單，貨早就寄出了，盤點數量已經反映；盤點後再完成它們會**重複扣庫存**。盤點當天照這個順序：

1. 盤點並輸入數量（「庫存 › 實物庫存」）。
2. 在 Odoo shell 取消「網店已出貨、但出貨單在盤點前還開著」的舊出貨單：
   ```python
   env['wc.sync.queue'].wc_close_deliveries_before_count('2026-10-01 00:00:00', dry_run=True)   # 先看數量
   env['wc.sync.queue'].wc_close_deliveries_before_count('2026-10-01 00:00:00', dry_run=False)
   env.cr.commit()
   ```
3. 打開「自動扣庫存」。

## 組合商品

組合是一個獨立商品，庫存扣在它的各個成分上。安裝 `mrp` 與 `sale_mrp` 後，替它建立**套組**類型的物料清單，賣出時出貨單就會列出每個成分。`wc_create_bundle_boms` 會替「名稱有列出成分、而且每個成分都唯一對到一個基本商品（例如 `迷你香 – 除障香`）」的組合自動建立；其餘會列出來，需要手動填內容。

從網店建立的組合商品，內部參考與條碼用組合的基本 SKU（`CM002`）；複製單品做成、沒有可用 SKU 的組合則用 `BND-001`、`BND-002`……

## 資料修復

`models/wc_maintenance.py` 用佇列保留的原始資料，以跟線上同步相同的規則，重新推導 18.0.3 之前匯入的資料。每個方法預設 `dry_run=True`（只回報、不寫入）；在 Odoo shell 執行，正式執行後要 `env.cr.commit()`。

| 方法 | 修復內容 |
|---|---|
| `wc_create_bundle_products` | 建立缺少的組合商品。要在修商品對應之前執行。 |
| `wc_repair_product_maps` | 依 SKU 重新對應每個商品；清除「組合被對到單品」的對應。 |
| `wc_repair_customers` | 把不同人的訂單從同一位客戶身上移開；無聯絡資料的單移到一般消費者；每個身分一筆對應。已開發票的單會略過。 |
| `wc_repair_shipping` | 每張單記錄物流與門市；仍開著的出貨單補上超商或收件地址。 |
| `wc_repair_channels` | 每張單補上推廣者（UTM 來源）與流量來源（UTM 媒介）。 |
| `wc_create_bundle_boms` | 替成分可解析的組合建立套組物料清單（需要 `mrp`）。 |
| `wc_close_deliveries_before_count` | 見「開啟庫存扣帳」。 |

移動訂單時會固定住價目表、業務員、銷售團隊、付款條件與財務狀況，只改客戶與地址。

## 問題排除

| 現象 | 查哪裡 |
|---|---|
| 警示「超過 N 小時沒有成功從網店抓單」 | 伺服器日誌 `WC Sync: fetch failed with HTTP …`；檢查網址與應用程式密碼（設定 › WooCommerce › 測試連線）。 |
| 訂單行掛在「待確認網店商品」 | 「WooCommerce › 商品對應表」標色的列，指定 Odoo 商品。 |
| 日誌出現 `WC Webhook: Invalid signature` | 網店有一組 webhook 的密鑰不同，日誌會寫出它的編號。 |
| 訂單在「一般消費者」名下 | 那張網店訂單沒有 email 也沒有電話。 |
| 佇列有「錯誤」 | 打開看錯誤訊息，處理後按「重試」。 |

## 目錄

```
addons/
├── wc_base_connector/
└── wc_order_sync/
    ├── controllers/webhook.py      webhook 接收、健康檢查
    ├── models/wc_sync_queue.py     抓單、匯入、對應規則
    ├── models/wc_watchdog.py       同步停擺警示
    ├── models/wc_maintenance.py    先空跑的資料修復
    └── data/                       排程、一般消費者
```

## 部署

把 repo 放進 Odoo 的 addons 路徑（例如 `/mnt/extra-addons/`），每個模組建一個符號連結，或直接把 repo 的 `addons/` 加進 `addons_path`：

```sh
git clone https://github.com/WOOWTECH/Woow_odoo_woocommerce_sync.git
for mod in wc_base_connector wc_order_sync; do
  ln -s "$PWD/Woow_odoo_woocommerce_sync/addons/$mod" "/mnt/extra-addons/$mod"
done
```

升級到 18.0.3.0.0：更新模組（`-u wc_order_sync`），再執行上面的資料修復（先空跑）。期初盤點前請保持「自動扣庫存」關閉。

## 版本紀錄

### wc_order_sync 18.0.3.0.0

- 抓單改用「上次成功之後的修改時間」；失敗會記錄錯誤。
- 每小時看門狗，異常時在討論頻道警示；健康檢查網址需要權杖。
- 客戶依會員帳號 → email → 電話辨識，不用姓名；無聯絡資料 → 一般消費者。
- 超商門市與另外的收件地址成為出貨地址。
- 商品依 SKU 對應；組合有保護；對不到的先暫代並通知確認，不再自動建立。
- 推廣者 → UTM 來源，網店流量來源 → UTM 媒介。
- 所有「已出貨」網店狀態都能完成出貨單，由「自動扣庫存」控制。
- 應用程式圖示；webhook 密鑰遮蔽；原始資料只有管理者看得到；日誌寫出被拒收的 webhook。
- `wc_maintenance.py` 資料修復方法。

## 授權

LGPL-3.0，見 [LICENSE](LICENSE)。
