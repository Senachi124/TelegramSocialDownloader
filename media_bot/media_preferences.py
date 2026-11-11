"""Download preferences with explicit fallback to reliable available media."""
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
