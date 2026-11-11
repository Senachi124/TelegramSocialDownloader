"""Instagram extraction options, quality preferences and Story routing."""
import re

def options(cookies):
    # Prefer Instagram's combined MP4, keeping all transfers in the metered HTTP
    # downloader (external DASH/yt-dlp requests would escape this quota).
    return {'api':'rest','cookies':cookies,'videos':'merged','previews':False,
            'metadata':True,'user-cache':'memory','user-agent':'+chrome',
            'include':['stories']}

def install(result,preferences=None):
    from .instagram_web_gallery import install_web_graphql
    from .instagram_quality import install as install_quality
    restore_web=install_web_graphql(result)
    restore_quality=install_quality(result,preferences)
    def restore():
        restore_quality()
        if restore_web:restore_web()
    return restore

def target(url,mode):
    if mode!='stories':return url
    if re.search(r'/(p|reel|reels|tv)/',url):
        from gallery_dl import extractor
        source=extractor.find(url)
        if source is None:raise ValueError('無法識別貼文作者')
        initialized=False;items=None;status=1
        try:
            # Direct items() calls bypass gallery-dl's __iter__ initialization.
            # Resolve the author through the same configured Cookie/API adapter.
            source.initialize();initialized=True
            items=source.items()
            for item in items:
                if not isinstance(item,(tuple,list)) or not item or not isinstance(item[-1],dict):continue
                author=item[-1].get('username')
                if isinstance(author,str) and re.fullmatch(r'[A-Za-z0-9._]{1,30}',author):
                    status=0;break
            else:raise ValueError('無法識別貼文作者')
        finally:
            try:
                if items is not None and callable(getattr(items,'close',None)):items.close()
                if initialized and callable(source.finalize):source.finalize(status)
            finally:
                if source.session is not None:source.session.close()
    else:
        match=re.search(r'instagram.com/(?:stories/)?([A-Za-z0-9._]+)',url)
        if not match:raise ValueError('無法識別作者')
        author=match[1]
    return 'https://www.instagram.com/stories/'+author+'/'
