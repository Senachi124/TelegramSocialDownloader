# Local Bot API 與下載網址

預設仍使用 Telegram 雲端 Bot API；Local API 與下載網址均可選。公開核心沒有正式服務的管理頁、帳號池、Threads、Takaneko 或主機服務設定。

## Local API

自行運行官方 [Telegram Bot API server](https://github.com/tdlib/telegram-bot-api)，使用 `--local` 並只監聽 loopback。`api_id`／`api_hash` 由操作者在 [Telegram API development tools](https://my.telegram.org/apps) 取得，透過受限的外部設定供給 daemon；不要寫入 repository。API daemon 的資料、快取和暫存需明確配置。

設定 `MEDIA_BOT_API_BASE=http://127.0.0.1:8081` 後，本核心單檔最多500,000,000 bytes直接送出。媒體回應／讀取timeout為1800秒，並非整次傳送的絕對時限；50 MB以上傳送一次一個，輪詢／狀態訊息使用獨立連線。傳送結果不明仍保留、不盲目重送。

預設使用multipart上傳。可選 `MEDIA_BOT_LOCAL_FILE_UPLOADS=1` 避免以multipart再次上傳媒體：daemon必須可讀相同絕對路徑。程式只傳已開啟、inode相符且位於媒體樹的檔案。使用專用身分與唯讀共享媒體權限；不要為此放寬整個資料／憑證目錄。daemon仍可能在自己的檔案目錄／快取建立副本，不能視為零複製。相簿仍使用multipart，50 MB以上影片逐一傳送。

遷移前停止舊輪詢，在雲端API完成官方要求的 `logOut`，再啟動本機API及此核心；同一token不可同時由兩個worker輪詢。回切雲端前按官方daemon遷移規則處理（包含登出後的限制），不應只變更環境變數而保持舊worker執行。此核心不會代替操作者自動登出或管理API daemon。

CPU負擔主要取決於下載和FFmpeg封裝（不轉碼）；最高畫質增加網路與磁碟負擔。下載2 GB上限以單一來源媒體計算，分離影音與最後合併檔可同時存在；另需計算Local API快取。既有5 GiB空間保留／3 GiB新工作暫存門檻仍適用；下載過程持續檢查空間，保留未完成檔供人工處理。

## 可選2小時下載網址

設定：

```sh
MEDIA_BOT_DOWNLOAD_BASE_URL=https://downloads.example.com/files
MEDIA_BOT_DOWNLOAD_PORT=8090
```

Bot同一程序在 `127.0.0.1:8090` 啟動專用下載服務；不接受公開bind，最多8個同時處理的連線、socket timeout20秒。反向代理須將外部 `/files/` 前綴去除後代理至loopback。以下為通用Nginx範例，TLS憑證／hostname由操作者配置：

```nginx
location ^~ /files/ {
    access_log off;
    error_log /dev/null crit;
    proxy_pass http://127.0.0.1:8090/;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 30s;
    proxy_set_header Host localhost;
    add_header Referrer-Policy no-referrer always;
    add_header Cache-Control "private, no-store" always;
}
```

不要用靜態目錄公開整個 `MEDIA_BOT_DATA`。關閉此路徑的代理／CDN快取與網址存取記錄；網址中的256-bit隨機token本身就是存取權限，收到者可分享。DB只保存token的SHA-256；服務不記錄請求URL。存取時重查來源Telegram使用者仍在允許名單、工作未取消、token未到期及檔案路徑。支援GET／HEAD及單一bytes Range，拒絕多重Range和路徑穿越。

網址2小時到期；正在傳送的下載也在到期後停止。已確認送達且工作完整完成的連結媒體於到期後由維護迴圈清理，故檔案實際移除可能延後一個維護週期。直接送達檔保留24小時。失敗、部分完成和收據不明原檔不會自動清除，但網址仍會到期。

未設定HTTPS base時不啟動服務，較大檔案只保留本機並標記部分完成，不產生不可用連結。核心無管理介面；操作者檢查狀態並確認Telegram是否已收件後再處理，避免重複傳送。

## 設定與遷移

參考根目錄 `.env.example`，所有敏感檔案保持在程式及資料樹外的0600一般檔案。啟動自動新增資料欄位與links表，不改寫現有工作、刪除舊媒體或自動補發。請先停止舊worker並備份控制資料，測試設定和新worker；保留失敗／不明檔案及隊列。未設定新選項時保持雲端傳送及匿名YouTube。
