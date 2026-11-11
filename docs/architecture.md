# 使用者流程、背景架構與資料庫

本文對照目前的 [worker.py](../media_bot/worker.py)、[core.py](../media_bot/core.py)、[inbox.py](../media_bot/inbox.py) 與 [instagram_policy.py](../media_bot/instagram_policy.py)，描述實際背景核心行為。平台名稱在資料庫中使用 `instagram`、`tiktok`、`twitter` 和 `youtube`，其中 `twitter` 即 X。

## 1. User flow：從貼上連結到收到媒體

```mermaid
flowchart TD
    A[使用者私訊 Telegram Bot] --> B{私人對話且 User ID 獲允許？}
    B -->|否| Z[不處理這則訊息]
    B -->|是| C{訊息內容}
    C -->|start 或 help| H[顯示使用說明]
    C -->|其他指令或沒有連結| I[提示使用 help 或貼上連結]
    C -->|下載連結| D{連結數量}
    D -->|超過 50| E[提示拆成多則訊息]
    D -->|2 至 50| F[建立批次工作；按原順序處理]
    D -->|1| G[驗證並解析連結]
    G -->|不支援或解析失敗| J[回覆錯誤]
    G -->|IG 或 TikTok 貼文| K{使用設定快照}
    K -->|此貼文| Q[排隊下載]
    K -->|作者限時動態| Q
    K -->|舊 choice 工作逾時| X[工作逾時取消]
    G -->|限時動態或 X 單篇貼文| Q
    F --> Q
    Q --> R{下載結果}
    R -->|平台限流或配額用完| S[保存冷卻時間，到期續作]
    S --> Q
    R -->|登入或下載問題| T[顯示狀態；保留工作與已有檔案]
    R -->|有可傳送媒體| U{單檔大小}
    R -->|已確認沒有媒體| N[回覆沒有可見媒體]
    U -->|超出介面上限| V[設定URL則傳2小時連結，否則保留並標記部分完成]
    U -->|Local最多500MB或雲端小於49MiB| W[傳送相簿、影片、圖片或文件]
    W --> Y{Telegram 結果}
    Y -->|確認成功| DONE[保存逐檔收據，回報結果]
    Y -->|429 明確拒絕| WAIT[等待後重試傳送]
    WAIT --> W
    Y -->|逾時或結果不明| HOLD[保留檔案，不自動重送]
    Y -->|其他明確拒絕| PAUSE[暫停傳送，保留檔案]
```

圖中下載結果是使用者視角的摘要，單筆與批次的精確差異如下：

- **單筆 IG／TikTok 貼文**使用 `/settings` 保存的每位使用者設定，工作建立時保存快照；分享連結解析後套用同一快照，無法套用時記錄備援原因。舊 choice 工作仍可完成。
- **批次**的貼文連結直接下載貼文，限時動態連結保留限時動態模式；不逐項詢問。重複連結保留為不同項目，單項失敗可繼續後面的連結。
- **X**只接受單篇貼文。個人頁及搜尋頁會被拒絕。
- **空結果**不是一律成功：單筆確認無媒體可完成；批次項目的無媒體結果記為 `failed`，父工作最後可能是 `partial`。
- 缺少必要 Cookie 時，工作會 `paused`。下載中的驗證失敗，單筆會暫停；批次可能將該項記為失敗後繼續。更新外部 Cookie 後可重新提交連結。
- 媒體傳送的 400 拒絕可能先退回逐檔或文件形式；429 等待重試，其他明確拒絕暫停。網路逾時、5xx 或缺少有效收據視為結果不明。
- 最多 100 個 active 工作；達上限會拒絕新工作。使用者等待選擇的工作亦計入。

## 2. 背景處理與資料流

