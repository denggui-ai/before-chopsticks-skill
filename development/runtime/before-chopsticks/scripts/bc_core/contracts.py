"""Offline validation for version 1 adapter declarations and observations.

Capabilities use status strings ``supported / unsupported / unknown``. A Known
wrapper is ``{"status": "known", "value": ...}`` or ``{"status": "unknown"}``.
Known values have these shapes:

* reference_input.carriers and edit_modes: lists of nonempty strings;
* reference_input.max_count: a nonnegative integer (never a bool);
* output_limits: an object with a nonnegative integer ``max_count``;
* size_limits and cost_info: JSON objects, preserved without inferred limits,
  prices, billing guarantees or additional capabilities.

Operations is a mapping from operation names to status strings. reference_input
also includes an ``ordered`` status; query and cancel are separate statuses.
observed_at must be an ISO timestamp with an explicit UTC offset. Optional
top-level limitations is a list of strings. Unknown values never become zero,
empty arrays, false, unsupported, or evidence that an operation is free.
"""

from copy import deepcopy
from datetime import datetime, timedelta
import math


_STATUSES = ("supported", "unsupported", "unknown")
_SUBMISSIONS = {
    "prepared": ("not_submitted",),
    "dispatching": ("uncertain",),
    "accepted": ("submitted",),
    "running": ("submitted",),
    "succeeded": ("submitted",),
    "failed": ("not_submitted", "submitted", "uncertain"),
    "cancelled": ("not_submitted", "submitted"),
    "unknown": ("submitted", "uncertain"),
}


def _object(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def _count(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")


def _strings(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    for entry in value:
        _string(entry, field)


def _status(value, field):
    if value not in _STATUSES:
        raise ValueError(f"{field} must be supported, unsupported or unknown")


def _json_value(value, field):
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            _json_value(item, field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} must have string object keys")
            _json_value(item, field)
        return
    raise ValueError(f"{field} must contain only JSON-compatible values")


def _known(value, field, validator):
    wrapper = _object(value, field)
    status = wrapper.get("status")
    if status == "known":
        if "value" not in wrapper:
            raise ValueError(f"{field}.value is required when known")
        validator(wrapper["value"], f"{field}.value")
    elif status == "unknown":
        if "value" in wrapper:
            raise ValueError(f"{field} cannot provide a value while unknown")
    else:
        raise ValueError(f"{field}.status must be known or unknown")


def _output_limits(value, field):
    limits = _object(value, field)
    _count(limits.get("max_count"), f"{field}.max_count")


def validate_capabilities(cap):
    """Return a detached, unchanged capability declaration or raise ValueError.

    This validates declaration structure, not host support or evidence truth.
    Unlisted operations remain unlisted and are treated as unknown by the gate.
    """
    _object(cap, "capabilities")
    if type(cap.get("schema_version")) is not int or cap["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    _string(cap.get("adapter_id"), "adapter_id")
    observed = cap.get("observed_at")
    _string(observed, "observed_at")
    try:
        timestamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("observed_at must be an ISO UTC timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise ValueError("observed_at must have an explicit UTC offset")

    operations = _object(cap.get("operations"), "operations")
    for operation, status in operations.items():
        _string(operation, "operation name")
        _status(status, f"operations.{operation}")
    references = _object(cap.get("reference_input"), "reference_input")
    _known(references.get("carriers"), "reference_input.carriers", _strings)
    _known(references.get("max_count"), "reference_input.max_count", _count)
    _status(references.get("ordered"), "reference_input.ordered")
    _known(cap.get("edit_modes"), "edit_modes", _strings)
    _known(cap.get("size_limits"), "size_limits", _object)
    _known(cap.get("output_limits"), "output_limits", _output_limits)
    _known(cap.get("cost_info"), "cost_info", _object)
    _status(cap.get("query"), "query")
    _status(cap.get("cancel"), "cancel")
    if "limitations" in cap:
        _strings(cap["limitations"], "limitations")
    _json_value(cap, "capabilities")
    return deepcopy(cap)


def validate_observation(state, submission, evidence):
    """Validate one external observation, without inventing a state transition.

    All states except prepared/dispatching require nonempty dict/list evidence.
    Preparation and dispatch may have None or empty dict/list evidence because
    they describe internal facts. This checks evidence shape, never its truth.
    Prior persisted facts and transition ordering are enforced by the caller.
    """
    if not isinstance(state, str) or state not in _SUBMISSIONS:
        raise ValueError(f"Unknown external observation state: {state!r}")
    if submission not in _SUBMISSIONS[state]:
        raise ValueError(f"Invalid external observation: {state}/{submission}")
    if evidence is not None and not isinstance(evidence, (dict, list)):
        raise ValueError("Observation evidence must be an object or list")
    if state not in ("prepared", "dispatching") and not evidence:
        raise ValueError(f"{state} requires nonempty structured evidence")
    _json_value(evidence, "evidence")


def check_operation(cap, operation_type, reference_count, expected_outputs=1):
    """Return ``{blocking: [str], unknowns: [str], limitations: [str]}``.

    Unknown entries are field paths, not unsupported verdicts. Only capabilities
    relevant to this operation are checked: reference order needs multiple
    inputs, edit_modes needs an edit operation, and query/cancel do not gate
    generation. Known absence or count excess blocks; uncertain capability does
    not silently pass as supported. The caller decides whether each unknown is
    essential under its authorization and preservation requirements.

    This API has no requested dimensions, carrier or specific edit mode; their
    compatibility requires adapter preparation. Known cost information describes
    declared channels and never guarantees the final bill.
    """
    cap = validate_capabilities(cap)
    _string(operation_type, "operation_type")
    _count(reference_count, "reference_count")
    _count(expected_outputs, "expected_outputs")
    blocking = []
    unknowns = []
    limitations = deepcopy(cap.get("limitations", []))

    def status_gate(status, field):
        if status == "unsupported":
            blocking.append(f"{field}: unsupported")
        elif status == "unknown":
            unknowns.append(field)

    def known_gate(wrapper, field):
        if wrapper["status"] == "unknown":
            unknowns.append(field)
            return False
        return True

    status_gate(cap["operations"].get(operation_type, "unknown"), f"operations.{operation_type}")
    references = cap["reference_input"]
    if reference_count:
        if known_gate(references["carriers"], "reference_input.carriers") and not references["carriers"]["value"]:
            blocking.append("reference_input.carriers: no supported input carrier")
        if known_gate(references["max_count"], "reference_input.max_count"):
            maximum = references["max_count"]["value"]
            if reference_count > maximum:
                blocking.append(f"reference_input.max_count: requested {reference_count}, maximum {maximum}")
        if reference_count > 1:
            status_gate(references["ordered"], "reference_input.ordered")
    if operation_type in ("generative_edit", "local_edit"):
        if known_gate(cap["edit_modes"], "edit_modes") and not cap["edit_modes"]["value"]:
            blocking.append("edit_modes: no supported edit mode")
    if known_gate(cap["size_limits"], "size_limits") and cap["size_limits"]["value"]:
        limitations.append("size_limits: requested dimensions still need adapter preparation checks")
    if known_gate(cap["output_limits"], "output_limits"):
        maximum = cap["output_limits"]["value"]["max_count"]
        if expected_outputs > maximum:
            blocking.append(f"output_limits.max_count: requested {expected_outputs}, maximum {maximum}")
    if known_gate(cap["cost_info"], "cost_info"):
        limitations.append("cost_info: declared channel information does not guarantee actual charges")
    return {"blocking": blocking, "unknowns": unknowns, "limitations": limitations}
