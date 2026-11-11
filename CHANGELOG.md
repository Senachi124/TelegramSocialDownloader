# Changelog

本文件記錄會影響使用者或開發者的變更。尚未發布的修改收錄於 `Unreleased`；版本區段以新到舊排列。維護要求見 [AGENTS.md](AGENTS.md)。

## [Unreleased]

### Added

- 支援公開 YouTube 影片、Shorts 和 youtu.be，可單獨或混合批次下載；需要 Deno，建議 FFmpeg。原匿名下載方式保留，新增的 Cookie、Local API 與偏好選項見下列條目。
- 完整架構文件含使用者流程、背景時序、狀態、SQLite ER 圖及參考 SQL；根目錄 `AGENTS.md` 定義每次變更須同步 changelog，README 提供文件與逐檔導覽入口。
- `/settings` 保存每位使用者的 IG／TikTok 貼文、指定項目／限時動態及 YouTube 畫質／HDR 偏好。新工作原子保存快照，貼連結後直接下載；不可套用的偏好會回報原因。既有 choice 工作仍可完成。
- 可選 loopback Local Bot API、最多500 MB直接傳送、較大檔案提供2小時URL。可選 loopback下載服務使用256-bit capability、資料庫只存digest，支援GET／HEAD／單一Range；操作者須配置HTTPS反向代理。未設定URL服務時保留超限檔案並標記部分完成，雲端預設仍有49 MiB安全門檻。
- 可選外部0600 Netscape YouTube Cookie檔；未設定時維持匿名，僅載入有效YouTube域Cookie。不加入私人帳號池、管理介面或主機設定。

### Changed

- 預設最高畫質（HDR優先可關閉），H.264只在相同畫質時優先。YouTube有FFmpeg時可直接合併原始串流，不重新壓制；取消720p偏好與低解析度完整H.264的優先地位。
- Instagram依明確API／DASH CICP標記識別HDR及明確圖片HDR，保留請求計量與Cookie隔離。DASH缺段會失敗，不靜默降級；不猜測HEVC或10-bit必為HDR。
- 下載已知或串流中的單檔大小超過2000 MB立即取消；同影片分離影音合計限制，保留partial、不將合併副本重複計數。已確認且完整交付的URL檔案2小時清理，直接交付24小時；失敗／部分完成／不明結果原檔保留。
- 啟動自動遷移新增 `jobs.preferences`、`files.link_retention_until` 與 `links` 表；不重下載既有完成檔、不重送結果不明的媒體。詳見 README、架構及Local API文件。
- YT-001仍保留：YouTube分段下載可能跳過404片段而交付缺段檔案；本次Instagram嚴格DASH處理不代表此YouTube風險已修復。

### Fixed

- TikTok 續傳格式紀錄不含網址或憑證；格式變更或缺少紀錄時保留舊 partial，避免 H.264／HEVC 混接；格式相同仍可 Range 續傳。保留配額阻擋、完整檔與 partial／紀錄符號連結拒絕。既有 H.264／avc1、完整影音、HTTP 備援及拒絕 HLS 回歸，按新版最高畫質優先規則更新；另涵蓋合併、大小界線、URL 存取／到期及不明收據不重送。

## [0.1.0] - 2026-11-10

初始程式碼版本記錄；套件版本為 `0.1.0`，未建立 release tag。

### Added

- 提供獨立 Telegram 背景下載核心，支援 Instagram 貼文／Reels／限時動態、TikTok 影片／照片／限時動態及 X 單篇貼文媒體。
- 提供每則訊息最多 50 個連結的批次處理、SQLite 持久工作與收件匣、平台限流冷卻及媒體下載配額。
- 提供 Telegram 相簿、影片、圖片與文件傳送，保存逐檔收據；傳送結果不明時保留檔案，避免自動重送。
- 使用外部 Token／Cookie 檔案及靜態 Telegram User ID 允許名單；明確指定資料目錄與專用暫存位置。
- 加入空間保護、49 MiB 單檔傳送限制及已確認完整傳送媒體的 24 小時清理規則；失敗、部分完成與結果不明的媒體保留。
- 提供固定版本依賴、可安裝套件、啟動／設定檢查命令、逐檔開發導覽及 73 項離線回歸測試。
