# 開發進度

需求由 [REQUIREMENTS.md](REQUIREMENTS.md) 管理；本檔記錄執行和驗證。

## 2026-11-10：YouTube／Shorts

1. 完成：`core.py` 正規化單片網址；`config.py`、`worker.py` 接入 YouTube 佇列和單筆／批次狀態；`gallery_runner.py` 分流至 `yt_runner.py`。使用既有媒體配額、傳送和檔案保留機制，不改 SQLite schema 或 Python 依賴。README、架構文件及 CHANGELOG 已同步。
2. 完成（離線與下載驗收）：在臨時 Python 3.14 環境依 `requirements.txt` 安裝固定版本，執行 `TMPDIR=/private/tmp python -m unittest discover -s tests`，77 項通過。測試涵蓋網址／批次、最終檔案選擇、兩條串流只計一次配額，以及 HTTP 429 不影響 TikTok 冷卻。CLI 說明正常。
3. 完成（實站本機下載）：使用 yt-dlp 2026.10.21、Deno 2.9.6 和 FFmpeg；驗收普通影片網址（附播放清單參數）、同一影片的 youtu.be 分享網址、另一條 Shorts。各產出一個 MP4，ffprobe 核對均含 H.264 畫面及 AAC 音訊。最初另一個測試影片擷取失敗；結果只適用於成功測試的內容及當時環境。
4. 未驗證：真實 Telegram 發送及部署環境的 PATH。下次啟動服務時，確認 bot 程序可找到 Deno，建議同時提供 FFmpeg／ffprobe，再貼上公開影片或 Shorts 試送。

YouTube 改動位於 `feat/youtube-support` 分支，基於 `origin/main`。
