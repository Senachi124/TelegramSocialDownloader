from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / 'deploy'


class InstallationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('bash'), 'requires bash')
    def test_shell_syntax(self):
        subprocess.run(['bash', '-n', str(DEPLOY / 'install.sh')], check=True)

    def test_unit_components_use_the_same_release_and_environment(self):
        for component in ('worker', 'web', 'broker', 'push'):
            unit = DEPLOY / 'systemd' / f'telegram-social-{component}.service'
            text = unit.read_text()
            self.assertIn('EnvironmentFile=/etc/telegram-social-downloader/.env', text)
            self.assertIn('WorkingDirectory=/opt/telegram-social-downloader/current', text)
            self.assertIn(f'/.venv/bin/python -m media_bot {component}\n', text)
            self.assertIn('NoNewPrivileges=true', text)
            self.assertIn('RequiresMountsFor=/var/lib/telegram-social-downloader', text)
