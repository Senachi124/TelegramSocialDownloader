import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

with tempfile.TemporaryDirectory() as bootstrap, patch.dict(os.environ, {'MEDIA_BOT_DATA':bootstrap}):
    from media_bot import push, push_socket


class PushTests(unittest.TestCase):
    def test_bundled_takaneko_coexists_with_other_providers(self):
        other = '{"feed":{"uid":801,"control":"/run/example/control.sock"}}'
        with patch.dict(os.environ, {'MEDIA_BOT_PUSH_PROVIDERS': other, 'MEDIA_BOT_TAKANEKO_UID': '802'}):
            self.assertEqual(set(push.providers()), {'feed', 'takaneko'})
            self.assertEqual(push.providers()['takaneko']['uid'], 802)
            with patch.dict(os.environ, {'MEDIA_BOT_TAKANEKO_UID': '801'}), self.assertRaises(ValueError):
                push.providers()

    def test_rechecks_user_and_subscription_before_dispatch(self):
        with patch.object(push.access,'allowed',return_value=False), patch.object(push, 'control') as provider:
            self.assertEqual(push.dispatch({}, {'op':'send','user_id':1,'kind':'text','text':'Hi'},None)['code'],403)
            provider.assert_not_called()
        with patch.object(push.access,'allowed',return_value=True), patch.object(push.core,'get_setting',return_value=True), patch.object(push,'control',return_value={'enabled':False}):
            self.assertEqual(push.dispatch({}, {'op':'send','user_id':1,'kind':'text','text':'Hi'},None)['code'],403)

    def test_review_authorization_reads_only_bot_access_for_takaneko(self):
        review = {'op': 'review_allowed', 'user_id': 123}
        with patch.object(push, 'control') as provider, patch.object(push.access, 'allowed') as allowed:
            for slug, fd, req in [('feed', None, review), (None, None, review), ('takaneko', 5, review),
                                  ('takaneko', None, dict(review, extra='field'))]:
                self.assertEqual(push.dispatch({}, req, fd, slug=slug), {'ok': False, 'code': 403})
            allowed.assert_not_called()
            for permitted, reply in [(True, {'ok': True}), (False, {'ok': False, 'code': 403})]:
                allowed.return_value = permitted
                self.assertEqual(push.dispatch({}, review, None, slug='takaneko'), reply)
                allowed.assert_called_with(123)
            provider.assert_not_called()

    def test_delivery_returns_only_message_id(self):
        received=[]
        def send(method,data):
            received.append((method,data))
            return {'message_id':11,'text':'personal'}
        with patch.object(push.access,'allowed',return_value=True), patch.object(push.core,'get_setting',return_value=True), patch.object(push,'control',return_value={'enabled':True}):
            result=push.dispatch({}, {'op':'send','user_id':5,'kind':'text','text':'Hello'},None,send=send)
            self.assertEqual(result,{'ok':True,'message_id':11})
            self.assertEqual(received[0][1]['chat_id'],5)
            self.assertEqual(push.dispatch({}, {'op':'send','user_id':5,'kind':'photo','name':'/tmp/photo'},None,send=send)['code'],400)

    def test_local_file_descriptor_and_peer_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp)/'producer.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
                server.bind(path)
                server.listen(1)
                def handle(uid, request, fd):
                    return {'data':os.read(fd,10).decode(),'sender':uid,'value':request['value']}
                permits=threading.Semaphore(0)
                def listen():
                    client,_=server.accept()
                    push_socket._serve_one(client,{os.getuid()},handle,permits)
                worker=threading.Thread(target=listen)
                worker.start()
                f=Path(tmp)/'private'
                f.write_text('hello')
                with f.open('rb') as data:
                    response=push_socket.exchange(path,os.getuid(),{'value':'ok'},data)
                worker.join(2)
                self.assertEqual(response,{'sender':os.getuid(),'value':'ok','data':'hello'})
                with self.assertRaises(PermissionError):
                    push_socket.exchange(path,os.getuid()+1,{'value':'bad'})

    def test_web_post_rejects_unapproved_subscription_and_bad_value(self):
        with patch.object(push,'providers',return_value={'feed':{}}), patch.object(push,'control') as provider, patch.object(push.access,'allowed',return_value=False):
            for value in ('1','yes'):
                with self.subTest(value=value),self.assertRaises(ValueError):
                    push.web_post({'user_id':'7','provider':'feed','enabled':value})
            provider.assert_not_called()
            push.web_post({'user_id':'7','provider':'feed','enabled':'0'})
            provider.assert_called_once_with({},'set',user_id=7,enabled=False)
