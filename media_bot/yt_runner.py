"""TikTok and YouTube video capture through yt-dlp; same quota/result contract as gallery."""
import json
import shutil
import os
from pathlib import Path
import time
import uuid
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

def _prepare_partial(path, info):
    """Resume only a partial recorded for this format; keep other bytes intact."""
    part = path.with_name(path.name + '.part')
    marker = path.with_name(path.name + '.format.json')
    for candidate in (part, marker):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise ValueError('Invalid partial state path')
    # Deliberately exclude URLs, headers and cookies from persistent state.
    fingerprint = {'version': 1, 'format': {key: info.get(key) for key in (
        'id', 'format_id', 'vcodec', 'acodec', 'ext', 'width', 'height', 'fps', 'filesize')}}
    try:
        previous = json.loads(marker.read_text())
    except (OSError, ValueError):
        previous = None
    if previous == fingerprint:
        return
    if part.exists():
        # Hardlink plus unlink preserves bytes on the same filesystem and never
        # overwrites another preservation file, even if a name already exists.
        preserved = part.with_name(part.name + '.preserved-' + uuid.uuid4().hex)
        os.link(part, preserved, follow_symlinks=False)
        part.unlink()
    temporary = marker.with_name(marker.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(fingerprint, stream, sort_keys=True)
        temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)

def run(payload):
    root=Path(payload['root']).resolve();root.mkdir(parents=True,exist_ok=True)
    platform=payload.get('platform','tiktok')
    youtube=platform=='youtube'
    cookies={} if youtube else payload.get('cookies',{})
    video_id=None
    final_path=None
    result={'engine':'yt-dlp','code':1,'retry_at':0,'auth_required':False,
            'files':[],'failure':'','http_errors':[],'cookie_used':bool(cookies),
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
            c.setting(platform+'_cooldown',until);response.close();raise StopCapture()
    def classify(message):
        message=message.lower()
        if any(s in message for s in ('login required','log in','login for access','sign in')):result['failure']='login_required'
        elif '403' in message:result['failure']='forbidden'
        elif any(s in message for s in ('captcha','challenge')):result['failure']='challenge'
        elif any(s in message for s in ('timed out','connection error','unable to download webpage')):result['failure']='network_error'
        elif any(s in message for s in ('not available','not found','video unavailable','private video')):result['failure']='unavailable'
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
                    # Separate YouTube audio/video streams count as one video.
                    item=str(root/video_id) if youtube else str(path)
                    if c.reserve_media(platform,item):result['media_attempts']+=1
                except c.RateLimited as error:
                    result.update(retry_at=error.until,wait_reason='quota');raise StopCapture()
                _prepare_partial(path, info)
            return super().dl(name,info,subtitle,test)
    params={'logger':Logger(),'quiet':True,'noprogress':True,'noplaylist':True,
            'socket_timeout':30,'retries':2,'extractor_retries':2,
            'format':TIKTOK_FORMAT,
            'outtmpl':str(root/'%(id)s_00_.%(ext)s'),'overwrites':False,
            'cachedir':False,'usenetrc':False,'cookiefile':None}
    def reject_live(info, *, incomplete):
        if info.get('is_live') or info.get('live_status') in ('is_live','is_upcoming','post_live'):
            result['failure']='live_unsupported'
            return 'Live broadcast not supported'
        return None
    if youtube:
        # Prefer complete H.264; merge H.264/AAC only as a fallback, never encode.
        h264="[ext=mp4][vcodec~='^(h264|avc1)']"
        combined='best[ext=mp4]/best[ext=webm]'
        separate=('bestvideo'+h264+"+bestaudio[ext=m4a][acodec~='^(aac|mp4a)']/"
                  if shutil.which('ffmpeg') else '')
        params.update(format='best'+h264+'/'+separate+combined,
                      format_sort=['res:720'],match_filter=reject_live)
    else:
        params['impersonate']=ImpersonateTarget.from_str('chrome')
    try:
        url=payload['url']
        if youtube:
            url=c.validate_url(url)
            if c.classify(url)!=('youtube','post'):raise ValueError('Invalid YouTube URL')
            video_id=url.rsplit('=',1)[1]
        with Downloader(params) as downloader:
            for key,value in cookies.items():
                downloader.cookiejar.set_cookie(create_cookie(key,value,domain='.tiktok.com'))
            info=downloader.extract_info(url,download=True,ie_key='Youtube' if youtube else 'TikTok')
            result['code']=int(not info or bool(result.get('error')) or (youtube and info.get('id')!=video_id)
                               or result['failure']=='live_unsupported')
            if youtube and not result['code']:
                downloads=info.get('requested_downloads') or []
                if len(downloads)==1 and downloads[0].get('filepath'):
                    final_path=Path(downloads[0]['filepath'])
    except StopCapture:pass
    except Exception as error:
        result['exception']=type(error).__name__
        if not result['failure']:result['failure']='extractor_error'
    finally:
        for path in sorted(root.iterdir()):
            if youtube and (path!=final_path or path.stem!=f'{video_id}_00_'):continue
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in ('.mp4','.webm','.mov','.mkv'):
                size=path.stat().st_size
                if not youtube or size:
                    result['files'].append({'path':str(path),'name':path.name,'size':size})
        if not result['files']:result['code']=1
        if result['code']:
            if 401 in result['http_errors']:result['failure']='login_required'
            elif 403 in result['http_errors']:result['failure']='forbidden'
            elif not result['failure']:result['failure']='extractor_error'
        else:result['failure']=''
        result['finished']=time.time()
        temp=root/'result.tmp';temp.write_text(json.dumps(result));temp.replace(root/'result.json')
    return result


def youtube_failure_message(reason):
    return {
        'login_required':'YouTube 要求登入或驗證；目前只支援毋須登入即可觀看的影片。',
        'forbidden':'YouTube 拒絕存取（HTTP 403）；請稍後重試或確認影片可公開觀看。',
        'live_unsupported':'暫不支援正在直播、未開始或仍在處理的影片。',
        'network_error':'連線 YouTube 失敗或逾時，請稍後重試。',
        'unavailable':'YouTube 回報影片不可用，請確認連結及觀看權限。',
    }.get(reason,'YouTube 下載失敗；請確認影片可公開觀看，或請管理員檢查 yt-dlp 及 Deno。')
