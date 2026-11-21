"""Download preferences with explicit fallback to reliable available media."""
import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs,urlsplit


def effective(preferences,url,mode,*,platform='instagram',result=None):
    """Instagram's selected image belongs to this URL, never saved story indices."""
    selected=dict(preferences or {})
    if platform=='tiktok':
        selected.pop('indices',None)
        if selected.get('content') in ('post_selected','stories_selected'):
            selected['content']=selected['content'].replace('_selected','_all')
        return selected
    if platform=='instagram':
        selected.pop('indices',None)
        if selected.get('content')=='stories_selected':selected['content']='stories_all'
    if platform!='instagram' or selected.get('content')!='post_images':return selected
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


# yt-dlp can report a codec name or an RFC 6381 profile such as avc1.64002a.
_VIDEO_CODEC = r'(?:avc1|avc3|h264|h265|hevc|hev1|hvc1)(?:[. _-].*)?'
_AAC_CODEC = r'(?:aac(?:[. _-].*)?|mp4a\.40\.(?:1|2|3|4|5|6|17|23|29|39))'


def native_mp4_variant(variant):
    """Prefer a provider rendition only when container and both codecs are identified."""
    ext = variant.get('ext') or PurePosixPath(urlsplit(variant.get('url') or '').path).suffix.lstrip('.')
    vcodec = variant.get('vcodec') or variant.get('video_codec') or ''
    acodec = variant.get('acodec') or variant.get('audio_codec') or ''
    return (str(ext).lower() == 'mp4' and bool(re.fullmatch(_VIDEO_CODEC, str(vcodec), re.I))
            and (acodec == 'none' or bool(re.fullmatch(_AAC_CODEC, str(acodec), re.I))))


def selector(preferences, *, merge=True, direct=False, prefer_mp4=False):
    cap = quality(preferences)
    ceilings = [f'[height<={cap}]', ''] if cap else ['']
    alternatives = []
    compatible_video = f"[ext=mp4][vcodec~='(?i)^{_VIDEO_CODEC}$']"
    compatible_audio = f"[acodec~='(?i)^{_AAC_CODEC}$']"
    # bestvideo* may select a complete file. Exclude incompatible embedded audio
    # because yt-dlp drops the added audio stream when the video already has one.
    video_audio = f"[acodec~='(?i)^(?:none|{_AAC_CODEC})$']"
    for ceiling in ceilings:
        protocols = ('[protocol=https]', '[protocol=http]') if direct else ('',)
        for protocol in protocols:
            best = 'best' + ceiling + protocol
            if prefer_mp4:
                if merge and not direct:
                    alternatives.append('bestvideo*' + ceiling + compatible_video + video_audio
                                        + "+bestaudio[ext~='^(m4a|mp4)$']" + compatible_audio)
                alternatives.append(best + compatible_video + compatible_audio)
            if merge and not direct:
                alternatives.append('bestvideo*' + ceiling + '+bestaudio')
            alternatives.append(best)
    return '/'.join(alternatives)


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
