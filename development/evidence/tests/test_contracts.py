"""Offline adapter capability and observation contract regression tests."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime/before-chopsticks/scripts"))

from bc_core.contracts import check_operation, validate_capabilities, validate_observation


def capabilities():
    return {
        "schema_version": 1, "adapter_id": "offline-fixture",
        "observed_at": "2026-10-29T00:00:00Z",
        "operations": {"generate": "supported", "generative_edit": "supported", "local_edit": "unsupported"},
        "reference_input": {
            "carriers": {"status": "known", "value": ["local_path"]},
            "max_count": {"status": "known", "value": 2},
            "ordered": "supported",
        },
        "edit_modes": {"status": "known", "value": ["whole_image"]},
        "size_limits": {"status": "known", "value": {"max_width": 2048}},
        "output_limits": {"status": "known", "value": {"max_count": 2}},
        "cost_info": {"status": "known", "value": {"channel": "existing-account"}},
        "query": "unknown", "cancel": "unknown",
        "limitations": ["Only this fixture's declared capabilities are known."],
    }


class CapabilityValidationTests(unittest.TestCase):
    def test_valid_capability_is_preserved_and_deep_copied(self):
        cap = capabilities()
        original = deepcopy(cap)
        validated = validate_capabilities(cap)
        self.assertEqual(validated, original)
        validated["reference_input"]["carriers"]["value"].append("changed")
        self.assertEqual(cap, original)

    def test_unknown_capabilities_remain_unknown(self):
        cap = capabilities()
        for key in ["edit_modes", "size_limits", "output_limits", "cost_info"]:
            cap[key] = {"status": "unknown"}
        cap["reference_input"] = {"carriers": {"status": "unknown"},
                                  "max_count": {"status": "unknown"}, "ordered": "unknown"}
        cap["operations"]["generate"] = "unknown"
        self.assertEqual(validate_capabilities(cap), cap)

    def test_required_fields_are_not_silently_defaulted(self):
        for key in capabilities():
            if key == "limitations":
                continue
            with self.subTest(key=key):
                cap = capabilities()
                del cap[key]
                with self.assertRaises(ValueError):
                    validate_capabilities(cap)

    def test_schema_version_rejects_unknown_version_and_boolean(self):
        for version in [True, 0, 2, "1"]:
            with self.subTest(version=version):
                cap = capabilities()
                cap["schema_version"] = version
                with self.assertRaises(ValueError):
                    validate_capabilities(cap)

    def test_observation_time_must_be_explicit_utc(self):
        cap = capabilities()
        cap["observed_at"] = "2026-10-29T00:00:00+00:00"
        self.assertEqual(validate_capabilities(cap), cap)
        for timestamp in ["2026-10-29", "2026-10-29T08:00:00+08:00", "yesterday", "2026-10-29T00:00:00"]:
            with self.subTest(timestamp=timestamp):
                cap["observed_at"] = timestamp
                with self.assertRaises(ValueError):
                    validate_capabilities(cap)

    def test_capability_statuses_reject_boolean_and_unrecognized_values(self):
        for status in [False, None, "yes", "known"]:
            for field in ["operation", "ordered", "query", "cancel"]:
                with self.subTest(status=status, field=field):
                    cap = capabilities()
                    if field == "operation":
                        cap["operations"]["generate"] = status
                    elif field == "ordered":
                        cap["reference_input"]["ordered"] = status
                    else:
                        cap[field] = status
                    with self.assertRaises(ValueError):
                        validate_capabilities(cap)

    def test_known_wrapper_requires_value_and_unknown_cannot_carry_value(self):
        for wrapper in [{"status": "known"}, {"status": "unknown", "value": 2}, {}, 0]:
            with self.subTest(wrapper=wrapper):
                cap = capabilities()
                cap["reference_input"]["max_count"] = wrapper
                with self.assertRaises(ValueError):
                    validate_capabilities(cap)

    def test_count_limits_are_nonnegative_integers_not_booleans(self):
        for count in [True, False, -1, 1.5, "2"]:
            for field in ["references", "outputs"]:
                with self.subTest(count=count, field=field):
                    cap = capabilities()
                    if field == "references":
                        cap["reference_input"]["max_count"]["value"] = count
                    else:
                        cap["output_limits"]["value"]["max_count"] = count
                    with self.assertRaises(ValueError):
                        validate_capabilities(cap)

    def test_known_value_shapes_are_checked(self):
        for field, value in [("edit_modes", "whole_image"), ("size_limits", []),
                             ("cost_info", "free"), ("output_limits", {}), ("output_limits", 2)]:
            with self.subTest(field=field, value=value):
                cap = capabilities()
                cap[field]["value"] = value
                with self.assertRaises(ValueError):
                    validate_capabilities(cap)
        cap = capabilities()
        cap["reference_input"]["carriers"]["value"] = [True]
        with self.assertRaises(ValueError):
            validate_capabilities(cap)

    def test_capability_value_must_be_json_compatible(self):
        cap = capabilities()
        cap["cost_info"]["value"]["price"] = float("nan")
        with self.assertRaises(ValueError):
            validate_capabilities(cap)


class OperationCheckTests(unittest.TestCase):
    def test_supported_operation_and_unknown_query_cancel_do_not_block(self):
        cap = capabilities()
        before = deepcopy(cap)
        result = check_operation(cap, "generate", 1)
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["unknowns"], [])
        self.assertIn(cap["limitations"][0], result["limitations"])
        self.assertEqual(cap, before)

    def test_unsupported_operation_and_known_count_limits_block(self):
        for operation, refs, outputs, reason in [
            ("local_edit", 0, 1, "operations.local_edit"),
            ("generate", 3, 1, "reference_input.max_count"),
            ("generate", 0, 3, "output_limits.max_count"),
        ]:
            with self.subTest(reason=reason):
                result = check_operation(capabilities(), operation, refs, outputs)
                self.assertTrue(any(reason in entry for entry in result["blocking"]))

    def test_missing_or_unknown_operation_is_unknown_not_unsupported(self):
        cap = capabilities()
        cap["operations"]["generate"] = "unknown"
        for operation in ["generate", "unlisted_operation"]:
            result = check_operation(cap, operation, 0)
            self.assertEqual(result["blocking"], [])
            self.assertIn(f"operations.{operation}", result["unknowns"])

    def test_unknown_required_capabilities_are_reported_without_invented_limits(self):
        cap = capabilities()
        cap["reference_input"] = {"carriers": {"status": "unknown"},
                                  "max_count": {"status": "unknown"}, "ordered": "unknown"}
        for field in ["edit_modes", "size_limits", "output_limits", "cost_info"]:
            cap[field] = {"status": "unknown"}
        result = check_operation(cap, "generative_edit", 2)
        self.assertEqual(result["blocking"], [])
        self.assertEqual(set(result["unknowns"]), {
            "reference_input.carriers", "reference_input.max_count", "reference_input.ordered",
            "edit_modes", "size_limits", "output_limits", "cost_info",
        })

    def test_reference_capabilities_are_irrelevant_without_reference_inputs(self):
        cap = capabilities()
        cap["reference_input"] = {"carriers": {"status": "unknown"},
                                  "max_count": {"status": "unknown"}, "ordered": "unsupported"}
        result = check_operation(cap, "generate", 0)
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["unknowns"], [])

    def test_missing_carrier_and_unordered_multiple_references_block(self):
        cap = capabilities()
        cap["reference_input"]["carriers"]["value"] = []
        cap["reference_input"]["ordered"] = "unsupported"
        result = check_operation(cap, "generate", 2)
        self.assertTrue(any("reference_input.carriers" in entry for entry in result["blocking"]))
        self.assertTrue(any("reference_input.ordered" in entry for entry in result["blocking"]))

    def test_unknown_order_is_irrelevant_to_single_reference(self):
        cap = capabilities()
        cap["reference_input"]["ordered"] = "unknown"
        self.assertEqual(check_operation(cap, "generate", 1)["unknowns"], [])

    def test_empty_edit_modes_block_only_editing(self):
        cap = capabilities()
        cap["edit_modes"]["value"] = []
        self.assertEqual(check_operation(cap, "generate", 0)["blocking"], [])
        self.assertTrue(any("edit_modes" in entry for entry in check_operation(cap, "generative_edit", 1)["blocking"]))

    def test_requested_counts_reject_boolean_and_invalid_numbers(self):
        for value in [True, -1, 1.5, "1"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    check_operation(capabilities(), "generate", value)
                with self.assertRaises(ValueError):
                    check_operation(capabilities(), "generate", 0, value)


class ObservationValidationTests(unittest.TestCase):
    def test_all_state_submission_pairs_follow_the_external_contract(self):
        valid = {
            "prepared": {"not_submitted"}, "dispatching": {"uncertain"},
            "accepted": {"submitted"}, "running": {"submitted"},
            "succeeded": {"submitted"},
            "failed": {"not_submitted", "submitted", "uncertain"},
            "cancelled": {"not_submitted", "submitted"},
            "unknown": {"submitted", "uncertain"},
        }
        for state, permitted in valid.items():
            for submission in ["not_submitted", "submitted", "uncertain"]:
                with self.subTest(state=state, submission=submission):
                    if submission in permitted:
                        self.assertIsNone(validate_observation(state, submission, {"fact": "offline test evidence"}))
                    else:
                        with self.assertRaises(ValueError):
                            validate_observation(state, submission, {"fact": "offline test evidence"})

    def test_external_observations_require_nonempty_structured_evidence(self):
        for state in ["accepted", "running", "succeeded", "failed", "cancelled", "unknown"]:
            for evidence in [None, {}, [], "done", True]:
                with self.subTest(state=state, evidence=evidence):
                    with self.assertRaises(ValueError):
                        validate_observation(state, "submitted", evidence)
            self.assertIsNone(validate_observation(state, "submitted", ["observed fact"]))

    def test_preparation_and_dispatch_do_not_require_external_evidence(self):
        for evidence in [None, {}, []]:
            self.assertIsNone(validate_observation("prepared", "not_submitted", evidence))
            self.assertIsNone(validate_observation("dispatching", "uncertain", evidence))

    def test_invalid_states_and_submissions_are_rejected(self):
        for state, submission in [("completed", "submitted"), ("unknown", "maybe"), (True, "submitted")]:
            with self.subTest(state=state, submission=submission):
                with self.assertRaises(ValueError):
                    validate_observation(state, submission, {"fact": "evidence"})


if __name__ == "__main__":
    unittest.main()
