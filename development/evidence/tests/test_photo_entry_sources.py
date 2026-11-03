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
    """Visible identity items must be recorded so style expansion cannot replace them."""

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
    """Reference feel must not be recorded as user_reference on identity keys."""

    def test_user_reference_record_would_override_identity_preservation(self):
        keep = record("keep", "器皿", "保留原图蓝碗", 2, "user_implied_edit_preservation")
        borrowed = record("borrowed", "器皿", "参考图的白碗", 2, "user_reference")
        result = resolve_requirements(project(), "w", changes=[keep, borrowed])
        self.assertEqual(effective(result)["器皿"], ("参考图的白碗", "user_reference"))

    def test_reference_feel_recorded_as_ai_design_keeps_identity(self):
        keep = record("keep", "器皿", "保留原图蓝碗", 2, "user_implied_edit_preservation")
        borrowed = record("borrowed", "器皿", "参考图的白碗", 2, "ai_design")
        result = resolve_requirements(project(), "w", changes=[keep, borrowed])
        self.assertEqual(effective(result)["器皿"], ("保留原图蓝碗", "user_implied_edit_preservation"))

    def test_creative_switch_releases_implied_vessel_but_keeps_explicit_dish_lock(self):
        vessel = record("vessel", "器皿", "保留蓝碗", 1, "user_implied_edit_preservation")
        dish = record("dish", "菜品", "保留原菜形态", 1, "user_explicit")
        state = add_version(project(), "retouched", vessel, dish)
        mode = record("mode", "模式", "创作", 2, "user_explicit")
        ai_vessel = record("ai-vessel", "器皿", "灰陶盘", 2, "ai_design")
        unchanged = resolve_requirements(state, "w", parent_version_id="retouched", changes=[mode, ai_vessel])
        self.assertEqual(effective(unchanged)["器皿"][0], "保留蓝碗")
        release = record("release", "器皿", "允许按故事更换器皿", 2, "user_explicit")
        design = record("design", "器皿设计", "灰陶盘", 2, "ai_design")
        changed = resolve_requirements(state, "w", parent_version_id="retouched", changes=[mode, release, design])
        self.assertEqual(effective(changed)["器皿"][0], "允许按故事更换器皿")
        self.assertEqual(effective(changed)["菜品"], ("保留原菜形态", "user_explicit"))
        self.assertEqual(effective(changed)["器皿设计"], ("灰陶盘", "ai_design"))
        self.assertFalse(changed["conflicts"])

    def test_switch_back_to_retouch_revokes_vessel_release_and_stale_design(self):
        release = record("release", "器皿", "允许按故事更换器皿", 2, "user_explicit")
        design = record("design", "器皿设计", "灰陶盘", 2, "ai_design")
        dish = record("dish", "菜品", "保留原菜形态", 1, "user_explicit")
        state = add_version(project(), "creative", release, design, dish)
        mode = record("mode", "模式", "修图", 3, "user_explicit")
        implied_keep = record("keep", "器皿", "保留原图蓝碗", 3, "user_implied_edit_preservation")
        ineffective = resolve_requirements(state, "w", parent_version_id="creative", changes=[mode, implied_keep])
        self.assertEqual(effective(ineffective)["器皿"][0], "允许按故事更换器皿")
        restore = record("restore", "器皿", "依指定修图底图保留，撤销更换许可", 3, "user_explicit")
        revoke = record("revoke", "器皿设计", "依指定修图底图", 3, "ai_design")
        result = resolve_requirements(state, "w", parent_version_id="creative", changes=[mode, restore, revoke])
        self.assertEqual(effective(result)["器皿"], (restore["value"], "user_explicit"))
        self.assertEqual(effective(result)["器皿设计"], (revoke["value"], "ai_design"))
        self.assertEqual(effective(result)["菜品"], ("保留原菜形态", "user_explicit"))
        self.assertFalse(result["conflicts"])
        self.assertEqual(state["versions"]["creative"]["effective_requirements"][1]["value"], "灰陶盘")

    def test_repeated_group_mode_switches_keep_permission_separate_from_ai_design(self):
        dish = record("dish", "菜品", "保留原菜形态", 1, "user_explicit")
        state = add_version(project(), "original", dish)
        state["groups"]["g"] = {"id": "g"}
        phases = [
            (2, "创作", "允许按故事更换器皿", "灰陶盘"),
            (3, "修图", "依指定修图底图保留，撤销更换许可", "依指定修图底图"),
            (4, "创作", "允许按故事更换器皿", "白瓷盘"),
            (5, "修图", "依指定修图底图保留，撤销更换许可", "依指定修图底图"),
        ]
        for revision, mode, permission, design in phases:
            records = [
                record(f"mode-{revision}", "模式", mode, revision, "user_explicit", scope_kind="group", scope_id="g"),
                record(f"permission-{revision}", "器皿", permission, revision, "user_explicit", scope_kind="group", scope_id="g"),
                record(f"design-{revision}", "器皿设计", design, revision, "ai_design", scope_kind="group", scope_id="g"),
            ]
            state["requirements"].update({r["id"]: r for r in records})
            result = resolve_requirements(state, "w", parent_version_id="original", group_id="g")
            self.assertEqual(effective(result)["器皿"], (permission, "user_explicit"))
            self.assertEqual(effective(result)["器皿设计"], (design, "ai_design"))
            self.assertFalse(result["conflicts"])


class ProvenanceKeyTests(unittest.TestCase):
    """The skill-only 画面来源 key under override, conflict, branch return and Work change."""

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

    def test_three_provenance_values_can_change_on_new_branch_without_rewriting_old_version(self):
        old = record("old", PROVENANCE, "用户照片", 1, "ai_design")
        state = add_version(project(), "retouched", old)
        snapshot = deepcopy(state["versions"]["retouched"])
        for value in ("照片重拍", "设计画面"):
            with self.subTest(value=value):
                new = record("new", PROVENANCE, value, 2, "ai_design")
                result = resolve_requirements(state, "w", parent_version_id="retouched", changes=[new])
                self.assertEqual(effective(result)[PROVENANCE], (value, "ai_design"))
                self.assertFalse(result["conflicts"])
                self.assertEqual(state["versions"]["retouched"], snapshot)
                restored = resolve_requirements(state, "w", parent_version_id="retouched")
                self.assertEqual(effective(restored)[PROVENANCE][0], "用户照片")


if __name__ == "__main__":
    unittest.main()
