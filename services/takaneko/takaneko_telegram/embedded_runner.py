"""Bounded provider video transfer for fanclub articles; stdin never enters argv."""
import json
from pathlib import Path
import sys
from yt_dlp import YoutubeDL
from media_bot import download_size,media_preferences


def run(payload):
    from .takaneko_api import canonical_video_url
    url=canonical_video_url(payload['url'])
    output=Path(payload['output'])
    current={}
    result={'ok':False,'too_large':False}
    class Logger:
        def debug(self,message):pass
        def warning(self,message):pass
        def error(self,message):pass
    class Downloader(YoutubeDL):
        def process_info(self,info):
            current.clear()
            download_size.check(sum(float(f.get('filesize') or 0) for f in info.get('requested_formats') or [info]))
            return super().process_info(info)
        def dl(self,name,info,subtitle=False,test=False):
            from media_bot.yt_runner import _prepare_partial
            path=Path(name).resolve()
            if not path.is_relative_to(output.parent.resolve()) or Path(name).is_symlink():raise ValueError('Invalid output path')
            download_size.check(info.get('filesize'))
            if not path.is_file():_prepare_partial(path,info)
            value=super().dl(name,info,subtitle,test)
            if path.is_file():download_size.check(path.stat().st_size)
            return value
        def urlopen(self,request):
            response=super().urlopen(request)
            try:download_size.declared(response.headers)
            except download_size.DownloadTooLarge:
                response.close();raise
            return response
    def progress(data):
        download_size.check(data.get('total_bytes'))
        download_size.check(data.get('downloaded_bytes'))
        key=data.get('filename')
        if key:
            current[key]=max(current.get(key,0),data.get('downloaded_bytes') or 0)
            download_size.check(sum(current.values()))
    try:
        with Downloader(dict(logger=Logger(),quiet=True,noprogress=True,noplaylist=True,
            cachedir=False,cookiefile=None,usenetrc=False,socket_timeout=20,retries=2,
            allowed_extractors=['Vimeo','Youtube'],http_headers={'Referer':'https://takanekofc.com/'},
            format=media_preferences.selector({}),format_sort=media_preferences.sort({}),format_sort_force=True,
            outtmpl=str(output)+'.%(ext)s',overwrites=False,progress_hooks=[progress])) as ydl:
            info=ydl.extract_info(url,download=True)
            downloads=(info or {}).get('requested_downloads') or []
            if len(downloads)==1 and downloads[0].get('filepath'):
                path=Path(downloads[0]['filepath'])
                if path.is_file() and path.parent==output.parent:
                    download_size.check(path.stat().st_size)
                    result.update(ok=True,path=str(path))
    except download_size.DownloadTooLarge:result['too_large']=True
    except Exception:pass
    return result


if __name__=='__main__':print(json.dumps(run(json.load(sys.stdin))))
