import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('receiver',Path(__file__).resolve().parents[1]/'deploy/actions_receiver.py')
receiver=importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class ReceiverTests(unittest.TestCase):
    def test_only_new_workflow_requests_are_selected(self):
        request=dict(id=9,task='telegram-upgrade',environment='telegram-app',
                     creator={'login':'github-actions[bot]'},created_at='2025-01-01T00:00:00Z')
        items=[dict(request,task='deploy'),dict(request,creator={'login':'other'}),request]
        self.assertIs(receiver.select(items,8,0),request)
        self.assertIsNone(receiver.select(items,9,0))
        self.assertIsNone(receiver.select(items,0,2_000_000_000))

    def test_untrusted_workflow_is_rejected_before_downloading(self):
        req=dict(sha='a'*40,payload={'tag':'v2.0.0','run_id':'19'})
        run={'path':'.github/workflows/other.yml'}
        with self.assertRaisesRegex(ValueError,'identity'), patch.object(receiver,'fetch',side_effect=AssertionError):
            receiver.deploy({'repository':'example/app','branch':'main'}, req,lambda _:run)

    def test_status_contains_only_public_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(receiver,'STATE',Path(tmp)):
            receiver.write_status(1,'a'*40,'success')
            path=Path(tmp)/'status.json'
            self.assertEqual(receiver.json.loads(path.read_text()),dict(deployment=1,sha='a'*40,state='success'))
            self.assertEqual(path.stat().st_mode & 0o777,0o644)
