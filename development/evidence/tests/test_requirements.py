"""Regression cases for target-aware, immutable requirement resolution."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/before-chopsticks/scripts"))

from bc_core.requirements import inherit_for_new_subject, resolve_requirements


def requirement(identifier, value, revision, *, key="color", kind="user_explicit",
                scope_kind="group", scope_id="g", item_ids=None, dependencies=()):
    scope = {"kind": scope_kind, "id": scope_id}
    if item_ids is not None:
        scope["item_ids"] = list(item_ids)
    return {
        "id": identifier, "key": key, "value": value,
        "source": {"kind": kind, "message_id": f"message-{revision}"},
        "scope": scope, "revision": revision,
        "dependencies": list(dependencies), "supersedes": [],
    }


def project(*requirements):
    return {
        "id": "p", "works": {"w": {"id": "w"}, "other": {"id": "other"}},
        "groups": {"g": {"id": "g", "item_ids": ["a", "b", "c"]}},
        "items": {item: {"id": item, "work_id": "w"} for item in ["a", "b", "c"]},
        "requirements": {record["id"]: record for record in requirements},
        "versions": {},
    }


def with_parent(state, *requirements, version_id="v", work_id="w"):
    state["versions"][version_id] = {
        "id": version_id, "work_id": work_id,
        "effective_requirements": deepcopy(list(requirements)),
    }
    return state


class ResolveRequirementsTests(unittest.TestCase):
    def resolve(self, state, parent=None, changes=(), item="b"):
        return resolve_requirements(state, "w", parent, changes, group_id="g", item_id=item)

    def test_empty_new_work_has_no_invented_defaults(self):
        self.assertEqual(self.resolve(project()), {"effective_requirements": [], "conflicts": []})

    def test_older_shared_requirement_does_not_override_newer_parent_local(self):
        shared = requirement("shared", "cool", 2)
        local = requirement("local", "warm", 3, scope_kind="branch", scope_id="request-local")
        result = self.resolve(with_parent(project(shared), shared, local), "v")
        self.assertEqual([r["id"] for r in result["effective_requirements"]], ["local"])
        self.assertFalse(result["conflicts"])

    def test_newer_shared_requirement_overrides_parent_local_with_target_provenance(self):
        local = requirement("local", "warm", 3, scope_kind="branch", scope_id="request-local")
        shared = requirement("shared", "neutral", 4)
        result = self.resolve(with_parent(project(shared), local), "v")
        winner = result["effective_requirements"][0]
        self.assertEqual(winner["id"], "shared")
        self.assertEqual(winner["source"], shared["source"])
        self.assertEqual(winner["supersedes"], shared["supersedes"])
        self.assertEqual(winner["target_supersedes"], [{
            "requirement_id": "local", "source": local["source"], "scope": local["scope"],
            "target": {"work_id": "w", "group_id": "g", "item_id": "b"},
        }])

    def test_item_local_override_leaves_other_group_items_cool(self):
        shared = requirement("shared", "cool", 1)
        local = requirement("local", "warm", 2, item_ids=["b"])
        state = project(shared, local)
        for item, expected in [("a", "shared"), ("b", "local"), ("c", "shared")]:
            with self.subTest(item=item):
                self.assertEqual(self.resolve(state, item=item)["effective_requirements"][0]["id"], expected)

    def test_return_to_old_version_does_not_read_other_branch_changes(self):
        shared = requirement("shared", "cool", 1)
        local = requirement("other-branch", "warm", 3, scope_kind="branch", scope_id="request-other")
        state = with_parent(project(shared, local), shared, version_id="old")
        self.assertEqual(self.resolve(state, "old")["effective_requirements"][0]["id"], "shared")
        new_shared = requirement("new-shared", "neutral", 4)
        state["requirements"]["new-shared"] = new_shared
        self.assertEqual(self.resolve(state, "old")["effective_requirements"][0]["id"], "new-shared")
        self.assertEqual(state["versions"]["old"]["effective_requirements"], [shared])

    def test_branch_change_in_current_call_is_applied(self):
        shared = requirement("shared", "cool", 1)
        change = requirement("local", "warm", 2, scope_kind="branch", scope_id="request-new")
        self.assertEqual(self.resolve(project(shared), changes=[change])["effective_requirements"][0]["id"], "local")

    def test_source_precedence_beats_later_ai_and_default_values(self):
        reference = requirement("reference", "cool", 2, kind="user_reference")
        explicit = requirement("explicit", "warm", 1)
        changes = [requirement("design", "neutral", 10, kind="ai_design"),
                   requirement("default", "blue", 11, kind="default")]
        self.assertEqual(self.resolve(project(reference, explicit), changes=changes)["effective_requirements"][0]["id"], "explicit")

    def test_design_and_default_fill_missing_keys(self):
        changes = [requirement("design", "side", 1, key="lighting", kind="ai_design"),
                   requirement("default", "4:3", 1, key="aspect_ratio", kind="default")]
        result = self.resolve(project(), changes=changes)
        self.assertEqual({r["key"]: r["value"] for r in result["effective_requirements"]},
                         {"lighting": "side", "aspect_ratio": "4:3"})

    def test_same_round_conflict_is_returned_without_order_based_winner(self):
        warm = requirement("warm", "warm", 2)
        cool = requirement("cool", "cool", 2)
        lighting = requirement("lighting", "side", 2, key="lighting")
        state = project(requirement("old", "neutral", 1))
        first = self.resolve(state, changes=[warm, lighting, cool])
        second = self.resolve(state, changes=[cool, warm, lighting])
        self.assertEqual(first, second)
        self.assertEqual([r["key"] for r in first["effective_requirements"]], ["lighting"])
        self.assertEqual(len(first["conflicts"]), 1)
        conflict = first["conflicts"][0]
        self.assertEqual(conflict["key"], "color")
        self.assertEqual(conflict["requirement_ids"], ["cool", "warm"])
        self.assertEqual(conflict["requirements"], [cool, warm])
        self.assertEqual(conflict["target"], {"work_id": "w", "group_id": "g", "item_id": "b"})

    def test_newer_explicit_value_resolves_old_conflict(self):
        state = project(requirement("cool", "cool", 1), requirement("warm", "warm", 1),
                        requirement("neutral", "neutral", 2))
        result = self.resolve(state)
        self.assertFalse(result["conflicts"])
        self.assertEqual(result["effective_requirements"][0]["id"], "neutral")

    def test_same_round_equal_values_do_not_conflict_or_depend_on_order(self):
        first = requirement("a", {"temperature": "cool"}, 1)
        second = requirement("b", {"temperature": "cool"}, 1)
        result = self.resolve(project(first, second))
        self.assertEqual(result, self.resolve(project(second, first)))
        self.assertFalse(result["conflicts"])
        self.assertEqual(len(result["effective_requirements"]), 1)

    def test_scopes_must_match_target_and_item_filter_needs_explicit_item(self):
        applicable = requirement("project", "cool", 1, scope_kind="project", scope_id="p")
        records = [applicable,
                   requirement("wrong-project", "bad", 9, scope_kind="project", scope_id="other"),
                   requirement("wrong-group", "bad", 9, scope_id="other"),
                   requirement("wrong-work", "bad", 9, scope_kind="work", scope_id="other"),
                   requirement("item-only", "warm", 9, item_ids=["b"])]
        result = resolve_requirements(project(*records), "w")
        self.assertEqual(result["effective_requirements"][0]["id"], "project")

    def test_snapshot_is_not_filtered_by_mutable_global_superseded_state(self):
        parent = requirement("local", "warm", 2, scope_kind="branch", scope_id="request-local")
        global_record = deepcopy(parent)
        global_record["state"] = "superseded"
        state = with_parent(project(global_record), parent)
        self.assertEqual(self.resolve(state, "v")["effective_requirements"][0]["value"], "warm")

    def test_parent_from_another_work_is_rejected(self):
        state = with_parent(project(), version_id="foreign", work_id="other")
        with self.assertRaises(ValueError):
            self.resolve(state, "foreign")

    def test_resolution_and_its_returned_nested_records_are_detached_from_inputs(self):
        shared = requirement("shared", {"temperature": "cool"}, 1)
        local = requirement("local", {"temperature": "warm"}, 2, scope_kind="branch", scope_id="request-local")
        state = with_parent(project(shared), local)
        changes = [requirement("new", {"temperature": "neutral"}, 3)]
        before = deepcopy((state, changes))
        result = self.resolve(state, "v", changes)
        self.assertEqual((state, changes), before)
        result["effective_requirements"][0]["value"]["temperature"] = "changed"
        result["effective_requirements"][0]["target_supersedes"][0]["source"]["kind"] = "changed"
        self.assertEqual((state, changes), before)


    def test_orientation_only_request_replaces_explicit_parent_ratio(self):
        # "Landscape" alone is user_explicit; the concrete ratio is noted as a design choice in the value.
        parent = requirement("ratio", "4:5", 1, key="aspect_ratio", scope_kind="work", scope_id="w")
        change = requirement("landscape", "横幅（具体16:9为设计选择）", 2, key="aspect_ratio",
                             scope_kind="branch", scope_id="request-new")
        result = self.resolve(with_parent(project(parent), parent), "v", [change])
        self.assertEqual([r["value"] for r in result["effective_requirements"]], ["横幅（具体16:9为设计选择）"])
        self.assertFalse(result["conflicts"])
        # The rejected encoding: an ai_design ratio cannot replace an explicit parent ratio.
        design = dict(change, id="design", value="16:9", source={"kind": "ai_design", "message_id": "message-2"})
        result = self.resolve(with_parent(project(parent), parent), "v", [design])
        self.assertEqual([r["value"] for r in result["effective_requirements"]], ["4:5"])


class InheritRequirementsTests(unittest.TestCase):
    def test_new_subject_only_inherits_requested_subject_independent_attributes(self):
        records = [requirement("lighting", "side", 1, key="lighting"),
                   requirement("color", "cool", 1, key="color"),
                   requirement("pieces", 6, 1, key="pieces", dependencies=["subject"]),
                   requirement("plate", "wok", 1, key="vessel", dependencies=["subject", "styling"])]
        inherited = inherit_for_new_subject(records, ["lighting", "pieces", "vessel"])
        self.assertEqual(inherited, [records[0]])

    def test_new_subject_inheritance_is_a_deep_copy_and_empty_borrow_means_empty(self):
        record = requirement("lighting", {"direction": "left"}, 1, key="lighting")
        original = deepcopy(record)
        inherited = inherit_for_new_subject([record], ["lighting"])
        inherited[0]["value"]["direction"] = "right"
        self.assertEqual(record, original)
        self.assertEqual(inherit_for_new_subject([record], []), [])


if __name__ == "__main__":
    unittest.main()
