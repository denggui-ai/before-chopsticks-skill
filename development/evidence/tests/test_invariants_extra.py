"""Independent P1 counterexamples derived from the v6 supplement.

These cases assert externally meaningful state invariants, allowing validation
to reject an action during preparation or at its final dispatch gate. Fixtures
are local images and declarations; no host or generation tool is invoked.
"""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/before-chopsticks/scripts"))

from bc_core.engine import ProjectEngine
from bc_core.storage import StateError


def offline_capabilities():
    return {
        "schema_version": 1, "adapter_id": "offline", "observed_at": "2026-10-29T00:00:00Z",
        "operations": {"generate": "supported", "generative_edit": "supported", "local_edit": "supported"},
        "reference_input": {"carriers": {"status": "known", "value": ["local_file"]},
                            "max_count": {"status": "known", "value": 3}, "ordered": "supported"},
        "edit_modes": {"status": "known", "value": ["global", "local"]},
        "size_limits": {"status": "known", "value": {}},
        "output_limits": {"status": "known", "value": {"max_count": 1}},
        "cost_info": {"status": "known", "value": {"channel": "offline"}},
        "query": "unsupported", "cancel": "unsupported",
    }


class AdditionalStateInvariants(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = ProjectEngine(self.root / "project")
        self.engine.initialize("Independent state regression")
        self.work = self.engine.create_work("番茄鸡蛋面")["id"]
        self.image = self.root / "fixture.png"
        Image.new("RGB", (9, 7), (40, 70, 90)).save(self.image)

    def tearDown(self):
        self.temp.cleanup()

    def authorize(self, *, work=None, operations=None, channel="offline", count=1):
        return self.engine.authorize(
            [work or self.work], operations or ["generate", "generative_edit"], count,
            retry_budget=0, paid_channel_scope=[channel], evidence={"message_id": "explicit-authorization"},
        )["id"]

    def prepare(self, grant=None, *, work=None, **kwargs):
        work = work or self.work
        return self.engine.prepare(
            work, authorization_id=grant or self.authorize(work=work),
            adapter="offline", capabilities=offline_capabilities(), **kwargs,
        )

    def output(self, attempt_id, *, verified=True, attributed_to=None):
        return {"output_index": 0, "path": str(self.image),
                "provenance": {"attempt_id": attributed_to or attempt_id,
                               "verified": verified, "source": "offline_fixture"}}

    def finish(self, prepared):
        attempt_id = prepared["attempt"]["id"]
        self.engine.dispatch(attempt_id)
        report = self.engine.observe(attempt_id, "succeeded", "submitted", {"fixture": "completed"},
                                     outputs=[self.output(attempt_id)])
        return report["project"]["works"][self.work]["latest_version_id"]

    def assert_denied_before_submission(self, action):
        with self.assertRaises(StateError):
            action()
        self.assertTrue(all(attempt["submission"] == "not_submitted"
                            for attempt in self.engine.inspect()["attempts"]))

    def test_authorization_cannot_be_spent_on_another_work(self):
        grant = self.authorize()
        other = self.engine.create_work("清蒸鱼")["id"]

        def unauthorized_action():
            prepared = self.prepare(grant, work=other)
            self.engine.dispatch(prepared["attempt"]["id"])

        self.assert_denied_before_submission(unauthorized_action)
        self.assertEqual(self.engine.inspect()["project"]["works"][other]["version_ids"], [])

    def test_generation_authorization_cannot_be_used_for_generative_edit(self):
        self.engine.import_image(self.work, str(self.image))
        grant = self.authorize(operations=["generate"])

        def unauthorized_action():
            prepared = self.prepare(grant, operation={"type": "generative_edit"})
            self.engine.dispatch(prepared["attempt"]["id"])

        self.assert_denied_before_submission(unauthorized_action)
        self.assertEqual(len(self.engine.inspect()["project"]["works"][self.work]["version_ids"]), 1)

    def test_authorization_for_another_channel_does_not_allow_offline_adapter(self):
        grant = self.authorize(channel="another-approved-channel")

        def unauthorized_action():
            prepared = self.prepare(grant)
            self.engine.dispatch(prepared["attempt"]["id"])

        self.assert_denied_before_submission(unauthorized_action)

    def test_rollback_uses_its_branch_then_applies_later_shared_requirement(self):
        self.engine.add_requirements([{"key": "temperature", "value": "cool"}],
                                     {"kind": "work", "id": self.work}, "shared-cool")
        initial = self.prepare()
        original_request = self.engine.store.record_path("requests", initial["request"]["id"])
        original_bytes = original_request.read_bytes()
        first_version = self.finish(initial)
        warm = self.prepare(changes=[{"key": "temperature", "value": "warm"}],
                            operation={"type": "generative_edit"})
        warm_version = self.finish(warm)
        self.assertNotEqual(warm_version, first_version)

        self.engine.set_edit_target(first_version)
        rollback = self.prepare(operation={"type": "generative_edit"})
        self.assertEqual(rollback["request"]["parent_version_id"], first_version)
        self.assertEqual({r["key"]: r["value"] for r in rollback["request"]["effective_requirements"]},
                         {"temperature": "cool"})

        self.engine.add_requirements([{"key": "temperature", "value": "neutral"}],
                                     {"kind": "work", "id": self.work}, "shared-neutral")
        refreshed = self.engine.dispatch(rollback["attempt"]["id"])
        self.assertEqual(refreshed["status"], "reprepared")
        self.assertEqual(refreshed["request"]["parent_version_id"], first_version)
        self.assertEqual({r["key"]: r["value"] for r in refreshed["request"]["effective_requirements"]},
                         {"temperature": "neutral"})
        self.assertEqual(refreshed["request"]["authorization_id"], rollback["request"]["authorization_id"])
        self.assertEqual(original_request.read_bytes(), original_bytes)
        self.assertEqual(self.engine.inspect()["project"]["works"][self.work]["edit_target_version_id"], first_version)

    def test_new_conflict_blocks_affected_work_without_blocking_independent_work(self):
        pending = self.prepare()
        self.engine.add_requirements([{"key": "temperature", "value": "warm"},
                                      {"key": "temperature", "value": "cool"}],
                                     {"kind": "work", "id": self.work}, "contradictory-turn")
        try:
            self.engine.dispatch(pending["attempt"]["id"])
        except StateError:
            pass
        blocked = self.engine.store.read_record("attempts", pending["attempt"]["id"])
        self.assertEqual(blocked["submission"], "not_submitted")
        self.assertNotIn(blocked["state"], ["dispatching", "accepted", "running", "succeeded"])

        other = self.engine.create_work("凉拌黄瓜")["id"]
        independent = self.prepare(work=other)
        dispatched = self.engine.dispatch(independent["attempt"]["id"])
        self.assertEqual(dispatched["attempt"]["state"], "dispatching")

    def test_unindexed_dispatching_attempt_is_recovered_and_still_consumes_budget(self):
        grant = self.authorize(count=1)
        prepared = self.prepare(grant)
        orphan = deepcopy(prepared["attempt"])
        orphan["id"] = "attempt_unindexed_dispatch"
        orphan["state"] = "dispatching"
        orphan["submission"] = "uncertain"
        orphan["dispatch"]["submission"] = "uncertain"
        self.engine.store.write_record("attempts", orphan["id"], orphan, immutable=True)
        self.assertNotIn(orphan["id"], self.engine.store.load()["attempt_ids"])

        recovered = self.engine.recover()
        orphan_after = next(attempt for attempt in recovered["attempts"] if attempt["id"] == orphan["id"])
        self.assertEqual(orphan_after["state"], "unknown")
        self.assertIn(orphan["id"], recovered["unresolved_attempt_ids"])
        with self.assertRaisesRegex(StateError, "budget"):
            self.engine.dispatch(prepared["attempt"]["id"])
        self.assertEqual(self.engine.store.read_record("attempts", prepared["attempt"]["id"])["submission"],
                         "not_submitted")

    def test_changed_reference_bytes_do_not_pass_as_the_original_verified_asset(self):
        imported = self.engine.import_image(self.work, str(self.image))
        reference = self.engine.add_reference(imported["version"]["asset_id"], [{
            "role": "lighting", "borrow": "window light", "exclude": ["subject"],
            "strength": "required", "source": {"message_id": "reference-authorization"},
        }])
        prepared = self.prepare(references=[reference["id"]])
        stored_image = self.engine.store.project_path(imported["asset"]["locator"])
        Image.new("RGB", (9, 7), (180, 20, 10)).save(stored_image)
        try:
            self.engine.dispatch(prepared["attempt"]["id"])
        except StateError:
            pass
        observed = self.engine.store.read_record("attempts", prepared["attempt"]["id"])
        self.assertEqual(observed["submission"], "not_submitted")
        self.assertNotIn(observed["state"], ["dispatching", "accepted", "running", "succeeded"])

    def test_output_without_verified_provenance_cannot_create_a_version(self):
        self.assert_untrusted_output_rejected(verified=False)

    def test_output_attributed_to_another_attempt_cannot_create_a_version(self):
        self.assert_untrusted_output_rejected(attributed_to="attempt_someone_else")

    def assert_untrusted_output_rejected(self, **provenance):
        prepared = self.prepare()
        attempt_id = prepared["attempt"]["id"]
        self.engine.dispatch(attempt_id)
        try:
            self.engine.observe(attempt_id, "succeeded", "submitted", {"fixture": "untrusted-output"},
                                outputs=[self.output(attempt_id, **provenance)])
        except ValueError:
            pass
        current = self.engine.inspect()
        self.assertEqual(current["project"]["works"][self.work]["version_ids"], [])
        self.assertIsNone(current["project"]["works"][self.work]["latest_version_id"])
        attempt = self.engine.store.read_record("attempts", attempt_id)
        self.assertNotEqual(attempt["state"], "succeeded")
        self.assertEqual(attempt["outputs"], [])

    def test_empty_completion_evidence_cannot_replace_known_submission_with_success(self):
        prepared = self.prepare()
        attempt_id = prepared["attempt"]["id"]
        self.engine.dispatch(attempt_id)
        self.engine.observe(attempt_id, "accepted", "submitted", {"receipt": "accepted-by-fixture"})
        result = self.engine.observe(attempt_id, "succeeded", "submitted", {}, outputs=[self.output(attempt_id)])
        self.assertEqual(result["attempt"]["state"], "unknown")
        self.assertEqual(result["attempt"]["submission"], "submitted")
        self.assertIn(attempt_id, self.engine.recover()["unresolved_attempt_ids"])


if __name__ == "__main__":
    unittest.main()
