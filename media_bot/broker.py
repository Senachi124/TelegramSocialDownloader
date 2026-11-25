"""Serialized credential operations on a private Unix socket."""
import http.server
import json
import os
from pathlib import Path
import re
import socketserver
import time
import tempfile
from . import api_keys, config, local_socket
from .core import SECRET_SOCKET
from . import accounts
from . import translation_cache

def secret_dir():
    path = Path(os.environ['MEDIA_BOT_SECRET_DIR'])
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('Secret directory must be an absolute, non-symlink path')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Secret directory requires private ownership and mode 0700')
    return path


def private_write(name, data):
    root = secret_dir()
    with tempfile.NamedTemporaryFile(mode='w', dir=root, delete=False) as stream:
        path = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(path, root / name)
            descriptor = os.open(root, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            path.unlink(missing_ok=True)


def parse_cookies(text,domain_name='tiktok.com'):
    if len(text)>131072 or '\x00' in text:raise ValueError('Cookie 太長或格式無效')
    if domain_name=='youtube.com':
        from .youtube_cookies import parse
        return parse(text)
    if domain_name in ('threads.com','threads.net'):
        return parse_threads_cookies(text)
    if domain_name in ('twitter.com','x.com'):
        from .twitter import parse_cookies
        return parse_cookies(text)
    result={}
    if '\t' in text:
        for line in text.splitlines():
            if line.startswith('#HttpOnly_'):line=line[10:]
            elif line.startswith('#') or not line.strip():continue
            values=line.split('\t')
            if len(values)!=7:raise ValueError('請使用 Netscape cookies.txt 或 Cookie header')
            domain,_,_,_,_,name,value=values
            if domain.lstrip('.')==domain_name or domain.endswith('.'+domain_name):result[name]=value
    else:
        text=text.strip()
        if text.lower().startswith('cookie:'):text=text[7:].strip()
        if any(ord(x)<32 for x in text):raise ValueError('Cookie header 必須單行')
        for part in text.split(';'):
            if not part.strip():continue
            name,sep,value=part.strip().partition('=')
            if not sep or not re.fullmatch(r'[A-Za-z0-9_-]+',name):raise ValueError('Cookie 格式無效')
            result[name]=value
    if not result:raise ValueError('找不到 '+domain_name+' Cookie')
    return result

def parse_threads_cookies(text):
    """Import only browser session cookies for explicitly scoped Threads hosts."""
    allowed={'sessionid','csrftoken','ds_user_id','mid','ig_did'}
    domains={'threads.com','www.threads.com','threads.net','www.threads.net'}
    result={}
    def add(name,value):
        if name not in allowed:return
        if not value or len(value)>16384 or any(ord(ch)<33 or ord(ch)>126 or ch in ';,\\"' for ch in value):
            raise ValueError('Threads Cookie 格式無效')
        if name in result and result[name]!=value:
            raise ValueError('Threads Cookie 有重複而不同的值，請只匯出一個登入帳號')
        result[name]=value
    if '\t' in text:
        for line in text.splitlines():
            if line.startswith('#HttpOnly_'):line=line[10:]
            elif line.startswith('#') or not line.strip():continue
            parts=line.split('\t')
            if len(parts)!=7:raise ValueError('請使用 Netscape cookies.txt 或 Cookie header')
            domain,_,path,secure,expiry,name,value=parts
            if domain.lstrip('.').lower() not in domains or name not in allowed:continue
            if path!='/' or secure.upper()!='TRUE':
                raise ValueError('請匯入 HTTPS、根路徑的 Threads 登入 Cookie')
            try:expires=int(expiry)
            except ValueError:raise ValueError('Threads Cookie 到期時間無效') from None
            if expires and expires<=time.time():continue
            add(name,value)
    else:
        text=text.strip()
        if text.lower().startswith('cookie:'):text=text[7:].strip()
        if any(ord(ch)<32 for ch in text):raise ValueError('Cookie header 必須單行')
        for part in text.split(';'):
            if not part.strip():continue
            name,sep,value=part.strip().partition('=')
            if not sep or not re.fullmatch(r'[A-Za-z0-9_-]+',name):raise ValueError('Cookie 格式無效')
            add(name,value)
    if not all(result.get(name) for name in ('sessionid','csrftoken')):
        raise ValueError('Threads 需要瀏覽器登入的 sessionid 與 csrftoken；請勿貼 Authorization Bearer')
    return result


def load():
    path = secret_dir() / 'secrets.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    if not data.get('token'):
        data['token'] = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    return accounts.normalize(data)


def bootstrap():
    """Import a previous file-based installation once, before accepting requests."""
    data = load()
    if data.get('external_files_imported'):
        return
    if not data.get('token') and os.environ.get('TELEGRAM_BOT_TOKEN_FILE'):
        data['token'] = config.token()
    for platform in config.PLATFORMS:
        if not accounts.pool(data, platform)['items'] and os.environ.get(platform.upper()+'_COOKIES_FILE'):
            credentials = config.cookies(platform)
            if credentials:
                accounts.update_primary(data, platform, credentials)
    data['external_files_imported'] = True
    save(data)


def save(data):
    private_write('secrets.json', json.dumps(data))

def local_api_status():
    return {'credentials_set':(secret_dir()/'local-api.env').is_file()}


def save_local_api(request):
    api_id=request.get('api_id','').strip();api_hash=request.get('api_hash','').strip()
    if not api_id and not api_hash:return local_api_status()
    if not re.fullmatch(r'[1-9][0-9]{0,9}',api_id) or int(api_id)>2147483647 or not re.fullmatch(r'[0-9a-fA-F]{32}',api_hash):
        raise ValueError('API ID／API Hash 格式無效；更新時需同時提供兩者')
    private_write('local-api.env', f'TELEGRAM_API_ID={api_id}\nTELEGRAM_API_HASH={api_hash.lower()}\n')
    return local_api_status()

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def setup(self):super().setup();self.connection.settimeout(10)
    def reply(self,data,status=200):
        raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):
        if self.path=='/local-api/status':return self.reply(local_api_status())
        p=load()
        if self.path=='/translation/status':return self.reply({'keys':api_keys.summary(p)})
        if self.path=='/translation/keys':return self.reply({'keys':api_keys.selected(p)})
        if self.path=='/telegram':return self.reply({'token':p.get('token','')})
        if self.path=='/worker':
            accounts.sync_legacy(p)
            return self.reply({k:p.get(k,{}) for k in ('token','cookies','instagram_cookies')})
        if self.path=='/accounts':return self.reply(accounts.public_summary(p))
        if self.path=='/status':
            available={platform:any(a['enabled'] and a['cookies'] for a in accounts.pool(p,platform)['items']) for platform in accounts.PLATFORMS}
            return self.reply({'token_set':bool(p['token']),'cookies_set':available['tiktok'],'instagram_cookies_set':available['instagram'],'twitter_cookies_set':available['twitter'],'threads_cookies_set':available['threads'],'youtube_cookies_set':available['youtube']})
        self.reply({'error':'Not found'},404)
    def translation_image(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=4096:raise ValueError('Invalid request')
            request=json.loads(self.rfile.read(size))
            data=translation_cache.read_image(load().get('token',''), **request)
        except (OSError,TypeError,ValueError):
            return self.reply({'error':'無法載入圖片'},400)
        self.send_response(200)
        self.send_header('Content-Type','application/octet-stream')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_POST(self):
        if self.path=='/translation/image':
            try:self.translation_image()
            except OSError:pass  # Client disconnected. Do not log cache paths.
            return
        try:
            if self.path not in ('/settings','/accounts','/select','/report','/translation/keys','/local-api',
                                 '/instagram-refresh/snapshot','/instagram-refresh/result'):raise ValueError('Invalid endpoint')
            n=int(self.headers.get('Content-Length','0'))
            if not 0<n<=300000:raise ValueError('資料太大')
            p=json.loads(self.rfile.read(n))
            if self.path=='/local-api':return self.reply(save_local_api(p))
            old=load()
            if self.path.startswith('/instagram-refresh/'):
                from . import instagram_cookie_maintenance as maintenance
                if self.path.endswith('/snapshot'):
                    return self.reply(maintenance.snapshot(old, p['id']))
                status = maintenance.apply_result(old, p)
                if status == 'updated':
                    save(old)
                return self.reply({'status': status})
            if self.path=='/translation/keys':
                result=api_keys.edit(old,p);save(old);return self.reply(result)
            if self.path!='/settings':
                operation={'/accounts':lambda:accounts.edit(old,p,parse_cookies),
                           '/select':lambda:accounts.select(old,p),'/report':lambda:accounts.report(old,p)}
                result=operation[self.path]();save(old);return self.reply(result)
            token=p.get('token','').strip()
            if token:
                if not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{25,100}',token):raise ValueError('Bot token 格式無效')
                old['token']=token
            for platform,key in accounts.LEGACY.items():
                if p.get(key,'').strip():accounts.update_primary(old,platform,parse_cookies(p[key],platform+'.com'))
            save(old);self.reply({'ok':True})
        except (ValueError,KeyError,TypeError,AttributeError):self.reply({'error':'Invalid account settings or credential format'},400)

def main():
    os.umask(0o077)
    class Server(socketserver.UnixStreamServer):pass
    secret_dir()
    path = Path(SECRET_SOCKET)
    if not path.is_absolute() or path.parent.resolve() != path.parent:
        raise ValueError('Invalid broker socket directory')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with local_socket.reserve(path), Server(str(path),Handler) as server:
        bootstrap()
        group = os.environ.get('MEDIA_BOT_SECRET_SOCKET_GID')
        if group:
            os.chown(path, -1, int(group))
        os.chmod(path, 0o660 if group else 0o600)
        server.serve_forever()

if __name__=='__main__':main()
