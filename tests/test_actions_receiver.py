import hashlib
import importlib.util
import io
import zipfile
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

    def test_trial_requires_exact_manual_dispatch_and_the_authenticated_ci_artifact(self):
        sha = 'b'*40
        host = {'repository': 'demo/service', 'branch': 'main'}
        req = {'sha': sha, 'payload': {'trial': True, 'run_id': '30'}}
        run = {'path': '.github/workflows/deploy.yml', 'head_branch': 'main',
               'head_sha': sha, 'display_title': 'Trial ' + sha, 'event': 'workflow_dispatch', 'status': 'in_progress',
               'head_repository': {'full_name': host['repository']}}
        ci = dict(run, id=24, path='.github/workflows/release.yml', event='push', conclusion='success')
        name = 'telegram_social_downloader-2.0.0.dev2-py3-none-any.whl'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr(name, b'fixture wheel')
        data = buffer.getvalue()
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        artifact = {'name': 'distribution', 'expired': False, 'digest': digest}
        responses = {'actions/runs/30': run,
                     'actions/workflows/release.yml/runs?event=push&status=success&head_sha=' + sha: {'workflow_runs': [ci]},
                     'actions/runs/24/artifacts': {'artifacts': [artifact]}}
        with tempfile.TemporaryDirectory() as root, patch.object(receiver, 'STATE', Path(root)), patch.object(
                receiver, 'staged_distribution', return_value=data), patch.object(receiver.subprocess, 'run') as install:
            receiver.deploy(host, req, responses.__getitem__)
            args, = install.call_args.args
            self.assertEqual(args[-4:], ['--trial-version', '2.0.0.dev2', '--sha', sha])
            install.reset_mock()
            for record, key, wrong in ((run, 'head_sha', 'a'*40), (run, 'event', 'workflow_run'),
                                       (run, 'display_title', 'Deploy to production'), (ci, 'head_branch', 'other'), (ci, 'conclusion', 'failure'),
                                       (artifact, 'digest', 'wrong'), (artifact, 'expired', True)):
                with self.subTest(key=key), patch.dict(record, {key: wrong}), self.assertRaises(ValueError):
                    receiver.deploy(host, req, responses.__getitem__)
            install.assert_not_called()

    def test_a_manual_release_must_name_the_stable_tag_to_allow_return_from_trial(self):
        tag = 'v1.0.0'
        request = {'sha': 'a'*40, 'payload': {'tag': tag, 'run_id':'32'}}
        host = {'repository':'demo/app','branch':'main'}
        run = {'path': '.github/workflows/deploy.yml', 'event':'workflow_dispatch', 'head_branch':'main',
               'status':'in_progress', 'head_repository':{'full_name':'demo/app'}}
        for title, allow in [('Release v1.0.0',True), ('Release v9.0.0',False)]:
            with self.subTest(title=title), tempfile.TemporaryDirectory() as root, patch.object(
                    receiver, 'STATE', Path(root)), patch.object(receiver, 'release_wheel',
                    return_value=('fixture.whl',b'test',['--tag',tag])), patch.object(receiver.subprocess, 'run') as install:
                receiver.deploy(host, request, lambda path: dict(run, display_title=title))
                args, = install.call_args.args
                self.assertEqual('--allow-trial-rollback' in args, allow)

    def test_status_contains_only_public_receipt(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(receiver,'STATE',Path(tmp)):
            receiver.write_status(1,'a'*40,'success')
            path=Path(tmp)/'status.json'
            self.assertEqual(receiver.json.loads(path.read_text()),dict(deployment=1,sha='a'*40,state='success'))
            self.assertEqual(path.stat().st_mode & 0o777,0o644)
