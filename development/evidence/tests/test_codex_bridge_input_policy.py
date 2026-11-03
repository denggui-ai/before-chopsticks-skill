"""Input-scope contracts using synthetic local images; no model/network calls."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'runtime/before-chopsticks/scripts'))

from bc_core.codex_bridge import CodexBridge, native_capabilities
from bc_core.engine import ProjectEngine
from bc_core.storage import StateError, digest


ADAPTER = 'codex-native-imagegen-v1'
INTERFACE = {'source': 'offline input-policy fixture, not live interface evidence'}
RETURN = {'tool_name': 'image_gen.imagegen', 'kind': 'native_tool_return', 'status': 'completed',
          'metadata': {'fixture': 'synthetic local output, no image generation'}}


class InputPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = ProjectEngine(self.root / 'project')
        self.engine.initialize('Offline input policy')
        self.work = self.engine.create_work('handmade bun')['id']
        self.auth = self.engine.authorize(
            [self.work], ['generate', 'generative_edit'], requested_count=8, retry_budget=0,
            paid_channel_scope=[ADAPTER], evidence={'fixture': 'local synthetic contracts'},
        )['id']
        self.paths = []
        for n, color in enumerate(((20, 40, 60), (90, 100, 110), (150, 160, 170))):
            path = self.root / f'{n}.png'
            Image.new('RGB', (5, 4), color).save(path)
            self.paths.append(path)
        self.base = self.engine.import_image(self.work, self.paths[0])
        self.other = self.engine.import_asset(self.paths[1])['asset']
        self.bridge = CodexBridge(self.root / 'project', interface_evidence=INTERFACE)

    def reference(self, *, asset=None, role='base_image', borrow='white skin, open top, coarse folds',
                  exclude=None):
        return self.engine.add_reference(
            (asset or self.base['asset'])['id'],
            [{'role': role, 'borrow': borrow,
              'exclude': ['original framing', 'original table'] if exclude is None else exclude,
              'strength': 'required', 'source': {'kind': 'ai_design'}}],
            scope={'kind': 'work', 'id': self.work},
        )['id']

    def prepare(self, **kwargs):
        kwargs.setdefault('parent_version_id', self.base['version']['id'])
        return self.bridge.prepare(self.work, self.auth, 'Use a low camera and a new table.', **kwargs)

    def assert_rejected_without_attempt(self, **kwargs):
        before = self.engine.inspect()
        with self.assertRaises(StateError):
            self.prepare(**kwargs)
        after = self.engine.inspect()
        self.assertEqual(after, before, 'Rejected preparation must not write state or reserve an attempt')

    def inspections(self, plan):
        return [{'asset_id': entry['asset_id'], 'sha256': entry['sha256'],
                 'evidence': {'fixture': 'synthetic image inspection'}} for entry in plan['inputs']]

    def test_default_is_versioned_legacy_edit_without_restricting_requested_changes(self):
        prepared = self.prepare()
        self.assertEqual(prepared['request']['design']['native_payload_version'], 3)
        self.assertEqual(prepared['request']['design']['input_policy'], 'legacy_edit')
        plan = self.bridge.preview(prepared['attempt']['id'])
        self.assertIn('Use a low camera and a new table.', plan['arguments']['prompt'])
        self.assertIn('preserve content except the requested changes.', plan['arguments']['prompt'])
        self.assertIn('reference_id', plan['inputs'][0])

    def test_explicit_scope_survives_preview_dispatch_and_returned_copy_changes(self):
        light = self.reference(asset=self.other, role='lighting', borrow='soft light', exclude=['food'])
        base = self.reference()
        prepared = self.prepare(
            input_policy='explicit_scope', references=[light, base],
            changes=[{'key': 'locked vessel', 'value': 'retain the blue cup',
                      'source': {'kind': 'user_explicit'}}],
        )
        attempt = prepared['attempt']['id']
        prepared['request']['design']['input_policy'] = 'legacy_edit'
        prepared['request']['references'][1]['roles'][0]['borrow'] = 'POISON'
        plan = self.bridge.preview(attempt)
        prompt = plan['arguments']['prompt']
        self.assertIn('white skin, open top, coarse folds', prompt)
        self.assertIn('original framing', prompt)
        self.assertIn('retain the blue cup', prompt)
        self.assertIn('do not inherit', prompt)
        self.assertIn('not bans on objects', prompt)
        self.assertNotIn('preserve content except', prompt)
        self.assertNotIn('POISON', prompt)
        self.assertEqual([x['reference_id'] for x in plan['inputs']], [light, base])
        call = self.bridge.dispatch(attempt, self.inspections(plan))['call']
        self.assertEqual(call['arguments'], plan['arguments'])
        self.assertEqual([x['reference_id'] for x in call['input_reference_map']], [light, base])
        self.bridge.capture_return(attempt, call['invocation_id'], [str(self.paths[2])], RETURN)
        self.paths[2].unlink()
        reopened = CodexBridge(self.root / 'project', interface_evidence=INTERFACE)
        self.assertEqual(reopened.consume_receipt(attempt, recovery=True)['status'], 'succeeded')
        self.assertEqual(reopened._read(reopened._path(attempt, 'call.json')), call)

    def test_explicit_base_does_not_implicitly_select_explicit_scope(self):
        ref = self.reference()
        default = self.prepare(references=[ref])
        explicit_legacy = self.prepare(references=[ref], input_policy='legacy_edit')
        a = self.bridge.preview(default['attempt']['id'])['arguments']
        b = self.bridge.preview(explicit_legacy['attempt']['id'])['arguments']
        self.assertEqual(a, b)
        self.assertIn('preserve content except the requested changes.', a['prompt'])

    def test_invalid_policies_are_rejected_without_attempt(self):
        for policy in ('', 'creative', None, [], {}):
            with self.subTest(policy=policy):
                self.assert_rejected_without_attempt(input_policy=policy)

    def test_explicit_scope_rejects_generate_even_without_references(self):
        self.assert_rejected_without_attempt(input_policy='explicit_scope', operation_type='generate',
                                             parent_version_id=None)

    def test_explicit_scope_requires_exactly_one_explicit_parent_base(self):
        first = self.reference()
        second = self.reference()
        wrong = self.reference(asset=self.other)
        light = self.reference(asset=self.other, role='lighting')
        for refs in ([], [light], [first, first], [first, second], [wrong]):
            with self.subTest(refs=refs):
                self.assert_rejected_without_attempt(input_policy='explicit_scope', references=refs)

    def test_explicit_scope_requires_nonempty_textual_borrow_and_exclusions(self):
        cases = [('', ['framing']), ('  ', ['framing']), ([], ['framing']),
                 ('editable image', ['framing']), ('  EdItAbLe ImAgE  ', ['framing']),
                 ('bun identity', []), ('bun identity', ['']), ('bun identity', ['  ']),
                 ('bun identity', [3])]
        for borrow, exclude in cases:
            with self.subTest(borrow=borrow, exclude=exclude):
                ref = self.reference(borrow=borrow, exclude=exclude)
                self.assert_rejected_without_attempt(input_policy='explicit_scope', references=[ref])

    def test_explicit_scope_can_use_selected_parent_when_operation_is_explicit(self):
        ref = self.reference()
        prepared = self.prepare(input_policy='explicit_scope', operation_type='generative_edit',
                                parent_version_id=None, references=[ref])
        self.assertEqual(prepared['request']['parent_version_id'], self.base['version']['id'])
        self.assertEqual(prepared['status'], 'prepared')

    def test_legacy_v1_and_v2_calls_and_receipts_remain_frozen(self):
        for version in (1, 2):
            with self.subTest(version=version):
                design = {'native_prompt': 'Warm the bun.', 'transparent_background': False}
                if version == 2:
                    design['native_payload_version'] = 2
                prepared = self.engine.prepare(
                    self.work, self.auth, ADAPTER, native_capabilities(INTERFACE),
                    operation={'type': 'generative_edit', 'expected_outputs': 1,
                               'edit_mode': 'semantic_reference_edit'},
                    parent_version_id=self.base['version']['id'], design=design,
                )
                request, attempt = prepared['request'], prepared['attempt']
                reference = request['references'][0]
                prompt = ('Create exactly one image.\nWarm the bun.\n'
                          'Current resolved requirements (authoritative over older design wording):\n[]')
                if version == 1:
                    prompt += ('\nThe single attached image is the edit target; '
                               'preserve its content except for the requested changes.')
                else:
                    prompt += ('\nImage roles in the exact attached order. Borrow only the named attributes; '
                               'exclusions must not be carried into the output.\n'
                               'Image 1: main edit target; preserve content except the requested changes.\n'
                               '{"borrow": "editable image", "exclude": [], "role": "base_image", '
                               '"strength": "required"}')
                asset = self.base['asset']
                path = str(self.engine.store.project_path(asset['locator']))
                arguments = {'prompt': prompt, 'transparent_background': False,
                             'referenced_image_paths': [path]}
                entry = {'asset_id': asset['id'], 'path': path, 'sha256': asset['sha256'],
                         'roles': copy.deepcopy(reference['roles'])}
                if version == 2:
                    entry['reference_id'] = reference['id']
                mapping = [{'tool_index': 0, 'reference_id': reference['id'], 'asset_id': asset['id']}]
                call = {'schema_version': 1, 'tool_name': 'image_gen.imagegen',
                        'attempt_id': attempt['id'], 'request_id': request['id'],
                        'arguments': arguments, 'inputs': [entry], 'input_reference_map': mapping,
                        'payload_sha256': digest(arguments), 'invocation_id': f'native_fixture_v{version}',
                        'created_at': '2026-10-31T00:00:00+00:00'}
                call['inspections'] = self.inspections(call)
                self.assertEqual(self.bridge.preview(attempt['id'])['arguments'], arguments)
                self.bridge._write(self.bridge._path(attempt['id'], 'call.json'), call)
                self.engine.dispatch(attempt['id'], input_reference_map=mapping)
                self.bridge.capture_return(attempt['id'], call['invocation_id'], [str(self.paths[2])], RETURN)
                reopened = CodexBridge(self.root / 'project', interface_evidence=INTERFACE)
                self.assertEqual(reopened.consume_receipt(attempt['id'], recovery=True)['status'], 'succeeded')
                self.assertEqual(reopened._read(reopened._path(attempt['id'], 'call.json')), call)


if __name__ == '__main__':
    unittest.main()
