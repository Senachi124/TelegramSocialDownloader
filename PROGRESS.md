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
