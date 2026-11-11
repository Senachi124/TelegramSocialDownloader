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
