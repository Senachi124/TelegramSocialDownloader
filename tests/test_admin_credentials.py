import os
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

with TemporaryDirectory() as bootstrap, patch.dict(os.environ, {'MEDIA_BOT_DATA': bootstrap}):
    from media_bot import accounts, features
    from media_bot.admin import views


class CredentialViewTests(unittest.TestCase):
    def test_instagram_account_displays_the_reported_upstream_limit(self):
        data = {}
        accounts.update_primary(data, 'instagram', {'fixture': 'value'})
        item, = accounts.public_summary(data)['instagram']['items']
        report = {'platform': 'instagram', 'id': item['id'], 'revision': item['revision']}
        for reason, label in [('http_429', 'Instagram returned HTTP 429'),
                              ('upstream_restriction', 'in its response body')]:
            accounts.report(data, dict(report, outcome=reason))
            saved, = accounts.public_summary(data)['instagram']['items']
            self.assertIn(label, views._account_record('instagram', '', saved, True))
        with self.assertRaises(ValueError):
            accounts.report(data, dict(report, platform='twitter', outcome='http_429'))

    def test_pool_editing_and_download_check_states(self):
        data = accounts.public_summary({})
        data['instagram']['items'] = [dict(id='fixture', label='Photo <team>', enabled=True,
                                            revision=2, last_result='auth_required')]
        latest = {'state': 'queued', 'account_label': '', 'detail': '', 'requested': 0}
        flags = {f'download_{p}': p != 'twitter' for p in accounts.PLATFORMS}
        with (patch.object(views.c, 'rpc', return_value=data),
              patch.object(views.c, 'get_setting', return_value=True),
              patch.object(features, 'snapshot', return_value=flags),
              patch.object(views.pool_checks, 'latest', side_effect=lambda p: latest if p == 'youtube' else None)):
            page = views.pools('', selected_platform='instagram')
        self.assertIn('id="instagram" open', page)
        self.assertNotIn('id="youtube" open', page)
        self.assertIn('Photo &lt;team&gt;', page)
        self.assertIn('Sign in again and replace cookies', page)
        self.assertIn('Choose an enabled account as primary', page)
        self.assertIn('features#download_twitter', page)
        self.assertNotIn('pool-check/twitter"', page)
        self.assertIn('pool-check/youtube"', page)  # An old queued check may be replaced.
        self.assertIn('multipart/form-data', page)
        self.assertIn('name="youtube_cookie_file"', page)

    def test_key_actions_match_enabled_and_primary_state(self):
        keys = [{'id': 'one', 'label': 'Primary', 'primary': True, 'enabled': True},
                {'id': 'two', 'label': 'Spare', 'primary': False, 'enabled': False}]
        with patch.object(views.c, 'rpc', side_effect=[{'credentials_set': True}, {'keys': keys}]) as rpc:
            page = views.keys('')
        self.assertEqual([args[0][1] for args in rpc.call_args_list], ['/local-api/status', '/translation/status'])
        self.assertIn('aria-label="Disable key Primary"', page)
        self.assertNotIn('aria-label="Delete key Primary"', page)
        self.assertIn('aria-label="Delete key Spare"', page)
        self.assertIn('aria-label="Enable key Spare"', page)
        self.assertIn('Replace credentials', page)
