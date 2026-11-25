"""Public Threads single-post identifiers and sanitized failure messages."""
import re
from urllib.parse import urlsplit

HOSTS = frozenset({'threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'})


def post_id(url):
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or p.hostname not in HOSTS or p.username or p.password
                or p.port not in (None, 443) or any(ord(ch) < 33 for ch in url)):
            raise ValueError
        match = re.fullmatch(r'/(?:@[A-Za-z0-9._]+/post|post|t)/([A-Za-z0-9_-]{5,64})/?', p.path)
        if not match:
            raise ValueError
        return match[1]
    except (ValueError, TypeError):
        raise ValueError('請提供 Threads 單篇貼文或分享連結') from None


def failure_message(code):
    return {
        'unavailable': 'Threads 未提供這篇貼文資料；可匯入 Threads Cookie 後重試，或確認貼文可讀',
        'login_required': 'Threads 登入已失效或需要驗證；請在管理頁更新 Threads Cookie',
        'forbidden': 'Threads 暫時拒絕讀取；請稍後再試',
        'rate_limited': 'Threads 要求等待，稍後會重試',
        'network_error': 'Threads 連線暫時失敗；請稍後再試',
        'conversion_failed':'影片預覽轉換失敗，原檔已保留。',
        'unsupported_media': '這篇 Threads 含有目前無法下載的媒體',
        'storage_error': 'SSD 儲存檢查失敗或可用空間不足；檔案已保留',
    }.get(code, 'Threads 媒體擷取失敗；請檢查貼文是否公開')
