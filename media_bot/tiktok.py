"""Bot-owned TikTok extraction settings and story routing."""
import re
from urllib.parse import urlsplit

def uses_yt_dlp(payload):
    return (payload.get('platform')=='tiktok' and payload.get('mode')=='post'
            and bool(re.fullmatch(r'/@[\w.-]+/video/\d+/?',urlsplit(payload['url']).path)))

def options(cookies):
    return {'cookies':cookies,'include':['stories'],'videos':True,
            'photos':True,'audio':False,'covers':False,'cookies-update':False,
            'username':None,'password':None,'netrc':False}

def install(result,preferences=None):
    """Only an explicit successful empty Story list proves no visible stories."""
    result['sleep_seconds']=0
    from gallery_dl.extractor.tiktok import TiktokStoryItemListRequest,TiktokExtractor
    original=TiktokStoryItemListRequest.extract_items
    original_urls=TiktokStoryItemListRequest.generate_urls
    original_video_urls=TiktokExtractor._extract_video_urls
    original_sleep=TiktokExtractor.sleep
    own_sleep=TiktokExtractor.__dict__.get('sleep')
    def sleep(self,seconds,reason):
        # gallery-dl's TikTok parser reuses the 30s socket timeout as a retry
        # delay. Keep network timeouts intact, but use short bounded backoff.
        if reason=='retry' and seconds==self._timeout:
            attempt=getattr(self,'_bot_parser_retry',0)+1
            self._bot_parser_retry=attempt
            seconds=2**min(attempt,3)
        result['sleep_seconds']=result.get('sleep_seconds',0)+seconds
        return original_sleep(self,seconds,reason)
    TiktokExtractor.sleep=sleep
    def extract_items(self,data):
        if data.get('statusCode',0)==0 and isinstance(data.get('itemList'),list):
            if data['itemList']:result['story_listing']='found'
            elif result.get('story_listing')!='found' and not data.get('hasMorePrevious',data.get('hasMore',False)):
                result['story_listing']='empty'
        return original(self,data)
    TiktokStoryItemListRequest.extract_items=extract_items
    def generate_urls(self,*args,**kwargs):
        urls=original_urls(self,*args,**kwargs)
        identities=[str(item.get('id')) for item in urls.values() if isinstance(item,dict)]
        result['_story_positions']={key:index for index,key in enumerate(identities,1)}
        result['_story_count']=len(identities)
        return urls
    TiktokStoryItemListRequest.generate_urls=generate_urls
    def video_urls(self,video):
        from .media_preferences import quality,record_notice
        cap=quality(preferences)
        variants=video.get('bitrateInfo')
        if cap and variants:
            variants=variants if isinstance(variants,list) else [variants]
            eligible=[v for v in variants if int((v.get('PlayAddr') or {}).get('Height') or 0)<=cap]
            if not eligible:
                minimum=min(int((v.get('PlayAddr') or {}).get('Height') or 0) for v in variants)
                eligible=[v for v in variants if int((v.get('PlayAddr') or {}).get('Height') or 0)==minimum]
                record_notice(result,'來源沒有偏好的畫質，已改為下載最接近的可用畫質。')
            video={**video,'bitrateInfo':eligible}
        return original_video_urls(self,video)
    TiktokExtractor._extract_video_urls=video_urls
    def restore():
        TiktokStoryItemListRequest.extract_items=original
        TiktokStoryItemListRequest.generate_urls=original_urls
        TiktokExtractor._extract_video_urls=original_video_urls
        if own_sleep is None:del TiktokExtractor.sleep
        else:TiktokExtractor.sleep=own_sleep
    return restore

def failure_message(reason,mode):
    content='Story' if mode=='stories' else '影片／貼文'
    detail={
        'forbidden':'TikTok 拒絕存取（HTTP 403）；可能是網路或平台限制，不能判定內容不存在。',
        'login_required':'TikTok 要求登入，或目前登入沒有觀看權限；請管理員更新 TikTok Cookie 後重試。',
        'challenge':'TikTok 要求驗證或阻擋自動存取，這次未能完成下載。',
        'region_restricted':'TikTok 回報地區限制，這次無法取得內容。',
        'unavailable':'TikTok 回報內容不可用，可能已刪除、過期或限制觀看。',
        'empty_unconfirmed':'未能確認 '+content+' 清單；可能受存取限制，不能當作「沒有 '+content+'」。',
        'network_error':'連線 TikTok 失敗或逾時，請稍後重試。',
        'extractor_error':'TikTok 回應無法解析，這次未能完成下載。',
    }
    return detail.get(reason,detail['extractor_error'])

def target(url,mode):
    if mode!='stories':return url
    match=re.search(r'/(@[\w.-]+)',url)
    if not match:raise ValueError('無法識別作者，請貼上主頁完整網址')
    return 'https://www.tiktok.com/'+match[1]+'/stories'
