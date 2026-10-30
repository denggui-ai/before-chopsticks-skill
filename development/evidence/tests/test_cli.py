import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

CLI = Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts/state.py'


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, data=None):
        result = subprocess.run([sys.executable, str(CLI), '--project', str(self.project), *args],
                                input=json.dumps(data, ensure_ascii=False) if data is not None else None,
                                capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        return result.returncode, json.loads(result.stdout)

    def test_import_edit_select_and_recover_across_processes(self):
        code, result = self.run_cli('init', '--name', '晚饭')
        self.assertEqual(code, 0)
        code, result = self.run_cli('apply', '--input', '-', data={'action': 'create_work', 'subject': '面条'})
        work = result['result']['id']
        source = self.root / 'input.png'
        Image.new('RGB', (10, 8), (50, 40, 30)).save(source)
        _, result = self.run_cli('apply', '--input', '-', data={'action': 'import_image', 'work_id': work, 'path': str(source)})
        v0 = result['result']['version']['id']
        code, result = self.run_cli('apply', '--input', '-', data={'action': 'local_edit', 'version_id': v0,
                      'operations': [{'type': 'brightness', 'params': {'factor': 1.1}}]})
        self.assertEqual(code, 0, result)
        v1 = result['result']['version']['id']
        self.run_cli('apply', '--input', '-', data={'action': 'select_version', 'version_id': v0})
        code, result = self.run_cli('recover')
        state = result['result']['project']['works'][work]
        self.assertEqual(state['latest_version_id'], v1)
        self.assertEqual(state['selected_version_id'], v0)
        self.assertEqual(state['edit_target_version_id'], v0)
        self.assertFalse(result['result']['attempts'])

    def test_bad_action_and_bad_json_are_structured_errors(self):
        self.run_cli('init')
        code, result = self.run_cli('apply', '--input', '-', data={'action': '__dict__'})
        self.assertNotEqual(code, 0)
        self.assertFalse(result['ok'])
        self.assertIn('action', result['error'])
        raw = subprocess.run([sys.executable, str(CLI), '--project', str(self.project), 'apply', '--input', '-'],
                             input='{', capture_output=True, text=True)
        self.assertNotEqual(raw.returncode, 0)
        self.assertFalse(json.loads(raw.stdout)['ok'])


if __name__ == '__main__':
    unittest.main()
