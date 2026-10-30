"""Small independent native bridge counterexamples; offline fixtures only."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))
from bc_core.codex_bridge import CodexBridge
from bc_core.engine import ProjectEngine


class CodexBridgeReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = ProjectEngine(self.root / 'project')
        self.engine.initialize('independent bridge review offline fixture')
        self.work = self.engine.create_work('soup')['id']
        self.auth = self.engine.authorize(
            [self.work], ['generate'], 2, retry_budget=0,
            paid_channel_scope=['codex-native-imagegen-v1'],
            evidence={'fixture': 'two offline dispatches'},
        )['id']
        self.bridge = CodexBridge(self.root / 'project', interface_evidence={'fixture': 'offline interface'})
        self.output = self.root / 'output.png'
        Image.new('RGB', (4, 4), (30, 60, 90)).save(self.output)
        self.tool_return = {
            'tool_name': 'image_gen.imagegen', 'kind': 'native_tool_return', 'status': 'completed',
            'metadata': {'fixture': 'synthetic tool return, no native call'},
        }

    def tearDown(self):
        self.temp.cleanup()

    def dispatched(self, prompt):
        attempt = self.bridge.prepare(self.work, self.auth, prompt)['attempt']['id']
        return attempt, self.bridge.dispatch(attempt)['call']

    def test_consumer_rejects_valid_journal_pair_placed_under_another_attempt(self):
        first, first_call = self.dispatched('Generate soup with a red napkin.')
        second, _ = self.dispatched('Generate soup with a blue napkin.')
        self.bridge.capture_return(first, first_call['invocation_id'], [str(self.output)], self.tool_return)
        journal_root = self.root / 'project/host/codex-native'
        # Fault injection: intact, digest-valid records restored to the wrong
        # Attempt directory. No evidence content or checksum is forged.
        for filename in ('call.json', 'receipt.json'):
            shutil.copyfile(journal_root / first / filename, journal_root / second / filename)
        try:
            self.bridge.consume_receipt(second)
        except ValueError:
            pass
        attempt = self.engine.store.read_record('attempts', second)
        self.assertNotEqual(attempt['state'], 'succeeded',
                            'Matching call/receipt records still must belong to the Attempt being consumed')
        self.assertFalse(attempt['outputs'])

    def test_completed_return_with_missing_output_preserves_known_submission_on_recovery(self):
        attempt_id, call = self.dispatched('Generate one soup image.')
        missing = self.root / 'native-output-that-is-no-longer-readable.png'
        try:
            self.bridge.capture_return(attempt_id, call['invocation_id'], [str(missing)], self.tool_return)
        except (OSError, ValueError):
            pass
        self.bridge.recover()
        attempt = self.engine.store.read_record('attempts', attempt_id)
        self.assertEqual(attempt['submission'], 'submitted',
                         'A valid completed native return proves submission even if its image cannot be saved')
        self.assertNotEqual(attempt['state'], 'succeeded')
        self.assertFalse(attempt['outputs'])


if __name__ == '__main__':
    unittest.main()
