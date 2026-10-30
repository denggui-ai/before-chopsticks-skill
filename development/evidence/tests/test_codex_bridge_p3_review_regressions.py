"""Two independently reproduced P3 bridge boundaries; offline fixtures only."""

import unittest

import test_codex_bridge_p3 as fixtures
from bc_core.storage import StateError


class P3BridgeReviewReproductions(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexBridgeP3Tests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_legacy_call_saved_before_dispatch_resumes_without_rewriting_journal(self):
        fixture = self.fixture
        # The baseline receipt test writes the exact frozen P2 call. Here route
        # its initial dispatch through the public bridge, as a restarted host
        # must do after call.json was saved but dispatch was not yet recorded.
        def resumed_dispatch(attempt_id, **unused):
            bridge = fixture.reopen()
            return bridge.dispatch(attempt_id, inspections=fixture.inspections(bridge.preview(attempt_id)))
        fixture.engine.dispatch = resumed_dispatch
        fixture.test_saved_v1_call_and_receipt_remain_consumable_with_exact_p2_prompt()

    def test_implicit_parent_cannot_dispatch_stale_selection_after_intervening_choice(self):
        fixture = self.fixture
        old = fixture.engine.import_image(fixture.work, fixture.paths[0])['version']['id']
        chosen = fixture.engine.import_image(fixture.work, fixture.paths[1])['version']['id']
        fixture.engine.select_version(old)
        original_inspect = fixture.bridge.engine.inspect

        def inspection_with_intervening_user_choice():
            snapshot = original_inspect()
            # Deterministic scheduling of another legal writer exactly between
            # bridge.inspect and engine.prepare; no immutable state is altered.
            fixture.engine.select_version(chosen)
            return snapshot

        fixture.bridge.engine.inspect = inspection_with_intervening_user_choice
        try:
            prepared = fixture.bridge.prepare(fixture.work, fixture.auth, 'Warm the selected image.',
                                              operation_type='generative_edit')
            attempt_id = prepared['attempt']['id']
            plan = fixture.bridge.preview(attempt_id)
            result = fixture.bridge.dispatch(attempt_id, inspections=fixture.inspections(plan))
        except StateError:
            return  # Refusing a stale implicit choice is also valid.
        if result['status'] == 'dispatching':
            selected = fixture.engine.inspect()['project']['works'][fixture.work]['selected_version_id']
            self.assertEqual(prepared['request']['parent_version_id'], selected,
                             'Stale implicit parent must be blocked or re-resolved before releasing a native call')


if __name__ == '__main__':
    unittest.main()
