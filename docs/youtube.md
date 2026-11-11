# YouTube／Shorts 下載

支援公開的 `youtube.com/watch?v=…`、`youtube.com/shorts/…`、`youtu.be/…` 單片網址。`core.validate_url()` 會統一為 watch URL 並移除播放清單、時間及分享參數；一個連結只下載一條影片。可以與 Instagram／TikTok／X 混合批次。播放清單、頻道、進行中／未開始／仍在處理的直播、登入內容和 MP3 擷取不在範圍內。

## 格式優先順序與 CPU

`yt_runner.py` 的 YouTube 選擇順序如下；每一級找不到合適格式才往下選：

1. 已有畫面與聲音的 H.264 MP4（識別 `h264` 和 `avc1`），直接下載。
2. 若 bot 的 `PATH` 有 FFmpeg，選 H.264 MP4 畫面＋AAC M4A 音訊。yt-dlp 的 merger 使用 `-c copy` 原樣合併，不重新編碼。
3. 其他已有畫面與聲音的 MP4。
4. 已有畫面與聲音的 WebM。

沒有 FFmpeg 時跳過第 2 級；只有分離串流時回報下載失敗，不傳送無聲影片。完整 H.264 即使解析度較低，也優先於需要合併的 H.264 或其他編碼。只有沒有可用的 H.264 方案才使用其他編碼備援，因此不保證所有影片均為 H.264。

各級沿用 yt-dlp 的 `res:720` 排序偏好：盡量選 720p 以下的最佳來源；只有更高解析度時仍可選取，並非轉碼縮放或硬性上限。沒有壓制、降低 bitrate 或為符合傳送門檻而重新編碼。直接下載通常最省 CPU；stream copy 仍需磁碟讀寫、額外暫存空間和少量封裝處理，但不進行影音壓縮。

## 依賴及操作界線

- 使用 repository 固定版本的 `yt-dlp[default,curl-cffi]`。YouTube 的 JavaScript challenge 需要 bot 程序可找到 Deno；FFmpeg 是上述分離串流備援的可選依賴，ffprobe 用於傳送前讀取影片資料。
- YouTube 不讀取 Cookie，也不增加 `YOUTUBE_COOKIES_FILE`；遇到登入／驗證要求會回報失敗，不借用其他平台帳號。
- 沿用 49 MiB 傳送門檻。超過門檻的原檔保留本機；H.264 偏好不保證檔案小於門檻，也不會自動壓縮。
- 使用獨立 `youtube` worker、`youtube_cooldown` 和媒體配額；分離的影音串流合計一次媒體預留。只將成功的最終完整檔加入傳送清單。
- 沿用資料目錄和空間保護。格式紀錄防止將不同格式的 partial 混用；不匹配的 partial 保留，已完成檔不覆寫。
- `jobs.platform`、`batch_items.platform`、`media_budget.platform` 沿用 TEXT 欄位，新增 `youtube` 值不需要 schema migration。詳見 [架構／資料字典](architecture.md) 和 [參考 SQL](state-schema.sql)。

## 離線驗證

```sh
python -m unittest discover -s tests -v
```

`tests/test_adapters.py` 使用真實 yt-dlp 選擇和 merger 邏輯，模擬平台資料、下載內容與 FFmpeg 執行，驗證完整 H.264 優先、H.264／AAC 備援的 `-c copy`、無 FFmpeg 的降級／拒絕、WebM 備援及 TikTok 續傳。`tests/test_runtime.py` 驗證網址、混合批次、只傳最終檔、影音只計一次配額和獨立 HTTP 429 冷卻。測試不呼叫真實平台或 Telegram API。

PR #1 原有實站下載紀錄與本次離線驗證分開記錄於 [PROGRESS.md](../PROGRESS.md)；本次修正不代表已完成真實 Telegram 傳送或正式服務部署。
