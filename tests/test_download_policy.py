"""Real format selection and bounded synthetic transfers; no provider credentials."""
import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import os
from unittest.mock import Mock,patch
from xml.etree import ElementTree as ET

_TEST_ROOT = Path(__file__).resolve().parents[1] / '.test-work'
_TEST_ROOT.mkdir(exist_ok=True)
_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='download-import-', dir=_TEST_ROOT)
with patch.dict(os.environ, {'MEDIA_BOT_DATA': _BOOTSTRAP.name}):
    from media_bot import core as c,download_size,media_preferences,instagram_quality,gallery_runner,twitter_runner,yt_runner

MANIFEST='''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT10S"><Period>
<AdaptationSet mimeType="video/mp4" codecs="avc1.640028">
<Representation id="sdr" width="3840" height="2160" bandwidth="5000000"><BaseURL>https://cdn.cdninstagram.com/sdr.mp4</BaseURL></Representation>
<Representation id="hdr" width="1920" height="1080" bandwidth="3000000"><EssentialProperty schemeIdUri="urn:mpeg:mpegB:cicp:TransferCharacteristics" value="16"/><BaseURL>https://cdn.cdninstagram.com/hdr.mp4</BaseURL></Representation>
</AdaptationSet><AdaptationSet mimeType="audio/mp4" codecs="mp4a.40.2"><Representation id="audio" bandwidth="128000"><BaseURL>https://cdn.cdninstagram.com/audio.m4a</BaseURL></Representation></AdaptationSet></Period></MPD>'''


