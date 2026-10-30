"""P3 core regressions. Images and adapter receipts are synthetic offline facts."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'
sys.path.insert(0, str(SCRIPTS))
from bc_core.engine import ProjectEngine
from bc_core.storage import StateError
from test_engine import caps


class P3CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project_dir = self.root / 'project'
        self.engine = ProjectEngine(self.project_dir)
        self.project_id = self.engine.initialize('P3 offline')['id']
        self.work = self.engine.create_work('红烧肉')['id']
        self.input = self.root / 'source.png'
        Image.new('RGB', (12, 10), (50, 70, 90)).save(self.input)

    def tearDown(self):
        self.tmp.cleanup()

    def authorize(self, work=None, count=1, retries=0):
        return self.engine.authorize([work or self.work], ['generate', 'generative_edit'], count,
                                     retry_budget=retries, paid_channel_scope=['offline'],
                                     evidence={'fixture': 'explicit offline authorization'})['id']

    def prepare(self, work=None, auth=None, **kwargs):
        return self.engine.prepare(work or self.work, auth or self.authorize(work), 'offline', caps(), **kwargs)

    def finish(self, prepared, count=1, state='succeeded'):
        aid = prepared['attempt']['id']
        self.engine.dispatch(aid)
        outputs = [{'output_index': index, 'path': str(self.input),
                    'provenance': {'attempt_id': aid, 'verified': True, 'source': 'offline_fixture'}}
                   for index in range(count)]
        return self.engine.observe(aid, state, 'submitted', {'fixture': 'synthetic receipt'}, outputs=outputs)

    def role(self, role='lighting'):
        return [{'role': role, 'borrow': 'window light', 'exclude': ['food', 'plate'],
                 'strength': 'preferred', 'source': {'kind': 'user_reference', 'message_id': 'p3-u1'}}]

    def reference(self, scope):
        asset = self.engine.import_asset(self.input)['asset']['id']
        return self.engine.add_reference(asset, self.role(), scope=scope)

    def test_x06_retry_multi_output_restore_and_explicit_label_edit(self):
        base = self.engine.import_image(self.work, self.input)['version']['id']
        group = self.engine.create_group([self.work] * 3)
        gid, (item1, item2, item3) = group['id'], group['item_ids']
        auth = self.authorize(count=4, retries=1)
        first = self.prepare(auth=auth, group_id=gid, item_id=item1)
        partial = self.finish(first, state='failed')['added_version_ids'][0]
        retried = self.engine.retry(first['attempt']['id'])
        retry_version = self.finish(retried)['added_version_ids'][0]
        second = self.prepare(auth=auth, group_id=gid, item_id=item2,
                              operation={'type': 'generate', 'expected_outputs': 2})
        pair = self.finish(second, count=2)['added_version_ids']
        self.finish(self.prepare(auth=auth, group_id=gid, item_id=item3))
        labels = self.engine.inspect()['project']['groups'][gid]['display_labels']
        self.assertEqual(labels['1-a'], partial)
        self.assertEqual(labels['1-b'], retry_version)
        self.assertEqual(labels['1-1'], partial)
        self.assertEqual(labels['1-2'], retry_version)
        self.assertEqual(labels['2-a'], pair[0])
        self.assertEqual(labels['2-b'], pair[1])
        self.assertEqual(labels['2-2'], pair[1])
        late = self.prepare(design={'fixture': 'unrelated later receipt'})
        self.engine.dispatch(late['attempt']['id'])
        (self.project_dir / 'project.json').write_text('{broken', encoding='utf-8')
        self.engine = ProjectEngine(self.project_dir)
        recovered = self.engine.recover()
        self.assertEqual(recovered['project']['groups'][gid]['display_labels'], labels)
        with self.assertRaisesRegex(StateError, 'ambiguous'):
            self.engine.resolve_version_label('2', group_id=gid)
        selected = self.engine.select_label('2-b', group_id=gid)
        self.assertEqual(selected['version_id'], pair[1])
        aid = late['attempt']['id']
        late_result = self.engine.observe(aid, 'succeeded', 'submitted', {'fixture': 'late receipt'},
            outputs=[{'output_index': 0, 'path': str(self.input),
                      'provenance': {'attempt_id': aid, 'verified': True, 'source': 'offline_fixture'}}])
        state = late_result['project']['works'][self.work]
        self.assertNotEqual(state['latest_version_id'], pair[1])
        self.assertEqual(state['selected_version_id'], pair[1])
        self.assertEqual(state['edit_target_version_id'], pair[1])
        edit = self.prepare(operation={'type': 'generative_edit'})
        self.assertEqual(edit['request']['parent_version_id'], pair[1])
        self.assertNotEqual(edit['request']['parent_version_id'], base)
        self.assertEqual(self.engine.resolve_version_label('2-2', group_id=gid)['version_id'], pair[1])
        self.assertEqual(self.engine.inspect()['project']['groups'][gid]['display_labels'], labels)

    def test_labels_use_explicit_scope_and_active_context_without_latest_fallback(self):
        first = self.engine.import_image(self.work, self.input)['version']['id']
        group = self.engine.create_group([self.work])
        only = self.finish(self.prepare(group_id=group['id'], item_id=group['item_ids'][0]))['added_version_ids'][0]
        revision = self.engine.inspect()['project']['revision']
        self.assertEqual(self.engine.resolve_version_label('1')['version_id'], only)
        self.assertEqual(self.engine.resolve_version_label('V1')['version_id'], first)
        self.assertEqual(self.engine.inspect()['project']['revision'], revision)
        other = self.engine.create_work('豆腐')['id']
        other_version = self.engine.import_image(other, self.input)['version']['id']
        self.assertEqual(self.engine.resolve_version_label('V1')['version_id'], other_version)
        self.assertEqual(self.engine.resolve_version_label('V1', work_id=self.work)['version_id'], first)
        self.assertEqual(self.engine.resolve_version_label(f'{group["id"]}:1-a')['version_id'], only)
        with self.assertRaisesRegex(StateError, 'scope|work'):
            self.engine.resolve_version_label('1-a', group_id=group['id'], work_id=other)
        with self.assertRaisesRegex(StateError, 'label'):
            self.engine.resolve_version_label('V999', work_id=self.work)

    def test_select_label_uses_readable_version_check_without_partial_selection(self):
        result = self.engine.import_image(self.work, self.input)
        before = self.engine.inspect()['project']
        (self.project_dir / result['asset']['locator']).unlink()
        with self.assertRaisesRegex(StateError, 'missing|changed'):
            self.engine.select_label('V1', work_id=self.work)
        after = self.engine.inspect()['project']
        self.assertEqual(after['revision'], before['revision'])
        self.assertIsNone(after['works'][self.work]['selected_version_id'])

    def test_new_group_context_clears_old_resolution_and_child_cannot_auto_target(self):
        base = self.engine.import_image(self.work, self.input)['version']['id']
        group = self.engine.create_group([self.work] * 3)
        self.engine.select_version(base)
        first = self.prepare(group_id=group['id'], item_id=group['item_ids'][0])
        self.assertEqual(first['project']['target_resolution'], 'ambiguous')
        result = self.finish(first)
        self.assertEqual(result['project']['works'][self.work]['edit_target_version_id'], base)
        self.assertEqual(result['project']['target_resolution'], 'ambiguous')
        with self.assertRaisesRegex(StateError, 'ambiguous'):
            self.prepare(operation={'type': 'generative_edit'})
        self.engine.select_version(result['added_version_ids'][0])
        second = self.prepare(group_id=group['id'], item_id=group['item_ids'][1])
        self.assertEqual(second['project']['target_resolution'], 'ambiguous')

    def test_new_group_edit_context_cannot_reuse_old_selected_parent_implicitly(self):
        base = self.engine.import_image(self.work, self.input)['version']['id']
        group = self.engine.create_group([self.work] * 2)
        self.engine.select_version(base)
        with self.assertRaisesRegex(StateError, 'ambiguous'):
            self.prepare(group_id=group['id'], item_id=group['item_ids'][0],
                         operation={'type': 'generative_edit'})

    def test_reference_scope_rejects_invalid_or_inconsistent_targets_when_added(self):
        other = self.engine.create_work('豆腐')['id']
        group = self.engine.create_group([self.work, other])
        scopes = [{}, {'kind': 'unknown', 'id': self.work}, {'kind': 'project', 'id': 'foreign'},
                  {'kind': 'work', 'id': 'missing'}, {'kind': 'group', 'id': 'missing'},
                  {'kind': 'item', 'id': 'missing'},
                  {'kind': 'group', 'id': group['id'], 'item_ids': 'invalid'},
                  {'kind': 'work', 'id': self.work, 'item_ids': [group['item_ids'][1]]}]
        foreign = self.engine.create_group([self.work])
        scopes.append({'kind': 'group', 'id': group['id'], 'item_ids': foreign['item_ids']})
        for scope in scopes:
            with self.subTest(scope=scope), self.assertRaisesRegex(StateError, 'reference scope'):
                self.reference(scope)

    def test_reference_scopes_enforced_and_roles_sources_preserved(self):
        other = self.engine.create_work('豆腐')['id']
        group = self.engine.create_group([self.work, other, self.work])
        foreign_group = self.engine.create_group([self.work])
        gid, (item1, item2, item3) = group['id'], group['item_ids']
        scenarios = [
            ({'kind': 'project', 'id': self.project_id}, self.work, None, None, None),
            ({'kind': 'work', 'id': self.work}, self.work, None, None, (other, None, None)),
            ({'kind': 'group', 'id': gid}, self.work, gid, item1,
             (self.work, foreign_group['id'], foreign_group['item_ids'][0])),
            ({'kind': 'item', 'id': item1}, self.work, gid, item1, (self.work, gid, item3)),
            ({'kind': 'group', 'id': gid, 'item_ids': [item1]}, self.work, gid, item1, (other, gid, item2)),
        ]
        for scope, work, group_id, item_id, mismatch in scenarios:
            with self.subTest(scope=scope):
                reference = self.reference(scope)
                prepared = self.prepare(work=work, group_id=group_id, item_id=item_id, references=[reference['id']])
                self.assertEqual(prepared['request']['references'], [reference])
                self.assertEqual(prepared['request']['references'][0]['roles'], self.role())
                if mismatch:
                    bad_work, bad_group, bad_item = mismatch
                    before = self.engine.inspect()['project']['revision']
                    with self.assertRaisesRegex(StateError, 'reference scope'):
                        self.prepare(work=bad_work, auth=self.authorize(bad_work), group_id=bad_group,
                                     item_id=bad_item, references=[reference['id']])
                    self.assertEqual(self.engine.inspect()['project']['revision'], before + 1)
                if scope['kind'] in ('group', 'item'):
                    with self.assertRaisesRegex(StateError, 'reference scope'):
                        self.prepare(references=[reference['id']])

    def test_multiple_references_keep_roles_and_actual_tool_order(self):
        base = self.engine.import_image(self.work, self.input)['version']['id']
        light = self.reference({'kind': 'work', 'id': self.work})
        subject = self.engine.add_reference(light['asset_id'], self.role('subject'),
                                            {'kind': 'project', 'id': self.project_id})
        prepared = self.prepare(parent_version_id=base, operation={'type': 'generative_edit'},
                                references=[light['id'], subject['id']])
        refs = prepared['request']['references']
        self.assertEqual([r['id'] for r in refs[1:]], [light['id'], subject['id']])
        self.assertEqual(refs[1]['roles'], light['roles'])
        mapping = [{'tool_index': index, 'reference_id': ref['id'], 'asset_id': ref['asset_id']}
                   for index, ref in enumerate(reversed(refs))]
        dispatched = self.engine.dispatch(prepared['attempt']['id'], input_reference_map=mapping)
        self.assertEqual(dispatched['attempt']['dispatch']['input_reference_map'], mapping)

    def test_subject_swap_borrows_only_named_subject_independent_source_facts(self):
        records = [{'key': 'lighting', 'value': 'warm window'}, {'key': 'color', 'value': 'amber'},
                   {'key': 'framing', 'value': 'close'},
                   {'key': 'chunks', 'value': 6, 'dependencies': ['subject']},
                   {'key': 'plate', 'value': 'meat platter', 'dependencies': ['subject']}]
        self.engine.add_requirements(records, {'kind': 'work', 'id': self.work}, 'source-work')
        base = self.engine.import_image(self.work, self.input)['version']['id']
        sibling = self.prepare(parent_version_id=base, operation={'type': 'generative_edit'},
                               changes=[{'key': 'lighting', 'value': 'blue studio'},
                                        {'key': 'garnish', 'value': 'sesame', 'dependencies': ['subject']}])
        self.finish(sibling)
        old_versions = copy.deepcopy(self.engine.inspect()['project']['versions'])
        other = self.engine.create_work('番茄炒蛋', borrowed_from=base,
                                       borrowed_keys=['lighting', 'color', 'chunks', 'plate', 'garnish'])['id']
        next_request = self.prepare(work=other)['request']
        self.assertEqual({r['key']: r['value'] for r in next_request['effective_requirements']},
                         {'lighting': 'warm window', 'color': 'amber'})
        self.assertIsNone(next_request['parent_version_id'])
        self.assertEqual(next_request['references'], [])
        self.assertEqual(self.engine.inspect()['project']['versions'], old_versions)
        self.assertTrue(all(r['scope'] == {'kind': 'work', 'id': other}
                            for r in next_request['effective_requirements']))

    def test_state_cli_resolves_and_selects_stable_label(self):
        version = self.engine.import_image(self.work, self.input)['version']['id']
        for action in ('resolve_version_label', 'select_label'):
            result = subprocess.run([sys.executable, str(SCRIPTS / 'state.py'), '--project', str(self.project_dir),
                                     'apply', '--input', '-'],
                                    input=json.dumps({'action': action, 'label': 'V1', 'work_id': self.work}),
                                    capture_output=True, text=True,
                                    env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
            payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload['result']['version_id'], version)
        self.assertEqual(self.engine.inspect()['project']['works'][self.work]['selected_version_id'], version)


if __name__ == '__main__':
    unittest.main()
