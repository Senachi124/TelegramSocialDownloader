"""TikTok video capture through yt-dlp; same quota/result contract as gallery."""
import json
from pathlib import Path
import time
from email.utils import parsedate_to_datetime
from requests.cookies import create_cookie
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.networking.impersonate import ImpersonateTarget
from . import core as c

# Prefer a complete H.264 MP4 for playback compatibility; no merging or transcoding.
TIKTOK_FORMAT = (
    "best[ext=mp4][vcodec~='^(h264|avc1)'][protocol=https]/"
    'best[ext=mp4][protocol=https]/best[protocol=https]/best[protocol=http]'
)

class StopCapture(BaseException):pass

def run(payload):
    root=Path(payload['root']).resolve();root.mkdir(parents=True,exist_ok=True)
    result={'engine':'yt-dlp','code':1,'retry_at':0,'auth_required':False,
            'files':[],'failure':'','http_errors':[],'cookie_used':bool(payload.get('cookies')),
            'started':time.time(),'requests':0,'media_attempts':0,'sleep_seconds':0,'wait_reason':''}
    def record_http(response):
        code=response.status
        if code in (401,403,404,429):result['http_errors'].append(code)
        if code==429:
            value=response.headers.get('Retry-After','900')
            try:until=time.time()+float(value)
            except (ValueError,TypeError):
                try:until=parsedate_to_datetime(value).timestamp()
                except Exception:until=time.time()+900
            until=max(time.time()+900,until)
            result.update(retry_at=until,wait_reason='platform')
            c.setting('tiktok_cooldown',until);response.close();raise StopCapture()
    def classify(message):
        message=message.lower()
        if any(s in message for s in ('login required','log in','login for access','sign in')):result['failure']='login_required'
        elif '403' in message:result['failure']='forbidden'
        elif any(s in message for s in ('captcha','challenge')):result['failure']='challenge'
        elif any(s in message for s in ('timed out','connection error','unable to download webpage')):result['failure']='network_error'
        elif any(s in message for s in ('not available','not found')):result['failure']='unavailable'
    class Logger:
        def debug(self,message):pass
        def warning(self,message):classify(message)
        def error(self,message):result['error']=True;classify(message)
    class Downloader(YoutubeDL):
        def urlopen(self,request):
            result['requests']+=1
            try:response=super().urlopen(request)
            except HTTPError as error:
                record_http(error.response);raise
            record_http(response);return response
        def dl(self,name,info,subtitle=False,test=False):
            path=Path(name).resolve()
            if not path.is_relative_to(root) or Path(name).is_symlink():raise ValueError('Invalid output path')
            # Keep the old gallery filename so completed files can resume/skip.
            if not path.is_file():
                try:
                    if c.reserve_media('tiktok',str(path)):result['media_attempts']+=1
                except c.RateLimited as error:
                    result.update(retry_at=error.until,wait_reason='quota');raise StopCapture()
            return super().dl(name,info,subtitle,test)
    params={'logger':Logger(),'quiet':True,'noprogress':True,'noplaylist':True,
            'socket_timeout':30,'retries':2,'extractor_retries':2,
            'format':TIKTOK_FORMAT,
            'outtmpl':str(root/'%(id)s_00_.%(ext)s'),'overwrites':False,
            'cachedir':False,'usenetrc':False,'cookiefile':None,
            'impersonate':ImpersonateTarget.from_str('chrome')}
    try:
        with Downloader(params) as downloader:
            for key,value in payload.get('cookies',{}).items():
                downloader.cookiejar.set_cookie(create_cookie(key,value,domain='.tiktok.com'))
            info=downloader.extract_info(payload['url'],download=True,ie_key='TikTok')
            result['code']=int(not info or bool(result.get('error')))
    except StopCapture:pass
    except Exception as error:
        result['exception']=type(error).__name__
        if not result['failure']:result['failure']='extractor_error'
    finally:
        for path in sorted(root.iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in ('.mp4','.webm','.mov','.mkv'):
                result['files'].append({'path':str(path),'name':path.name,'size':path.stat().st_size})
        if not result['files']:result['code']=1
        if result['code']:
            if 401 in result['http_errors']:result['failure']='login_required'
            elif 403 in result['http_errors']:result['failure']='forbidden'
            elif not result['failure']:result['failure']='extractor_error'
        else:result['failure']=''
        result['finished']=time.time()
        temp=root/'result.tmp';temp.write_text(json.dumps(result));temp.replace(root/'result.json')
    return result