```mermaid
sequenceDiagram
    participant U as 使用者
    participant T as Telegram API
    participant P as Poller
    participant DB as state.sqlite3
    participant I as Inbox worker
    participant W as Platform worker
    participant R as Runner 子程序
    participant S as 社交平台
    participant FS as 媒體檔案系統
    U->>T: 傳送下載連結
    P->>T: getUpdates(offset)
    T-->>P: updates
    P->>DB: 同一 transaction 寫 inbox 與新 offset
    I->>DB: 按對話順序認領 update
    I->>DB: 建立 job 或 batch_items
    I->>T: 保存設定快照並回覆排隊狀態
    I->>DB: inbox done，清空 payload
    W->>DB: 選取到期的 queued/running/cooldown 工作
    W->>R: stdin 傳入工作、偏好與執行時 Cookie
    R->>S: 擷取 metadata 與媒體
    R->>DB: 媒體配額與平台 cooldown
    R->>FS: 媒體、部分下載、result.json
    R-->>W: 子程序結束
    W->>FS: 讀取 result.json
    W->>DB: 登記 files；批次同時提交該項結果
    W->>DB: 傳送前把 files 標記 uncertain
    W->>T: 串流傳送相簿或單檔
    alt 有完整成功收據
        T-->>W: message_id
        W->>DB: files sent + telegram_id
        W->>DB: job completed 或 partial
    else 傳送結果不明
        W->>DB: 保留 uncertain；job partial
    end
    W->>T: 更新工作狀態訊息
```

執行緒是 1 個 Telegram poller、4 個 inbox consumers、每個平台 1 個下載 worker；主迴圈約每 30 秒監督執行緒及做維護。每個平台依序處理工作，不同平台可以同時下載；批次父工作依目前項目切換所屬平台。下載器在獨立子程序中執行。

| 內容類型 | 實際引擎與相關模組 |
| --- | --- |
| Instagram | `gallery_runner` → gallery-dl + `instagram_web_gallery`／`instagram_web_client`；`instagram_policy` 限制 API 請求。 |
| TikTok 影片貼文 | `gallery_runner` → `yt_runner` → yt-dlp；最高畫質完整 HTTPS 影音，H.264僅同畫質優先；沒有時備援HTTP。 |
| YouTube 影片／Shorts | `core` 將單片網址統一為 watch URL → `gallery_runner` → `yt_runner` → yt-dlp。依快照選HDR／畫質；有FFmpeg可合併最佳影音，不重新編碼。獨立佇列、配額與冷卻，預設匿名，可選外部YouTube域Cookie。 |
| TikTok 照片／限時動態 | `gallery_runner` → gallery-dl + `tiktok` adapter。 |
| X 貼文 | `gallery_runner` → `twitter_runner` → twscrape；媒體另由 HTTP 客戶端串流下載。 |

TikTok完整影音按畫質優先，H.264同畫質優先；不合併、不轉碼。所有平台套用單一媒體2000 MB下載限制；分離影音合計但不重算合併副本。

進入 TikTok 媒體下載前，runner 先取得配額，再核對輸出旁的 `.format.json`（影片／格式 ID、編碼、容器、尺寸、幀率及大小；不含 URL／Cookie）。紀錄相符才使用 HTTP Range 續傳；紀錄缺失、損壞或不同時，將原 `.part` 保留到同目錄唯一的 `.part.preserved-*` 名稱，原子更新格式紀錄後從頭下載。保留檔不列入傳送或已完成媒體清理；已完成檔直接沿用，配額阻擋時不修改 partial／marker。符號連結或非一般檔案的 partial／marker 會拒絕處理。

## 3. 工作與檔案狀態

```mermaid
stateDiagram-v2
    [*] --> choice: 單筆 IG 或 TikTok 貼文
    [*] --> queued: 其他連結或批次
    choice --> queued: 使用者選擇
    choice --> cancelled: 選擇逾時
    queued --> choice: 僅舊工作需要選擇
    queued --> running: 開始下載
    queued --> cooldown: 解析分享連結時遇到限流
    running --> cooldown: 平台限流或媒體配額
    cooldown --> running: 到期續作
    cooldown --> choice: 舊工作仍需選擇
    running --> queued: 批次交接下一項或準備結算
    running --> paused: 必要登入資料缺少或部分可恢復錯誤
    running --> failed: 工作無法完成
    queued --> failed: 解析或執行錯誤
    running --> completed: 單筆確認沒有媒體
    running --> sending: 有媒體可傳送或批次結算
    sending --> sending: Telegram 429 到期再傳
    sending --> paused: Telegram 明確拒絕
    sending --> partial: 部分結果或不明傳送或超大檔案
    sending --> completed: 完整完成
    running --> cancelled: 取消旗標或使用權限撤回
```

