"""Independent P2 bridge contract tests; all images/receipts are offline fixtures.

These tests never invoke image_gen, view_image, query, or a network service.
Fixture receipt metadata is deliberately labelled so it cannot be confused with
the separate two-call native acceptance run.
"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))

from bc_core.codex_bridge import CodexBridge
from bc_core.engine import ProjectEngine
from bc_core.storage import sha256_file


ADAPTER_ID = 'codex-native-imagegen-v1'
INTERFACE_FIXTURE = {
    'kind': 'offline_contract_fixture',
    'source': 'independent P2 bridge regression; not live interface evidence',
    'tool_name': 'image_gen.imagegen',
}
RETURN_FIXTURE = {
    'tool_name': 'image_gen.imagegen',
    'kind': 'native_tool_return',
    'status': 'completed',
    'metadata': {'fixture': 'synthetic completed return, no external invocation'},
}


class CodexBridgeIndependentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_root = self.root / 'project'
        self.engine = ProjectEngine(self.project_root)
        self.engine.initialize('P2 independent offline contract fixture')
        self.work_id = self.engine.create_work('soup')['id']
        self.auth_id = self.engine.authorize(
            [self.work_id], ['generate', 'generative_edit'], requested_count=2,
            retry_budget=0, paid_channel_scope=[ADAPTER_ID],
            evidence={'fixture': 'two calls only; no retries'},
        )['id']
        self.bridge = self.reopen_bridge()
        self.source = self.root / 'source.png'
        self.output = self.root / 'tool-output.png'
        Image.new('RGB', (4, 4), (40, 60, 80)).save(self.source)
        Image.new('RGB', (4, 4), (80, 60, 40)).save(self.output)

    def tearDown(self):
        self.temp.cleanup()

    def reopen_bridge(self):
        return CodexBridge(self.project_root, interface_evidence=copy.deepcopy(INTERFACE_FIXTURE))

    def prepare(self, prompt='One bowl of soup.', parent_version_id=None):
        result = self.bridge.prepare(
            self.work_id, self.auth_id, prompt, parent_version_id=parent_version_id,
        )
        self.assertEqual(result['status'], 'prepared')
        return result

    def import_base(self):
        return self.engine.import_image(self.work_id, self.source)

    def inspection(self, imported):
        return {
            'asset_id': imported['asset']['id'],
            'sha256': imported['asset']['sha256'],
            'evidence': {'fixture': 'offline view_image attestation for this exact asset'},
        }

    def assert_refused(self, action, forbidden_statuses=('dispatching', 'succeeded')):
        try:
            result = action()
        except ValueError:
            return
        self.assertIsInstance(result, dict, 'A refused operation must report structured failure')
        self.assertNotIn(result.get('status'), forbidden_statuses)
        self.assertNotIn('call', result, 'A refused operation must not release a callable payload')

    def capture(self, attempt_id, call):
        return self.bridge.capture_return(
            attempt_id, call['invocation_id'], [str(self.output)], copy.deepcopy(RETURN_FIXTURE),
        )

    def used_units(self):
        return sum(a['units'] for a in self.engine.inspect()['attempts']
                   if a['submission'] != 'not_submitted')

    def test_edit_needs_exact_viewed_asset_and_uses_only_local_reference_parameter(self):
        imported = self.import_base()
        original = imported['version']['id']
        # Rejections must occur before a dispatch consumes one of the two slots.
        for name, inspections in (
            ('missing', []),
            ('wrong_hash', [{**self.inspection(imported), 'sha256': '0' * 64}]),
            ('empty_evidence', [{**self.inspection(imported), 'evidence': {}}]),
        ):
            with self.subTest(inspection=name):
                prepared = self.prepare('Warm the light.', original)
                attempt_id = prepared['attempt']['id']
                self.assert_refused(lambda: self.bridge.dispatch(attempt_id, inspections=inspections))
                self.assertEqual(self.used_units(), 0)

        prepared = self.prepare('Warm the light without changing the soup.', original)
        attempt_id = prepared['attempt']['id']
        preview = self.bridge.preview(attempt_id)
        self.assertEqual(preview['tool_name'], 'image_gen.imagegen')
        result = self.bridge.dispatch(attempt_id, inspections=[self.inspection(imported)])
        self.assertEqual(result['status'], 'dispatching')
        arguments = result['call']['arguments']
        self.assertEqual(len(arguments['referenced_image_paths']), 1)
        self.assertNotIn('num_last_images_to_include', arguments)
        self.assertEqual(sha256_file(arguments['referenced_image_paths'][0]), imported['asset']['sha256'])
        self.assertEqual(result['call']['arguments'], preview['arguments'])
        self.assertEqual(self.used_units(), 1)

    def test_generation_omits_reference_fields_and_duplicate_dispatch_never_releases_call(self):
        prepared = self.prepare()
        attempt_id = prepared['attempt']['id']
        result = self.bridge.dispatch(attempt_id)
        self.assertEqual(result['status'], 'dispatching')
        call = result['call']
        self.assertEqual(call['attempt_id'], attempt_id)
        self.assertEqual(call['request_id'], prepared['request']['id'])
        self.assertTrue(call['invocation_id'])
        self.assertTrue(call['payload_sha256'])
        self.assertNotIn('referenced_image_paths', call['arguments'])
        self.assertNotIn('num_last_images_to_include', call['arguments'])
        self.assert_refused(lambda: self.bridge.dispatch(attempt_id))
        self.assertEqual(self.used_units(), 1)
        self.assertEqual(len(self.engine.inspect()['attempts']), 1)

    def test_wrong_invocation_or_empty_return_cannot_become_a_success_receipt(self):
        attempt_id = self.prepare()['attempt']['id']
        call = self.bridge.dispatch(attempt_id)['call']
        for name, invocation_id, tool_return in (
            ('wrong_invocation', 'not-the-dispatched-invocation', copy.deepcopy(RETURN_FIXTURE)),
            ('empty_return', call['invocation_id'], {}),
        ):
            with self.subTest(receipt=name):
                self.assert_refused(lambda: self.bridge.capture_return(
                    attempt_id, invocation_id, [str(self.output)], tool_return,
                ))
                self.assertNotEqual(self.engine.store.read_record('attempts', attempt_id)['state'], 'succeeded')
                self.assertFalse(self.engine.inspect()['project']['versions'])
        # Invalid receipt attempts cannot erase the ability to receive the real one.
        self.capture(attempt_id, call)
        self.bridge.consume_receipt(attempt_id)
        self.assertEqual(self.engine.store.read_record('attempts', attempt_id)['state'], 'succeeded')
        self.assertEqual(len(self.engine.inspect()['project']['versions']), 1)

    def test_captured_edit_survives_reopen_and_source_loss_without_retargeting_or_duplicate_version(self):
        imported = self.import_base()
        original = imported['version']['id']
        self.engine.select_version(original)
        attempt_id = self.prepare('Change only the background light.', original)['attempt']['id']
        call = self.bridge.dispatch(attempt_id, inspections=[self.inspection(imported)])['call']
        output_hash = sha256_file(self.output)
        self.capture(attempt_id, call)
        self.assertEqual(len(self.engine.inspect()['project']['versions']), 1,
                         'Capture must durably save the receipt without adopting a version')
        self.output.unlink()  # Recovery must use the captured copy, not an ephemeral tool path.

        reopened = self.reopen_bridge()
        reopened.recover()
        first = self.engine.inspect()
        reopened.recover()
        second = self.engine.inspect()
        attempt = self.engine.store.read_record('attempts', attempt_id)
        self.assertEqual(attempt['state'], 'succeeded')
        self.assertEqual(len(attempt['outputs']), 1)
        self.assertEqual(attempt['outputs'][0]['asset']['sha256'], output_hash)
        work = second['project']['works'][self.work_id]
        self.assertEqual(len(work['version_ids']), 2)
        self.assertEqual(work['selected_version_id'], original)
        self.assertEqual(work['edit_target_version_id'], original)
        self.assertEqual(first['project']['versions'], second['project']['versions'])
        self.assertEqual(first['project']['adoptions'], second['project']['adoptions'])
        self.assertEqual(len(second['project']['adoptions']), 1)
        self.assertEqual(len(second['attempts']), 1)
        self.assertEqual(self.used_units(), 1)

    def test_missing_receipt_recovery_stays_unknown_and_creates_no_new_attempt(self):
        attempt_id = self.prepare()['attempt']['id']
        self.bridge.dispatch(attempt_id)
        reopened = self.reopen_bridge()
        reopened.recover()
        reopened.recover()
        state = self.engine.inspect()
        self.assertEqual([a['id'] for a in state['attempts']], [attempt_id])
        self.assertEqual(state['attempts'][0]['state'], 'unknown')
        self.assertEqual(state['attempts'][0]['submission'], 'uncertain')
        self.assertEqual(state['unresolved_attempt_ids'], [attempt_id])
        self.assertFalse(state['project']['versions'])
        self.assertEqual(self.used_units(), 1)
        self.assert_refused(lambda: reopened.dispatch(attempt_id))

    def test_two_call_authorization_keeps_unknown_calls_reserved_and_blocks_third_dispatch(self):
        for prompt in ('One bowl of soup.', 'One bowl of noodles.'):
            attempt_id = self.prepare(prompt)['attempt']['id']
            self.assertEqual(self.bridge.dispatch(attempt_id)['status'], 'dispatching')
        self.bridge.recover()
        third = self.prepare('One plate of rice.')['attempt']['id']
        self.assert_refused(lambda: self.bridge.dispatch(third))
        self.assertEqual(self.used_units(), 2)
        attempts = self.engine.inspect()['attempts']
        self.assertEqual(sum(a['state'] == 'unknown' for a in attempts), 2)
        self.assertEqual(self.engine.store.read_record('attempts', third)['submission'], 'not_submitted')


if __name__ == '__main__':
    unittest.main()
