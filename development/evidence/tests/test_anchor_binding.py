"""Anchor regressions: synthetic images/offline bridge only, no native call."""
import unittest
from pathlib import Path
from PIL import Image
import test_engine
from bc_core.codex_bridge import CodexBridge, ADAPTER_ID
from bc_core.storage import StateError


class AnchorBindingTests(unittest.TestCase):
    setUp = test_engine.EngineTests.setUp
    tearDown = test_engine.EngineTests.tearDown

    def fixtures(self):
        self.a = self.engine.import_image(self.work, self.input)['version']
        other = self.root / 'other.png'
        Image.new('RGB', (12, 10), (180, 130, 90)).save(other)
        self.b = self.engine.import_image(self.work, other)['version']
        self.group = self.engine.create_group([self.work] * 3, kind='visual_series')
        self.item = self.group['item_ids'][1]
        self.engine.set_series_anchor(self.group['id'], self.a['id'], ['lighting'])
        self.ref = self.engine.add_reference(self.a['asset_id'], [
            {'role': 'lighting', 'borrow': 'light direction', 'exclude': ['food'],
             'strength': 'required', 'source': {'kind': 'ai_design'}}],
            scope={'kind': 'group', 'id': self.group['id']})
        self.authid = self.engine.authorize([self.work], ['generative_edit'], 2,
            paid_channel_scope=[ADAPTER_ID], evidence={'fixture': 'offline only'})['id']
        self.bridge = CodexBridge(self.root / 'project', {'source': 'offline test'})

    def prepare(self, refs=()):
        return self.bridge.prepare(self.work, self.authid, 'Keep dish; change named light only',
            parent_version_id=self.b['id'], group_id=self.group['id'], item_id=self.item,
            references=list(refs))

    def dispatch(self, prepared):
        plan = self.bridge.preview(prepared['attempt']['id'])
        return self.bridge.dispatch(prepared['attempt']['id'], inspections=[
            {'asset_id': x['asset_id'], 'sha256': x['sha256'],
             'evidence': {'fixture': 'synthetic offline; not a real visual inspection'}}
            for x in {entry['asset_id']: entry for entry in plan['inputs']}.values()])

    def test_independent_missing_anchor_does_not_block(self):
        self.fixtures()
        project = self.engine.inspect()['project']
        (self.root / 'project' / project['assets'][self.a['asset_id']]['locator']).unlink()
        self.assertEqual(self.prepare()['status'], 'prepared')

    def test_follow_anchor_changes_actual_native_input_without_rewriting_old_request(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [self.ref['id']])
        p = self.prepare([self.ref['id']])
        record = self.engine.store.record_path('requests', p['request']['id'])
        before = record.read_bytes()
        self.engine.set_series_anchor(self.group['id'], self.b['id'], ['lighting'])
        result = self.dispatch(p)
        self.assertEqual(result['status'], 'reprepared')
        plan = self.bridge.preview(result['attempt']['id'])
        self.assertEqual([x['asset_id'] for x in plan['inputs']], [self.b['asset_id']] * 2)
        self.assertEqual(record.read_bytes(), before)
        self.assertEqual(result['request']['authorization_id'], self.authid)
        self.assertEqual(self.dispatch(result)['status'], 'dispatching')

    def test_ordinary_reference_and_independent_item_do_not_follow_group_anchor(self):
        self.fixtures()
        p = self.prepare([self.ref['id']])
        self.engine.set_series_anchor(self.group['id'], self.b['id'], ['lighting'])
        out = self.dispatch(p)
        self.assertEqual(out['status'], 'dispatching')
        self.assertEqual(out['attempt']['id'], p['attempt']['id'])
        self.assertEqual(out['call']['inputs'][1]['asset_id'], self.a['asset_id'])

    def test_fixed_anchor_does_not_follow(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [self.ref['id']], follow=False)
        p = self.prepare([self.ref['id']])
        self.engine.set_series_anchor(self.group['id'], self.b['id'], ['lighting'])
        self.assertEqual(self.dispatch(p)['status'], 'dispatching')

    def test_bound_missing_anchor_blocks_only_dependent_item(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [self.ref['id']])
        project = self.engine.inspect()['project']
        (self.root / 'project' / project['assets'][self.a['asset_id']]['locator']).unlink()
        self.assertEqual(self.prepare([self.ref['id']])['status'], 'blocked')
        self.item = self.group['item_ids'][2]
        self.assertEqual(self.prepare()['status'], 'prepared')

    def test_legacy_item_requires_explicit_binding_decision(self):
        self.fixtures()
        with self.engine.store.lock():
            p = self.engine._load()
            p['items'][self.item].pop('anchor_binding', None)
            self.engine._commit(p)
        result = self.prepare([self.ref['id']])
        self.assertEqual(result['status'], 'blocked')
        self.engine.set_item_anchor(self.group['id'], self.item, None)
        self.assertEqual(self.prepare([self.ref['id']])['status'], 'prepared')

    def test_submitted_anchor_snapshot_frozen_and_never_redispatched(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [self.ref['id']])
        p = self.prepare([self.ref['id']]); self.dispatch(p)
        self.engine.set_series_anchor(self.group['id'], self.b['id'], ['lighting'])
        self.engine.recover()
        req = self.engine.store.read_record('requests', p['request']['id'])
        self.assertEqual(req['prepared_from']['anchor']['version_id'], self.a['id'])
        with self.assertRaises(StateError): self.dispatch(p)
        self.assertEqual(len(self.engine.store.list_records('attempts')), 1)

    def test_binding_rejects_base_or_mismatched_reference(self):
        self.fixtures()
        wrong = self.engine.add_reference(self.b['asset_id'], self.ref['roles'])
        with self.assertRaises(StateError):
            self.engine.set_item_anchor(self.group['id'], self.item, [wrong['id']])
        base = self.engine.add_reference(self.a['asset_id'], [
            {**self.ref['roles'][0], 'role': 'base_image'}])
        with self.assertRaises(StateError):
            self.engine.set_item_anchor(self.group['id'], self.item, [base['id']])

    def test_bound_reference_cannot_be_omitted_and_missing_ordinary_ref_still_blocks(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [self.ref['id']])
        self.assertEqual(self.prepare()['status'], 'blocked')
        self.engine.set_item_anchor(self.group['id'], self.item, None)
        project = self.engine.inspect()['project']
        (self.root / 'project' / project['assets'][self.a['asset_id']]['locator']).unlink()
        self.assertEqual(self.prepare([self.ref['id']])['status'], 'blocked')

    def test_anchor_role_change_blocks_instead_of_silently_reinterpreting_reference(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [self.ref['id']])
        p = self.prepare([self.ref['id']])
        self.engine.set_series_anchor(self.group['id'], self.b['id'], ['color'])
        self.assertEqual(self.dispatch(p)['status'], 'blocked')

    def test_style_only_narrative_anchor_borrows_light_and_color_without_material(self):
        self.fixtures()
        group = self.engine.create_group([self.work] * 2, kind='narrative_series')
        item = group['item_ids'][1]
        self.engine.set_series_anchor(group['id'], self.a['id'], ['lighting', 'color'])
        roles = [
            {'role': 'lighting', 'borrow': 'soft warm light character',
             'exclude': ['light direction', 'tabletop', 'props', 'camera angle', 'food'],
             'strength': 'preferred', 'source': {'kind': 'ai_design'}},
            {'role': 'color', 'borrow': 'neutral warm color tendency',
             'exclude': ['tabletop', 'props', 'food'],
             'strength': 'preferred', 'source': {'kind': 'ai_design'}},
        ]
        ref = self.engine.add_reference(self.a['asset_id'], roles, scope={'kind': 'group', 'id': group['id']})
        self.engine.set_item_anchor(group['id'], item, [ref['id']])
        prepared = self.bridge.prepare(self.work, self.authid, 'Redesign this stage independently.',
            parent_version_id=self.b['id'], group_id=group['id'], item_id=item, references=[ref['id']])
        plan = self.bridge.preview(prepared['attempt']['id'])
        self.assertEqual(len(plan['inputs']), 2)
        anchor = prepared['request']['references'][-1]
        self.assertEqual(anchor['roles'], roles)
        self.assertEqual(plan['inputs'][-1]['asset_id'], self.a['asset_id'])
        material = self.engine.add_reference(self.a['asset_id'], [
            {**roles[0], 'role': 'material', 'borrow': 'tabletop'}], scope={'kind': 'group', 'id': group['id']})
        with self.assertRaises(StateError):
            self.engine.set_item_anchor(group['id'], item, [material['id']])

    def test_story_design_and_scoped_scene_reach_same_scene_and_transition_requests(self):
        self.fixtures()
        group = self.engine.create_group([self.work] * 3, kind='narrative_series')
        same_scene, transition = group['item_ids'][1:]
        story = 'Prepare at the kitchen counter, then carry the meal to a dining table.'
        self.engine.add_requirements(
            [{'key': '组级故事设计', 'value': story, 'source': {'kind': 'ai_design'}}],
            {'kind': 'group', 'id': group['id']}, message_id='offline-story-plan')
        self.engine.add_requirements(
            [{'key': '厨房连续条件', 'value': 'left window and charcoal counter',
              'source': {'kind': 'ai_design'}}],
            {'kind': 'group', 'id': group['id'], 'item_ids': [same_scene]},
            message_id='offline-story-plan')
        self.engine.set_series_anchor(group['id'], self.a['id'], ['continuity', 'color'])
        scene_roles = [{'role': 'continuity',
                        'borrow': 'left window, charcoal counter, cream sleeves',
                        'exclude': ['old pose', 'old camera angle', 'finished food'],
                        'strength': 'required', 'source': {'kind': 'ai_design'}}]
        transition_roles = [{'role': 'color', 'borrow': 'restrained warm palette',
                             'exclude': ['kitchen counter', 'kitchen layout', 'old pose', 'food'],
                             'strength': 'preferred', 'source': {'kind': 'ai_design'}}]
        references = {}
        for item, roles in ((same_scene, scene_roles), (transition, transition_roles)):
            ref = self.engine.add_reference(self.a['asset_id'], roles,
                scope={'kind': 'item', 'id': item})
            references[item] = ref['id']
            self.engine.set_item_anchor(group['id'], item, [ref['id']])
        base = self.engine.add_reference(self.b['asset_id'], [{
            'role': 'base_image', 'borrow': 'handmade open buns with broad folds',
            'exclude': ['original camera angle', 'original table'], 'strength': 'required',
            'source': {'kind': 'user_implied_edit_preservation'}}],
            scope={'kind': 'group', 'id': group['id']})
        for item, task, roles in (
                (same_scene, 'Hands finish folding at the same kitchen counter.', scene_roles),
                (transition, 'A hand sets the plate on the new dining table.', transition_roles)):
            prepared = self.bridge.prepare(self.work, self.authid, story + ' ' + task,
                parent_version_id=self.b['id'], group_id=group['id'], item_id=item,
                references=[base['id'], references[item]], input_policy='explicit_scope',
                changes=[{'key': '本张叙事任务', 'value': task, 'source': {'kind': 'ai_design'}}])
            self.assertEqual(prepared['status'], 'prepared')
            effective = {r['key']: r['value'] for r in prepared['request']['effective_requirements']}
            self.assertEqual(effective['组级故事设计'], story)
            self.assertEqual(effective['本张叙事任务'], task)
            self.assertEqual('厨房连续条件' in effective, item == same_scene)
            preview = self.bridge.preview(prepared['attempt']['id'])
            self.assertIn(story, preview['arguments']['prompt'])
            self.assertIn(task, preview['arguments']['prompt'])
            self.assertEqual(preview['inputs'][0]['asset_id'], self.b['asset_id'])
            self.assertEqual(preview['inputs'][1]['asset_id'], self.a['asset_id'])
            self.assertEqual(preview['inputs'][1]['roles'], roles)
            self.assertEqual(prepared['attempt']['submission'], 'not_submitted')

    def test_text_generation_can_use_logical_anchor_without_sending_images(self):
        self.fixtures()
        self.engine.set_item_anchor(self.group['id'], self.item, [])
        auth = self.engine.authorize([self.work], ['generate'], 1,
            paid_channel_scope=[ADAPTER_ID], evidence={'fixture': 'offline text fallback'})['id']
        prepared = self.bridge.prepare(self.work, auth, 'Rebuild by dish name; soft warm light.',
            operation_type='generate', group_id=self.group['id'], item_id=self.item)
        self.assertEqual(prepared['status'], 'prepared')
        self.assertIsNone(prepared['request']['parent_version_id'])
        self.assertEqual(prepared['request']['references'], [])
        plan = self.bridge.preview(prepared['attempt']['id'])
        self.assertEqual(plan['inputs'], [])
        self.assertNotIn('referenced_image_paths', plan['arguments'])
        self.assertNotIn('num_last_images_to_include', plan['arguments'])
