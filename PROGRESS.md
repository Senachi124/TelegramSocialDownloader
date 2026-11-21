# 開發進度

需求由 [REQUIREMENTS.md](REQUIREMENTS.md) 管理；本檔記錄執行和驗證。

## 2026-11-10：YouTube／Shorts

1. 完成：`core.py` 正規化單片網址；`config.py`、`worker.py` 接入 YouTube 佇列和單筆／批次狀態；`gallery_runner.py` 分流至 `yt_runner.py`。使用既有媒體配額、傳送和檔案保留機制，不改 SQLite schema 或 Python 依賴。README、架構文件及 CHANGELOG 已同步。
2. 完成（離線與下載驗收）：在臨時 Python 3.14 環境依 `requirements.txt` 安裝固定版本，執行 `TMPDIR=/private/tmp python -m unittest discover -s tests`，77 項通過。測試涵蓋網址／批次、最終檔案選擇、兩條串流只計一次配額，以及 HTTP 429 不影響 TikTok 冷卻。CLI 說明正常。
3. 完成（實站本機下載）：使用 yt-dlp 2026.10.21、Deno 2.9.6 和 FFmpeg；驗收普通影片網址（附播放清單參數）、同一影片的 youtu.be 分享網址、另一條 Shorts。各產出一個 MP4，ffprobe 核對均含 H.264 畫面及 AAC 音訊。最初另一個測試影片擷取失敗；結果只適用於成功測試的內容及當時環境。
4. 未驗證：真實 Telegram 發送及部署環境的 PATH。下次啟動服務時，確認 bot 程序可找到 Deno，建議同時提供 FFmpeg／ffprobe，再貼上公開影片或 Shorts 試送。

YouTube 改動位於 `feat/youtube-support` 分支，基於 `origin/main`。

## 2026-11-11：PR #1 衝突與 review 修正

- 將 `main` 的 `8423da4` 整合進 `feat/youtube-support`，解決 `README.md`、`docs/architecture.md`、`media_bot/yt_runner.py` 的衝突；保留 TikTok H.264 優先與格式不匹配 partial 保留／安全續傳。
- 依 review 與後續 H.264 要求：先下載已有影音的 H.264 MP4，其次在 FFmpeg 可用時直接合併 H.264＋AAC，最後備援完整 MP4／WebM。合併使用 stream copy，不重新壓制。
- 新增 `docs/youtube.md`，同步架構、README、需求與 changelog；不變更套件版本或資料庫 schema。
- 驗證：Python 3.14、yt-dlp 2026.10.21 下，`python -m unittest discover -s tests -q` 共 94 項通過；包含 6 項新增 YouTube 格式／合併回歸，以及 main 的 TikTok 格式與 HTTP Range 續傳回歸。`pip check`、CLI `--help`、diff whitespace 檢查通過。獨立 Code Reviewer 已檢查 runner 與測試，未發現阻擋問題。
- 本次只做合成資料／模擬平台及本機 HTTP 測試；沒有真實 YouTube 下載、Telegram 發送或正式部署。上一節的實站下載紀錄屬原 PR 版本，不能當作本次選擇順序的實站驗收。正式服務登記與部署索引不受此次 PR 修改影響。
- 衝突解決、review 修改、測試和文件放在同一個新增 merge commit，保留原分支歷史；PR 合併另行處理。需要撤回本次修正時應用新的 revert commit，勿覆寫遠端歷史或還原執行中資料。

## 2026-11-11：接受已知風險並合併

使用者要求將 review 發現的分段 404 完整性問題保留在 log，並允許 PR #1 進入 main。已登記 YT-001 至 `docs/youtube.md`／CHANGELOG；未變更下載行為，不宣稱已修復或已完成真實 Telegram 驗收。正式 PrivateMediaBot 的功能整合與部署在獨立 private repository 維護。

## 2026-11-21：Shorts MKV 播放問題

- 診斷工具採 systematic-debugging、原始碼及 ffprobe。提供的檔案是 2160×3840、30 fps、SDR VP9＋AAC 的 Matroska。最高畫質選擇器允許此組合，worker 只將經探測的 MP4 當作影片送出，MKV 因此按文件傳送。
- 已在臨時目錄製作 H.264 yuv420p＋AAC、faststart MP4，保留原尺寸。以 FFmpeg libx264 CRF 18 重新壓制畫面、copy 音訊。ffprobe 核對尺寸與編碼，完整 FFmpeg 解碼驗證通過。檔案約 20.5 MB，原檔約 9.0 MB。
- 當時待確認處理策略，後續使用者已指定 MKV 轉 H.265 MP4（見下節）。轉碼會改變原始畫面資料並增加資源使用。

### H.265 MP4 修正執行

- 本機實作及驗證完成。使用者已指定 MKV 轉 H.265 MP4 後傳送。使用 systematic-debugging／TDD、Python unittest 及本機 FFmpeg／ffprobe，先加入轉換、失敗不交付與 runner 分流回歸，再以提供的短片驗證，預計測試少於 10 分鐘。
- FFmpeg／ffprobe 和 libx265 已核對可用。Python 固定依賴於臨時環境安裝。此目錄沒有 .git，原有文件／程式已包含先前更新，採局部修改。

- 修改 video.py、yt_runner.py 及 TikTok 轉換錯誤提示，新增 tests/test_video_transcode.py。整合於下載結果登記前，正常輸出只登記 H.265 MP4。轉換失敗不登記原 MKV 或暫存 MP4，原檔保留。需求、README、YouTube／架構說明及 CHANGELOG 已同步。
- 最終驗證：臨時 Python 3.14 環境安裝 requirements.txt 固定直接依賴，完整 unittest 共 141 項通過（7.904 秒）。4 項新測試使用本機 FFmpeg 生成真實影音，涵蓋 HEVC／AAC／hvc1、原尺寸與幀率、HDR PQ／BT.2020 10-bit、失敗暫存清理，以及 YouTube runner 成功／失敗的登記與敏感錯誤遮蔽。舊 HTTP 測試需要解除 sandbox 的 loopback bind 限制，通過時沒有真實外部服務操作。
- 提供的 8.38 秒 4K VP9＋AAC 短片經產品 helper 實測，耗時 42.97 秒，輸出 9,371,572 bytes、HEVC hvc1＋AAC、2160×3840、30 fps。ffprobe 核對及 FFmpeg 全片解碼通過。輸出在臨時目錄，來源檔案保留。
- 邊界：未部署、未發送真實 Telegram 訊息，實際客戶端 H.265 播放待確認。HDR 動態 metadata 尚未驗證；HDR 測試只證實 PQ 色彩標記與 10-bit。原始 MKV 不屬目前交付檔案清理範圍，管理員須另行處理。此目錄無 Git metadata，沒有 commit／push。

### GitHub 發布準備（2026-11-21）

- 基於上游 main 的 54d1451，建立 fix/mkv-hevc-mp4 分支。只整理本次 H.265 轉換相關 10 個檔案，沿用原始 Git 歷史。
- GitHub 上游帳號目前只提供讀取權限，將新分支發布至使用者擁有的 nokilkh/TelegramSocialDownloader fork，供上游 review。
- 發布分支重新執行完整 unittest，141 項通過（7.994 秒），git diff --check 通過。
