# 專案需求

本檔管理目前任務嘅要求及驗收；執行狀態見 [PROGRESS.md](PROGRESS.md)，既有功能見 [README.md](README.md)。

## 原始需求（2026-11-10）

> 增加對youtube同youtube short support 功能

## 目前有效要求

- R001：接受 YouTube 普通影片、Shorts 和 youtu.be 分享網址；單連結直接下載，混合批次照原有流程逐項處理。
- R002：沿用現有 Telegram 傳送、限流、媒體配額及本機檔案保留規則；Instagram、TikTok、X 行為保持相容。
- R003：下載單條可公開觀看的影片。播放清單、頻道、正在直播的影片、需登入的內容及 MP3 不在本次範圍。含播放清單參數的影片網址只取該影片。
- R004（PR #1 review）：更新 `docs/`；優先 H.264，先下載已有影音的 H.264 MP4，沒有時才以 FFmpeg 直接合併 H.264＋AAC，不重新壓制；其他完整影音作最後備援。
- 驗收：離線驗證合法及不支援網址、批次分流、下載結果及限流；可行時實測普通影片和 Shorts，區分本機下載和實際 Telegram 傳送。

目前實作：重用 yt-dlp runner；YouTube 不讀取其他平台 Cookie，可選獨立外部 Netscape Cookie。R004是先前版本需求，以下新需求優先。

- R005：Local Bot API單檔500 MB直接傳送，較大檔2小時URL；下載大於2000 MB取消。公開核心的URL服務可選，未設定時保留本機，不假裝交付。
- R006：預設最高畫質、HDR優先可關閉，H.264同畫質優先；IG明確HDR識別；每位使用者預先設定內容／畫質，工作保存快照，貼連結不再詢問。

## 2026-11-21 播放問題

使用者回報 Shorts 下載成 MKV 後無法在 Telegram 直接播放。

- R007（2026-11-21 使用者確認）：下載完成若為 MKV，傳送前轉成 H.265 MP4。保留來源解像度、幀率與可識別的 HDR 色彩／位深，音訊採 AAC。驗證輸出後才登記傳送檔案，失敗保留原檔並回報。
- 實作範圍：現有 yt-dlp runner 的 MKV 結果（YouTube／TikTok）；現有其他格式沿用原流程。採 FFmpeg libx265 CRF 20、fast、hvc1、faststart，屬 AI 實作選擇。轉碼為有損處理，仍受現有下載／傳送大小與整體工作逾時限制。

本次後續原文

> 可唔可修正到下載左如果係mkv,會轉返做h265 mp4先send
