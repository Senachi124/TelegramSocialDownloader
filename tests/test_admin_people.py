import os
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

with TemporaryDirectory() as bootstrap, patch.dict(os.environ, {'MEDIA_BOT_DATA': bootstrap}):
    from media_bot import core, push
    from media_bot.admin import views


class PeoplePanelTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.execute('CREATE TABLE bot_users(id INTEGER PRIMARY KEY, name TEXT, username TEXT, state TEXT)')
        db.executemany('INSERT INTO bot_users VALUES (?, ?, ?, ?)', [
            (101, 'Owner', '', 'owner'), (102, 'Approved', '', 'approved'),
            (103, 'New <friend>', 'newbie', 'pending'), (104, 'Former', '', 'revoked')])
        self.enterContext(patch.object(core, 'db', return_value=db))
        self.enterContext(patch.object(core, 'get_setting', side_effect=lambda key, default: default))
        self.enterContext(patch.object(core, 'BASE', '/bot/'))

    def test_access_actions_follow_user_state_and_keep_page(self):
        body = views.users('', 0)
        self.assertLess(body.index('New &lt;friend&gt;'), body.index('Owner'))
        self.assertIn('Approve · New &lt;friend&gt; (103)', body)
        self.assertIn('Reject · New &lt;friend&gt; (103)', body)
        self.assertIn('Revoke access · Approved (102)', body)
        self.assertIn('Approve · Former (104)', body)
        self.assertNotIn('Reject · Approved', body)
        self.assertNotIn('Revoke access · Owner', body)
        self.assertIn('name="offset" value="1"', views.users('', 1))

    def test_subscribe_only_eligible_and_unsubscribe_after_losing_access(self):
        self.enterContext(patch.object(push, 'providers', return_value={'takaneko': {}}))
        reply = {'users': [101, 104, 105], 'capabilities': ['set_session']}
        self.enterContext(patch.object(push, 'control', return_value=reply))
        body = push.web_panel('')
        self.assertIn('Unsubscribe Owner (101)', body)
        self.assertIn('Subscribe Approved (102)', body)
        self.assertNotIn('New &lt;friend&gt;', body)
        self.assertIn('Subscribed · access revoked', body)
        self.assertIn('Unsubscribe Former (104)', body)
        self.assertIn('Unsubscribe Unregistered user (105)', body)
        self.assertIn('href="/bot/features#takaneko"', body)

    def test_unreachable_provider_does_not_show_actionable_recipients(self):
        self.enterContext(patch.object(push, 'providers', return_value={'news': {}}))
        self.enterContext(patch.object(push, 'control', side_effect=OSError))
        body = push.web_panel('')
        self.assertIn('Subscriber list unavailable', body)
        self.assertNotIn('Subscribe Approved', body)
