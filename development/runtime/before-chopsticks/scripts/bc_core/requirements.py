"""Pure, target-aware requirement resolution for immutable request snapshots.

The caller assigns IDs, revisions and scopes. Revisions from one user turn must
be equal, so contradictory values cannot be resolved by their array order.
Stored branch requirements are deliberately excluded: a selected parent version
already contains the branch history that is allowed to reach the next request.
"""

from copy import deepcopy
import json


_SOURCE_PRIORITY = {
    "default": 0,
    "ai_design": 1,
    "user_implied_edit_preservation": 2,
    "user_reference": 3,
    "user_explicit": 4,
}


def _applies(requirement, project_id, work_id, group_id, item_id, *, current=False):
    scope = requirement["scope"]
    kind = scope["kind"]
    if kind == "project":
        applies = scope["id"] == project_id
    elif kind == "group":
        applies = group_id is not None and scope["id"] == group_id
    elif kind == "work":
        applies = scope["id"] == work_id
    elif kind == "branch":
        applies = current
    else:
        raise ValueError(f"Unknown requirement scope: {kind!r}")
    if "item_ids" in scope:
        applies = applies and item_id is not None and item_id in scope["item_ids"]
    return applies


def _priority(requirement):
    kind = requirement["source"]["kind"]
    if kind not in _SOURCE_PRIORITY:
        raise ValueError(f"Unknown requirement source: {kind!r}")
    revision = requirement["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("Requirement revision must be a non-negative integer")
    return _SOURCE_PRIORITY[kind], revision


def _replacement_history(winner, candidates, target):
    """Keep source provenance while describing replacement only for this target."""
    replaced = {}
    for candidate in candidates:
        for relation in candidate.get("target_supersedes", []):
            replacement = deepcopy(relation)
            replacement["target"] = deepcopy(target)
            replaced.setdefault(replacement["requirement_id"], replacement)
        if candidate["id"] != winner["id"]:
            replaced[candidate["id"]] = {
                "requirement_id": candidate["id"],
                "source": deepcopy(candidate["source"]),
                "scope": deepcopy(candidate["scope"]),
                "target": deepcopy(target),
            }
    replaced.pop(winner["id"], None)
    return [replaced[identifier] for identifier in sorted(replaced)]


def resolve_requirements(project, work_id, parent_version_id=None, changes=(),
                         group_id=None, item_id=None):
    """Return effective records and unresolved conflicts without changing inputs.

    Source priority precedes revision; within a source, later applicable records
    win regardless of whether their scope is shared or local. Equal-priority,
    equal-revision values must agree. A conflicted key has no effective winner.

    Existing ``supersedes`` fields remain unchanged. ``target_supersedes`` adds
    the replaced IDs, sources, original scopes and this resolution's target.
    Input branch changes are understood to belong to the caller's current
    request; unrelated historical branches are never pulled from the project.
    """
    target = {"work_id": work_id, "group_id": group_id, "item_id": item_id}
    candidates = {}
    if parent_version_id is not None:
        parent = project["versions"][parent_version_id]
        if parent["work_id"] != work_id:
            raise ValueError("Parent version belongs to a different work")
        for requirement in parent["effective_requirements"]:
            candidates.setdefault(requirement["id"], requirement)

    for requirement in project.get("requirements", {}).values():
        if _applies(requirement, project["id"], work_id, group_id, item_id):
            # Prefer a version's frozen record over a later index representation.
            candidates.setdefault(requirement["id"], requirement)
    for requirement in changes:
        if _applies(requirement, project["id"], work_id, group_id, item_id, current=True):
            candidates.setdefault(requirement["id"], requirement)

    by_key = {}
    for requirement in candidates.values():
        by_key.setdefault(requirement["key"], []).append(requirement)

    effective = []
    conflicts = []
    for key in sorted(by_key):
        records = sorted(by_key[key], key=lambda record: record["id"])
        priority = max(_priority(record) for record in records)
        top = [record for record in records if _priority(record) == priority]
        values = {
            json.dumps(record["value"], sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False)
            for record in top
        }
        if len(values) > 1:
            conflicts.append({
                "key": key,
                "requirement_ids": [record["id"] for record in top],
                "requirements": deepcopy(top),
                "target": deepcopy(target),
            })
            continue
        winner = deepcopy(top[0])
        winner["target_supersedes"] = _replacement_history(winner, records, target)
        effective.append(winner)
    return {"effective_requirements": effective, "conflicts": conflicts}


def inherit_for_new_subject(effective_requirements, borrowed_keys):
    """Copy only explicitly borrowed attributes that do not depend on subject.

    The caller identifies the photographic attributes in ``borrowed_keys`` and
    creates/re-scopes the new Work's records; this helper invents no key taxonomy.
    """
    borrowed = set(borrowed_keys)
    return [
        deepcopy(requirement)
        for requirement in effective_requirements
        if requirement["key"] in borrowed
        and "subject" not in requirement.get("dependencies", [])
    ]
