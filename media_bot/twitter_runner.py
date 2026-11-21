"""Twitter capture with a memory-only credential pool.

Caller-supplied credentials are never persisted here. API limits are persisted
in Bot state and survive restarting a child.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
os.environ['TWS_TELEMETRY']='0'
from twscrape import API, models
from twscrape.account import Account
from twscrape.accounts_pool import AccountsPool
from twscrape.logger import logger
import httpx
from . import core as c, twitter
from . import download_size


class CaptureFailure(Exception):pass


def cooldown(result,until):
    until=max(time.time()+900,float(until),c.get_setting('twitter_cooldown',0))
    c.setting('twitter_cooldown',until)
    result.update(code=1,retry_at=until,wait_reason='platform')


class MemoryPool(AccountsPool):
    """Only the pinned account; no provider SQLite file, rotation or login attempts."""
    def __init__(self,cookies,result):
        self.result=result;self.used=False
        self.account=Account(username='bot-session',password='_',email='_',email_password='_',
                             user_agent='@chrome',active=True,cookies=cookies)
    async def get_for_queue_or_wait(self,queue):
        if self.result['retry_at'] or self.result['auth_required'] or self.used:
            raise CaptureFailure('extractor_error')
        self.used=True
        return self.account
    async def unlock(self,username,queue,req_count=0):pass
    async def lock_until(self,username,queue,unlock_at,req_count=0):cooldown(self.result,unlock_at)
    async def mark_inactive(self,username,error_msg=None):
        self.result.update(auth_required=True,failure='login_required')


def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def media_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or not (p.hostname or '').endswith('.twimg.com'):
        raise CaptureFailure('extractor_error')


async def fetch(client,url,path,result):
    media_url(url)
    receipt=path.with_suffix(path.suffix+'.receipt.json')
    if path.is_file() and receipt.is_file():
        download_size.check(path.stat().st_size)
        saved=json.loads(receipt.read_text())
        if path.stat().st_size==saved['size'] and digest(path)==saved['sha256']:return
    if c.reserve_media('twitter',str(path.resolve())):result['media_attempts']+=1
    part=path.with_suffix(path.suffix+'.part')
    async with client.stream('GET',url) as response:
        result['requests']+=1
        if response.status_code==429:
            value=response.headers.get('retry-after','900')
            try:until=time.time()+float(value)
            except ValueError:
                try:until=parsedate_to_datetime(value).timestamp()
                except (ValueError,TypeError,OverflowError):until=time.time()+900
            cooldown(result,until);raise CaptureFailure('rate_limited')
        if response.status_code in (401,403):raise CaptureFailure('forbidden')
        response.raise_for_status()
        download_size.declared(response.headers)
        kind=response.headers.get('content-type','').split(';')[0]
        if not (kind.startswith(('image/','video/')) or kind=='application/octet-stream'):
            raise CaptureFailure('extractor_error')
        size=0
        with part.open('wb') as stream:
            async for chunk in response.aiter_bytes():
                size+=len(chunk)
                download_size.check(size)
                stream.write(chunk)
        expected=response.headers.get('content-length')
        if not size or expected and size!=int(expected):raise CaptureFailure('network_error')
    part.replace(path)
    receipt.write_text(json.dumps({'size':size,'sha256':digest(path)}))


async def capture(p,result):
    key=twitter.post_id(p['url'])
    if not all(p['cookies'].get(k) for k in ('auth_token','ct0')):
        result['auth_required']=True;raise CaptureFailure('login_required')
    until=c.get_setting('twitter_cooldown',0)
    if until>time.time():result.update(retry_at=until,wait_reason='platform');return
    pool=MemoryPool(p['cookies'],result)
    tweet=await API(pool).tweet_details(int(key))
    if result.get('provider_error'):raise CaptureFailure('extractor_error')
    if not tweet:raise CaptureFailure('unavailable')
    if str(tweet.id)!=key:raise CaptureFailure('extractor_error')
    items=twitter.assets(tweet)
    if not items:result.update(code=0,no_media=True);return
    async with httpx.AsyncClient(timeout=60,follow_redirects=False) as client:
        for n,(url,ext) in enumerate(items,1):
            path=Path(p['root'])/f'{key}_{n:02}.{ext}'
            await fetch(client,url,path,result)
            result['files'].append({'path':str(path),'name':path.name,'size':path.stat().st_size})
    result['code']=0


def run(p):
    root=Path(p['root']);root.mkdir(parents=True,exist_ok=True)
    result=dict(code=1,retry_at=0,auth_required=False,files=[],failure='',cookie_used=bool(p.get('cookies')),
                started=time.time(),requests=0,media_attempts=0,wait_reason='',engine='twscrape')
    # Upstream errors can include raw URLs/payloads. Capture only closed categories.
    logger.remove()
    def observe(message):
        if message.record['level'].no>=30:result['provider_error']=True
    sink=logger.add(observe,level='WARNING')
    original_dump=models._write_dump
    def no_dump(*args,**kwargs):result['provider_error']=True
    models._write_dump=no_dump
    try:asyncio.run(asyncio.wait_for(capture(p,result),timeout=300))
    except download_size.DownloadTooLarge:download_size.mark(result)
    except c.RateLimited as error:result.update(retry_at=error.until,wait_reason='quota')
    except CaptureFailure as error:result['failure']=str(error)
    except (httpx.HTTPError,TimeoutError):result['failure']='network_error'
    except Exception:result['failure']='extractor_error'
    finally:
        models._write_dump=original_dump;logger.remove(sink)
        if result['retry_at'] or result['auth_required']:result['code']=1
        result['finished']=time.time()
        tmp=root/'result.tmp';tmp.write_text(json.dumps(result));tmp.replace(root/'result.json')
