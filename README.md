# Telegram Social Downloader

獨立背景服務核心：接收 Telegram 私人訊息中的 Instagram、TikTok、Twitter／X 和 YouTube 連結，下載媒體並傳回同一個對話。

- Instagram 貼文、Reels、限時動態；用 `/settings` 預先指定內容與 HDR 偏好，貼連結後直接下載。
- TikTok 影片、照片貼文及平台允許存取的限時動態；支援分享短連結。
- Twitter／X 單篇貼文的圖片、影片及動態圖片。
- YouTube 公開影片及 Shorts：接受 `youtube.com/watch?v=…`、`youtube.com/shorts/…` 和 `youtu.be/…`。可以混入批次；有播放清單參數時只下載該影片。不支援播放清單、頻道或進行中直播；預設匿名，可選擇外部 YouTube Cookie。
- 每則訊息最多 50 個連結、SQLite 持久佇列、平台限流冷卻及下載續作。
- 串流上傳、圖片／影片相簿和逐檔傳送收據。傳送結果不明時保留檔案，不自動重送。

以 Python 套件提供背景服務核心、固定版本依賴及離線測試。開發者可以了解從 Telegram 收件、連結解析、平台下載到媒體傳送的完整流程，並使用外部設定啟動獨立程序。

## 安裝與啟動

需要 Linux、Python 3.11+，建議安裝 `ffprobe` 以取得影片尺寸；缺少時會以原始文件傳送影片。