class PreferenceTests(unittest.TestCase):
    def test_hdr_manifest_actual_selector_toggle_and_sdr_fallback(self):
        from gallery_dl import config
        from gallery_dl.downloader.ytdl import YoutubeDLDownloader
        from yt_dlp import YoutubeDL
        for preferences,manifest,wanted in [({},MANIFEST,'dash-hdr+dash-audio'),
                ({'prefer_hdr':False},MANIFEST,'dash-sdr+dash-audio'),
                ({},MANIFEST.replace('value="16"','value="1"'),'dash-sdr+dash-audio')]:
            result={};restore=instagram_quality.install(result,preferences)
            try:
                with YoutubeDL({'quiet':True,**config.get(('downloader','ytdl'),'raw-options')}) as ydl:
                    info=YoutubeDLDownloader._extract_manifest(Mock(),ydl,'https://www.instagram.com/p/fixture/1.mp4',
                        {'media_id':'1','_ytdl_manifest':'dash','_ytdl_manifest_data':manifest})
                    self.assertEqual(info['format_id'],wanted)
                    self.assertEqual(result['hdr_selected'],wanted.startswith('dash-hdr'))
                    self.assertEqual(list(ydl.cookiejar),[])
            finally:restore()

    def test_hdr_not_guessed_from_codec_and_metadata_no_input_mutation(self):
        self.assertIsNone(instagram_quality.dynamic_range({'vcodec':'hevc','bit_depth':10}))
        post={'video_versions':[{'url':'sdr','width':3840,'height':2160,'is_hdr':False},
                                {'url':'hdr','width':1920,'height':1080,'is_hdr':True}],
              'image_versions2':{'candidates':[{'url':'small','width':100,'height':100},{'url':'large','width':500,'height':500}]}}
        chosen=instagram_quality.prepare_post(post,{})
        self.assertEqual(chosen['video_versions'][-1]['url'],'hdr')
        self.assertEqual(chosen['image_versions2']['candidates'][0]['url'],'large')
        self.assertEqual(post['image_versions2']['candidates'][0]['url'],'small')
        self.assertEqual(instagram_quality.prepare_post(post,{'prefer_hdr':False})['video_versions'][-1]['url'],'sdr')

    def test_selected_indices_and_wrong_type_fall_back_with_notice(self):
        result={}
        self.assertFalse(media_preferences.include({'content':'post_selected','indices':[2]},1,total=3,result=result))
        self.assertTrue(media_preferences.include({'content':'post_selected','indices':[2]},2,total=3,result=result))
        self.assertTrue(media_preferences.include({'content':'stories_selected','indices':[8]},1,total=3,result=result))
        self.assertIn('preference_notice',result)
        self.assertFalse(media_preferences.include({'content':'post_images'},1,video=True,has_images=True))
        self.assertTrue(media_preferences.include({'content':'post_images'},1,video=True,has_images=False,result=result))
        prefs={'content':'post_images','indices':[1]}
        self.assertTrue(media_preferences.include(prefs,1,has_images=True,image_positions=[1,3]))
        self.assertFalse(media_preferences.include(prefs,3,has_images=True,image_positions=[1,3]))
        self.assertFalse(media_preferences.include(prefs,2,video=True,has_images=True,image_positions=[1,3]))
        self.assertTrue(media_preferences.include({'content':'post_images','indices':[2]},3,
            has_images=True,image_positions=[1,3],result=result))

    def test_url_image_index_overrides_saved_story_indices_without_mutation(self):
        saved={'content':'post_images','indices':[1,5],'prefer_hdr':True}
        url='https://www.instagram.com/p/ExamplePost/?img_index=3'
        chosen=media_preferences.effective(saved,url,'post')
        self.assertEqual(chosen['indices'],[3]);self.assertEqual(saved['indices'],[1,5])
        for suffix in ('','?img_index=','?img_index=0','?img_index=-1','?img_index=1001',
                       '?img_index=3&img_index=1','?img_index=wrong','?img_index=３'):
            result={}
            chosen=media_preferences.effective(saved,url.split('?')[0]+suffix,'post',result=result)
            self.assertEqual(chosen['content'],'post_all');self.assertNotIn('indices',chosen)
            self.assertIn('preference_notice',result)
        result={}
        chosen=media_preferences.effective(saved,url,'stories',result=result)
        self.assertEqual(chosen['content'],'post_all');self.assertIn('preference_notice',result)

    def test_legacy_tiktok_selection_is_normalized_to_all(self):
        for content,wanted in [('post_selected','post_all'),('stories_selected','stories_all')]:
            chosen=media_preferences.effective({'content':content,'indices':[2]},
                'https://www.tiktok.com/@example/photo/123','post',platform='tiktok')
            self.assertEqual(chosen['content'],wanted);self.assertNotIn('indices',chosen)
            self.assertTrue(all(media_preferences.include(chosen,n,total=3) for n in (1,2,3)))

    def test_legacy_instagram_story_selection_downloads_all_without_saved_indices(self):
        for content in ('stories_selected','stories_all'):
            saved={'content':content,'indices':[2],'prefer_hdr':True}
            chosen=media_preferences.effective(saved,'https://www.instagram.com/stories/example/123/','stories')
            self.assertEqual(chosen['content'],'stories_all');self.assertNotIn('indices',chosen)
            self.assertEqual(saved['indices'],[2])
            self.assertTrue(all(media_preferences.include(chosen,n,total=3) for n in (1,2,3)))

    def test_numeric_quality_uses_nearest_available_fallback(self):
        from yt_dlp import YoutubeDL
        def fmt(height):return dict(format_id=str(height),url=f'https://example.test/{height}.mp4',
            ext='mp4',vcodec='h264',acodec='aac',width=height*16//9,height=height)
        prefs={'quality':'720','prefer_hdr':False}
        for heights,expected in [([480,720,1080],'720'),([1080,2160],'1080')]:
            with YoutubeDL(dict(quiet=True,format=media_preferences.selector(prefs),
                    format_sort=media_preferences.sort(prefs),format_sort_force=True)) as ydl:
                info=ydl.process_ie_result(dict(id='fixture',title='fixture',formats=[fmt(h) for h in heights]),download=False)
                self.assertEqual(info['format_id'],expected)

    def test_instagram_direct_nearest_quality_and_explicit_image_hdr(self):
        post={'video_versions':[{'url':'4k','width':3840,'height':2160},
                                {'url':'1080','width':1920,'height':1080}],
              'image_versions2':{'candidates':[{'url':'large-sdr','width':2000,'height':2000,'is_hdr':False},
                                              {'url':'small-hdr','width':1000,'height':1000,'dynamic_range':'HDR10'}]}}
        chosen=instagram_quality.prepare_post(post,{'quality':'720'})
        self.assertEqual(chosen['video_versions'][-1]['url'],'1080')
        self.assertEqual(chosen['image_versions2']['candidates'][0]['url'],'small-hdr')
        chosen=instagram_quality.prepare_post(post,{'prefer_hdr':False})
        self.assertEqual(chosen['image_versions2']['candidates'][0]['url'],'large-sdr')

    def test_selected_index_quality_and_hdr_fallback_notices_survive_together(self):
        from gallery_dl import config
        from gallery_dl.downloader.ytdl import YoutubeDLDownloader
        from yt_dlp import YoutubeDL
        prefs=media_preferences.effective({'content':'post_images','quality':'720','prefer_hdr':True},
            'https://www.instagram.com/p/ExamplePost/?img_index=1','post')
        result={}
        self.assertTrue(media_preferences.include(prefs,1,total=1,video=True,has_images=False,result=result))
        index_notice=result['preference_notice']
        restore=instagram_quality.install(result,prefs)
        try:
            with YoutubeDL({'quiet':True,**config.get(('downloader','ytdl'),'raw-options')}) as ydl:
                for _ in range(2):
                    info=YoutubeDLDownloader._extract_manifest(Mock(),ydl,'https://www.instagram.com/p/fixture/1.mp4',
                        {'media_id':'1','_ytdl_manifest':'dash','_ytdl_manifest_data':MANIFEST.replace('value="16"','value="1"')})
                    self.assertEqual(info['height'],1080)
            notices=result['preference_notice'].splitlines()
            self.assertEqual(len(notices),3)
            self.assertIn(index_notice,notices)
            self.assertTrue(any('最接近' in note for note in notices))
            self.assertTrue(any('HDR' in note for note in notices))
        finally:restore()


class DownloadCeilings(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir=_TEST_ROOT);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        patcher=patch.object(c,'DATA',self.root);patcher.start();self.addCleanup(patcher.stop)
        cap=patch.object(download_size.limits,'MAX_DOWNLOAD_BYTES',8);cap.start();self.addCleanup(cap.stop)
        c.initialize()

    def test_gallery_uses_per_url_image_and_falls_back_to_actual_content(self):
        from gallery_dl import job
        from media_bot import instagram
        base='https://www.instagram.com/p/ExamplePost/'
        for index,(url,mode,wanted,notice,content) in enumerate([
                (base+'?img_index=3','post',[3],False,'post_images'),
                (base,'post',[1,2,3],True,'post_images'),
                (base+'?img_index=0','post',[1,2,3],True,'post_images'),
                (base+'?img_index=2','post',[1,3],True,'post_images'),
                (base+'?img_index=9','post',[1,3],True,'post_images'),
                (base+'?img_index=3','stories',[1,2,3],True,'post_images'),
                (base+'?img_index=3','post',[1,2,3],False,'post_all')]):
            captured=[];root=self.root/str(index)
            runner=Mock();runner.pathfmt.realpath=str(root/'fixture.jpg')
            def original(instance,url,data):captured.append(data['num'])
            def run():
                for n in (1,2,3):
                    data={'num':n,'count':3,'extension':'mp4' if n==2 else 'jpg',
                          '_preference_has_images':True,'_preference_image_indices':[1,3]}
                    job.DownloadJob.handle_url(runner,'https://cdn.cdninstagram.com/fixture',data)
                return 0
            payload=dict(platform='instagram',root=str(root),url=url,mode=mode,cookies={},
                         preferences={'content':content,'indices':[1]})
            with patch.object(job.DownloadJob,'handle_url',side_effect=original), \
                    patch.object(job.DownloadJob,'run',side_effect=run), \
                    patch.object(instagram,'install',return_value=lambda:None), \
                    patch.object(instagram,'target',return_value=base), \
                    patch('sys.stdin',io.StringIO(json.dumps(payload))):gallery_runner.main()
            self.assertEqual(captured,wanted)
            result=json.loads((root/'result.json').read_text())
            self.assertEqual(bool(result.get('preference_notice')),notice)

    def test_header_range_total_and_exact_boundary(self):
        self.assertEqual(download_size.declared({'Content-Length':'8'}),8)
        download_size.check(8)
        for headers in ({'Content-Length':'9'},{'Content-Length':'2','Content-Range':'bytes 7-8/9'}):
            with self.assertRaises(download_size.DownloadTooLarge):download_size.declared(headers)
        with self.assertRaises(download_size.DownloadTooLarge):download_size.check(9)

    def test_gallery_unknown_length_stream_stops_before_excess_chunk(self):
        import requests
        response=Mock(status_code=200,headers={})
        response.iter_content.return_value=iter([b'1234',b'5678',b'9'])
        received=[]
        payload=dict(platform='instagram',root=str(self.root/'media'/'capture'),cookies={},url='https://instagram.com/p/fixture/',mode='post')
        def run():
            actual=requests.adapters.HTTPAdapter().send(Mock())
            received.extend(actual.iter_content(4))
            return 0
        with patch('requests.adapters.HTTPAdapter.send',return_value=response),patch('gallery_dl.job.DownloadJob.run',side_effect=run),patch('sys.stdin',io.StringIO(json.dumps(payload))):
            gallery_runner.main()
        result=json.loads((self.root/'media/capture/result.json').read_text())
        self.assertEqual(received,[b'1234',b'5678'])
        self.assertEqual(result['failure'],'too_large');self.assertTrue(result['oversize'])

    def test_gallery_declared_oversize_does_not_read_media(self):
        import requests
        response=Mock(status_code=200,headers={'Content-Length':'9'})
        payload=dict(platform='instagram',root=str(self.root/'media'/'capture'),cookies={},url='https://instagram.com/p/fixture/',mode='post')
        def run():requests.adapters.HTTPAdapter().send(Mock());return 0
        with patch('requests.adapters.HTTPAdapter.send',return_value=response),patch('gallery_dl.job.DownloadJob.run',side_effect=run),patch('sys.stdin',io.StringIO(json.dumps(payload))):gallery_runner.main()
        response.iter_content.assert_not_called();response.close.assert_called_once()
        self.assertTrue(json.loads((self.root/'media/capture/result.json').read_text())['oversize'])

    def test_twitter_header_and_chunk_limits_preserve_partial(self):
        import httpx
        async def go(headers):
            async def handle(request):return httpx.Response(200,headers={'Content-Type':'video/mp4',**headers},content=b'123456789')
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with self.assertRaises(download_size.DownloadTooLarge):
                    await twitter_runner.fetch(client,'https://video.twimg.com/fixture.mp4',self.root/'fixture.mp4',dict(requests=0,media_attempts=0))
        asyncio.run(go({'Content-Length':'9'}))
        self.assertFalse((self.root/'fixture.mp4').exists())

    def test_youtube_rejects_combined_declared_stream_sizes_before_transfer(self):
        def extract(downloader,url,**kwargs):
            downloader.process_info({'requested_formats':[{'filesize':5},{'filesize':4}]})
        with patch.object(yt_runner.YoutubeDL,'extract_info',extract),patch.object(yt_runner.YoutubeDL,'dl') as transfer:
            result=yt_runner.run(dict(platform='youtube',root=str(self.root/'capture'),url='https://www.youtube.com/watch?v=Abc123_X-yZ'))
        transfer.assert_not_called();self.assertTrue(result['oversize'])
        self.assertEqual(result['files'],[])

    def test_youtube_progress_counts_streams_without_merger_directory_sum(self):
        def extract(downloader,url,**kwargs):
            hook=downloader.params['progress_hooks'][0]
            hook(dict(status='downloading',filename='video.mp4',downloaded_bytes=5))
            hook(dict(status='finished',filename='video.mp4',downloaded_bytes=5))
            hook(dict(status='downloading',filename='audio.m4a',downloaded_bytes=4))
        with patch.object(yt_runner.YoutubeDL,'extract_info',extract):
            result=yt_runner.run(dict(platform='youtube',root=str(self.root/'capture'),url='https://www.youtube.com/watch?v=Abc123_X-yZ'))
        self.assertTrue(result['oversize']);self.assertEqual(result['failure'],'too_large')


if __name__=='__main__':unittest.main()
