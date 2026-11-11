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

實作選擇：重用現有 yt-dlp runner；YouTube 不讀取其他平台 Cookie，也不新增帳號設定。
