import json
import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'
sys.path.insert(0, str(SCRIPTS))
from bc_core.engine import ProjectEngine
from bc_core.storage import StateError


def caps():
    return {
        'schema_version': 1, 'adapter_id': 'offline', 'observed_at': '2026-10-29T00:00:00Z',
        'operations': {'generate': 'supported', 'generative_edit': 'supported', 'local_edit': 'supported'},
        'reference_input': {'carriers': {'status': 'known', 'value': ['local_file']},
                            'max_count': {'status': 'known', 'value': 4}, 'ordered': 'supported'},
        'edit_modes': {'status': 'known', 'value': ['global', 'local']},
        'size_limits': {'status': 'unknown'},
        'output_limits': {'status': 'known', 'value': {'max_count': 4}},
        'query': 'unsupported', 'cancel': 'unsupported', 'cost_info': {'status': 'unknown'},
    }


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.engine = ProjectEngine(self.root / 'project')
        self.engine.initialize('Dinner')
        self.work = self.engine.create_work('牛肉面')['id']
        self.input = self.root / 'input.png'
        Image.new('RGB', (12, 10), (40, 60, 80)).save(self.input)

    def tearDown(self):
        self.tmp.cleanup()

    def auth(self, count=1, retries=0):
        return self.engine.authorize([self.work], ['generate', 'generative_edit'], count,
                                     retry_budget=retries, paid_channel_scope=['offline'],
                                     evidence={'message_id': 'user-approved'})['id']

    def prepare(self, auth=None, **kwargs):
        return self.engine.prepare(self.work, authorization_id=auth or self.auth(),
                                   adapter='offline', capabilities=caps(), **kwargs)

    def output(self, attempt, index=0, path=None):
        return {'output_index': index, 'path': str(path or self.input),
                'provenance': {'attempt_id': attempt, 'verified': True, 'source': 'offline_fixture'}}

    def finish(self, prepared):
        attempt = prepared['attempt']['id']
        self.engine.dispatch(attempt)
        return self.engine.observe(attempt, 'succeeded', 'submitted', {'fixture': 'completed'},
                                   outputs=[self.output(attempt)])

    def test_import_and_selection_survive_mirror_loss(self):
        version = self.engine.import_image(self.work, str(self.input))['version']
        self.engine.select_version(version['id'])
        (self.root / 'project/project.json').write_text('{bad')
        reopened = ProjectEngine(self.root / 'project').recover()
        work = reopened['project']['works'][self.work]
        self.assertEqual(work['selected_version_id'], version['id'])
        self.assertEqual(work['edit_target_version_id'], version['id'])
        self.assertFalse(reopened['attempts'])

    def test_new_result_is_edit_target_not_user_selection(self):
        result = self.finish(self.prepare())
        work = result['project']['works'][self.work]
        self.assertIsNotNone(work['edit_target_version_id'])
        self.assertIsNone(work['selected_version_id'])

    def test_prepared_request_reprepared_when_shared_requirements_change(self):
        grant = self.auth()
        original = self.prepare(auth=grant)
        old_id = original['request']['id']
        old_bytes = self.engine.store.record_path('requests', old_id).read_bytes()
        self.engine.add_requirements([{'key': 'temperature', 'value': 'cold'}],
                                     {'kind': 'work', 'id': self.work}, 'u2')
        result = self.engine.dispatch(original['attempt']['id'])
        self.assertEqual(result['status'], 'reprepared')
        self.assertNotEqual(result['request']['id'], old_id)
        self.assertEqual(result['request']['authorization_id'], grant)
        self.assertEqual(self.engine.store.read_record('attempts', original['attempt']['id'])['submission'], 'not_submitted')
        self.assertEqual(self.engine.store.record_path('requests', old_id).read_bytes(), old_bytes)
        self.assertEqual(result['request']['effective_requirements'][0]['value'], 'cold')
        self.assertEqual(self.engine.dispatch(result['attempt']['id'])['attempt']['state'], 'dispatching')

    def test_unrelated_work_change_does_not_expire_request(self):
        original = self.prepare()
        other = self.engine.create_work('豆腐')['id']
        self.engine.add_requirements([{'key': 'temperature', 'value': 'warm'}],
                                     {'kind': 'work', 'id': other}, 'u-other')
        result = self.engine.dispatch(original['attempt']['id'])
        self.assertEqual(result['attempt']['id'], original['attempt']['id'])
        self.assertEqual(result['status'], 'dispatching')

    def test_recovery_does_not_resubmit_and_preserves_unknown_budget(self):
        grant = self.auth()
        first = self.prepare(auth=grant)
        self.engine.dispatch(first['attempt']['id'])
        report = self.engine.recover()
        self.assertEqual(report['attempts'][0]['state'], 'unknown')
        self.assertEqual(report['unresolved_attempt_ids'], [first['attempt']['id']])
        second = self.prepare(auth=grant, design={'scene': 'different'})
        with self.assertRaisesRegex(StateError, 'budget'):
            self.engine.dispatch(second['attempt']['id'])
        self.assertEqual(len(self.engine.recover()['attempts']), 2)

    def test_late_result_does_not_steal_explicit_target(self):
        v0 = self.engine.import_image(self.work, str(self.input))['version']['id']
        prepared = self.prepare()
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        self.engine.set_edit_target(v0)
        result = self.engine.observe(aid, 'succeeded', 'submitted', {'fixture': 'late'}, outputs=[self.output(aid)])
        work = result['project']['works'][self.work]
        self.assertNotEqual(work['latest_version_id'], v0)
        self.assertEqual(work['edit_target_version_id'], v0)

    def test_unknown_candidate_and_user_choice_do_not_assert_tool_success(self):
        prepared = self.prepare()
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        result = self.engine.observe(aid, 'unknown', 'submitted', {'fixture': 'missing_terminal'}, outputs=[self.output(aid)])
        vid = result['project']['works'][self.work]['latest_version_id']
        self.engine.select_version(vid)
        self.engine.acknowledge_unknown(aid, {'message_id': 'user-saw-image'})
        recovered = self.engine.recover()
        self.assertEqual(recovered['attempts'][0]['state'], 'unknown')
        self.assertIn(aid, recovered['unresolved_attempt_ids'])

    def test_retry_needs_budget_and_unknown_risk_ack(self):
        prepared = self.prepare(auth=self.auth(retries=1))
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        self.engine.recover()
        with self.assertRaisesRegex(StateError, 'duplicate'):
            self.engine.retry(aid)
        retried = self.engine.retry(aid, duplicate_risk_ack={'message_id': 'again-despite-risk'})
        self.assertEqual(retried['attempt']['retry_of'], aid)
        self.engine.dispatch(retried['attempt']['id'])

    def test_retry_ack_does_not_cover_an_unrelated_unknown_attempt(self):
        grant = self.auth(count=2, retries=1)
        first = self.prepare(auth=grant)
        second = self.prepare(auth=grant)
        self.engine.dispatch(first['attempt']['id'])
        self.engine.dispatch(second['attempt']['id'])
        self.engine.recover()
        retried = self.engine.retry(first['attempt']['id'],
                                   duplicate_risk_ack={'message_id': 'ack-first-only'})
        with self.assertRaisesRegex(StateError, 'duplicate risk'):
            self.engine.dispatch(retried['attempt']['id'])
        self.assertEqual(self.engine.store.read_record('attempts', retried['attempt']['id'])['state'],
                         'prepared')

    def test_lossy_export_edit_uses_actual_pixels_with_a_warning(self):
        imported = self.engine.import_image(self.work, self.input)
        edited = self.engine.local_edit(imported['version']['id'], [
            {'type': 'brightness', 'params': {'factor': 1.2}},
        ])
        exported = self.engine.export_version(edited['version']['id'], 'jpeg')
        resumed = self.engine.local_edit(exported['version']['id'], [
            {'type': 'brightness', 'params': {'factor': 1.1}},
        ], mode='replace')
        self.assertEqual(resumed['version']['edit_pipeline']['master_asset_id'], exported['asset']['id'])
        self.assertTrue(any('not losslessly reversible' in warning for warning in resumed['warnings']))
        self.assertEqual(self.engine.inspect()['project']['versions'][edited['version']['id']]['edit_pipeline'],
                         edited['version']['edit_pipeline'])

    def test_output_adoption_is_idempotent(self):
        prepared = self.prepare()
        aid = prepared['attempt']['id']
        result = self.finish(prepared)
        first = result['project']['works'][self.work]['latest_version_id']
        again = self.engine.observe(aid, 'succeeded', 'submitted', {'fixture': 'same'}, outputs=[self.output(aid)])
        self.assertEqual(again['project']['works'][self.work]['version_ids'], [first])
        self.assertEqual(self.engine.recover()['project']['works'][self.work]['version_ids'], [first])

    def test_crash_after_attempt_output_record_recovers_once_without_target_change(self):
        v0 = self.engine.import_image(self.work, str(self.input))['version']['id']
        self.engine.select_version(v0)
        prepared = self.prepare()
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        def crash(stage):
            if stage == 'attempt_outputs_saved':
                raise RuntimeError('crash before adoption')
        self.engine.store.failpoint = crash
        with self.assertRaises(RuntimeError):
            self.engine.observe(aid, 'succeeded', 'submitted', {'fixture': 'complete'}, outputs=[self.output(aid)])
        recovered = ProjectEngine(self.root / 'project').recover()
        work = recovered['project']['works'][self.work]
        self.assertEqual(len(work['version_ids']), 2)
        self.assertEqual(work['edit_target_version_id'], v0)
        self.assertEqual(work['selected_version_id'], v0)
        again = ProjectEngine(self.root / 'project').recover()
        self.assertEqual(again['project']['works'][self.work]['version_ids'], work['version_ids'])

    def test_multiple_outputs_require_disambiguation(self):
        prepared = self.prepare()
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        result = self.engine.observe(aid, 'succeeded', 'submitted', {'fixture': 'multi'},
                                     outputs=[self.output(aid, 0), self.output(aid, 1)])
        self.assertEqual(result['project']['target_resolution'], 'ambiguous')
        with self.assertRaisesRegex(StateError, 'ambiguous'):
            self.prepare(operation={'type': 'generative_edit'})

    def test_same_bytes_different_attempts_keep_versions(self):
        first = self.finish(self.prepare())
        second = self.finish(self.prepare())
        versions = list(second['project']['versions'].values())
        self.assertEqual(len(versions), 2)
        self.assertNotEqual(versions[0]['attempt_id'], versions[1]['attempt_id'])
        self.assertEqual(versions[0]['asset_id'], versions[1]['asset_id'])

    def test_reference_role_mapping_and_missing_asset_block_submission(self):
        imported = self.engine.import_image(self.work, str(self.input))
        ref = self.engine.add_reference(imported['version']['asset_id'], [{'role': 'lighting', 'borrow': 'window light',
                          'exclude': ['food'], 'strength': 'required', 'source': {'message_id': 'user'}}])
        prepared = self.prepare(references=[ref['id']])
        path = self.engine.store.project_path(imported['asset']['locator'])
        path.unlink()
        result = self.engine.dispatch(prepared['attempt']['id'])
        self.assertEqual(result['attempt']['state'], 'failed')
        self.assertEqual(result['attempt']['submission'], 'not_submitted')

    def test_local_edit_creates_version_without_external_attempt(self):
        v0 = self.engine.import_image(self.work, str(self.input))['version']['id']
        result = self.engine.local_edit(v0, [{'type': 'brightness', 'params': {'factor': 1.2}}])
        version = result['version']
        self.assertEqual(version['origin'], 'local_edit')
        self.assertEqual(version['parent_version_id'], v0)
        self.assertEqual(version['edit_pipeline']['master_asset_id'], self.engine.inspect()['project']['versions'][v0]['asset_id'])
        self.assertEqual(len(self.engine.inspect()['attempts']), 0)

    def test_export_does_not_change_edit_target(self):
        v0 = self.engine.import_image(self.work, str(self.input))['version']['id']
        edited = self.engine.local_edit(v0, [{'type': 'brightness', 'params': {'factor': 1.1}}])['version']['id']
        result = self.engine.export_version(edited, 'png')
        self.assertEqual(result['version']['origin'], 'export')
        self.assertEqual(result['project']['works'][self.work]['edit_target_version_id'], edited)

    def test_empty_replace_clears_last_operation_from_master_and_preserves_branch(self):
        imported = self.engine.import_image(self.work, str(self.input))
        edited = self.engine.local_edit(imported['version']['id'], [
            {'type': 'brightness', 'params': {'factor': 2}}])
        exported = self.engine.export_version(edited['version']['id'], 'png')
        for parent in (edited, exported):
            with self.subTest(origin=parent['version']['origin']):
                result = self.engine.local_edit(parent['version']['id'], [], mode='replace')
                pipeline = result['version']['edit_pipeline']
                self.assertEqual(pipeline['operations'], [])
                self.assertEqual(pipeline['master_asset_id'], imported['asset']['id'])
                self.assertEqual(result['version']['parent_version_id'], parent['version']['id'])
                restored_path = self.engine.store.project_path(result['asset']['locator'])
                with Image.open(restored_path) as restored, Image.open(self.input) as original:
                    self.assertEqual(restored.size, original.size)
                    self.assertEqual(restored.tobytes(), original.tobytes())
                earlier_path = self.engine.store.project_path(edited['asset']['locator'])
                with Image.open(earlier_path) as earlier:
                    self.assertEqual(earlier.getpixel((0, 0)), (80, 120, 160))
                self.assertEqual(result['project']['works'][self.work]['edit_target_version_id'],
                                 result['version']['id'])
        self.assertEqual(self.engine.inspect()['attempts'], [])

    def test_empty_append_and_non_list_replace_do_not_change_state(self):
        version = self.engine.import_image(self.work, str(self.input))['version']['id']
        before = self.engine.inspect()['project']
        for operations, mode in (([], 'append'), (None, 'replace'), ({}, 'replace')):
            with self.subTest(operations=operations, mode=mode):
                with self.assertRaises(StateError):
                    self.engine.local_edit(version, operations, mode=mode)
                self.assertEqual(self.engine.inspect()['project'], before)

    def test_read_only_intents_create_no_execution(self):
        for intent in ('explore', 'critique', 'shoot_guidance'):
            result = self.engine.record_read_only(self.work, intent, {'topic': 'window light'})
            self.assertIsNone(result['attempt'])
        self.assertEqual(self.engine.inspect()['attempts'], [])
        self.assertEqual(self.engine.inspect()['project']['versions'], {})

    def test_invalid_observation_never_erases_submitted_fact(self):
        prepared = self.prepare()
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        self.engine.observe(aid, 'accepted', 'submitted', {'fixture': 'receipt'})
        result = self.engine.observe(aid, 'succeeded', 'uncertain', {'fixture': 'bad_adapter'})
        self.assertEqual(result['attempt']['state'], 'unknown')
        self.assertEqual(result['attempt']['submission'], 'submitted')
        self.assertTrue(result['attempt']['protocol_errors'])


if __name__ == '__main__':
    unittest.main()
