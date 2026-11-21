# YouTube／Shorts 下載

支援 watch、Shorts、youtu.be 單片連結與混合批次；播放清單參數會移除。不支援頻道、播放清單、進行中直播及 MP3 擷取。

## 畫質與登入

預設 HDR 優先，其次最高解析度與幀率，再以 H.264／AAC 作相同畫質的偏好；沒有強制 720p 或優先低畫質完整 H.264。`/settings` 可選最高畫質、1080p／720p／480p 以下偏好，以及關閉 HDR 優先。只有更高解析度時使用最接近的可用版本並說明；畫質選擇不會縮放來源；MKV 下載後的轉碼規則見下節。

有 FFmpeg 時先直接合併最高可用畫面＋音訊（stream copy）；沒有時只選完整影音，沒有完整版本會失敗，不交付無聲片。高畫質可能使用 HEVC／AV1，MKV 轉碼另需 CPU 時間與輸出檔空間。

Bot 程序 PATH 需要 Deno，依賴使用固定版本 `yt-dlp[default,curl-cffi]`。預設不登入；可設 `YOUTUBE_COOKIES_FILE` 指向外部0600 Netscape Cookie 檔。Parser 僅接受有效 YouTube 域登入 Cookie，保留 host/path/secure scope，記憶體載入，不借用 IG／TikTok Cookie。登入不保證來源可下載，也不繞過權限或地區限制。

單一影片的選中影音合計超過 2000 MB 取消下載；partial 保留。Local Bot API 500 MB 以內直接傳送，較大檔案可用2小時 URL；雲端 API 仍有49 MiB安全門檻。未設定URL服務時保留檔案並標記部分完成。詳見 [部署說明](local-api.md)。

## MKV 傳送前轉換

yt-dlp runner（YouTube／TikTok）下載 MKV 後，會先使用 FFmpeg 產出 .h265.mp4，再交給原有影片傳送流程。需要 FFmpeg、ffprobe，以及 FFmpeg 的 libx265 encoder。採用軟件編碼 CRF 20、fast、hvc1 及 faststart，來源已有 HEVC 時直接複製畫面串流；AAC 音訊直接複製，其他音訊轉 AAC 192 kbps。

保持來源尺寸與幀率；PQ／HLG 或高位深來源採 10-bit，複製可識別的色彩標記。此處的 HDR 驗證涵蓋 PQ 色彩標記與位深，Dolby Vision／HDR10+ 動態 metadata 的保留尚未驗證。重新編碼會改變畫面資料，最終大小取決於內容。

轉換寫入獨立暫存 MP4，以 ffprobe 核對 HEVC／hvc1、尺寸、幀率、時長、音訊與色彩標記，再原子發布輸出。失敗保留 MKV、回報轉換失敗並停止交付該結果；重試會重新轉換。轉換仍受整體工作 30 分鐘期限、5 GiB 空間保護及 2000 MB 單檔上限約束。原始 MKV 保留本機，現有逐檔清理只涵蓋交付的 MP4，原檔需由管理員處理。

輸出經由現有 sendVideo／相簿流程，超過目前 Telegram 傳送上限時沿用下載連結或保留規則。H.265 客戶端播放相容性仍需在實際使用的 Telegram 客戶端確認。

## 驗證

離線測試使用真實 yt-dlp 選擇器及合併參數，模擬平台／Telegram，涵蓋最高畫質、HDR、H.264同畫質偏好、無FFmpeg備援、分離影音大小合計、格式安全續傳、Cookie域範圍及429冷卻。測試不等於真實平台或Telegram驗收；既有紀錄見 [PROGRESS.md](../PROGRESS.md)。

## 已知風險 YT-001：分段缺失仍可能回報成功

2026-11-11 review 已以離線 HLS fixture 重現：三個片段中的中間片段回傳 HTTP 404，yt-dlp 預設 `skip_unavailable_fragments=True` 會跳過該片段；runner 仍可能回傳 `code=0`，把只有前後片段的非空檔案交付 Telegram。可能造成跳段、影音異常或播放失敗。此風險針對分段格式，不代表每條影片或每次 404 都會發生。

使用者於 2026-11-11 明確接受將此問題記錄後合併 PR #1 並整合至部署中的 Bot；本次保留現行行為，未將風險標記為已修復。後續建議設定 `skip_unavailable_fragments=False`、限制 fragment retries，並加入缺段不交付及 partial 保留的回歸。原始離線格式測試通過不能證明影片內容完整。
