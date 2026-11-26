"""First-party YouTube cookie renewal, using only synthetic cookies and a local broker."""
import copy
from email.message import Message
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from media_bot import accounts, youtube_cookies as yc


URL = 'https://www.youtube.com/watch?v=fixture'
EXPORT = ('#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t4102444800\tSAPISID\told-synthetic\n'
          'www.youtube.com\tFALSE\t/\tTRUE\t4102444800\tPREF\tfixture-pref\n'
          'music.youtube.com\tFALSE\t/\tTRUE\t4102444800\tKEEP\tfixture-other-host\n')
ROTATE = 'SAPISID=new-synthetic; Domain=youtube.com; Path=/; Secure; HttpOnly; Max-Age=36000'
VISITOR = 'VISITOR_INFO1_LIVE=partition-fixture; Domain=.youtube.com; Path=/; Secure; HttpOnly; Partitioned'


def headers(*cookies):
    result = Message()
    for cookie in cookies:
        result['Set-Cookie'] = cookie
    return result


class FirstPartyCookieTests(unittest.TestCase):
    def setUp(self):
        self.original = yc.parse(EXPORT)
        self.baseline = copy.deepcopy(self.original)

    def test_rotate_add_and_delete_only_actual_response_cookies(self):
        result = yc.apply_response(self.original, URL, headers(
            ROTATE,
            'NEW=synthetic; Path=/; Secure; HttpOnly',
            'PREF=; Path=/; Max-Age=0',
            'FOREIGN=ignored; Domain=google.com; Path=/; Secure'))
        by_name = {i['name']: i for i in result['items']}
        self.assertEqual(set(by_name), {'SAPISID', 'KEEP', 'NEW'})
        rotated = by_name['SAPISID']
        self.assertEqual((rotated['value'], rotated['domain'], rotated['path']), ('new-synthetic', '.youtube.com', '/'))
        self.assertTrue(rotated['secure'] and rotated['include_subdomains'] and rotated['http_only'])
        self.assertGreater(rotated['expires'], 0)
        host = by_name['NEW']
        self.assertEqual((host['domain'], host['include_subdomains'], host['expires']), ('www.youtube.com', False, 0))
        self.assertEqual(by_name['KEEP'], self.original['items'][2])
        self.assertEqual(self.original, self.baseline)

    def test_untrusted_urls_headers_and_redirect_destinations(self):
        for url in ('http://www.youtube.com/', 'https://www.youtube.com.evil.test/',
                    'https://sub.www.youtube.com/', 'https://accounts.google.com/',
                    'https://user@www.youtube.com/', 'https://www.youtube.com:444/',
                    'https://www.youtube.com/\r\n', 'https://evil.test\\@www.youtube.com/', '%%%'):
            with self.subTest(url=url):
                self.assertEqual(yc.apply_response(self.original, url, headers(ROTATE)), self.original)
        other = headers()
        other['Location'] = 'https://www.youtube.com/'
        other['Cookie'] = 'SAPISID=from-request'
        other['Set-Cookie2'] = ROTATE
        result = yc.apply_response(self.original, URL, other)
        self.assertEqual(result, self.original)
        result['items'][0]['value'] = 'mutated-copy'
        self.assertEqual(self.original, self.baseline)

    def test_partitioned_visitors_do_not_replace_or_expire_unpartitioned_cookies(self):
        original = yc.parse(EXPORT + '.youtube.com\tTRUE\t/\tTRUE\t4102444800\tVISITOR_INFO1_LIVE\tunpartitioned\n')
        other = VISITOR.replace('VISITOR_INFO1_LIVE', 'VISITOR_PRIVACY_METADATA')
        deletion = VISITOR.replace('=partition-fixture;', '=; Max-Age=0;')
        for only_visitor in (VISITOR, deletion):
            with self.subTest(only_visitor=only_visitor):
                result = yc.apply_response(original, URL, headers(only_visitor, other))
                self.assertEqual(result, original)
                result = yc.apply_response(original, URL, headers(only_visitor, ROTATE, other))
                self.assertEqual({row['name']:row['value'] for row in result['items']}, {
                    'VISITOR_INFO1_LIVE': 'unpartitioned', 'SAPISID': 'new-synthetic',
                    'PREF': 'fixture-pref', 'KEEP': 'fixture-other-host',
                })

    def test_unsafe_changes_are_rejected_as_a_unit(self):
        for bad in (
            ROTATE.replace('Secure; ', ''), ROTATE.replace('HttpOnly; ', ''),
            ROTATE.replace('Max-Age=36000', 'Max-Age=0'),
            ROTATE.replace('; Max-Age=36000', ''),
            ROTATE.replace('Domain=youtube.com', 'Domain=www.youtube.com'),
            ROTATE + '; Domain=evil.test',
            'PREF=new; Domain=.youtube.com; Path=/; Secure',
            '__Host-TEST=wrong; Domain=youtube.com; Path=/; Secure',
            'BAD=partitioned; Path=/; Secure; Partitioned',
            ROTATE + '; Partitioned', VISITOR.replace('Secure; ', ''),
            VISITOR.replace('; Partitioned', '; Partitioned=1'), VISITOR + '; PARTITIONED',
            VISITOR.replace('=partition-fixture', ''), VISITOR + '; SameParty',
            VISITOR + ', SAPISID=ambiguous',
            'BAD=x, other=y; Secure', 'BAD=injected\nheader',
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                yc.apply_response(self.original, URL, headers(VISITOR, 'OPTIONAL=foo; Secure', bad))
        self.assertEqual(self.original, self.baseline)

    def test_sibling_domain_expiry_cannot_remove_an_existing_cookie(self):
        original = yc.parse(EXPORT.replace('music.youtube.com\tFALSE', '.music.youtube.com\tTRUE'))
        with self.assertRaises(ValueError):
            yc.apply_response(original, URL, headers('KEEP=; Domain=music.youtube.com; Path=/; Max-Age=0'))
        self.assertEqual(original['items'][2]['value'], 'fixture-other-host')

    def test_broker_revalidates_scope_flags_and_auth_continuity(self):
        data = {}
        accounts.update_primary(data, 'youtube', self.original)
        account = accounts.pool(data, 'youtube')['items'][0]
        row = yc.refresh_snapshot(data, {'id': account['id']})
        for field, value, i in (('secure', False, 0), ('http_only', False, 0),
                                ('path', '/watch', 0), ('value', 'tampered-sibling', 2)):
            candidate = copy.deepcopy(row['cookies'])
            candidate['items'][i][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                yc.apply_refresh(data, {**row, 'cookies': candidate})
        auxiliary = {'version': 1, 'items': [self.original['items'][1]]}
        with self.assertRaises(ValueError):
            yc.apply_refresh(data, {**row, 'cookies': auxiliary})
        with self.assertRaises(ValueError):
            yc.apply_refresh(data, {**row, 'revision': True})
        self.assertEqual(row['cookies'], self.baseline)
        self.assertEqual(account['revision'], 1)


class BrokerRefreshTests(unittest.TestCase):
    def test_serialized_broker_writes_only_changed_current_account(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'MEDIA_BOT_DATA': directory, 'MEDIA_BOT_SECRET_DIR': str(Path(directory)/'private')}):
            from media_bot import broker, core
            key = 'fixture-account'
            data = {}
            accounts.update_primary(data, 'youtube', yc.parse(EXPORT))
            pool = accounts.pool(data, 'youtube')
            item = pool['items'][0]
            pool['primary'] = item['id'] = key
            item['last_result_at'] = 123
            item['last_result'] = 'success'
            broker.save(data)
            sock = str(Path(directory)/'broker.sock')
            with socketserver.UnixStreamServer(sock, broker.Handler) as server:
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                thread.start()
                try:
                    def rpc(path, payload):
                        return core.rpc(sock, path, payload)
                    def snapshot():
                        return rpc('/youtube-refresh/snapshot', {'id': key})
                    def submit(row):
                        return rpc('/youtube-refresh/result', row)
                    with patch.object(broker, 'save', wraps=broker.save) as save:
                        row = snapshot()
                        self.assertEqual(submit(row), {'status': 'unchanged', 'revision': 1})
                        save.assert_not_called()
                        row['cookies'] = yc.apply_response(row['cookies'], URL, headers(ROTATE))
                        self.assertEqual(submit(row), {'status': 'updated', 'revision': 2})
                        self.assertEqual(submit(row), {'status': 'superseded'})
                        self.assertEqual(save.call_count, 1)
                        latest = snapshot()
                        latest['cookies']['items'].reverse()
                        self.assertEqual(submit(latest), {'status': 'unchanged', 'revision': 2})
                        self.assertEqual(save.call_count, 1)
                    current = accounts.pool(broker.load(), 'youtube')['items'][0]
                    self.assertEqual((current['last_result'], current['last_result_at']), ('success', 123))
                    def edit(action, **kw):
                        return rpc('/accounts', {'platform': 'youtube', 'id': key, 'action': action, **kw})
                    edit('disable')
                    self.assertEqual(snapshot(), {})
                    self.assertEqual(submit(latest), {'status': 'superseded'})
                    edit('enable')
                    self.assertEqual(submit(latest), {'status': 'superseded'})
                    latest = snapshot()
                    edit('save', label='fixture', cookie=EXPORT)
                    self.assertEqual(submit(latest), {'status': 'superseded'})
                    latest = snapshot()
                    rpc('/report', {'platform': 'youtube', 'id': key, 'revision':latest['revision'], 'outcome':'auth_required'})
                    self.assertEqual(snapshot(), {})
                    self.assertEqual(submit(latest), {'status': 'superseded'})
                    edit('disable')
                    edit('delete')
                    self.assertEqual(snapshot(), {})
                    self.assertEqual(submit(latest), {'status': 'superseded'})
                finally:
                    server.shutdown()
                    thread.join(2)
