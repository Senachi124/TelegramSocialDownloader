"""Offline cookie scope and runner isolation; synthetic values only."""
import copy
from http.cookiejar import CookieJar
from pathlib import Path
import tempfile
import unittest
import os
from unittest.mock import patch
from urllib.request import Request

_TEST_ROOT = Path(__file__).resolve().parents[1] / '.test-work'
_TEST_ROOT.mkdir(exist_ok=True)
_BOOTSTRAP = tempfile.TemporaryDirectory(prefix='download-import-', dir=_TEST_ROOT)
with patch.dict(os.environ, {'MEDIA_BOT_DATA': _BOOTSTRAP.name}):
    from media_bot import core as c, youtube_cookies, yt_runner

COOKIE='.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\tsynthetic-session\n'


class YouTubeCookies(unittest.TestCase):
    def test_parse_excludes_other_platforms_and_jar_enforces_scope(self):
        data=youtube_cookies.parse(COOKIE+'.tiktok.com\tTRUE\t/\tTRUE\t4102444800\tsessionid\tother-platform\n'
            +'www.youtube.com\tFALSE\t/watch\tTRUE\t4102444800\tHOST\thost-only\n')
        jar=CookieJar();youtube_cookies.load_into(jar,data)
        def header(url):
            request=Request(url);jar.add_cookie_header(request)
            return request.get_header('Cookie') or ''
        self.assertIn('SAPISID=',header('https://www.youtube.com/watch'))
        self.assertIn('HOST=',header('https://www.youtube.com/watch'))
        self.assertNotIn('HOST=',header('https://sub.www.youtube.com/watch'))
        self.assertNotIn('HOST=',header('https://www.youtube.com/other'))
        for url in ('http://www.youtube.com/watch','https://google.com/',
                    'https://youtube.com.evil.test/','https://www.tiktok.com/',
                    'https://r.googlevideo.com/video'):
            self.assertEqual(header(url),'')
        self.assertNotIn('other-platform',str(data))

    def test_invalid_or_expired_payload_does_not_partially_load(self):
        original=youtube_cookies.parse(COOKIE)
        for update in ({'expires':1},{'domain':'.google.com'},
                       {'value':'bad\r\nheader'},{'secure':'FALSE'}):
            data=copy.deepcopy(original);data['items'][0].update(update)
            jar=CookieJar()
            with self.assertRaises(ValueError):youtube_cookies.load_into(jar,data)
            self.assertEqual(list(jar),[])
        for text in (COOKIE.replace('SAPISID','PREF'),COOKIE.replace('4102444800','1'),
                     COOKIE+COOKIE.replace('synthetic-session','different')):
            with self.assertRaises(ValueError):youtube_cookies.parse(text)

    def test_runner_uses_explicit_youtube_payload_or_anonymous(self):
        with tempfile.TemporaryDirectory(dir=_TEST_ROOT) as directory, patch.object(c,'DATA',Path(directory)):
            c.initialize()
            for cookies,wanted in (({},[]),(youtube_cookies.parse(COOKIE),['SAPISID'])):
                def extract(downloader,url,**kwargs):
                    self.assertEqual([cookie.name for cookie in downloader.cookiejar],wanted)
                    self.assertFalse(downloader.params['cookiefile'])
                    return None
                with patch.object(yt_runner.YoutubeDL,'extract_info',extract):
                    result=yt_runner.run({'platform':'youtube','root':directory,
                        'url':'https://youtu.be/Abc123_X-yZ','cookies':cookies})
                self.assertEqual(result['cookie_used'],bool(cookies))
                self.assertNotIn('synthetic-session',(Path(directory)/'result.json').read_text())
