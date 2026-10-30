"""Narrow tests for the newly persisted raw-return boundary; offline only."""
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))
from bc_core.codex_bridge import ADAPTER_ID, CodexBridge


class CodexReturnBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = CodexBridge(self.root / 'project', {'fixture': 'offline'})
        self.bridge.engine.initialize()
        work = self.bridge.engine.create_work('soup')['id']
        auth = self.bridge.engine.authorize([work], ['generate'], 1, paid_channel_scope=[ADAPTER_ID],
                                           evidence={'fixture': 'offline authorization'})['id']
        prepared = self.bridge.prepare(work, auth, 'One soup image')
        self.attempt = prepared['attempt']['id']
        self.call = self.bridge.dispatch(self.attempt)['call']
        self.output = self.root / 'output.png'
        Image.new('RGB', (4, 4), (10, 20, 30)).save(self.output)
        self.observed = {'tool_name': 'image_gen.imagegen', 'kind': 'native_tool_return',
                         'status': 'completed', 'metadata': {'fixture': 'not a real tool call'}}

    def tearDown(self):
        self.temp.cleanup()

    def test_crash_after_raw_return_keeps_submission_without_inventing_an_output(self):
        def crash(stage):
            if stage == 'native_return_saved':
                raise RuntimeError('crash before output copy')
        self.bridge.engine.store.failpoint = crash
        with self.assertRaises(RuntimeError):
            self.bridge.capture_return(self.attempt, self.call['invocation_id'], [str(self.output)], self.observed)
        result = CodexBridge(self.root / 'project').recover()
        attempt = next(a for a in result['attempts'] if a['id'] == self.attempt)
        self.assertEqual((attempt['state'], attempt['submission']), ('unknown', 'submitted'))
        self.assertFalse(attempt['outputs'])
        self.assertFalse(result['project']['versions'])
        self.assertEqual(len(result['attempts']), 1)

    def test_unexpected_multiple_outputs_are_retained_for_review_not_silently_truncated(self):
        receipt = self.bridge.capture_return(self.attempt, self.call['invocation_id'],
                                             [str(self.output), str(self.output)], self.observed)
        self.assertEqual(receipt['status'], 'requires_review')
        self.assertEqual(len(receipt['outputs']), 2)
        result = self.bridge.recover()
        attempt = next(a for a in result['attempts'] if a['id'] == self.attempt)
        self.assertEqual((attempt['state'], attempt['submission']), ('unknown', 'submitted'))
        self.assertFalse(result['project']['versions'])


if __name__ == '__main__':
    unittest.main()
