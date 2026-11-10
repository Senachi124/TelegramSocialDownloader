"""Twitter post policy and media selection."""
import json
import re
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit, parse_qs


def post_id(url):
    from .core import validate_url, TWITTER_HOSTS
    p=urlsplit(validate_url(url))
    match=re.fullmatch(r'/(?:[A-Za-z0-9_]{1,15}/status|i/status|i/web/status)/(\d{1,25})(?:/(?:photo|video)/[1-4])?/?',p.path)
    if p.hostname not in TWITTER_HOSTS or not match:
        raise ValueError('Twitter / X 請貼上單一貼文連結（/status/…）；不支援主頁或搜尋頁')
    return match[1]


def parse_cookies(text):
    """Accept header/JSON/Netscape formats, retaining only session keys."""
    found={}
    if text.lstrip().startswith(('[','{')):
        rows=json.loads(text)
        if isinstance(rows,dict):rows=rows.get('cookies',[])
        for row in rows:
            if str(row.get('domain','')).lstrip('.') not in ('x.com','twitter.com'):continue
            name=row.get('name')
            if name not in ('auth_token','ct0'):continue
            expiry=row.get('expirationDate',row.get('expires',0)) or 0
            if expiry>0 and expiry<=time.time():raise ValueError('X Cookie 已過期')
            found[name]=str(row.get('value',''))
    elif '\t' in text:
        for line in text.splitlines():
            line=line.removeprefix('#HttpOnly_')
            if not line or line.startswith('#'):continue
            fields=line.split('\t')
            if len(fields)!=7:raise ValueError('Cookie 格式無效')
            domain,_,_,_,expiry,name,value=fields
            if domain.lstrip('.') not in ('x.com','twitter.com') or name not in ('auth_token','ct0'):continue
            if float(expiry)>0 and float(expiry)<=time.time():raise ValueError('X Cookie 已過期')
            found[name]=value
    else:
        header=text.strip()
        if header.lower().startswith('cookie:'):header=header[7:].strip()
        if '\n' in header or '\r' in header:raise ValueError('Cookie 標頭須在同一行')
        cookies=SimpleCookie()
        try:cookies.load(header)
        except CookieError:raise ValueError('Cookie 格式無效') from None
        found={key:cookies[key].value for key in ('auth_token','ct0') if key in cookies}
    if not all(found.get(key) for key in ('auth_token','ct0')):raise ValueError('需要 auth_token 與 ct0')
    if any(len(v)>4096 or any(ord(ch)<33 for ch in v) for v in found.values()):raise ValueError('Cookie 格式無效')
    return found


def assets(tweet):
    """Original photos, highest-bitrate MP4, and GIF-as-MP4."""
    media=getattr(tweet,'media',None);items=[]
    for photo in getattr(media,'photos',[]) or []:
        url=str(getattr(photo,'fullUrl',None) or photo.url)
        ext=parse_qs(urlsplit(url).query).get('format',[urlsplit(url).path.rsplit('.',1)[-1]])[0]
        items.append((url,ext if ext in ('jpg','jpeg','png','webp','gif') else 'jpg'))
    for video in getattr(media,'videos',[]) or []:
        best=max((v for v in video.variants if 'mp4' in v.contentType),key=lambda v:v.bitrate or 0,default=None)
        if best is None:raise ValueError('unsupported_video')
        items.append((str(best.url),'mp4'))
    for animated in getattr(media,'animated',[]) or []:items.append((str(animated.videoUrl),'mp4'))
    return items


def failure_message(reason):
    return {'login_required':'Twitter / X 登入失效，請管理員更新此帳號的 Cookie。',
            'unavailable':'此 Twitter / X 貼文不存在、已刪除或目前帳號沒有觀看權限。',
            'forbidden':'Twitter / X 拒絕存取此內容；請檢查觀看權限或平台限制。',
            'network_error':'連線 Twitter / X 失敗或逾時，請稍後重試。'}.get(reason,'Twitter / X 擷取失敗；請檢查連結、Cookie 或平台限制。')
