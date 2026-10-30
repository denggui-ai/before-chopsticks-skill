"""Independent P3 bridge regressions, using synthetic images and local receipts.

No test invokes image_gen, view_image, query/cancel, or any network service.
The v1 journal is built with the frozen P2 payload, not the v2 prompt builder.
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))

from bc_core.codex_bridge import CodexBridge
from bc_core.engine import ProjectEngine
from bc_core.storage import StateError, digest, sha256_file


ADAPTER_ID = 'codex-native-imagegen-v1'
INTERFACE = {'kind': 'offline_contract_fixture', 'source': 'P3 independent test, not live evidence'}
RETURN = {
    'tool_name': 'image_gen.imagegen', 'kind': 'native_tool_return', 'status': 'completed',
    'metadata': {'fixture': 'synthetic local output; no native invocation'},
}


class CodexBridgeP3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_root = self.root / 'project'
        self.engine = ProjectEngine(self.project_root)
        self.engine.initialize('P3 independent offline bridge fixture')
        self.work = self.engine.create_work('soup')['id']
        self.auth = self.engine.authorize(
            [self.work], ['generate', 'generative_edit'], requested_count=4, retry_budget=0,
            paid_channel_scope=[ADAPTER_ID], evidence={'fixture': 'four offline calls, no retry'},
        )['id']
        self.bridge = self.reopen()
        self.paths = []
        for index, color in enumerate(((20, 30, 40), (50, 60, 70), (80, 90, 100), (110, 120, 130))):
            path = self.root / f'image-{index}.png'
            Image.new('RGB', (5, 4), color).save(path)
            self.paths.append(path)

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self):
        return CodexBridge(self.project_root, interface_evidence=copy.deepcopy(INTERFACE))

    def role(self, name, borrow, exclude):
        return {'role': name, 'borrow': borrow, 'exclude': [exclude], 'strength': 'required',
                'source': {'kind': 'user_explicit', 'message_id': 'offline-p3-fixture'}}

    def reference(self, asset, name, borrow, exclude):
        return self.engine.add_reference(
            asset['id'], [self.role(name, borrow, exclude)], scope={'kind': 'work', 'id': self.work},
        )

    def three_references(self):
        base = self.engine.import_image(self.work, self.paths[0])
        light = self.engine.import_asset(self.paths[1])['asset']
        styling = self.engine.import_asset(self.paths[2])['asset']
        # Deliberately non-base-first: indices must track the supplied input order.
        refs = [
            self.reference(light, 'lighting', 'BORROW_LIGHT_LEFT_SOFT', 'EXCLUDE_LIGHT_FOOD'),
            self.reference(base['asset'], 'base_image', 'BORROW_BASE_SOUP', 'EXCLUDE_BASE_COLOR_CAST'),
            self.reference(styling, 'food_styling', 'BORROW_RADIAL_PLATING', 'EXCLUDE_STYLING_PLATE_COLOR'),
        ]
        return base, refs

    def prepared(self, **kwargs):
        result = self.bridge.prepare(self.work, self.auth, 'Create one soup photograph.', **kwargs)
        self.assertEqual(result['status'], 'prepared')
        return result

    def inspections(self, plan):
        return [{'asset_id': entry['asset_id'], 'sha256': entry['sha256'],
                 'evidence': {'fixture': 'offline exact-image inspection; no view_image call'}}
                for entry in plan['inputs']]

    def used_units(self):
        return sum(attempt['units'] for attempt in self.engine.inspect()['attempts']
                   if attempt['submission'] != 'not_submitted')

    def capture_and_consume(self, prepared, output_path):
        attempt_id = prepared['attempt']['id']
        plan = self.bridge.preview(attempt_id)
        call = self.bridge.dispatch(attempt_id, inspections=self.inspections(plan))['call']
        self.bridge.capture_return(attempt_id, call['invocation_id'], [str(output_path)], copy.deepcopy(RETURN))
        result = self.bridge.consume_receipt(attempt_id)
        self.assertEqual(result['status'], 'succeeded')
        return self.engine.store.read_record('attempts', attempt_id)['outputs'][0]['version']['id']

    def test_three_image_edit_preserves_order_and_fixed_roles_borrow_exclude(self):
        base, refs = self.three_references()
        prepared = self.prepared(parent_version_id=base['version']['id'], references=[ref['id'] for ref in refs])
        self.assertEqual(prepared['request']['design']['native_payload_version'], 2)
        fixed_refs = copy.deepcopy(prepared['request']['references'])
        # A caller's mutable returned copy cannot replace immutable Request roles.
        prepared['request']['references'][0]['roles'][0]['borrow'] = 'POISON_RETURNED_COPY'
        plan = self.bridge.preview(prepared['attempt']['id'])
        expected_assets = [ref['asset_id'] for ref in refs]
        self.assertEqual([entry['asset_id'] for entry in plan['inputs']], expected_assets)
        self.assertEqual(plan['input_reference_map'], [
            {'tool_index': index, 'reference_id': ref['id'], 'asset_id': ref['asset_id']}
            for index, ref in enumerate(refs)
        ])
        arguments = plan['arguments']
        self.assertEqual(arguments['referenced_image_paths'], [entry['path'] for entry in plan['inputs']])
        self.assertEqual([sha256_file(path) for path in arguments['referenced_image_paths']],
                         [entry['sha256'] for entry in plan['inputs']])
        self.assertNotIn('num_last_images_to_include', arguments)
        self.assertNotIn('POISON_RETURNED_COPY', arguments['prompt'])
        positions = []
        for entry, reference in zip(plan['inputs'], fixed_refs):
            self.assertEqual(entry['roles'], reference['roles'])
            for role in reference['roles']:
                for value in (role['role'], role['borrow'], role['strength'], *role['exclude']):
                    self.assertIn(value, arguments['prompt'])
                positions.append(arguments['prompt'].index(role['borrow']))
        self.assertEqual(positions, sorted(positions), 'Prompt role bindings must follow actual attachment order')
        dispatched = self.bridge.dispatch(prepared['attempt']['id'], inspections=self.inspections(plan))
        self.assertEqual(dispatched['call']['arguments'], arguments)
        self.assertEqual(dispatched['call']['input_reference_map'], plan['input_reference_map'])

    def test_each_input_requires_one_exact_hash_inspection_before_dispatch(self):
        base, refs = self.three_references()
        prepared = self.prepared(parent_version_id=base['version']['id'], references=[ref['id'] for ref in refs])
        attempt_id = prepared['attempt']['id']
        valid = self.inspections(self.bridge.preview(attempt_id))
        for index in range(3):
            missing = [entry for item, entry in enumerate(valid) if item != index]
            wrong = copy.deepcopy(valid)
            wrong[index]['sha256'] = '0' * 64
            for kind, inspections in (('missing', missing), ('wrong_hash', wrong)):
                with self.subTest(input_index=index, defect=kind):
                    with self.assertRaises(StateError):
                        self.bridge.dispatch(attempt_id, inspections=inspections)
                    self.assertEqual(self.used_units(), 0)
                    self.assertFalse(self.bridge._path(attempt_id, 'call.json').exists())
        self.assertEqual(self.bridge.dispatch(attempt_id, inspections=valid)['status'], 'dispatching')
        self.assertEqual(self.used_units(), 1)

    def test_brand_new_generation_omits_both_reference_fields_and_rejects_references(self):
        asset = self.engine.import_asset(self.paths[1])['asset']
        ref = self.reference(asset, 'lighting', 'soft light', 'food')
        try:
            rejected = self.bridge.prepare(self.work, self.auth, 'New soup image.',
                                           operation_type='generate', references=[ref['id']])
        except StateError:
            pass
        else:
            self.assertEqual(rejected['status'], 'blocked', 'Generation may not silently discard supplied references')
            self.assertEqual(rejected['attempt']['submission'], 'not_submitted')
        self.assertEqual(self.used_units(), 0)
        prepared = self.prepared(operation_type='generate')
        call = self.bridge.dispatch(prepared['attempt']['id'])['call']
        self.assertEqual(call['inputs'], [])
        self.assertEqual(call['input_reference_map'], [])
        self.assertNotIn('referenced_image_paths', call['arguments'])
        self.assertNotIn('num_last_images_to_include', call['arguments'])

    def test_select_candidate_two_edits_exact_selected_version_not_latest_three(self):
        group = self.engine.create_group([self.work, self.work, self.work], kind='candidates')
        versions = []
        for index, item_id in enumerate(group['item_ids']):
            prepared = self.prepared(group_id=group['id'], item_id=item_id, begin_context=False)
            versions.append(self.capture_and_consume(prepared, self.paths[index]))
        self.assertEqual(self.engine.inspect()['project']['works'][self.work]['latest_version_id'], versions[2])
        selected = self.engine.select_label('2', group_id=group['id'])
        self.assertEqual(selected['version_id'], versions[1])
        reopened = self.reopen()
        edited = reopened.prepare(self.work, self.auth, 'Warm only the light.', operation_type='generative_edit')
        self.assertEqual(edited['status'], 'prepared')
        self.assertEqual(edited['request']['parent_version_id'], versions[1])
        plan = reopened.preview(edited['attempt']['id'])
        project = self.engine.inspect()['project']
        selected_asset = project['versions'][versions[1]]['asset_id']
        self.assertEqual([entry['asset_id'] for entry in plan['inputs']], [selected_asset])
        self.assertNotEqual(selected_asset, project['versions'][versions[2]]['asset_id'])
        self.assertEqual(project['works'][self.work]['selected_version_id'], versions[1])

    def test_saved_v1_call_and_receipt_remain_consumable_with_exact_p2_prompt(self):
        base = self.engine.import_image(self.work, self.paths[0])
        legacy_capabilities = {
            'schema_version': 1, 'adapter_id': ADAPTER_ID, 'observed_at': '2026-10-28T00:00:00+00:00',
            'operations': {'generate': 'supported', 'generative_edit': 'supported'},
            'reference_input': {'carriers': {'status': 'known', 'value': ['local_file']},
                                'max_count': {'status': 'known', 'value': 1}, 'ordered': 'unknown'},
            'edit_modes': {'status': 'known', 'value': ['semantic_reference_edit']},
            'size_limits': {'status': 'unknown'},
            'output_limits': {'status': 'known', 'value': {'max_count': 1}},
            'cost_info': {'status': 'unknown'}, 'query': 'unsupported', 'cancel': 'unsupported',
            'interface_evidence': copy.deepcopy(INTERFACE),
        }
        prepared = self.engine.prepare(
            self.work, self.auth, ADAPTER_ID, legacy_capabilities,
            operation={'type': 'generative_edit', 'expected_outputs': 1, 'edit_mode': 'semantic_reference_edit'},
            parent_version_id=base['version']['id'],
            design={'native_prompt': 'Warm the old soup image.', 'transparent_background': False},
        )
        request, attempt = prepared['request'], prepared['attempt']
        self.assertNotIn('native_payload_version', request['design'])
        ref = request['references'][0]
        asset = base['asset']
        path = self.engine.store.project_path(asset['locator'])
        effective = [{'key': entry['key'], 'value': entry['value']} for entry in request['effective_requirements']]
        arguments = {
            'prompt': 'Create exactly one image.\nWarm the old soup image.'
                      '\nCurrent resolved requirements (authoritative over older design wording):\n'
                      + json.dumps(effective, ensure_ascii=False, sort_keys=True)
                      + '\nThe single attached image is the edit target; preserve its content except for the requested changes.',
            'transparent_background': False, 'referenced_image_paths': [str(path)],
        }
        mapping = [{'tool_index': 0, 'reference_id': ref['id'], 'asset_id': asset['id']}]
        call = {
            'schema_version': 1, 'tool_name': 'image_gen.imagegen', 'attempt_id': attempt['id'],
            'request_id': request['id'], 'arguments': arguments,
            'inputs': [{'asset_id': asset['id'], 'path': str(path), 'sha256': asset['sha256'],
                        'roles': copy.deepcopy(ref['roles'])}],
            'input_reference_map': mapping, 'payload_sha256': digest(arguments),
            'invocation_id': 'native_invocation_offline_legacy_fixture', 'created_at': '2026-10-28T00:00:00+00:00',
        }
        call['inspections'] = self.inspections(call)
        self.assertEqual(self.bridge.preview(attempt['id'])['arguments'], arguments)
        self.bridge._write(self.bridge._path(attempt['id'], 'call.json'), call)
        self.assertEqual(self.engine.dispatch(attempt['id'], input_reference_map=mapping)['status'], 'dispatching')
        self.bridge.capture_return(attempt['id'], call['invocation_id'], [str(self.paths[3])], copy.deepcopy(RETURN))
        self.paths[3].unlink()
        result = self.reopen().consume_receipt(attempt['id'], recovery=True)
        self.assertEqual(result['status'], 'succeeded')
        saved = self.engine.store.read_record('attempts', attempt['id'])
        self.assertEqual(len(saved['outputs']), 1)
        self.assertEqual(saved['outputs'][0]['version']['parent_version_id'], base['version']['id'])
        self.assertEqual(self.reopen()._read(self.bridge._path(attempt['id'], 'call.json')), call)

    def test_group_item_identity_and_begin_context_survive_bridge_dispatch_and_adoption(self):
        group = self.engine.create_group([self.work, self.work], kind='candidates')
        item_id = group['item_ids'][1]
        epoch = self.engine.inspect()['project']['interaction_epoch']
        prepared = self.prepared(group_id=group['id'], item_id=item_id, begin_context=False)
        self.assertEqual(prepared['request']['target'],
                         {'work_id': self.work, 'group_id': group['id'], 'item_id': item_id})
        self.assertEqual(prepared['request']['interaction_epoch'], epoch)
        version_id = self.capture_and_consume(prepared, self.paths[1])
        project = self.engine.inspect()['project']
        self.assertEqual(project['items'][item_id]['request_ids'], [prepared['request']['id']])
        self.assertEqual(project['items'][item_id]['version_ids'], [version_id])
        self.assertEqual(project['items'][group['item_ids'][0]]['version_ids'], [])
        self.assertEqual(project['groups'][group['id']]['display_labels']['2-1'], version_id)

    def test_more_than_three_total_references_is_rejected_without_dropping_inputs(self):
        base = self.engine.import_image(self.work, self.paths[0])
        refs = []
        for index in range(1, 4):
            asset = self.engine.import_asset(self.paths[index])['asset']
            refs.append(self.reference(asset, 'lighting', f'light-{index}', f'food-{index}'))
        try:
            result = self.bridge.prepare(self.work, self.auth, 'Edit using all supplied roles.',
                                         parent_version_id=base['version']['id'], references=[ref['id'] for ref in refs])
        except StateError:
            pass
        else:
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(len(result['request']['references']), 4,
                             'The failure must retain evidence for every requested input, including the base')
            self.assertEqual(result['attempt']['submission'], 'not_submitted')
            self.assertFalse(self.bridge._path(result['attempt']['id'], 'call.json').exists())
        self.assertEqual(self.used_units(), 0)


if __name__ == '__main__':
    unittest.main()
