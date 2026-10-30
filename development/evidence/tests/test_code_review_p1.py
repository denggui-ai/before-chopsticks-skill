"""Fresh P1 review regressions: synthetic local images, no external calls.

These tests assert the intended public behavior and were first run against the
reviewed implementation to retain actual failing counterexamples.
"""

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))

from bc_core.engine import ProjectEngine
from bc_core.storage import StateError


def offline_capabilities():
    return {
        'schema_version': 1, 'adapter_id': 'review_offline',
        'observed_at': '2026-10-29T00:00:00Z',
        'operations': {'generate': 'supported', 'generative_edit': 'supported'},
        'reference_input': {
            'carriers': {'status': 'known', 'value': ['local_file']},
            'max_count': {'status': 'known', 'value': 4}, 'ordered': 'supported',
        },
        'edit_modes': {'status': 'known', 'value': ['global']},
        'size_limits': {'status': 'unknown'},
        'output_limits': {'status': 'known', 'value': {'max_count': 1}},
        'cost_info': {'status': 'unknown'},
        'query': 'unsupported', 'cancel': 'unsupported',
    }


class CodeReviewP1Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = ProjectEngine(self.root / 'project')
        self.engine.initialize('P1 code review fixture')
        self.work = self.engine.create_work('soup')['id']
        self.image = self.root / 'source.png'
        Image.new('RGB', (1, 1), (40, 40, 40)).save(self.image)

    def tearDown(self):
        self.temp.cleanup()

    def pixel(self, result):
        path = self.engine.store.project_path(result['asset']['locator'])
        with Image.open(path) as image:
            return image.getpixel((0, 0))

    def test_unsubmitted_retry_keeps_its_retry_allocation(self):
        grant = self.engine.authorize(
            [self.work], ['generate'], requested_count=1, retry_budget=1,
            paid_channel_scope=['review_offline'], evidence={'message_id': 'approved-one-retry'},
        )['id']
        original = self.engine.prepare(self.work, grant, 'review_offline', offline_capabilities())
        original_id = original['attempt']['id']
        self.engine.dispatch(original_id)
        self.engine.observe(original_id, 'failed', 'submitted', {'fixture': 'explicit-tool-failure'})

        retry = self.engine.retry(original_id)
        retry_id = retry['attempt']['id']
        self.engine.cancel_prepared(retry_id, {'message_id': 'pause-before-submission'})
        try:
            resumed = self.engine.retry(retry_id)
        except StateError as exc:
            self.fail(f'An unsubmitted retry must retain its unused retry allocation: {exc}')
        dispatched = self.engine.dispatch(resumed['attempt']['id'])

        self.assertEqual(dispatched['status'], 'dispatching')
        self.assertEqual(dispatched['attempt']['budget_kind'], 'retry')
        attempts = self.engine.inspect()['attempts']
        self.assertEqual(sum(a['units'] for a in attempts
                             if a['budget_kind'] == 'retry' and a['submission'] != 'not_submitted'), 1)

    def test_unsubmitted_retry_preserves_acknowledged_unknown_ancestor(self):
        # A spare initial slot makes this independent of the budget-kind bug:
        # the reviewed code reaches its duplicate-risk check and fails there.
        grant = self.engine.authorize(
            [self.work], ['generate'], requested_count=2, retry_budget=1,
            paid_channel_scope=['review_offline'], evidence={'message_id': 'approved-retry'},
        )['id']
        original = self.engine.prepare(self.work, grant, 'review_offline', offline_capabilities())
        original_id = original['attempt']['id']
        self.engine.dispatch(original_id)
        self.engine.recover()
        retry = self.engine.retry(original_id, duplicate_risk_ack={'message_id': 'accept-original-risk'})
        self.engine.cancel_prepared(retry['attempt']['id'], {'message_id': 'pause-before-submission'})
        resumed = self.engine.retry(retry['attempt']['id'])
        try:
            dispatched = self.engine.dispatch(resumed['attempt']['id'])
        except StateError as exc:
            self.fail(f'Repreparing an unsubmitted retry must preserve the existing risk acknowledgement: {exc}')

        self.assertEqual(dispatched['status'], 'dispatching')
        self.assertEqual(dispatched['request']['authorization_id'], grant)
        self.assertEqual(dispatched['attempt']['budget_kind'], 'retry')
        self.assertEqual(self.engine.store.read_record('attempts', original_id)['state'], 'unknown')

    def test_lossless_export_keeps_replayable_parameter_replacement(self):
        imported = self.engine.import_image(self.work, self.image)
        edited = self.engine.local_edit(imported['version']['id'], [
            {'type': 'brightness', 'params': {'factor': 2}},
        ])
        exported = self.engine.export_version(edited['version']['id'], 'png')

        direct = self.engine.local_edit(edited['version']['id'], [
            {'type': 'brightness', 'params': {'factor': 3}},
        ], mode='replace')
        through_export = self.engine.local_edit(exported['version']['id'], [
            {'type': 'brightness', 'params': {'factor': 3}},
        ], mode='replace')

        self.assertEqual(self.pixel(direct), (120, 120, 120))
        self.assertEqual(self.pixel(through_export), self.pixel(direct),
                         'A lossless export must not silently bake in an otherwise replayable edit')

    def test_conflicting_work_requirements_block_local_edit_adoption(self):
        imported = self.engine.import_image(self.work, self.image)
        self.engine.add_requirements([
            {'key': 'color', 'value': 'warm'},
            {'key': 'color', 'value': 'cool'},
        ], {'kind': 'work', 'id': self.work}, 'same-conflicting-user-turn')
        before = self.engine.inspect()['project']

        try:
            result = self.engine.local_edit(imported['version']['id'], [
                {'type': 'brightness', 'params': {'factor': 1.1}},
            ])
        except StateError:
            result = None
        after = self.engine.inspect()['project']

        self.assertEqual(after['works'][self.work]['version_ids'],
                         before['works'][self.work]['version_ids'],
                         'An affected local edit cannot create a version while requirements conflict')
        self.assertEqual(after['works'][self.work]['edit_target_version_id'],
                         before['works'][self.work]['edit_target_version_id'])
        if result is not None:
            self.assertEqual(result.get('status'), 'blocked')


if __name__ == '__main__':
    unittest.main()