此圖呈現主要路徑。取消旗標／權限撤回也能影響其他 active 狀態；本核心的 Telegram 指令提供 `/start`、`/help`、`/settings`，並相容舊下載選擇。`paused`、`partial`、`failed` 不會被平台迴圈自動重新挑選；程序重啟會恢復可執行狀態，並將曾留下 `uncertain` 檔案的工作設為 `partial`。

```mermaid
stateDiagram-v2
    [*] --> pending: 已下載並登記
    pending --> uncertain: 發送前先保存 checkpoint
    uncertain --> sent: 收到有效 message_id 並保存
    uncertain --> pending: Telegram 明確拒絕
    uncertain --> uncertain: 逾時或缺少收據，保留待確認
    uncertain --> too_large: 超出介面上限且未設定URL，不發送
    sent --> expired: 父工作 completed 且達連結2小時或直接交付24小時期限
```

`uncertain` 同時是發送前的防重送 checkpoint 和傳送結果不明時保留的狀態，不能單憑它判斷對方已收到或未收到。SQLite transaction 與 Telegram 網路傳送不能一起原子提交，因此這個設計**不保證 exactly-once delivery**；它以避免自動重複傳送為優先。

## 4. `state.sqlite3` ER diagram

```mermaid
erDiagram
    jobs ||--o{ files : contains
    files ||--o{ links : grants
    jobs ||--o| batch_requests : has
    jobs ||--o{ batch_items : contains
    jobs {
        TEXT id PK
        TEXT update_key UK
        INTEGER chat
        TEXT url
        TEXT platform
        TEXT mode
        TEXT state
        TEXT message
        INTEGER status_id
        REAL retry_at
        REAL created
        REAL delivered
        INTEGER cancel
        TEXT preferences
    }
    files {
        INTEGER id PK
        TEXT job FK
        TEXT path
        TEXT name
        INTEGER size
        TEXT state
        TEXT telegram_id
        REAL link_retention_until
    }
    links {
        TEXT token PK
        INTEGER file FK
        REAL expires
    }
    batch_requests {
        TEXT job PK,FK
        INTEGER message_id
    }
    batch_items {
        TEXT job PK,FK
        INTEGER position PK
        TEXT original_url
        TEXT url
        TEXT platform
        TEXT mode
        TEXT state
        TEXT error
        INTEGER error_notified
    }
    telegram_inbox {
        INTEGER id PK
        INTEGER chat
        TEXT payload
        REAL received
        TEXT state
        REAL started
        REAL finished
    }
    settings {
        TEXT key PK
        TEXT value
    }
    media_budget {
        TEXT platform PK
        TEXT item PK
        REAL time
    }
```

ER 圖顯示應用使用的關聯；精確 nullable、default、UNIQUE 與 FK 定義以 [state-schema.sql](state-schema.sql) 為準。三條 `job → jobs.id` 及 `links.file → files.id` 為資料庫外鍵。`telegram_inbox` 與 `jobs` 透過程式產生的 `update_key` 關聯，**沒有 FK**；`chat` 是 Telegram ID，沒有本機 users 表。`settings`、`media_budget` 亦沒有指向 jobs 的外鍵。

YouTube 格式優先順序、stream copy 與驗證方式見 [YouTube 下載說明](youtube.md)。`youtube` 平台值沿用現有TEXT；本次設定／URL功能會新增欄位與links表。

### jobs：每次下載請求的父工作

| 欄位 | 意義 |
| --- | --- |
| `id` | 隨機 16 字元十六進位工作 ID，主鍵。 |
| `update_key` | 唯一入列鍵；單筆為 update ID 加連結序號，批次為 update ID 加 `:batch`，防止同一 update 重複建工作。不是 URL 去重。 |
| `chat` | 使用者的 Telegram 私人 chat ID；也是回傳目的地。 |
| `url` | 單筆目前使用的網址；批次父工作初始化時保存第一個有效項目網址，逐項網址以 `batch_items` 為準。 |
| `platform` | 單筆的平台；批次目前應由哪個平台 worker 處理，交接時會改變。 |
| `mode` | `choice`、`resolve`、`post`、`stories` 或 `batch`。 |
| `state` | 工作狀態，見前述 state diagram。 |
| `message` | 最近的使用者狀態文字，預設空字串。 |
| `status_id` | 可被編輯的 Telegram 進度訊息 ID。 |
| `retry_at` | Unix 秒；最早可再次選取的時間，預設 0。 |
| `created` | 工作建立的 Unix 秒。 |
| `delivered` | 完成傳送結算時的 Unix 秒；`partial` 也可能有值，不能單靠它證明所有媒體均送達。 |
| `cancel` | 取消旗標，預設 0。 |

