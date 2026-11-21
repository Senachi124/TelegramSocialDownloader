# YouTube／Shorts 下載

支援 watch、Shorts、youtu.be 單片連結與混合批次；播放清單參數會移除。不支援頻道、播放清單、進行中直播及 MP3 擷取。

## 畫質與登入

YouTube 會優先選用相容的原生 MP4，按設定選擇可用畫質。Bot 直接傳送下載的原片，分離影音只合併串流，不重新壓製。未能取得相容 MP4 時使用其他可用原始格式。

Bot 程序 PATH 需要 Deno，依賴使用固定版本 `yt-dlp[default,curl-cffi]`。預設不登入；可設 `YOUTUBE_COOKIES_FILE` 指向外部0600 Netscape Cookie 檔。Parser 僅接受有效 YouTube 域登入 Cookie，保留 host/path/secure scope，記憶體載入，不借用 IG／TikTok Cookie。登入不保證來源可下載，也不繞過權限或地區限制。

單一影片的選中影音合計超過 2000 MB 取消下載；partial 保留。Local Bot API 500 MB 以內直接傳送，較大檔案可用2小時 URL；雲端 API 仍有49 MiB安全門檻。未設定URL服務時保留檔案並標記部分完成。詳見 [部署說明](local-api.md)。

## 影片交付

Bot 探測本機原片的尺寸與片長，依 Telegram 傳送限制交付。明確拒收影片時以同一原檔作為文件，傳送結果未明時保留收據狀態與檔案。已確認交付的原檔按既有期限清理。

## 驗證

離線測試使用真實 yt-dlp 選擇器及合併參數，模擬平台／Telegram，涵蓋最高畫質、HDR、原生 MP4 選擇、無FFmpeg備援、分離影音大小合計、格式安全續傳、Cookie域範圍及429冷卻。測試不等於真實平台或Telegram驗收；既有紀錄見 [PROGRESS.md](../PROGRESS.md)。

## 已知風險 YT-001：分段缺失仍可能回報成功

2026-11-11 review 已以離線 HLS fixture 重現：三個片段中的中間片段回傳 HTTP 404，yt-dlp 預設 `skip_unavailable_fragments=True` 會跳過該片段；runner 仍可能回傳 `code=0`，把只有前後片段的非空檔案交付 Telegram。可能造成跳段、影音異常或播放失敗。此風險針對分段格式，不代表每條影片或每次 404 都會發生。

使用者於 2026-11-11 明確接受將此問題記錄後合併 PR #1 並整合至部署中的 Bot；本次保留現行行為，未將風險標記為已修復。後續建議設定 `skip_unavailable_fragments=False`、限制 fragment retries，並加入缺段不交付及 partial 保留的回歸。原始離線格式測試通過不能證明影片內容完整。