YouTube 請將 [Deno](https://github.com/yt-dlp/yt-dlp/wiki/EJS) 加入 bot 程序的 `PATH`；既有依賴已包括 EJS。預設 HDR 優先，然後選最高畫質，H.264 僅在相同畫質時優先。建議安裝 FFmpeg 合併原始影音串流（不重新壓制）；沒有 FFmpeg 時只選完整影音。完整選擇規則見 [YouTube 下載說明](docs/youtube.md)。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m media_bot --help
```

啟動前在外部設定下列環境變數。不要把真實值或檔案放進此 repository。

| 變數 | 用途 |
| --- | --- |
| `MEDIA_BOT_DATA` | 已存在的絕對資料目錄；保存佇列、媒體和暫存。必須指向預期的媒體儲存裝置，沒有預設目錄。 |
| `MEDIA_BOT_ALLOWED_USERS` | 允許使用的 Telegram 數字 User ID，以逗號分隔；必填，缺少時拒絕所有使用者。 |
| `TELEGRAM_BOT_TOKEN_FILE` | 外部 Bot token 純文字檔的絕對路徑；必填。 |
| `INSTAGRAM_COOKIES_FILE` | 外部 JSON Cookie 物件路徑；Instagram 需要 `sessionid`、`csrftoken`。 |
| `TIKTOK_COOKIES_FILE` | 外部 JSON Cookie 物件路徑；沒有設定時嘗試公開內容。 |
| `TWITTER_COOKIES_FILE` | 外部 JSON Cookie 物件路徑；X 需要 `auth_token`、`ct0`。 |
| `YOUTUBE_COOKIES_FILE` | 可選外部 Netscape Cookie 檔；只接受 YouTube 域 Cookie，未設定時匿名。 |
| `MEDIA_BOT_API_BASE` | 預設官方雲端 API；可設 `http://127.0.0.1:8081` 使用自行運行的 Local Bot API。只接受 literal loopback HTTP。 |
| `MEDIA_BOT_LOCAL_FILE_UPLOADS` | 可選 `1`，讓 Local Bot API 讀取相同絕對路徑的媒體；預設 multipart。 |
| `MEDIA_BOT_DOWNLOAD_BASE_URL` | 可選 HTTPS 下載前綴，例如 `https://downloads.example.com/files`；未設時不啟動 URL 服務。 |
| `MEDIA_BOT_DOWNLOAD_PORT` | URL 服務在 `127.0.0.1` 監聽的埠，預設 `8090`。 |

Instagram／TikTok／X Cookie 檔案格式是名稱到字串值的 JSON 物件；YouTube 使用 Netscape 格式。Token／Cookie 檔案必須在程式目錄及資料目錄以外，檔案權限為 `0600`，不能是符號連結。登入資料由執行環境提供。只向明確允許的使用者提供此 Bot；他們能取得執行者 Cookie 有權存取的內容。

```sh
.venv/bin/python -m media_bot --check
.venv/bin/python -m media_bot
```

第一個指令只驗證設定，不連線；第二個持續執行背景輪詢。每個 Bot token／資料目錄只執行一個實例，不要同時使用 webhook 或另一個輪詢程序。可由自己的程序監督工具執行上述命令。

在 Telegram 輸入 `/help`、`/settings` 或貼上連結即可。IG「指定連結中的圖片」讀取每條貼文連結的 `?img_index=3`（原始輪播第 3 項），不設定全域圖片序號。缺少或格式無效時下載全部內容；超出範圍或位置不是圖片時改下載可確認的圖片，沒有圖片則保留可確認的原內容並說明。IG 限時動態一律下載全部可存取內容，不需設定序號。舊指定限時動態偏好改為全部限時動態，舊手動序號忽略。TikTok 只提供貼文全部內容／作者全部限時動態，不提供指定項目。工作保存送出時的設定，之後修改不影響排隊工作。偏好無法套用時回報備援原因。啟用權限由外部靜態 User ID 設定決定。

## 儲存與失敗處理

媒體和暫存同在 `MEDIA_BOT_DATA` 檔案系統；至少保留 5 GiB 可用空間，媒體暫存達 3 GiB 時停止新增下載。掛載及儲存裝置身分驗證由執行環境負責。

單檔下載超過 **2000 MB** 立即取消（十進位 MB）；已知大小在下載前拒絕，未知大小在串流過程中停止，已有 partial 保留。分離影音以同一影片的合計大小計算，不重複計算合併檔。

使用 Local Bot API 時，**最多 500 MB** 直接傳送；超過 500 MB 提供 URL 並說明原因。使用預設雲端 API 時，達 49 MiB 改走 URL。URL 服務是可選項：未設定時保留本機檔案並標記 `partial`／`too_large`，不會虛構網址或記錄為傳送成功。

下載網址及已確認交付、工作完整完成的連結檔案保留 **2 小時**；直接成功傳送的檔案保留 24 小時。失敗、部分完成或結果不明的原檔保留供處理，網址仍會到期。Cookie 失效工作暫停；更新外部 Cookie 後可重新提交。結果不明的傳送不會自動重送。

Local Bot API 的 `api_id`／`api_hash` 由操作者向 Telegram 取得並供給獨立 API 程序，並非 Bot token。本 repository 不包含 daemon、主機服務設定或登入資訊。URL 服務需要自行設定 HTTPS 反向代理；啟動、權限、資源及遷移說明見 [Local API 與下載網址](docs/local-api.md)。

第三方平台可能要求登入、驗證或限制地區；相容性取決於平台回應。依賴版本刻意固定，Instagram adapter 必須配合指定的 gallery-dl 版本升級。

## 開發驗證

修改前請閱讀 [AGENTS.md](AGENTS.md)。每次程式、依賴、設定介面、測試或文件變更，都必須在同一 commit 更新 [CHANGELOG.md](CHANGELOG.md) 的 `Unreleased` 區段；版本發布時再整理版本與日期。

```sh
.venv/bin/python -m unittest discover -s tests -v
```

測試使用合成資料及模擬網路回應，不需要真實 Bot token、Cookie 或帳號。這套離線測試不會發送 Telegram 訊息或下載真實平台媒體；另外的實站驗證記錄見 [PROGRESS.md](PROGRESS.md)。


## 開發者導覽

使用者流程、背景時序、工作／檔案狀態圖及完整資料字典，見 [架構與資料庫文件](docs/architecture.md)。兩個 SQLite 資料庫的參考 DDL 分別在 [工作資料庫](docs/state-schema.sql) 與 [Instagram 限速資料庫](docs/instagram-rate-schema.sql)。

這個 repository 展示四個平台下載器的實際程式流程、模組分工、輸入設定、持久化資料結構，以及下載／傳送失敗時的處理規則。可從 `__main__.py` 開始看啟動，再看 `worker.py` 的協調流程，接著閱讀 `core.py` 和各平台 runner。

### 一則訊息如何變成下載結果

1. `__main__.py` 解析啟動參數，`config.py` 驗證外部設定，再啟動 `worker.main()`。
2. `worker.poll()` 向 Telegram 取得訊息；`inbox.store()` 同時保存合資格的私人訊息與輪詢 offset。不同對話可並行，同一對話保持處理順序。
3. `worker.handle_update()` 處理連結及每位使用者的設定快照；`core.py` 驗證網址並寫入單筆或批次工作。
4. 各平台 worker 挑選工作，透過子程序執行 `gallery_runner.py`。TikTok 影片和 YouTube 影片轉交 `yt_runner.py`，X 貼文轉交 `twitter_runner.py`；Instagram 與其他 TikTok 內容使用 gallery-dl。
5. Runner 保存媒體與結果；`worker.deliver()` 傳送相簿、影片、圖片或文件。`video.py` 提供影片尺寸與時長，SQLite 保存逐檔 Telegram 收據。
6. 平台限流會記錄冷卻時間；傳送結果不明時保留檔案以避免自動重送。維護迴圈清理符合保留期限的已完成媒體與已處理收件紀錄。

TikTok 影片從完整 HTTPS 影音選擇最高畫質，H.264 僅在相同畫質時優先；無 HTTPS 時可備援 HTTP。不合併分離影音、不轉碼。Instagram 依明確 API／DASH CICP 標記判斷 HDR，不把 HEVC 或 10-bit 自動視為 HDR；可關閉 HDR 優先。
TikTok 續傳以每個輸出檔旁的 `.format.json` 記錄格式識別與影音規格，不記錄媒體網址或憑證。只有格式紀錄相符才續傳 `.part`；舊版沒有紀錄或格式改變時，先將未完成內容保留為同目錄的 `.part.preserved-<隨機識別碼>`，再從頭下載，避免把 HEVC 前段與 H.264 後段拼接。保留檔不會傳送，也不由已完成媒體清理自動刪除；配額阻擋時不改動續傳資料。

輸入是 Telegram 訊息、網址及外部設定；輸出是 Telegram 媒體訊息、本機媒體、下載結果與 SQLite 狀態。執行時主要資料包含 `state.sqlite3`、`instagram-api-rate.sqlite3`、`media/` 及 `tmp/`，全部在明確指定的資料目錄下建立。

### 根目錄檔案

| 檔案 | 內容與用途 |
| --- | --- |
| [README.md](README.md) | 功能、安裝方式、環境變數、儲存與錯誤處理規則、測試命令，以及本份逐檔導覽。 |
| [REQUIREMENTS.md](REQUIREMENTS.md)／[PROGRESS.md](PROGRESS.md) | 需求、驗收條件及目前工作交接。 |
| [AGENTS.md](AGENTS.md) | 全 repository 的開發維護規則，包含必須同步更新 changelog 的要求、紀錄分類、版本整理與提交前檢查。 |
| [CHANGELOG.md](CHANGELOG.md) | 初始版本及尚未發布的功能、行為和文件變更紀錄，說明對使用者或開發者的影響。 |
| [.gitignore](.gitignore) | Git 忽略規則，涵蓋環境檔、Cookie／token 檔名、資料庫、媒體、暫存、虛擬環境與建置產物。 |
| [pyproject.toml](pyproject.toml) | 套件名稱與版本、Python 最低版本、setuptools 建置設定、套件範圍，以及 `telegram-social-downloader` 命令入口。 |
| [requirements.txt](requirements.txt) | 固定版本的 gallery-dl、yt-dlp、twscrape、requests、requests-toolbelt 和 curl-cffi 依賴。 |

### 背景服務與共用核心

| 檔案 | 內容與用途 |
| --- | --- |
| [media_bot/__init__.py](media_bot/__init__.py) | Python 套件說明與 `__version__` 版本常數。 |
| [media_bot/__main__.py](media_bot/__main__.py) | `python -m media_bot` 和安裝後命令的入口；處理 `--help`、`--check`、設定驗證及背景 worker 啟動。 |
| [media_bot/config.py](media_bot/config.py) | 讀取外部資料目錄、允許的 Telegram User ID、token 與 Cookie 檔案；驗證權限、路徑及格式，設定專用暫存目錄。 |
| [media_bot/core.py](media_bot/core.py) | SQLite schema、連線與寫入重試、工作與批次入列、網址分類與安全重新導向、持久媒體配額、檔案路徑檢查及已完成媒體清理。 |
| [media_bot/inbox.py](media_bot/inbox.py) | Telegram 持久收件匣；保存 update 與 offset、依對話順序認領訊息、限制容量、處理重啟恢復及收件紀錄清理。 |
| [media_bot/worker.py](media_bot/worker.py) | 主要協調模組；Telegram API 呼叫、輪詢、訊息與選擇按鈕、平台工作執行、下載子程序、批次協調、相簿／文件傳送、收據與錯誤狀態、工作執行緒監督及停止處理。 |
| [media_bot/video.py](media_bot/video.py) | 使用本機 `ffprobe` 讀取 MP4 尺寸、時長、像素比例與旋轉資訊，整理成 Telegram 影片參數；讀取失敗時回傳文件傳送所需的 fallback 訊號。 |
| [media_bot/gallery_runner.py](media_bot/gallery_runner.py) | 下載子程序入口；從 stdin 接收工作與 Cookie，設定 gallery-dl、選擇平台 runner、追蹤限流／驗證錯誤、整理媒體清單並寫入 `result.json`。 |

### Instagram 模組

| 檔案 | 內容與用途 |
| --- | --- |
| [media_bot/instagram.py](media_bot/instagram.py) | Instagram gallery-dl 選項、adapter 安裝，以及從貼文或個人頁解析限時動態目標；處理 extractor 的初始化與關閉。 |
| [media_bot/instagram_policy.py](media_bot/instagram_policy.py) | `RequestWindow` 請求限速器；以 SQLite 記錄歷史請求、滾動時間窗、最短間隔和封鎖期限，重啟後沿用限速狀態。 |
| [media_bot/instagram_web_client.py](media_bot/instagram_web_client.py) | 向 Instagram 發送受限制的 HTTP／GraphQL 請求；檢查目標、請求預算、登入驗證、限流和重新導向，提供經過遮蔽的錯誤分類。 |
| [media_bot/instagram_web_gallery.py](media_bot/instagram_web_gallery.py) | 將 Instagram 客戶端接到指定版本 gallery-dl；替換貼文、使用者和分頁查詢，保留其媒體下載流程，並在完成時還原 adapter、關閉 session。 |
| [media_bot/instagram_web_timeline.py](media_bot/instagram_web_timeline.py) | Instagram timeline GraphQL 參數、回應解析、輪播媒體 ID 整理和 cursor 分頁；偵測重複頁面、無效 cursor 與不完整結果。 |

### TikTok 與 X 模組

| 檔案 | 內容與用途 |
| --- | --- |
| [media_bot/tiktok.py](media_bot/tiktok.py) | TikTok 影片／照片／限時動態分流、gallery-dl 選項、Story 目標和空清單判定、重試退避及使用者錯誤訊息。 |
| [media_bot/yt_runner.py](media_bot/yt_runner.py) | 使用 yt-dlp 下載 TikTok 和 YouTube；依快照套用畫質、HDR與同畫質H.264偏好，套用媒體大小、配額及冷卻。YouTube可選獨立Cookie與FFmpeg原樣合併。 |
| [media_bot/twitter.py](media_bot/twitter.py) | X 貼文 URL／ID 驗證、Cookie 格式解析輔助函式、原圖與最高 bitrate MP4 選擇，以及錯誤訊息分類。外部啟動設定仍使用 `config.py` 的 JSON Cookie 物件格式。 |
| [media_bot/twitter_runner.py](media_bot/twitter_runner.py) | 以 twscrape 查詢 X 貼文，使用記憶體中的登入資料，從媒體 CDN 串流下載；以 SHA-256 收據驗證已有檔案，保存限流狀態並停用 provider dump／telemetry。 |

### 離線測試

| 檔案 | 內容與用途 |
| --- | --- |
| [tests/test_config.py](tests/test_config.py) | 外部設定與靜態允許名單、資料目錄、Cookie 檔案權限與位置、符號連結拒絕、錯誤資訊遮蔽及暫存目錄測試。 |
| [tests/test_runtime.py](tests/test_runtime.py) | 核心流程回歸：URL 與使用權限、去重入列、批次、持久收件匣與配額、Telegram 收據、相簿、傳送結果不明、大檔保留、空間保護和下載子程序停止。 |
| [tests/test_adapters.py](tests/test_adapters.py) | 平台 adapter／runner 測試：TikTok 分流、真實 yt-dlp 格式選擇與備援、X 媒體選擇、CDN 限制、checksum 收據、配額、檔案保留及敏感錯誤資訊遮蔽。 |
| [tests/test_instagram_stories_target.py](tests/test_instagram_stories_target.py) | 貼文轉限時動態目標的測試；驗證作者解析、extractor 初始化、iterator／session 關閉及失敗路徑。 |
| [tests/test_instagram_timeline.py](tests/test_instagram_timeline.py) | timeline 分頁與解析測試；涵蓋 cursor／頁面重複、資料格式錯誤、空清單、頁數上限、輪播 ID 與可忽略的選填欄位錯誤。 |
| [tests/test_instagram_transport.py](tests/test_instagram_transport.py) | Instagram HTTP／GraphQL transport 測試；涵蓋 429 冷卻、登入與驗證挑戰、重新導向、目標限制、請求預算和 session 資料使用。 |

### 架構文件

| 檔案 | 內容與用途 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | 使用者流程、背景處理時序、工作／檔案狀態圖、兩個 SQLite 資料庫的 ER 圖、逐欄位說明及恢復界線。 |
| [docs/state-schema.sql](docs/state-schema.sql) | 從核心初始化程式整理的工作資料庫 DDL，涵蓋 7 張表、外鍵、唯一鍵及索引。 |
| [docs/instagram-rate-schema.sql](docs/instagram-rate-schema.sql) | 從 Instagram 限速器整理的 3 張表 DDL；只描述結構，初始化時間由程式設定。 |

新增模組：`preferences.py`（每位使用者設定）、`media_preferences.py`（下載選擇）、`limits.py`／`download_size.py`（傳送／下載限制）、`links.py`（可選URL服務）、`instagram_quality.py`（明確HDR判斷）、`youtube_cookies.py`（限定域Netscape parser）。
