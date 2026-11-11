"""Download preferences with explicit fallback to reliable available media."""
import re
from urllib.parse import parse_qs,urlsplit


def effective(preferences,url,mode,*,platform='instagram',result=None):
    """Instagram's selected image belongs to this URL, never saved story indices."""
    selected=dict(preferences or {})
    if platform=='tiktok':
        selected.pop('indices',None)
        if selected.get('content') in ('post_selected','stories_selected'):
            selected['content']=selected['content'].replace('_selected','_all')
        return selected
    if platform!='instagram' or selected.get('content')!='post_images':return selected
    selected.pop('indices',None)
    try:
        parsed=urlsplit(url)
        values=parse_qs(parsed.query,keep_blank_values=True).get('img_index',[])
        valid=(mode=='post' and parsed.scheme=='https'
               and parsed.hostname in ('instagram.com','www.instagram.com')
               and not parsed.username and not parsed.password and parsed.port in (None,443)
               and re.fullmatch(r'/(?:p|reels?|tv)/[A-Za-z0-9_-]+/?',parsed.path)
               and len(values)==1 and re.fullmatch(r'[1-9][0-9]{0,3}',values[0])
               and int(values[0])<=1000)
    except (ValueError,TypeError):valid=False
    if valid:
        selected['indices']=[int(values[0])]
    else:
        selected['content']='post_all'
        if result is not None:
            record_notice(result,'連結沒有可套用的圖片序號 img_index，已下載此連結實際可識別的全部內容。')
    return selected


def quality(preferences):
    value=(preferences or {}).get('quality','best')
    return None if value=='best' else int(value) if str(value) in ('1080','720','480') else None


def sort(preferences):
    cap=quality(preferences)
    return ['hdr' if (preferences or {}).get('prefer_hdr',True) else 'hdr:SDR',
            f'res:{cap}' if cap else 'res','fps','vcodec:h264','acodec:aac']


def selector(preferences, *, merge=True, direct=False):
    cap=quality(preferences)
    ceiling=f'[height<={cap}]' if cap else ''
    if direct:
        filtered=f'best[protocol=https]{ceiling}/best[protocol=http]{ceiling}'
        return filtered+('/best[protocol=https]/best[protocol=http]' if cap else '')
    filtered=f'bestvideo*{ceiling}+bestaudio/best{ceiling}' if merge else 'best'+ceiling
    return filtered+('/bestvideo*+bestaudio/best' if cap and merge else '/best' if cap else '')


def record_notice(result,message):
    """Keep independent media, quality and HDR fallback explanations together."""
    previous=result.get('preference_notice','')
    notices=previous.splitlines() if isinstance(previous,str) else []
    if message not in notices:notices.append(message)
    result['preference_notice']='\n'.join(notices)


def include(preferences,position,*,video=False,total=None,has_images=None,image_positions=None,result=None):
    preferences=preferences or {}
    content=preferences.get('content','post_all')
    def notice():
        if result is not None:record_notice(result,'指定內容偏好無法套用，已改為下載此連結實際可識別的內容。')
        return True
    if content=='post_images':
        if video:return notice() if has_images is not True else False
        wanted=preferences.get('indices',[])
        if wanted:
            eligible=set(image_positions or []) & set(wanted)
            if not eligible:return notice()
            if eligible!=set(wanted):notice()
            return position in eligible
        return True
    if content in ('post_selected','stories_selected'):
        wanted=preferences.get('indices',[])
        if not total or not any(type(i) is int and 1<=i<=total for i in wanted):return notice()
        if any(type(i) is not int or not 1<=i<=total for i in wanted):notice()
        return position in wanted
    return True