### files：本機媒體與 Telegram 收據

| 欄位 | 意義 |
| --- | --- |
| `id` | INTEGER 主鍵。 |
| `job` | 所屬父工作 ID，FK 至 `jobs.id`。 |
| `path` | 本機媒體路徑；讀取／清理時必須位於資料目錄的 `media` 下。 |
| `name` | 傳送時使用的檔名。 |
| `size` | 登記時的 byte 大小。 |
| `state` | `pending`、`uncertain`、`sent`、`too_large` 或 `expired`。 |
| `telegram_id` | 成功傳送的 Telegram **message_id**，以文字保存；不是 Telegram file_id。 |

`UNIQUE(job, path)` 防止同一工作重複登記同一路徑。批次檔案仍只 FK 到父工作，**沒有 batch item FK**；目錄中的 `item-<position>` 區分下載來源。

### batch_requests 與 batch_items：一則訊息的多個連結

| 表／欄位 | 意義 |
| --- | --- |
| `batch_requests.job` | 主鍵及 FK，一個批次父工作至多一列。 |
| `batch_requests.message_id` | 使用者原始訊息 ID，用於回覆單項錯誤。 |
| `batch_items.job` + `position` | 複合主鍵；position 從 0 起，維持連結順序。job 亦為 FK。 |
| `original_url` | 收到的原始連結，用於錯誤回覆。 |
| `url` | 驗證／解析後目前使用的網址。 |
| `platform`、`mode` | 該項目的平台及下載模式；解析分享連結後可更新。 |
| `state` | `pending`、`done` 或 `failed`；父工作另記 `running`，子項不使用 running 狀態。 |
| `error` | 該項的失敗／無媒體說明，預設空字串。 |
| `error_notified` | 0：尚待通知；1：已先設 checkpoint，可能傳送中或結果不明；2：已確認通知成功。這不是 Boolean。 |

### telegram_inbox：持久收件匣

| 欄位 | 意義 |
| --- | --- |
| `id` | Telegram update_id，INTEGER 主鍵，用於去重。 |
| `chat` | 對話 ID，用於同一對話內的順序處理。 |
| `payload` | 尚待處理的 update JSON；完成或結果不明後清為空字串。 |
| `received`、`started`、`finished` | 接收、認領及處理結束的 Unix 秒。 |
| `state` | `pending`、`working`、`done` 或 `uncertain`。 |

`telegram_inbox_order(chat, state, id)` 支援順序挑選；待處理／處理中的容量上限為 1000。重啟時 `working` 轉為 `uncertain`，不直接重播可能已送出回覆的 handler。結束超過 24 小時的非 active 收件紀錄可清理。

### settings 與 media_budget：執行狀態與配額

| 表／欄位 | 意義 |
| --- | --- |
| `settings.key` | 文字主鍵。 |
| `settings.value` | JSON 編碼的文字值；不是 SQL JSON 型別。 |
| `media_budget.platform` + `item` | 複合主鍵；平台加媒體路徑識別，在同一時間窗內避免重複預留。 |
| `media_budget.time` | 配額預留的 Unix 秒；`media_budget_time(platform, time)` 為查詢索引。 |

常用 settings keys：`enabled`、`telegram_offset`、`telegram_health`、`telegram_last_poll`、`<platform>_cooldown`、`<platform>_media_limit`、`partial:<job>`、`partial_reason:<job>`。預設每平台每滾動 60 秒允許 20 個新媒體預留；它與 Instagram 的 API 請求限速是兩個獨立限制。

## 5. `instagram-api-rate.sqlite3` schema

```mermaid
erDiagram
    requests {
        REAL sent_at
    }
    state {
        INTEGER id PK
        REAL blocked_until
    }
    history_coverage {
        INTEGER id PK
        REAL since
    }
```

