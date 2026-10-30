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

