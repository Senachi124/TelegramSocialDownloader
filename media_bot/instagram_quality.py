"""Select Instagram quality/HDR from provided variants and standard DASH metadata.

No guessed HDR from codec or bit depth. DASH transfers keep metering, limits and
credential isolation; FFmpeg only merges streams, never tone-maps or encodes.
"""
import copy
from pathlib import Path
import shutil
import time
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from . import core as c,download_size,media_preferences


def dynamic_range(item):
    value=str(item.get('dynamic_range') or '').upper()
    if value in ('HDR','HDR10','HDR10+','HLG','DV'):return value
    if item.get('is_hdr') is True:return 'HDR'
    return 'SDR' if value=='SDR' or item.get('is_hdr') is False else None


def rank(item,preferences):
    hdr=bool(dynamic_range(item) not in (None,'SDR'))
    prefer=(preferences or {}).get('prefer_hdr',True)
    width=int(item.get('width') or 0);height=int(item.get('height') or 0)
    return (hdr if prefer else not hdr,min(width,height),width*height,
            float(item.get('fps') or 0),float(item.get('bitrate') or item.get('bandwidth') or 0))


def prepare_post(post,preferences):
    post=copy.deepcopy(post)
    items=post.get('items') or post.get('carousel_media') or [post]
    ceiling=media_preferences.quality(preferences)
    for item in items:
        images=(item.get('image_versions2') or {}).get('candidates')
        if images:
            def image_rank(variant):
                hdr=dynamic_range(variant) not in (None,'SDR')
                preferred=hdr if (preferences or {}).get('prefer_hdr',True) else not hdr
                return preferred,int(variant.get('width') or 0)*int(variant.get('height') or 0)
            images.sort(key=image_rank,reverse=True)
        videos=item.get('video_versions')
        if videos:
            eligible=[v for v in videos if not ceiling or int(v.get('height') or 0)<=ceiling]
            # Preferences never substitute a video's cover for the video. If no
            # rendition fits, choose the closest available height above the cap.
            if not eligible:
                nearest=min(int(v.get('height') or 0) for v in videos)
                eligible=[v for v in videos if int(v.get('height') or 0)==nearest]
            item['video_versions']=sorted(eligible or videos,key=lambda v:rank(v,preferences))
    return post


def annotate_manifest_formats(root,formats):
    """CICP transfer 16=PQ, 18=HLG. Inherit adaptation-set declarations."""
    ranges={};sizes={}
    def transfer(node,inherited=None):
        value=inherited
        for child in node:
            if child.tag.rsplit('}',1)[-1] not in ('EssentialProperty','SupplementalProperty'):continue
            scheme=child.get('schemeIdUri','')
            if scheme=='urn:mpeg:mpegB:cicp:TransferCharacteristics':
                value={'16':'HDR10','18':'HLG','1':'SDR','6':'SDR','13':'SDR'}.get(child.get('value'),value)
        return value
    for adaptation in root.iter():
        if adaptation.tag.rsplit('}',1)[-1]!='AdaptationSet':continue
        inherited=transfer(adaptation)
        for representation in adaptation:
            if representation.tag.rsplit('}',1)[-1]!='Representation':continue
            value=transfer(representation,inherited)
            if value:ranges[representation.get('id','')]=value
            size=representation.get('FBContentLength','')
            if size.isdecimal():sizes[representation.get('id','')]=int(size)
    for fmt in formats:
        key=str(fmt.get('format_id',''))
        matches=[value for ident,value in ranges.items() if ident and key in (ident,'dash-'+ident)]
        if matches:fmt['dynamic_range']=matches[0]
        size_matches=[value for ident,value in sizes.items() if ident and key in (ident,'dash-'+ident)]
        if size_matches:fmt['filesize']=size_matches[0]
    return formats


