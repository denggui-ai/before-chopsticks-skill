"""Engine behaviours the photo-entry rules depend on (requirement sources and provenance)."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/before-chopsticks/scripts"))

from bc_core.requirements import inherit_for_new_subject, resolve_requirements


PROVENANCE = "画面来源"


def record(identifier, key, value, revision, kind, *, scope_kind="branch", scope_id="request-local"):
    return {
        "id": identifier, "key": key, "value": value,
        "source": {"kind": kind, "message_id": f"message-{revision}"},
        "scope": {"kind": scope_kind, "id": scope_id}, "revision": revision,
        "dependencies": [], "supersedes": [],
    }


def project(*requirements):
    return {
        "id": "p", "works": {"w": {"id": "w"}, "w2": {"id": "w2"}},
        "groups": {}, "items": {},
        "requirements": {r["id"]: r for r in requirements},
        "versions": {},
    }


def add_version(state, version_id, *effective, work_id="w"):
    state["versions"][version_id] = {
        "id": version_id, "work_id": work_id,
        "effective_requirements": deepcopy(list(effective)),
    }
    return state


def effective(result):
    return {r["key"]: (r["value"], r["source"]["kind"]) for r in result["effective_requirements"]}


class IdentityPreservationSourceTests(unittest.TestCase):
    """R1.25: visible identity items must be recorded so style expansion cannot replace them."""

    def test_implied_preservation_beats_same_turn_style_expansion(self):
        keep = record("keep", "器皿", "保留原图蓝碗", 2, "user_implied_edit_preservation")
        style = record("style", "器皿", "白瓷盘", 2, "ai_design")
        result = resolve_requirements(project(), "w", changes=[keep, style])
        self.assertEqual(effective(result)["器皿"], ("保留原图蓝碗", "user_implied_edit_preservation"))
        self.assertFalse(result["conflicts"])

    def test_default_source_would_lose_to_style_expansion(self):
        keep = record("keep", "器皿", "保留原图蓝碗", 2, "default")
        style = record("style", "器皿", "白瓷盘", 2, "ai_design")
        result = resolve_requirements(project(), "w", changes=[keep, style])
        self.assertEqual(effective(result)["器皿"], ("白瓷盘", "ai_design"))


class ReferenceFeelSourceTests(unittest.TestCase):
    """R14.06 (T1): reference feel must not be recorded as user_reference on identity keys."""

    def test_user_reference_record_would_override_identity_preservation(self):
        keep = record("keep", "台面", "保留原图浅色木桌", 2, "user_implied_edit_preservation")
        borrowed = record("borrowed", "台面", "参考图的深色木台面", 2, "user_reference")
        result = resolve_requirements(project(), "w", changes=[keep, borrowed])
        self.assertEqual(effective(result)["台面"], ("参考图的深色木台面", "user_reference"))

    def test_reference_feel_recorded_as_ai_design_keeps_identity(self):
        keep = record("keep", "台面", "保留原图浅色木桌", 2, "user_implied_edit_preservation")
        borrowed = record("borrowed", "台面", "参考图的深色木台面", 2, "ai_design")
        result = resolve_requirements(project(), "w", changes=[keep, borrowed])
        self.assertEqual(effective(result)["台面"], ("保留原图浅色木桌", "user_implied_edit_preservation"))


class ProvenanceKeyTests(unittest.TestCase):
    """R1.26: the skill-only 画面来源 key under override, conflict, branch return and Work change."""

    def test_higher_source_on_same_key_replaces_provenance(self):
        ours = record("ours", PROVENANCE, "用户照片", 2, "ai_design")
        other = record("other", PROVENANCE, "设计画面", 2, "user_explicit")
        result = resolve_requirements(project(), "w", changes=[ours, other])
        self.assertEqual(effective(result)[PROVENANCE], ("设计画面", "user_explicit"))

    def test_two_values_in_one_revision_conflict_and_leave_no_provenance(self):
        first = record("first", PROVENANCE, "用户照片", 2, "ai_design")
        second = record("second", PROVENANCE, "设计画面", 2, "ai_design")
        result = resolve_requirements(project(), "w", changes=[first, second])
        self.assertNotIn(PROVENANCE, effective(result))
        self.assertEqual([c["key"] for c in result["conflicts"]], [PROVENANCE])

    def test_returning_to_old_branch_reads_that_versions_provenance(self):
        old = record("old", PROVENANCE, "用户照片", 2, "ai_design")
        new = record("new", PROVENANCE, "设计画面", 4, "ai_design")
        state = add_version(add_version(project(), "v1", old), "v2", new)
        result = resolve_requirements(state, "w", parent_version_id="v1")
        self.assertEqual(effective(result)[PROVENANCE], ("用户照片", "ai_design"))

    def test_new_work_without_borrowing_the_key_has_no_provenance(self):
        prov = record("prov", PROVENANCE, "用户照片", 2, "ai_design")
        light = record("light", "光线", "柔和侧窗光", 2, "user_explicit")
        inherited = inherit_for_new_subject([prov, light], ["光线"])
        self.assertEqual([r["key"] for r in inherited], ["光线"])


if __name__ == "__main__":
    unittest.main()
