# YouTube／Shorts 下載

支援 watch、Shorts、youtu.be 單片連結與混合批次；播放清單參數會移除。不支援頻道、播放清單、進行中直播及 MP3 擷取。

## 畫質與登入

預設 HDR 優先，其次最高解析度與幀率，再以 H.264／AAC 作相同畫質的偏好；沒有強制 720p 或優先低畫質完整 H.264。`/settings` 可選最高畫質、1080p／720p／480p 以下偏好，以及關閉 HDR 優先。只有更高解析度時使用最接近的可用版本並說明；畫質選擇不會縮放來源；影片下載後的處理見下節。

有 FFmpeg 時先直接合併最高可用畫面＋音訊（stream copy）；沒有時只選完整影音，沒有完整版本會失敗，不交付無聲片。高畫質可能使用 HEVC／AV1，產生播放版本另需 CPU 時間與輸出檔空間。

Bot 程序 PATH 需要 Deno，依賴使用固定版本 `yt-dlp[default,curl-cffi]`。預設不登入；可設 `YOUTUBE_COOKIES_FILE` 指向外部0600 Netscape Cookie 檔。Parser 僅接受有效 YouTube 域登入 Cookie，保留 host/path/secure scope，記憶體載入，不借用 IG／TikTok Cookie。登入不保證來源可下載，也不繞過權限或地區限制。

單一影片的選中影音合計超過 2000 MB 取消下載；partial 保留。Local Bot API 500 MB 以內直接傳送，較大檔案可用2小時 URL；雲端 API 仍有49 MiB安全門檻。未設定URL服務時保留檔案並標記部分完成。詳見 [部署說明](local-api.md)。

## 影片傳送前處理

Bot 會探測下載影片的容器、視訊及音訊。視訊為 H.264 或 HEVC、音訊為 AAC 或無音軌的 MP4 只送一次。支援轉換的容器包括 MKV、WebM、MOV、AVI、MPEG-TS，以及影音編碼不相容的 MP4。這些影片會產生 H.265／hvc1、faststart MP4 作為播放版本，另外以文件傳送未改動的原檔。圖片沿用原有流程。實際播放相容性取決於 Telegram 客戶端與裝置的 HEVC 解碼能力。

需要含 libx265 的 FFmpeg 及 ffprobe。原有 HEVC 畫面與 AAC 音訊可直接複製，其餘按需要重新編碼。軟件轉碼會耗用 CPU、額外磁碟空間和工作時間。程式驗證播放版本的尺寸、幀率、時長與可識別的色彩標記。PQ 色彩標記及 10-bit 有測試覆蓋，Dolby Vision 和 HDR10+ 的動態資料仍待驗證。

播放版本轉換失敗，或 Telegram 明確拒絕該播放版本時，Bot 會標示部分完成，並繼續以文件傳送已下載的完整原檔。每個交付檔案獨立套用 Telegram 大小上限及下載連結設定。Bot 為播放版本和原檔各自保存收據，整個工作完成後，已確認直傳的檔案按 24 小時清理，連結交付則按連結期限清理。部分完成或傳送結果未明的工作會保留檔案。

## 驗證

離線測試使用真實 yt-dlp 選擇器及合併參數，模擬平台／Telegram，涵蓋最高畫質、HDR、H.264同畫質偏好、無FFmpeg備援、分離影音大小合計、格式安全續傳、Cookie域範圍及429冷卻。測試不等於真實平台或Telegram驗收；既有紀錄見 [PROGRESS.md](../PROGRESS.md)。

## 已知風險 YT-001：分段缺失仍可能回報成功

2026-11-11 review 已以離線 HLS fixture 重現：三個片段中的中間片段回傳 HTTP 404，yt-dlp 預設 `skip_unavailable_fragments=True` 會跳過該片段；runner 仍可能回傳 `code=0`，把只有前後片段的非空檔案交付 Telegram。可能造成跳段、影音異常或播放失敗。此風險針對分段格式，不代表每條影片或每次 404 都會發生。

使用者於 2026-11-11 明確接受將此問題記錄後合併 PR #1 並整合至部署中的 Bot；本次保留現行行為，未將風險標記為已修復。後續建議設定 `skip_unavailable_fragments=False`、限制 fragment retries，並加入缺段不交付及 partial 保留的回歸。原始離線格式測試通過不能證明影片內容完整。
