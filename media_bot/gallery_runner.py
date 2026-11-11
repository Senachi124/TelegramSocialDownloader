"""Isolated downloads; credentials stay on stdin, quota and files survive restart."""
import json
import logging
from pathlib import Path
import sys
import time
from email.utils import parsedate_to_datetime
from . import core as c

class StopDownload(BaseException):pass

def main():
    from . import instagram,tiktok
    p=json.load(sys.stdin);platform=p.get('platform','tiktok')
    if platform=='twitter':
        from .twitter_runner import run
        run(p);return
    if platform=='youtube' or tiktok.uses_yt_dlp(p):
        from .yt_runner import run
        run(p);return
    import requests
    from gallery_dl import config,job
    adapter=instagram if platform=='instagram' else tiktok
    root=Path(p['root']);root.mkdir(parents=True,exist_ok=True)
    result={'code':1,'retry_at':0,'auth_required':False,'files':[],
            'failure':'','http_errors':[],'cookie_used':bool(p.get('cookies')),'started':time.time(),'requests':0,'media_attempts':0,'wait_reason':''}
    options={'extractor':{'base-directory':str(root),'directory':[],
        'filename':'{media_id}.{extension}' if platform=='instagram' else '{id}_{num:02}_{file_id}.{extension}',
        'sleep':0,'sleep-request':0,
        'sleep-retries':'exp=2','retries':2,'timeout':30,
        platform:adapter.options(p['cookies'])},'output':{'progress':False}}
    config.clear()
    for key,value in options.items():config.set((),key,value)
    original=requests.adapters.HTTPAdapter.send
    def send(session,*args,**kwargs):
        result['requests']+=1
        response=original(session,*args,**kwargs)
        if response.status_code in (401,403,404,429):result['http_errors'].append(response.status_code)
        if platform=='tiktok' and '/login' in response.headers.get('Location',''):result['failure']='login_required'
        if response.status_code==429:
            value=response.headers.get('Retry-After','900')
            try:until=time.time()+float(value)
            except ValueError:
                try:until=parsedate_to_datetime(value).timestamp()
                except Exception:until=time.time()+900
            result.update(retry_at=max(time.time()+900,until),wait_reason='platform')
            c.setting(platform+'_cooldown',result['retry_at'])
            response.close();raise StopDownload()
        return response
    requests.adapters.HTTPAdapter.send=send
    class Errors(logging.Handler):
        def emit(self,record):
            message=record.getMessage().lower()
            if record.levelno>=logging.ERROR:result['error']=True
            if platform=='instagram':
                if any(x in message for x in ('authrequired','login required','loginrequired','login_required','captcha','challenge','checkpoint','feedback_required')):result['auth_required']=True
                return
            if record.levelno<logging.WARNING:return
            if any(x in message for x in ('login required','login page','login_required')):result['failure']='login_required'
            elif any(x in message for x in ('timed out','connection error','connectionerror','connection refused')):result['failure']='network_error'
            elif 'region locked' in message:result['failure']='region_restricted'
            elif any(x in message for x in ('captcha','failed to solve javascript challenge')):result['failure']='challenge'
            elif any(x in message for x in ('not available','not found','could not be found')):result['failure']='unavailable'
    handler=Errors();logging.getLogger().addHandler(handler)
    original_download=job.DownloadJob.download
    def download(self,url):
        try:
            if c.reserve_media(platform,str(Path(self.pathfmt.realpath).resolve())):result['media_attempts']+=1
        except c.RateLimited as error:
            result.update(retry_at=error.until,wait_reason='quota');raise StopDownload()
        return original_download(self,url)
    job.DownloadJob.download=download
    ordered=[];handle_url=job.DownloadJob.handle_url
    def handle(self,url,data):
        value=handle_url(self,url,data)
        path=Path(self.pathfmt.realpath)
        if path.is_file() and not path.is_symlink() and path not in ordered:ordered.append(path)
        return value
    job.DownloadJob.handle_url=handle
    restore_adapter=lambda:None
    try:
        restore_adapter=adapter.install(result)
        url=adapter.target(p['url'],p.get('mode','post'))
        result['code']=job.DownloadJob(url).run() or 0
        if result.get('error') or result['auth_required']:result['code']=1
    except StopDownload:result['code']=1
    except Exception as error:
        result['code']=1;result['exception']=type(error).__name__
        if platform=='tiktok' and isinstance(error,requests.RequestException):result['failure']='network_error'
    finally:
        requests.adapters.HTTPAdapter.send=original;logging.getLogger().removeHandler(handler)
        job.DownloadJob.handle_url=handle_url
        job.DownloadJob.download=original_download
        restore_adapter()
        for f in [*ordered,*sorted(set(root.iterdir())-set(ordered))]:
            if f.is_file() and not f.is_symlink() and f.suffix.lower() in ('.mp4','.webm','.mov','.jpg','.jpeg','.png','.webp','.gif'):
                result['files'].append({'path':str(f),'name':f.name,'size':f.stat().st_size})
        if platform=='tiktok':
            failed=bool(result['code'] or result.get('error'))
            if p.get('mode')=='stories' and result.get('story_listing') not in ('found','empty'):
                failed=True
            if not result['files'] and not (p.get('mode')=='stories' and result.get('story_listing')=='empty'):
                failed=True
            if failed:
                result['code']=1
                if 401 in result['http_errors']:result['failure']='login_required'
                elif 403 in result['http_errors'] and result['failure']!='login_required':result['failure']='forbidden'
                if not result['failure']:
                    result['failure']=('login_required' if 401 in result['http_errors'] else 'forbidden' if 403 in result['http_errors'] else 'unavailable' if 404 in result['http_errors'] else 'extractor_error' if result.get('error') else 'empty_unconfirmed')
            else:result['failure']=''
        result['finished']=time.time()
        tmp=root/'result.tmp';tmp.write_text(json.dumps(result));tmp.replace(root/'result.json')

if __name__=='__main__':main()