def install(result,preferences=None):
    from gallery_dl import config
    from gallery_dl.extractor.instagram import InstagramExtractor
    from gallery_dl.downloader.ytdl import YoutubeDLDownloader
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.exceptions import HTTPError
    preferences=preferences or {}
    old_parse=InstagramExtractor._parse_post
    old_extract=YoutubeDLDownloader._extract_manifest
    old_download=YoutubeDLDownloader._download_video
    old_open=YoutubeDL.urlopen
    old_dl=YoutubeDL.dl
    config_keys=[(('extractor','instagram'),'videos'),(('downloader','ytdl'),'forward-cookies'),
                 (('downloader','ytdl'),'raw-options')]
    old_config=[(path,key,config.get(path,key)) for path,key in config_keys]
    active={}
    def progress(info):
        if info.get('status') not in ('downloading','finished'):return
        count=info.get('downloaded_bytes') or 0
        download_size.check(count);download_size.check(info.get('total_bytes'))
        key=info.get('filename') or info.get('tmpfilename')
        if key:
            active[key]=max(count,active.get(key,0))
            download_size.check(sum(active.values()))
    def parse(extractor,post):
        prepared=prepare_post(post,preferences)
        value=old_parse(extractor,prepared)
        files=value.get('_files',[])
        has_images=any(not f.get('video_url') and not f.get('audio_url') for f in files)
        image_positions=[f['num'] for f in files if not f.get('video_url') and not f.get('audio_url')]
        for file in files:
            file['_preference_has_images']=has_images
            file['_preference_image_indices']=image_positions
        for item in prepared.get('items') or prepared.get('carousel_media') or [prepared]:
            versions=item.get('video_versions') or []
            if versions:
                result['hdr_available']=bool(result.get('hdr_available') or any(dynamic_range(v) not in (None,'SDR') for v in versions))
                if not item.get('video_dash_manifest'):
                    result['hdr_selected']=bool(result.get('hdr_selected') or dynamic_range(versions[-1]) not in (None,'SDR'))
        return value
    def extract(downloader,ydl,url,data):
        if data.get('_ytdl_manifest')!='dash':return old_extract(downloader,ydl,url,data)
        text=data.get('_ytdl_manifest_data')
        if not isinstance(text,str) or len(text)>8*1024**2 or '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
            raise ValueError('Instagram DASH metadata invalid')
        root=ET.fromstring(text)
        generic=ydl.get_info_extractor('Generic')
        formats,subtitles=generic._parse_mpd_formats_and_subtitles(root,mpd_id='dash')
        annotate_manifest_formats(root,formats)
        result['hdr_available']=bool(result.get('hdr_available') or any(f.get('dynamic_range') not in (None,'SDR') for f in formats))
        info=ydl.process_ie_result({'id':str(data.get('media_id') or 'instagram'),
            'title':'Instagram','extractor':'','formats':formats,'subtitles':subtitles},download=False)
        chosen=info.get('requested_formats') or [info]
        if (cap:=media_preferences.quality(preferences)) and int(info.get('height') or 0)>cap:
            media_preferences.record_notice(result,'來源沒有偏好的畫質，已改為下載最接近的可用畫質。')
        result['hdr_selected']=bool(result.get('hdr_selected') or any(f.get('dynamic_range') not in (None,'SDR') for f in chosen))
        if preferences.get('prefer_hdr',True) and not any(f.get('dynamic_range') not in (None,'SDR') for f in chosen):
            media_preferences.record_notice(result,'來源未提供可識別的 HDR 版本，已採用可用畫質。')
        return info
    def download(downloader,ydl,pathfmt,info):
        active.clear()
        download_size.check(sum(float(f.get('filesize') or 0) for f in (info.get('requested_formats') or [info])))
        result_value=old_download(downloader,ydl,pathfmt,info)
        from pathlib import Path
        output=Path(pathfmt.temppath or pathfmt.realpath)
        if output.is_file():download_size.check(output.stat().st_size)
        return result_value
    def urlopen(ydl,request):
        url=request if isinstance(request,str) else getattr(request,'url',None) or request.get_full_url()
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443) or not (parsed.hostname or '').endswith(('.cdninstagram.com','.fbcdn.net')):
            raise ValueError('Instagram DASH media host invalid')
        result['requests']+=1
        try:response=old_open(ydl,request)
        except HTTPError as error:
            response=error.response
            if response.status==429:
                until=time.time()+900
                c.setting('instagram_cooldown',until);result.update(retry_at=until,wait_reason='platform')
                response.close()
                raise download_size.DownloadStopped() from None
            raise
        try:download_size.declared(response.headers)
        except download_size.DownloadTooLarge:
            response.close();raise
        return response
    def transfer(ydl,name,info,subtitle=False,test=False):
        from .yt_runner import _prepare_partial
        path=Path(name).resolve()
        base=Path(config.get(('extractor',),'base-directory') or '.').resolve()
        if not path.is_relative_to(base) or Path(name).is_symlink():raise ValueError('Invalid Instagram output path')
        download_size.check(info.get('filesize'))
        if not path.is_file():_prepare_partial(path,info)
        value=old_dl(ydl,name,info,subtitle,test)
        if path.is_file():download_size.check(path.stat().st_size)
        return value
    if shutil.which('ffmpeg'):
        config.set(('extractor','instagram'),'videos',True)
        config.set(('downloader','ytdl'),'forward-cookies',False)
        config.set(('downloader','ytdl'),'raw-options',dict(
            format=media_preferences.selector(preferences),format_sort=media_preferences.sort(preferences),
            format_sort_force=True,progress_hooks=[progress],cachedir=False,cookiefile=None,usenetrc=False,
            skip_unavailable_fragments=False))
    InstagramExtractor._parse_post=parse
    YoutubeDLDownloader._extract_manifest=extract
    YoutubeDLDownloader._download_video=download
    YoutubeDL.urlopen=urlopen
    YoutubeDL.dl=transfer
    def restore():
        InstagramExtractor._parse_post=old_parse
        YoutubeDLDownloader._extract_manifest=old_extract
        YoutubeDLDownloader._download_video=old_download
        YoutubeDL.urlopen=old_open
        YoutubeDL.dl=old_dl
        for path,key,value in old_config:
            if value is None:config.unset(path,key)
            else:config.set(path,key,value)
    return restore