| 表 | 用途與約束 |
| --- | --- |
| `requests` | 每次 API 請求預留的 `sent_at`；NOT NULL，沒有顯式主鍵或索引。保留約一小時紀錄。 |
| `state` | 單列 `id=1`，由 CHECK 限制；`blocked_until` 為持久冷卻期限。 |
| `history_coverage` | 單列 `id=1`，由 CHECK 限制；`since` 表示歷史資料已知的起點，避免把缺漏歷史當成零請求。 |

這三表沒有外鍵。精確 DDL 見 [instagram-rate-schema.sql](instagram-rate-schema.sql)。`RequestWindow` 另以目前時間初始化單列狀態，因此 DDL 文件只有結構，不可代替應用啟動程序。實際 Instagram adapter 明確設定 **每分鐘最多 10 次 API 請求、至少相隔 6 秒**，計算額外加 0.05 秒邊界；與 `instagram_policy.py` 通用類別的預設 12 次／5 秒不同。

## 6. 資料與恢復界線

```text
MEDIA_BOT_DATA/
  state.sqlite3                 工作、收件、傳送與配額
  state.sqlite3-wal / -shm       主 DB 的 WAL 輔助檔，存在時須一併考量
  instagram-api-rate.sqlite3    Instagram API 請求歷史
  media/
    <job-id>/                   單筆工作媒體與 result.json
    <job-id>/item-<position>/    批次項目媒體與 result.json
  tmp/                          子程序使用的暫存目錄
```

- 主 DB 使用 WAL，`core.db()` 開啟 foreign keys；限速 DB 使用獨立 SQLite 連線，程式沒有為它設定 WAL。所有 REAL 時間欄位是 Unix 秒，對外冷卻提示使用 UTC。
- 狀態字串及平台／模式值由程式維持，主 DB 沒有 ENUM／CHECK 約束；精確 nullable 和 defaults 保留在 DDL，不推測成額外約束。
- Token／Cookie 由外部受限檔案載入，沒有資料庫憑證欄位。媒體內容存在檔案系統，沒有存進 SQLite BLOB。
- 至少保留 5 GiB 可用空間，媒體暫存達 3 GiB 時拒絕新增下載。單筆空間／執行錯誤可能成為 `failed`；批次對部分儲存問題設為 `paused`。這兩種狀態不會自動被選取。
- 清理只刪除 `files.state=sent`、父工作 `completed` 且連結保留期限已到或直接交付超過24小時的媒體，每輪最多 16 個。`partial`、`uncertain`、`too_large` 媒體保留；jobs／files metadata 沒有同時刪除。
- X 的 `.receipt.json` 保存媒體大小與 SHA-256，用來驗證下載檔案；SQLite 的 `telegram_id` 則證明 Telegram 回覆成功。兩種收據證明不同階段。
- 本文件及 SQL 是從程式整理的開發參考；SQL 可在全新隔離 DB 檢查結構，日常啟動仍交由程式初始化，不應當作現有資料的重設或 migration 指令。

## 設定快照、Local API與可選下載服務

`preferences.py` 只允許當前私人對話使用者修改自己的設定；`jobs.preferences` 為不可變JSON快照。`settings.download_preferences:<chat>` 保存未來工作偏好；`preference_notice:<job>` 保存無法套用偏好的原因。舊資料表由startup新增欄位，不改寫舊工作。

`limits.py` 統一500,000,000-byte直傳／2,000,000,000-byte下載上限。`links.py` 可選啟動loopback服務；`links(token,file,expires)` 的token是SHA-256而非原始capability，file外鍵連接files，expires為Unix秒。`files.link_retention_until` 預設0代表原有24小時直接交付；新URL檔記錄最後到期時間，只有已確認且完整交付的檔案可到期刪除。API及URL操作流程見 [部署說明](local-api.md)，DDL見 [state-schema.sql](state-schema.sql)。

Telegram媒體使用有限response/read timeout及全域大型傳送semaphore；結果不明的收據不自動重送。GET／HEAD／Range存取會檢查capability、2小時到期、使用者允許名單及媒體路徑；網址服務未設定時超限檔保留本機，不能當成已交付。

Instagram「指定連結中的圖片」以每條URL的 `img_index` 取得原輪播位置，不使用設定中的全域圖片序號。手動序號只用於IG限時動態；TikTok只有全部貼文／全部限時動態，舊selected偏好讀取時轉為相應all模式。
