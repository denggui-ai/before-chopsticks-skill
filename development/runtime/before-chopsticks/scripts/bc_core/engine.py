"""Local project operations. This module never calls a model or image service.

prepare/dispatch/observe form the adapter boundary: dispatch persists an intent
before the caller invokes its actual tool. A caller must use the returned ID
after re-preparation. Recovery only classifies facts; it never resubmits work.
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .contracts import check_operation, validate_capabilities, validate_observation
from .local_edits import inspect_image, render_pipeline
from .requirements import inherit_for_new_subject, resolve_requirements
from .storage import ProjectStore, StateError, digest, new_id, sha256_file, sync_dir, utc_now


_EXTERNAL_OPERATIONS = {'generate', 'generative_edit'}
_READ_ONLY = {'explore', 'critique', 'shoot_guidance'}
_ROLES = {'base_image', 'subject', 'food_styling', 'action', 'composition', 'lighting',
          'color', 'material', 'style', 'continuity'}
_TERMINAL = {'succeeded', 'failed', 'cancelled'}


def _positive_integer(value, label, allow_zero=False):
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise StateError(f'{label} must be a {"nonnegative" if allow_zero else "positive"} integer')
    return value


def _evidence(value, label='evidence'):
    if not isinstance(value, (dict, list)) or not value:
        raise StateError(f'{label} must be a nonempty object or list')
    return copy.deepcopy(value)


def _alpha_label(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord('a') + remainder) + result
    return result


class ProjectEngine:
    def __init__(self, root, failpoint=None):
        self.store = ProjectStore(root, failpoint=failpoint)

    def initialize(self, name='Untitled'):
        return self.store.initialize(name)

    def _load(self):
        project = self.store.load()
        self.store.require_healthy()
        return project

    def _commit(self, project):
        return self.store.commit(project, expected_revision=project['revision'])

    def inspect(self):
        with self.store.lock():
            project = self.store.load()
            attempts = self.store.list_records('attempts')
            return {'project': project, 'attempts': attempts,
                    'recovery': copy.deepcopy(self.store.recovery_report),
                    'unresolved_attempt_ids': [a['id'] for a in attempts if a['state'] in ('unknown', 'dispatching')]}

    def _work(self, project, work_id):
        if work_id not in project['works']:
            raise StateError('work does not belong to this project')
        return project['works'][work_id]

    def _version(self, project, version_id, readable=False):
        if version_id not in project['versions']:
            raise StateError('version does not belong to this project')
        version = project['versions'][version_id]
        if readable:
            self._asset_path(project, version['asset_id'])
        return version

    def _asset_path(self, project, asset_id):
        if asset_id not in project['assets']:
            raise StateError('asset not found in project')
        asset = project['assets'][asset_id]
        if asset.get('availability') != 'local_verified':
            raise StateError(f'asset {asset_id} is not a verified local file')
        path = self.store.project_path(asset['locator'])
        if not path.is_file() or sha256_file(path) != asset['sha256']:
            raise StateError(f'asset {asset_id} is missing or changed')
        try:
            inspect_image(path)
        except ValueError as exc:
            raise StateError(f'asset {asset_id} is unreadable') from exc
        return path

    def create_work(self, subject, borrowed_from=None, borrowed_keys=()):
        if not isinstance(subject, str) or not subject.strip():
            raise StateError('subject is required')
        with self.store.lock():
            project = self._load()
            work = {'id': new_id('work'), 'subject': subject, 'created_at': utc_now(),
                    'version_ids': [], 'latest_version_id': None, 'selected_version_id': None,
                    'edit_target_version_id': None, 'display_labels': {}, 'auto_target_from_attempt_id': None}
            project['works'][work['id']] = work
            if borrowed_from is not None:
                source = self._version(project, borrowed_from, readable=True)
                records = inherit_for_new_subject(source['effective_requirements'], borrowed_keys)
                scoped = self._requirements(project, records, {'kind': 'work', 'id': work['id']},
                                             f'new_subject:{borrowed_from}')
                project['requirements'].update({r['id']: r for r in scoped})
            project['active_work_id'] = work['id']
            project['active_group_id'] = None
            project['interaction_epoch'] += 1
            project['target_resolution'] = 'none'
            self._commit(project)
            return copy.deepcopy(work)

    def create_group(self, work_ids, kind='candidates'):
        if kind not in ('candidates', 'visual_series', 'narrative_series', 'independent_items') or not work_ids:
            raise StateError('invalid group kind or empty item list')
        with self.store.lock():
            project = self._load()
            group = {'id': new_id('group'), 'kind': kind, 'item_ids': [], 'shared_revision': 0,
                     'shared_requirement_ids': [], 'anchor': None, 'display_labels': {}}
            for index, work_id in enumerate(work_ids, 1):
                self._work(project, work_id)
                item = {'id': new_id('item'), 'group_id': group['id'], 'work_id': work_id,
                        'label': str(index), 'request_ids': [], 'version_ids': [], 'depends_on': [],
                        'anchor_binding': None, 'completion': 'planned', 'unresolved_attempt_ids': []}
                project['items'][item['id']] = item
                group['item_ids'].append(item['id'])
            project['groups'][group['id']] = group
            project['active_group_id'] = group['id']
            project['interaction_epoch'] += 1
            project['target_resolution'] = 'ambiguous' if len(work_ids) > 1 else 'none'
            self._commit(project)
            return copy.deepcopy(group)

    def set_series_anchor(self, group_id, version_id, borrowed_roles):
        if not isinstance(borrowed_roles, list) or not borrowed_roles or not all(isinstance(x, str) and x.strip() for x in borrowed_roles):
            raise StateError('anchor requires explicit borrowed roles')
        with self.store.lock():
            project = self._load()
            group = project['groups'].get(group_id)
            if not group or group['kind'] not in ('visual_series', 'narrative_series'):
                raise StateError('anchor requires a series group')
            version = self._version(project, version_id, readable=True)
            if version['work_id'] not in [project['items'][i]['work_id'] for i in group['item_ids']]:
                raise StateError('anchor version must belong to a series work')
            self._check_series_version(version)
            group['anchor'] = {'version_id': version_id, 'borrowed_roles': list(dict.fromkeys(borrowed_roles))}
            self._commit(project)
            return copy.deepcopy(group['anchor'])

    def _check_series_version(self, version):
        if version.get('attempt_id'):
            attempt = self.store.read_record('attempts', version['attempt_id'])
            if attempt['state'] != 'succeeded':
                raise StateError('series dependency must come from a succeeded attempt')

    def set_item_anchor(self, group_id, item_id, reference_ids, follow=True):
        """Explicit item opt-in; None declares independence, [] borrows without image refs."""
        if type(follow) is not bool:
            raise StateError('anchor follow must be boolean')
        if reference_ids is not None and (not isinstance(reference_ids, list)
                or not all(isinstance(r, str) for r in reference_ids)
                or len(set(reference_ids)) != len(reference_ids)):
            raise StateError('anchor reference_ids must be a unique list or null')
        with self.store.lock():
            project = self._load()
            group = project['groups'].get(group_id)
            if (not group or group['kind'] not in ('visual_series', 'narrative_series')
                    or item_id not in group['item_ids']):
                raise StateError('anchor binding requires an item in this series')
            binding = None
            if reference_ids is not None:
                anchor = group.get('anchor')
                if not anchor:
                    raise StateError('set a series anchor before binding an item')
                version = self._version(project, anchor['version_id'], readable=True)
                self._check_series_version(version)
                item = project['items'][item_id]
                target = self._target(project, item['work_id'], group_id, item_id)
                for rid in reference_ids:
                    ref = project['references'].get(rid)
                    if not ref or ref['asset_id'] != version['asset_id']:
                        raise StateError('bound reference must identify the current anchor asset')
                    self._reference_scope(project, ref.get('scope'), target)
                    if any(r['role'] == 'base_image' or r['role'] not in anchor['borrowed_roles']
                           for r in ref['roles']):
                        raise StateError('bound reference roles must be borrowed anchor roles, not base_image')
                binding = {'follow': follow, 'anchor': copy.deepcopy(anchor),
                           'reference_ids': list(reference_ids)}
            project['items'][item_id]['anchor_binding'] = binding
            self._commit(project)
            return copy.deepcopy(binding)

    def set_item_dependencies(self, group_id, item_id, dependencies):
        if not isinstance(dependencies, list):
            raise StateError('dependencies must be a list')
        with self.store.lock():
            project = self._load()
            group = project['groups'].get(group_id)
            if not group or group['kind'] not in ('visual_series', 'narrative_series') or item_id not in group['item_ids']:
                raise StateError('item must belong to a series group')
            item = project['items'][item_id]
            cleaned = []
            for dep in dependencies:
                if not isinstance(dep, dict) or set(dep) != {'item_id', 'version_id'}:
                    raise StateError('dependency requires item_id and version_id')
                upstream = dep['item_id']
                if upstream not in group['item_ids'] or group['item_ids'].index(upstream) >= group['item_ids'].index(item_id):
                    raise StateError('dependency must be an earlier item in this series')
                if dep['version_id'] is not None:
                    version = self._version(project, dep['version_id'], readable=True)
                    if version['id'] not in project['items'][upstream]['version_ids']:
                        raise StateError('dependency version does not belong to upstream item')
                    self._check_series_version(version)
                if upstream in [d['item_id'] for d in cleaned]:
                    raise StateError('duplicate series dependency')
                cleaned.append(copy.deepcopy(dep))
            item['depends_on'] = cleaned
            self._commit(project)
            return copy.deepcopy(cleaned)

    def _requirements(self, project, records, scope, message_id):
        if not isinstance(scope, dict) or scope.get('kind') not in ('project', 'group', 'work', 'branch'):
            raise StateError('invalid requirement scope')
        kind, identifier = scope['kind'], scope.get('id')
        if kind == 'project' and identifier != project['id']:
            raise StateError('requirement project scope mismatch')
        if kind in ('group', 'work') and identifier not in project[kind + 's']:
            raise StateError('requirement scope target does not exist')
        if 'item_ids' in scope:
            for item_id in scope['item_ids']:
                if item_id not in project['items'] or (kind == 'group' and project['items'][item_id]['group_id'] != identifier):
                    raise StateError('item scope does not belong to its group')
        result = []
        for entry in records:
            if not isinstance(entry, dict) or not isinstance(entry.get('key'), str) or not entry['key'] or 'value' not in entry:
                raise StateError('requirement needs a key and value')
            record = {'id': new_id('requirement'), 'key': entry['key'], 'value': copy.deepcopy(entry['value']),
                      'source': copy.deepcopy(entry.get('source', {'kind': 'user_explicit', 'message_id': message_id})),
                      'scope': copy.deepcopy(scope), 'revision': project['revision'] + 1,
                      'state': 'recorded', 'supersedes': [], 'dependencies': copy.deepcopy(entry.get('dependencies', []))}
            if record['source'].get('kind') not in ('user_explicit', 'user_reference', 'user_implied_edit_preservation', 'ai_design', 'default'):
                raise StateError('invalid requirement source')
            if not isinstance(record['dependencies'], list):
                raise StateError('dependencies must be a list')
            result.append(record)
        return result

    def add_requirements(self, records, scope, message_id):
        with self.store.lock():
            project = self._load()
            if scope.get('kind') == 'branch':
                raise StateError('local branch changes must be attached to a prepared request')
            added = self._requirements(project, records, scope, message_id)
            project['requirements'].update({r['id']: r for r in added})
            if scope['kind'] == 'group':
                group = project['groups'][scope['id']]
                group['shared_revision'] += 1
                group['shared_requirement_ids'].extend(r['id'] for r in added)
            return {'requirements': added, 'project': self._commit(project)}

    def authorize(self, work_ids, allowed_operations, requested_count, retry_budget=0,
                  paid_channel_scope=None, evidence=None):
        _positive_integer(requested_count, 'requested_count')
        _positive_integer(retry_budget, 'retry_budget', allow_zero=True)
        _evidence(evidence, 'authorization evidence')
        if not allowed_operations or not set(allowed_operations).issubset(_EXTERNAL_OPERATIONS):
            raise StateError('authorization operations must name supported external operations')
        if not isinstance(paid_channel_scope, list) or not paid_channel_scope or not all(isinstance(v, str) and v for v in paid_channel_scope):
            raise StateError('authorization must explicitly name its allowed channel scope')
        with self.store.lock():
            project = self._load()
            if not work_ids:
                raise StateError('authorization must name its work targets')
            for work_id in work_ids:
                self._work(project, work_id)
            auth = {'id': new_id('authorization'), 'work_ids': list(dict.fromkeys(work_ids)),
                    'allowed_operations': list(allowed_operations), 'requested_count': requested_count,
                    'retry_budget': retry_budget, 'paid_channel_scope': list(paid_channel_scope),
                    'evidence': copy.deepcopy(evidence), 'created_at': utc_now(), 'revision': 0,
                    'active': True, 'extensions': []}
            project['authorizations'][auth['id']] = auth
            self._commit(project)
            return copy.deepcopy(auth)

    def extend_retry_budget(self, authorization_id, additional_count, evidence):
        _positive_integer(additional_count, 'additional_count')
        _evidence(evidence)
        with self.store.lock():
            project = self._load()
            auth = project['authorizations'].get(authorization_id)
            if auth is None or not auth.get('active'):
                raise StateError('authorization unavailable')
            auth['retry_budget'] += additional_count
            auth['revision'] += 1
            auth['extensions'].append({'count': additional_count, 'evidence': copy.deepcopy(evidence), 'time': utc_now()})
            self._commit(project)
            return copy.deepcopy(auth)

    def _authorization(self, project, request):
        auth = project['authorizations'].get(request.get('authorization_id'))
        if auth is None or not auth.get('active'):
            raise StateError('authorization unavailable or revoked')
        if request['target']['work_id'] not in auth['work_ids']:
            raise StateError('work is outside authorization scope')
        if request['operation']['type'] not in auth['allowed_operations']:
            raise StateError('operation is outside authorization scope')
        if request['adapter'] not in auth['paid_channel_scope']:
            raise StateError('adapter/channel is outside authorization scope')
        return auth

    def _materialize(self, source, provenance, derived_from=None, transform=None):
        source = Path(source).expanduser().resolve()
        media_info = inspect_image(source)
        content_hash = sha256_file(source)
        asset_id = 'asset_' + content_hash
        suffix = {'JPEG': '.jpg', 'PNG': '.png', 'TIFF': '.tiff', 'WEBP': '.webp', 'GIF': '.gif'}.get(media_info['format'], '.img')
        relative = f'assets/{asset_id}{suffix}'
        dest = self.store.project_path(relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if sha256_file(dest) != content_hash:
                raise StateError('stored asset bytes changed; cannot overwrite')
        else:
            descriptor, tmp = tempfile.mkstemp(prefix='.asset-', suffix='.tmp', dir=dest.parent)
            try:
                with os.fdopen(descriptor, 'wb') as out, source.open('rb') as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
                if sha256_file(tmp) != content_hash:
                    raise StateError('input changed while being imported')
                os.link(tmp, dest)
                sync_dir(dest.parent)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        asset = {'id': asset_id, 'locator': relative, 'availability': 'local_verified', 'portable': True,
                 'sha256': content_hash, 'media_info': media_info, 'provenance': copy.deepcopy(provenance)}
        if derived_from is not None:
            asset.update(derived_from=derived_from, transform=copy.deepcopy(transform),
                         coordinate_frame={'width': media_info['width'], 'height': media_info['height']})
        self.store.hit('asset_saved')
        return asset

    def _register_asset(self, project, asset):
        if asset['id'] not in project['assets']:
            project['assets'][asset['id']] = copy.deepcopy(asset)
        else:
            existing = project['assets'][asset['id']]
            if existing['sha256'] != asset['sha256']:
                raise StateError('asset identity collision')
            existing['availability'] = 'local_verified'
            origins = existing.setdefault('additional_provenance', [])
            if asset['provenance'] != existing['provenance'] and asset['provenance'] not in origins:
                origins.append(copy.deepcopy(asset['provenance']))

    def _new_local_request(self, project, work_id, intent, parent_version_id=None, operation=None):
        parent = project['versions'].get(parent_version_id)
        resolved = resolve_requirements(project, work_id, parent_version_id)
        request = {'id': new_id('request'), 'schema_version': 1, 'created_at': utc_now(), 'intent': intent,
                   'target': {'work_id': work_id, 'group_id': None, 'item_id': None},
                   'parent_version_id': parent_version_id,
                   'effective_requirements': resolved['effective_requirements'], 'conflicts': resolved['conflicts'],
                   'references': copy.deepcopy(parent.get('references', [])) if parent else [],
                   'design': {}, 'operation': operation or {'type': intent},
                   'authorization': {'kind': 'local_user_operation'}, 'host_requirements': {},
                   'interaction_epoch': project['interaction_epoch']}
        self.store.write_record('requests', request['id'], request, immutable=True)
        project['request_ids'].append(request['id'])
        return request

    def _version_record(self, project, request, asset, origin, attempt_id=None, version_id=None, pipeline=None):
        return {'id': version_id or new_id('version'), 'work_id': request['target']['work_id'],
                'parent_version_id': request.get('parent_version_id'), 'asset_id': asset['id'],
                'request_id': request['id'], 'attempt_id': attempt_id, 'origin': origin, 'created_at': utc_now(),
                'effective_requirements': copy.deepcopy(request['effective_requirements']),
                'references': copy.deepcopy(request.get('references', [])),
                'checks': {'artifact_integrity': {'status': 'pass', 'sha256': asset['sha256']},
                           'requirement_checks': {'status': 'unclear', 'reason': 'not visually checked'},
                           'visual_observations': [], 'user_decision': None},
                'edit_pipeline': copy.deepcopy(pipeline)}

    def _register_version(self, project, version, asset, group_id=None, item_id=None):
        if version['id'] in project['versions']:
            return False
        self._register_asset(project, asset)
        project['versions'][version['id']] = copy.deepcopy(version)
        work = self._work(project, version['work_id'])
        work['version_ids'].append(version['id'])
        work['latest_version_id'] = version['id']
        label = f'V{len(work["version_ids"])}'
        self._bind_label(project, work, label, version['id'])
        if item_id is not None:
            item = project['items'][item_id]
            item['version_ids'].append(version['id'])
            group = project['groups'][group_id]
            ordinal = len(item['version_ids'])
            self._bind_label(project, group, f'{item["label"]}-{ordinal}', version['id'])
            # Existing numeric labels remain valid; never rename old deliveries.
            label = f'{item["label"]}-{_alpha_label(ordinal)}'
            while label in group['display_labels'] or f'{group_id}:{label}' in project['display_labels']:
                ordinal += 1
                label = f'{item["label"]}-{_alpha_label(ordinal)}'
            self._bind_label(project, group, label, version['id'])
            item['completion'] = 'available'
        return True

    def _bind_label(self, project, owner, label, version_id):
        qualified = f'{owner["id"]}:{label}'
        if (owner['display_labels'].get(label, version_id) != version_id
                or project['display_labels'].get(qualified, version_id) != version_id):
            raise StateError('persisted version label cannot be rebound')
        owner['display_labels'][label] = version_id
        project['display_labels'][qualified] = version_id

    def import_image(self, work_id, path):
        with self.store.lock():
            project = self._load()
            work = self._work(project, work_id)
            asset = self._materialize(path, {'kind': 'imported', 'source_path': str(Path(path).expanduser().resolve())})
            project['interaction_epoch'] += 1
            request = self._new_local_request(project, work_id, 'import')
            version = self._version_record(project, request, asset, 'imported')
            self._register_version(project, version, asset)
            work['edit_target_version_id'] = version['id']
            work['auto_target_from_attempt_id'] = None
            project['active_work_id'] = work_id
            project['target_resolution'] = 'resolved'
            project = self._commit(project)
            return {'version': version, 'asset': asset, 'project': project}

    def select_version(self, version_id):
        return self._set_target(version_id, select=True)

    def resolve_version_label(self, label, work_id=None, group_id=None):
        """Resolve persisted labels or unambiguous item shorthand, never latest."""
        with self.store.lock():
            return self._resolve_version_label(self._load(), label, work_id, group_id)

    def _resolve_version_label(self, project, label, work_id=None, group_id=None):
        if not isinstance(label, str) or not label.strip():
            raise StateError('version label must be a nonempty string')
        label = label.strip()
        local_label = label
        if ':' in label:
            owner_id, local_label = label.split(':', 1)
            if owner_id in project['groups']:
                if group_id is not None and group_id != owner_id:
                    raise StateError('version label group scope mismatch')
                group_id = owner_id
            elif owner_id in project['works']:
                if work_id is not None and work_id != owner_id:
                    raise StateError('version label work scope mismatch')
                work_id = owner_id
            else:
                raise StateError('unknown version label scope')
        if work_id is not None:
            self._work(project, work_id)
        if group_id is not None and group_id not in project['groups']:
            raise StateError('version label group scope does not exist')
        if group_id is None and work_id is None:
            active_group = project['groups'].get(project.get('active_group_id'))
            if active_group and (local_label in active_group['display_labels'] or any(
                    project['items'][iid]['label'] == local_label for iid in active_group['item_ids'])):
                group_id = active_group['id']
            else:
                work_id = project.get('active_work_id')
        item_id = None
        if group_id is not None:
            group = project['groups'][group_id]
            version_id = group['display_labels'].get(local_label)
            if version_id is None:
                items = [project['items'][iid] for iid in group['item_ids']
                         if project['items'][iid]['label'] == local_label]
                versions = {vid for item in items for vid in item['version_ids']}
                if len(versions) > 1:
                    raise StateError('ambiguous group item label; specify a version label')
                version_id = next(iter(versions), None)
            if version_id is not None:
                matches = [iid for iid in group['item_ids'] if version_id in project['items'][iid]['version_ids']]
                if len(matches) != 1:
                    raise StateError('version label does not identify one group item')
                item_id = matches[0]
        else:
            version_id = project['works'].get(work_id, {}).get('display_labels', {}).get(local_label)
        if version_id is None:
            raise StateError('version label not found in the requested or active scope')
        version = self._version(project, version_id)
        if work_id is not None and version['work_id'] != work_id:
            raise StateError('version label work scope mismatch')
        return {'label': label, 'version_id': version_id, 'work_id': version['work_id'],
                'group_id': group_id, 'item_id': item_id}

    def select_label(self, label, work_id=None, group_id=None):
        with self.store.lock():
            project = self._load()
            resolved = self._resolve_version_label(project, label, work_id, group_id)
            project['active_group_id'] = resolved['group_id']
            result = self._set_target_locked(project, resolved['version_id'], select=True)
            return {**resolved, **result}

    def set_edit_target(self, version_id):
        return self._set_target(version_id, select=False)

    def _set_target(self, version_id, select):
        with self.store.lock():
            return self._set_target_locked(self._load(), version_id, select)

    def _set_target_locked(self, project, version_id, select):
        version = self._version(project, version_id, readable=True)
        work = self._work(project, version['work_id'])
        work['edit_target_version_id'] = version_id
        work['auto_target_from_attempt_id'] = None
        if select:
            work['selected_version_id'] = version_id
        project['active_work_id'] = work['id']
        project['interaction_epoch'] += 1
        project['target_resolution'] = 'resolved'
        return {'project': self._commit(project)}

    def _reference_scope(self, project, scope, target=None):
        if (not isinstance(scope, dict) or scope.get('kind') not in ('project', 'work', 'group', 'item')
                or not isinstance(scope.get('id'), str)):
            raise StateError('invalid reference scope')
        kind, identifier = scope['kind'], scope['id']
        if kind == 'project':
            valid = identifier == project['id']
        else:
            valid = identifier in project[kind + 's']
        if not valid:
            raise StateError('reference scope target does not exist in this project')
        if 'item_ids' in scope:
            item_ids = scope['item_ids']
            if not isinstance(item_ids, list) or not item_ids:
                raise StateError('reference scope item_ids must be a nonempty list')
            for item_id in item_ids:
                item = project['items'].get(item_id) if isinstance(item_id, str) else None
                if (item is None or (kind == 'group' and item['group_id'] != identifier)
                        or (kind == 'work' and item['work_id'] != identifier)
                        or (kind == 'item' and item_id != identifier)):
                    raise StateError('reference scope item does not belong to its target')
        if target is not None:
            applies = kind == 'project' or target.get(kind + '_id') == identifier
            if 'item_ids' in scope:
                applies = applies and target.get('item_id') in scope['item_ids']
            if not applies:
                raise StateError('explicit reference scope does not match request target')

    def add_reference(self, asset_id, roles, scope=None):
        if not isinstance(roles, list) or not roles:
            raise StateError('reference needs explicit roles')
        for role in roles:
            if (not isinstance(role, dict) or role.get('role') not in _ROLES
                    or role.get('strength') not in ('required', 'preferred', 'informative')
                    or not isinstance(role.get('exclude'), list) or 'borrow' not in role
                    or not role.get('source')):
                raise StateError('reference role needs role, borrow, exclude, strength and source')
        with self.store.lock():
            project = self._load()
            self._asset_path(project, asset_id)
            scope = {'kind': 'project', 'id': project['id']} if scope is None else scope
            self._reference_scope(project, scope)
            reference = {'id': new_id('reference'), 'asset_id': asset_id, 'roles': copy.deepcopy(roles),
                         'scope': copy.deepcopy(scope),
                         'provenance': {'created_at': utc_now()}}
            project['references'][reference['id']] = reference
            self._commit(project)
            return copy.deepcopy(reference)

    def record_read_only(self, work_id, intent, design):
        if intent not in _READ_ONLY:
            raise StateError('not a read-only intent')
        with self.store.lock():
            project = self._load()
            self._work(project, work_id)
            request = {'id': new_id('request'), 'schema_version': 1, 'created_at': utc_now(),
                       'intent': intent, 'target': {'work_id': work_id, 'group_id': None, 'item_id': None},
                       'parent_version_id': None, 'effective_requirements': [], 'references': [],
                       'design': copy.deepcopy(design), 'operation': {'type': intent},
                       'authorization': {'kind': 'read_only'}, 'host_requirements': {}}
            self.store.write_record('requests', request['id'], request, immutable=True)
            project['request_ids'].append(request['id'])
            return {'request': request, 'attempt': None, 'project': self._commit(project)}

    def _target(self, project, work_id, group_id, item_id):
        self._work(project, work_id)
        if (group_id is None) != (item_id is None):
            raise StateError('group and item must be supplied together')
        if group_id is not None:
            if group_id not in project['groups'] or item_id not in project['groups'][group_id]['item_ids']:
                raise StateError('item is not in the specified group')
            if project['items'][item_id]['work_id'] != work_id:
                raise StateError('item belongs to a different work')
        return {'work_id': work_id, 'group_id': group_id, 'item_id': item_id}

    def _resolve_inputs(self, project, request):
        target = request['target']
        resolved = resolve_requirements(project, target['work_id'], request.get('parent_version_id'),
                                        changes=request.get('local_changes', []),
                                        group_id=target.get('group_id'), item_id=target.get('item_id'))
        refs = []
        for reference_id in request.get('reference_ids', []):
            if reference_id not in project['references']:
                raise StateError('request reference no longer exists')
            reference = project['references'][reference_id]
            self._reference_scope(project, reference.get('scope'), target)
            refs.append(copy.deepcopy(reference))
        if request['operation']['type'] == 'generative_edit':
            parent = self._version(project, request['parent_version_id'])
            bases = [ref for ref in refs if any(role['role'] == 'base_image' for role in ref['roles'])]
            if any(ref['asset_id'] != parent['asset_id'] for ref in bases):
                raise StateError('base-image reference must match the explicit parent version')
            if not bases:
                refs.insert(0, {'id': 'base_' + parent['id'], 'asset_id': parent['asset_id'],
                               'roles': [{'role': 'base_image', 'borrow': 'editable image', 'exclude': [],
                                          'strength': 'required', 'source': {'kind': 'user_implied_edit_preservation'}}],
                               'scope': {'kind': 'work', 'id': target['work_id']},
                               'provenance': {'version_id': parent['id']}})
        group = project['groups'].get(target.get('group_id'))
        item = project['items'].get(target.get('item_id'))
        anchor, anchor_errors = None, []
        if item and group and group['kind'] in ('visual_series', 'narrative_series'):
            if 'anchor_binding' not in item:
                if group.get('anchor') or request.get('prepared_from', {}).get('anchor'):
                    anchor_errors.append('legacy item anchor use is ambiguous; set_item_anchor explicitly')
            elif item['anchor_binding'] is not None:
                binding = item['anchor_binding']
                anchor = copy.deepcopy(group.get('anchor') if binding['follow'] else binding['anchor'])
                if anchor is None:
                    anchor_errors.append('bound series anchor unavailable')
                else:
                    version = self._version(project, anchor['version_id'])
                    for rid in binding['reference_ids']:
                        matches = [r for r in refs if r['id'] == rid]
                        if len(matches) != 1:
                            anchor_errors.append('bound anchor reference must be explicitly submitted')
                            continue
                        ref = matches[0]
                        if any(r['role'] not in anchor['borrowed_roles'] for r in ref['roles']):
                            anchor_errors.append('new anchor roles conflict with the explicit reference binding')
                            continue
                        # A derived immutable snapshot; the original Reference and old Requests stay intact.
                        ref['id'] = rid + '@' + version['id']
                        ref['asset_id'] = version['asset_id']
                        ref['provenance'] = {**ref['provenance'], 'anchor_reference_id': rid,
                                             'anchor_version_id': version['id']}
        inputs = {'effective_requirements': resolved['effective_requirements'], 'conflicts': resolved['conflicts'],
                  'references': refs, 'target': copy.deepcopy(target),
                  'parent_version_id': request.get('parent_version_id'),
                  'anchor': anchor, 'anchor_errors': anchor_errors,
                  'dependencies': copy.deepcopy(item.get('depends_on', [])) if item else []}
        return inputs

    def _input_fingerprint(self, inputs, request):
        return digest({'inputs': inputs, 'design': request['design'], 'operation': request['operation']})

    def _preflight(self, project, request, inputs, capabilities):
        problems = list(inputs.get('anchor_errors', []))
        if inputs['conflicts']:
            problems.append('unresolved requirement conflicts')
        for ref in inputs['references']:
            try:
                self._asset_path(project, ref['asset_id'])
            except StateError as exc:
                problems.append(str(exc))
        if request.get('parent_version_id'):
            try:
                self._version(project, request['parent_version_id'], readable=True)
            except StateError as exc:
                problems.append(str(exc))
        if inputs.get('anchor'):
            try:
                anchor = self._version(project, inputs['anchor']['version_id'], readable=True)
                self._check_series_version(anchor)
            except (StateError, KeyError) as exc:
                problems.append(f'series anchor unavailable: {exc}')
        for dependency in inputs['dependencies']:
            if not isinstance(dependency, dict) or not dependency.get('version_id'):
                problems.append('dependency must bind a concrete version before submission')
            else:
                try:
                    version = self._version(project, dependency['version_id'], readable=True)
                    self._check_series_version(version)
                    upstream = project['items'].get(dependency.get('item_id'), {})
                    if version['id'] not in upstream.get('version_ids', []):
                        raise StateError('dependency version no longer belongs to its upstream item')
                except StateError as exc:
                    problems.append(str(exc))
        gate = check_operation(capabilities, request['operation']['type'], len(inputs['references']), request['units'])
        problems.extend(gate['blocking'])
        operation_status = capabilities['operations'].get(request['operation']['type'], 'unknown')
        if operation_status == 'unknown':
            problems.append('operation capability unknown; adapter preparation must establish it')
        if inputs['references']:
            carriers = capabilities['reference_input']['carriers']
            if carriers['status'] != 'known' or 'local_file' not in carriers['value']:
                problems.append('local reference input carrier not established')
        return problems, gate

    def prepare(self, work_id, authorization_id, adapter, capabilities, operation=None,
                parent_version_id=None, changes=(), references=(), design=None,
                group_id=None, item_id=None, begin_context=True):
        with self.store.lock():
            project = self._load()
            return self._prepare_locked(project, work_id, authorization_id, adapter, capabilities,
                                        operation=operation, parent_version_id=parent_version_id,
                                        changes=changes, references=references, design=design,
                                        group_id=group_id, item_id=item_id, begin_context=begin_context)

    def _prepare_locked(self, project, work_id, authorization_id, adapter, capabilities, operation=None,
                        parent_version_id=None, changes=(), references=(), design=None,
                        group_id=None, item_id=None, begin_context=True,
                        preserved_changes=None, preserved_epoch=None, retry_of=None,
                        budget_kind='initial', duplicate_risk_ack=None, goal_id=None):
        work = self._work(project, work_id)
        operation = copy.deepcopy(operation or {'type': 'generate'})
        if operation.get('type') not in _EXTERNAL_OPERATIONS:
            raise StateError('prepare is only for external image operations')
        units = _positive_integer(operation.setdefault('expected_outputs', 1), 'expected_outputs')
        cap = validate_capabilities(capabilities)
        if adapter != cap['adapter_id']:
            raise StateError('capability adapter identity mismatch')
        target = self._target(project, work_id, group_id, item_id)
        if begin_context and group_id is not None and len(project['groups'][group_id]['item_ids']) > 1:
            project['target_resolution'] = 'ambiguous'
        if operation['type'] == 'generative_edit' and parent_version_id is None:
            if project['target_resolution'] == 'ambiguous':
                raise StateError('ambiguous editing target; specify a version')
            parent_version_id = work.get('edit_target_version_id')
            if parent_version_id is None:
                raise StateError('editing requires a readable parent version')
        if parent_version_id is not None and self._version(project, parent_version_id)['work_id'] != work_id:
            raise StateError('parent version belongs to another work')
        request_id = new_id('request')
        local_changes = (copy.deepcopy(preserved_changes) if preserved_changes is not None else
                         self._requirements(project, changes, {'kind': 'branch', 'id': request_id}, request_id))
        if begin_context:
            project['interaction_epoch'] += 1
            project['active_work_id'] = work_id
            project['active_group_id'] = group_id
        epoch = project['interaction_epoch'] if preserved_epoch is None else preserved_epoch
        request = {'id': request_id, 'schema_version': 1, 'created_at': utc_now(),
                   'intent': operation['type'], 'target': target, 'parent_version_id': parent_version_id,
                   'local_changes': local_changes, 'reference_ids': list(references),
                   'design': copy.deepcopy(design or {}), 'operation': operation,
                   'authorization_id': authorization_id, 'adapter': adapter, 'units': units,
                   'goal_id': goal_id or new_id('goal'), 'host_requirements': {},
                   'capabilities': cap, 'interaction_epoch': epoch,
                   'expected_edit_target': work.get('edit_target_version_id')}
        auth = self._authorization(project, request)
        request['authorization'] = copy.deepcopy(auth)
        inputs = self._resolve_inputs(project, request)
        problems, gate = self._preflight(project, request, inputs, cap)
        request.update(effective_requirements=inputs['effective_requirements'], references=inputs['references'],
                       conflicts=inputs['conflicts'], prepared_from=inputs,
                       input_fingerprint=self._input_fingerprint(inputs, request), capability_check=gate)
        state = 'failed' if problems else 'prepared'
        attempt = {'id': new_id('attempt'), 'schema_version': 1, 'created_at': utc_now(),
                   'request_id': request_id, 'authorization_id': authorization_id, 'adapter': adapter,
                   'state': state, 'submission': 'not_submitted', 'units': units,
                   'budget_kind': budget_kind, 'goal_id': request['goal_id'], 'retry_of': retry_of,
                   'duplicate_risk_ack': copy.deepcopy(duplicate_risk_ack),
                   'dispatch': {'submission': 'not_submitted', 'adapter': adapter},
                   'outputs': [], 'transitions': [{'from': None, 'to': state, 'time': utc_now(),
                                                  'reason': 'validation_failed' if problems else 'prepared',
                                                  'evidence': {'problems': problems}}],
                   'error': {'problems': problems} if problems else None,
                   'recovery': {'query_supported': cap['query'], 'disposition': 'pending'}}
        self.store.write_record('requests', request_id, request, immutable=True)
        self.store.write_record('attempts', attempt['id'], attempt, immutable=True)
        project['requirements'].update({r['id']: copy.deepcopy(r) for r in local_changes})
        project['request_ids'].append(request_id)
        project['attempt_ids'].append(attempt['id'])
        if item_id is not None:
            item = project['items'][item_id]
            item['request_ids'].append(request_id)
            item['request_id'] = request_id
            item['completion'] = 'blocked' if problems else 'ready'
        project = self._commit(project)
        return {'status': 'blocked' if problems else 'prepared', 'request': request, 'attempt': attempt, 'project': project}

    def _transition(self, attempt, state, submission, reason, evidence):
        attempt['transitions'].append({'from': attempt['state'], 'to': state, 'time': utc_now(),
                                       'reason': reason, 'evidence': copy.deepcopy(evidence)})
        attempt['state'] = state
        attempt['submission'] = submission
        attempt['dispatch']['submission'] = submission
        attempt['updated_at'] = utc_now()

    def _budget(self, project, request, attempt):
        auth = self._authorization(project, request)
        totals = {'initial': 0, 'retry': 0}
        for prior in self.store.list_records('attempts'):
            if prior.get('authorization_id') == auth['id'] and prior.get('submission') != 'not_submitted':
                kind = prior.get('budget_kind')
                if kind not in totals or type(prior.get('units')) is not int or prior['units'] < 1:
                    raise StateError('unverifiable attempt budget; submission blocked')
                totals[kind] += prior['units']
        kind = attempt['budget_kind']
        limit = auth['requested_count'] if kind == 'initial' else auth['retry_budget']
        if totals[kind] + attempt['units'] > limit:
            raise StateError(f'{kind} submission budget exhausted; unknown attempts remain reserved')
        return totals

    def _acknowledged_retry_risks(self, attempt):
        """Keep explicit acknowledgements attached to their exact retry ancestry."""
        acknowledged, seen = set(), {attempt['id']}
        child = attempt
        while child.get('retry_of'):
            parent_id = child['retry_of']
            if parent_id in seen:
                raise StateError('invalid cyclic retry lineage')
            seen.add(parent_id)
            parent = self.store.read_record('attempts', parent_id)
            if (parent['authorization_id'] != attempt['authorization_id']
                    or parent['goal_id'] != attempt['goal_id']):
                raise StateError('retry lineage crosses authorization or goal')
            ack = child.get('duplicate_risk_ack')
            if isinstance(ack, (dict, list)) and ack:
                acknowledged.add(parent_id)
            child = parent
        return acknowledged

    def dispatch(self, attempt_id, capabilities=None, input_reference_map=None):
        with self.store.lock():
            project = self._load()
            attempt = self.store.read_record('attempts', attempt_id)
            request = self.store.read_record('requests', attempt['request_id'])
            if attempt['state'] != 'prepared' or attempt['submission'] != 'not_submitted':
                raise StateError('attempt is not prepared; never dispatch it again')
            if request['id'] not in project['request_ids']:
                raise StateError('request was not committed to project state')
            self._authorization(project, request)
            cap = validate_capabilities(capabilities or request['capabilities'])
            if cap['adapter_id'] != request['adapter']:
                raise StateError('cannot switch the authorized adapter during dispatch')
            inputs = self._resolve_inputs(project, request)
            if self._input_fingerprint(inputs, request) != request['input_fingerprint']:
                self._transition(attempt, 'cancelled', 'not_submitted', 'stale_inputs', {'new_inputs': inputs})
                self.store.write_record('attempts', attempt_id, attempt)
                target = request['target']
                result = self._prepare_locked(project, target['work_id'], request['authorization_id'], request['adapter'], cap,
                                              operation=request['operation'], parent_version_id=request.get('parent_version_id'),
                                              references=request['reference_ids'], design=request['design'],
                                              group_id=target.get('group_id'), item_id=target.get('item_id'),
                                              begin_context=False, preserved_changes=request['local_changes'],
                                              preserved_epoch=request['interaction_epoch'], retry_of=attempt.get('retry_of'),
                                              budget_kind=attempt['budget_kind'], duplicate_risk_ack=attempt.get('duplicate_risk_ack'),
                                              goal_id=request['goal_id'])
                result['status'] = 'reprepared' if result['status'] == 'prepared' else 'blocked'
                result['replaced_attempt_id'] = attempt_id
                return result
            problems, gate = self._preflight(project, request, inputs, cap)
            if problems:
                self._transition(attempt, 'failed', 'not_submitted', 'preflight_failed', {'problems': problems})
                attempt['error'] = {'problems': problems}
                self.store.write_record('attempts', attempt_id, attempt)
                return {'status': 'blocked', 'attempt': attempt, 'request': request, 'project': project}
            self._budget(project, request, attempt)
            acknowledged_risks = self._acknowledged_retry_risks(attempt)
            for other in self.store.list_records('attempts'):
                if (other['id'] != attempt_id and other['state'] == 'unknown'
                        and other.get('dispatch', {}).get('request_fingerprint') == request['input_fingerprint']
                        and other['id'] not in acknowledged_risks):
                    raise StateError('duplicate risk: equivalent input has an unknown attempt')
            default_map = [{'tool_index': i, 'reference_id': ref['id'], 'asset_id': ref['asset_id']}
                           for i, ref in enumerate(inputs['references'])]
            mapping = copy.deepcopy(default_map if input_reference_map is None else input_reference_map)
            if (not isinstance(mapping, list) or len(mapping) != len(default_map)
                    or any(not isinstance(m, dict) or type(m.get('tool_index')) is not int for m in mapping)
                    or sorted(m['tool_index'] for m in mapping) != list(range(len(mapping)))
                    or sorted((m.get('reference_id'), m.get('asset_id')) for m in mapping)
                       != sorted((m['reference_id'], m['asset_id']) for m in default_map)):
                raise StateError('actual input reference map must include each declared reference exactly once')
            attempt['dispatch'].update(started_at=utc_now(), capabilities=cap,
                                       request_fingerprint=request['input_fingerprint'],
                                       input_reference_map=mapping, capability_check=gate)
            self._transition(attempt, 'dispatching', 'uncertain', 'before_external_call', {'request_id': request['id']})
            self.store.write_record('attempts', attempt_id, attempt)
            self.store.hit('dispatch_persisted')
            return {'status': 'dispatching', 'request': request, 'attempt': attempt, 'project': project}

    def cancel_prepared(self, attempt_id, evidence):
        _evidence(evidence)
        with self.store.lock():
            attempt = self.store.read_record('attempts', attempt_id)
            if attempt['state'] != 'prepared' or attempt['submission'] != 'not_submitted':
                raise StateError('external cancellation requires actual tool confirmation')
            self._transition(attempt, 'cancelled', 'not_submitted', 'user_cancelled_before_submission', evidence)
            self.store.write_record('attempts', attempt_id, attempt)
            return {'attempt': attempt}

    def retry(self, attempt_id, duplicate_risk_ack=None):
        with self.store.lock():
            project = self._load()
            old = self.store.read_record('attempts', attempt_id)
            request = self.store.read_record('requests', old['request_id'])
            if old['state'] not in ('failed', 'cancelled', 'unknown'):
                raise StateError('only failed, cancelled or unknown attempts can be retried')
            if old['state'] == 'unknown':
                _evidence(duplicate_risk_ack, 'duplicate risk acknowledgement')
            kind = old['budget_kind'] if old['submission'] == 'not_submitted' else 'retry'
            candidate = {'budget_kind': kind, 'units': request['units']}
            self._budget(project, request, candidate)
            target = request['target']
            return self._prepare_locked(project, target['work_id'], request['authorization_id'], request['adapter'], request['capabilities'],
                                        operation=request['operation'], parent_version_id=request.get('parent_version_id'),
                                        references=request['reference_ids'], design=request['design'],
                                        group_id=target.get('group_id'), item_id=target.get('item_id'),
                                        begin_context=True, preserved_changes=request['local_changes'],
                                        retry_of=attempt_id, budget_kind=kind,
                                        duplicate_risk_ack=duplicate_risk_ack, goal_id=request['goal_id'])

    def _protocol_error(self, attempt, error, evidence):
        attempt.setdefault('protocol_errors', []).append({'time': utc_now(), 'error': str(error),
                                                        'observation': copy.deepcopy(evidence)})
        if attempt['state'] not in _TERMINAL:
            submitted = attempt['submission']
            if submitted == 'not_submitted':
                state = 'failed'
            else:
                state = 'unknown'
            self._transition(attempt, state, submitted, 'invalid_adapter_observation', {'error': str(error)})
        self.store.write_record('attempts', attempt['id'], attempt)

    def _adopt(self, project, request, attempt, auto_target=False):
        added = []
        target = request['target']
        work = self._work(project, target['work_id'])
        for output in attempt['outputs']:
            key = output['adoption_key']
            if key in project['adoptions']:
                if project['adoptions'][key] != output['version']['id']:
                    raise StateError('adoption key already identifies a different version')
                continue
            asset = output['asset']
            path = self.store.project_path(asset['locator'])
            if not path.is_file() or sha256_file(path) != asset['sha256']:
                continue  # A missing saved image is not reconstructed from a nearby file.
            inspect_image(path)
            self._register_version(project, output['version'], asset, target.get('group_id'), target.get('item_id'))
            project['adoptions'][key] = output['version']['id']
            added.append(output['version']['id'])
        if not added:
            return added
        owns_context = (project['interaction_epoch'] == request['interaction_epoch']
                        and project['active_work_id'] == work['id']
                        and work.get('edit_target_version_id') == request.get('expected_edit_target'))
        if len(attempt['outputs']) > 1:
            if owns_context or work.get('auto_target_from_attempt_id') == attempt['id']:
                project['target_resolution'] = 'ambiguous'
                project['interaction_epoch'] += 1
                work['auto_target_from_attempt_id'] = None
        elif (auto_target and owns_context and request['units'] == 1 and target.get('group_id') is None):
            work['edit_target_version_id'] = added[0]
            work['auto_target_from_attempt_id'] = attempt['id']
            project['target_resolution'] = 'resolved'
            project['interaction_epoch'] += 1
        return added

    def _refresh_groups(self, project, attempts):
        for item in project['items'].values():
            linked = []
            for attempt in attempts:
                req = self.store.read_record('requests', attempt['request_id'])
                if req['target'].get('item_id') == item['id']:
                    linked.append(attempt)
            unresolved = [a['id'] for a in linked if a['state'] == 'unknown']
            item['unresolved_attempt_ids'] = unresolved
            if item['version_ids']:
                if item['completion'] != 'delivered':
                    item['completion'] = 'available'
            elif unresolved:
                item['completion'] = 'unresolved'
            elif any(a['state'] in ('dispatching', 'accepted', 'running') for a in linked):
                item['completion'] = 'in_progress'
            elif any(a['state'] == 'prepared' for a in linked):
                item['completion'] = 'ready'
            elif linked:
                item['completion'] = 'cancelled' if all(a['state'] == 'cancelled' for a in linked) else 'blocked'

    def observe(self, attempt_id, state, submission, evidence, outputs=(), handle=None, tool_call_id=None,
                *, auto_target=True):
        if type(auto_target) is not bool:
            raise StateError('auto_target must be boolean')
        with self.store.lock():
            project = self._load()
            attempt = self.store.read_record('attempts', attempt_id)
            request = self.store.read_record('requests', attempt['request_id'])
            try:
                validate_observation(state, submission, evidence)
                if state in ('prepared', 'dispatching'):
                    raise StateError('an adapter cannot report internal prepared/dispatching states')
                if attempt['state'] == 'prepared':
                    raise StateError('external observation before the persisted dispatch boundary')
                if attempt['state'] in _TERMINAL and state != attempt['state']:
                    raise StateError('terminal execution facts cannot be replaced by a contradictory observation')
                if attempt['submission'] == 'submitted' and submission != 'submitted':
                    raise StateError('observation cannot erase a known submitted fact')
                if attempt['submission'] == 'not_submitted' and submission != 'not_submitted':
                    raise StateError('observation cannot invent an unrecorded external call')
            except ValueError as exc:
                self._protocol_error(attempt, exc, {'state': state, 'submission': submission, 'evidence': evidence})
                return {'status': 'protocol_error', 'attempt': attempt, 'request': request, 'project': project}

            if handle is not None:
                attempt['dispatch']['handle'] = copy.deepcopy(handle)
                attempt['recovery']['query_locator'] = copy.deepcopy(handle)
            if tool_call_id is not None:
                if attempt['dispatch'].get('tool_call_id') not in (None, tool_call_id):
                    self._protocol_error(attempt, 'tool call identity changed', {'tool_call_id': tool_call_id})
                    return {'status': 'protocol_error', 'attempt': attempt, 'project': project}
                attempt['dispatch']['tool_call_id'] = tool_call_id

            errors = []
            for entry in outputs:
                try:
                    if not isinstance(entry, dict):
                        raise StateError('output must be an object')
                    index = _positive_integer(entry.get('output_index'), 'output_index', allow_zero=True)
                    provenance = entry.get('provenance', {})
                    if (provenance.get('verified') is not True or provenance.get('attempt_id') != attempt_id
                            or not isinstance(provenance.get('source'), str) or not provenance['source']):
                        raise StateError('output requires verified attribution to this attempt')
                    call_id = attempt['dispatch'].get('tool_call_id')
                    if call_id is not None and provenance.get('tool_call_id') != call_id:
                        raise StateError('output tool call identity does not match its attempt')
                    asset = self._materialize(entry['path'], provenance)
                    key = f'{attempt_id}:{index}'
                    existing = next((o for o in attempt['outputs'] if o['output_index'] == index), None)
                    if existing:
                        if existing['asset']['sha256'] != asset['sha256']:
                            raise StateError('stable output index cannot change its image bytes')
                        continue
                    version_id = 'version_' + uuid.uuid5(uuid.NAMESPACE_URL, f'{project["id"]}:{key}').hex
                    origin = 'generative_edit' if request['operation']['type'] == 'generative_edit' else 'generated'
                    version = self._version_record(project, request, asset, origin, attempt_id=attempt_id, version_id=version_id)
                    attempt['outputs'].append({'output_index': index, 'adoption_key': key, 'asset': asset, 'version': version})
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append({'output_index': entry.get('output_index') if isinstance(entry, dict) else None, 'error': str(exc)})
            attempt['outputs'].sort(key=lambda item: item['output_index'])
            if errors:
                attempt.setdefault('output_errors', []).extend(errors)
                if state == 'succeeded':
                    state = 'unknown'
                evidence = {'adapter_evidence': copy.deepcopy(evidence), 'output_errors': errors}
            if state == 'succeeded' and not attempt['outputs']:
                state = 'failed'
                evidence = {'adapter_evidence': copy.deepcopy(evidence), 'artifact_error': 'no verified readable output'}
            # A not-submitted failure can never justify an externally generated candidate.
            if submission == 'not_submitted' and attempt['outputs']:
                raise StateError('external outputs contradict a not-submitted observation')
            if attempt['state'] in _TERMINAL and state != attempt['state']:
                self._protocol_error(attempt, 'output validation conflicts with previous terminal state', evidence)
                return {'status': 'protocol_error', 'attempt': attempt, 'project': project}
            self._transition(attempt, state, submission, 'adapter_observation', evidence)
            if state in _TERMINAL:
                attempt['dispatch']['finished_at'] = utc_now()
            self.store.write_record('attempts', attempt_id, attempt)
            self.store.hit('attempt_outputs_saved')
            added = self._adopt(project, request, attempt, auto_target=auto_target)
            self._refresh_groups(project, self.store.list_records('attempts'))
            if added or request['target'].get('group_id'):
                project = self._commit(project)
            return {'status': state, 'attempt': attempt, 'request': request, 'added_version_ids': added, 'project': project}

    def acknowledge_unknown(self, attempt_id, evidence, stop_active_query=True):
        _evidence(evidence)
        with self.store.lock():
            attempt = self.store.read_record('attempts', attempt_id)
            if attempt['state'] != 'unknown':
                raise StateError('only an unknown attempt needs this acknowledgement')
            attempt['recovery'].update(disposition='user_accepted_uncertain', acknowledgement=copy.deepcopy(evidence),
                                       stop_active_query=bool(stop_active_query), acknowledged_at=utc_now())
            self.store.write_record('attempts', attempt_id, attempt)
            return {'attempt': attempt}

    def recover(self):
        with self.store.lock():
            project = self.store.load()
            recovery = copy.deepcopy(self.store.recovery_report)
            attempts = self.store.list_records('attempts')
            before = digest(project)
            actions = []
            warnings = []
            for attempt in attempts:
                request = self.store.read_record('requests', attempt['request_id'])
                if attempt['state'] == 'dispatching':
                    self._transition(attempt, 'unknown', attempt['submission'], 'recovery_without_terminal_receipt',
                                     {'recorded_dispatch': attempt['dispatch']})
                    self.store.write_record('attempts', attempt['id'], attempt)
                if attempt['state'] in ('accepted', 'running', 'unknown'):
                    supported = attempt['recovery'].get('query_supported') == 'supported'
                    has_locator = bool(attempt['recovery'].get('query_locator'))
                    action = 'query' if supported and has_locator else 'verify_known_outputs_or_wait'
                    if attempt['recovery'].get('stop_active_query'):
                        action = 'user_acknowledged_no_active_query'
                    actions.append({'attempt_id': attempt['id'], 'action': action, 'automatic_resubmit': False})
                if request['id'] not in project['request_ids'] and attempt['submission'] == 'not_submitted':
                    warnings.append({'attempt_id': attempt['id'], 'reason': 'uncommitted_prepared_record; not scheduled'})
                    continue
                if recovery['degraded']:
                    continue
                if request['target']['work_id'] not in project['works']:
                    warnings.append({'attempt_id': attempt['id'], 'reason': 'work state missing; no inferred adoption'})
                    continue
                if attempt['id'] not in project['attempt_ids']:
                    project['attempt_ids'].append(attempt['id'])
                if request['id'] not in project['request_ids']:
                    project['request_ids'].append(request['id'])
                self._adopt(project, request, attempt, auto_target=False)
            if not recovery['degraded']:
                for asset in project['assets'].values():
                    path = self.store.project_path(asset['locator'])
                    if not path.is_file():
                        asset['availability'] = 'missing'
                    elif sha256_file(path) != asset['sha256']:
                        asset['availability'] = 'unreadable'
                    else:
                        try:
                            inspect_image(path)
                            asset['availability'] = 'local_verified'
                        except ValueError:
                            asset['availability'] = 'unreadable'
                for work in project['works'].values():
                    for field in ('latest_version_id', 'selected_version_id', 'edit_target_version_id'):
                        version = project['versions'].get(work.get(field))
                        work[field.replace('_id', '_status')] = ('none' if version is None else
                            'available' if project['assets'][version['asset_id']]['availability'] == 'local_verified' else 'missing')
                self._refresh_groups(project, attempts)
                if digest(project) != before:
                    project = self._commit(project)
                else:
                    self.store.repair_mirror()
            return {'project': project, 'attempts': attempts, 'recovery': recovery, 'actions': actions,
                    'warnings': warnings, 'unresolved_attempt_ids': [a['id'] for a in attempts if a['state'] == 'unknown']}

    def import_asset(self, path):
        with self.store.lock():
            project = self._load()
            asset = self._materialize(path, {'kind': 'imported_asset', 'source_path': str(Path(path).expanduser().resolve())})
            self._register_asset(project, asset)
            return {'asset': asset, 'project': self._commit(project)}

    def local_edit(self, version_id, operations, mode='append'):
        if mode not in ('append', 'replace'):
            raise StateError('local edit mode must be append or replace')
        if not isinstance(operations, list) or (mode == 'append' and not operations):
            raise StateError('local edit requires explicit operations')
        with self.store.lock():
            project = self._load()
            parent = self._version(project, version_id, readable=True)
            if resolve_requirements(project, parent['work_id'], version_id)['conflicts']:
                raise StateError('unresolved requirement conflicts block local editing')
            old_pipeline = parent.get('edit_pipeline')
            warnings = []
            master_id = parent['asset_id']
            prior_operations = []
            if old_pipeline:
                try:
                    from .local_edits import ENGINE_VERSION
                    self._asset_path(project, old_pipeline['master_asset_id'])
                    for operation in old_pipeline['operations']:
                        if operation.get('engine_version') != ENGINE_VERSION:
                            raise StateError('old pipeline engine is unavailable')
                        if operation.get('mask_asset_id'):
                            self._asset_path(project, operation['mask_asset_id'])
                    master_id = old_pipeline['master_asset_id']
                    prior_operations = copy.deepcopy(old_pipeline['operations'])
                except (StateError, KeyError):
                    warnings.append('Previous pipeline cannot be replayed; selected readable image is the new master. Earlier processing is not losslessly reversible.')
            elif parent['origin'] == 'export':
                warnings.append('Selected export has no replayable edit pipeline; its actual pixels are the new master. Earlier processing is not losslessly reversible.')
            incoming = copy.deepcopy(operations)
            for operation in incoming:
                if not isinstance(operation, dict):
                    raise StateError('local operation must be an object')
                operation.setdefault('id', new_id('operation'))
            combined = (prior_operations + incoming) if mode == 'append' else incoming
            masks = {}
            for operation in combined:
                if operation.get('mask_asset_id'):
                    masks[operation['mask_asset_id']] = self._asset_path(project, operation['mask_asset_id'])
            with tempfile.TemporaryDirectory(prefix='.local-edit-', dir=self.store.root) as temporary:
                output = Path(temporary) / 'render.png'
                render = render_pipeline(self._asset_path(project, master_id), combined, output, mask_paths=masks)
                asset = self._materialize(output, {'kind': 'local_edit', 'parent_version_id': version_id},
                                          derived_from=parent['asset_id'], transform=render['operations'])
            project['interaction_epoch'] += 1
            request = self._new_local_request(project, parent['work_id'], 'local_edit', version_id,
                                               {'type': 'local_edit', 'operations': render['operations'], 'mode': mode})
            pipeline = {'master_asset_id': master_id, 'operations': render['operations'], 'render_asset_id': asset['id']}
            version = self._version_record(project, request, asset, 'local_edit', pipeline=pipeline)
            version['checks']['encoding_check'] = {'status': 'pass', 'evidence': 'saved output reopened by deterministic renderer'}
            self._register_version(project, version, asset)
            work = project['works'][parent['work_id']]
            work['edit_target_version_id'] = version['id']
            work['auto_target_from_attempt_id'] = None
            project['active_work_id'] = work['id']
            project['target_resolution'] = 'resolved'
            return {'version': version, 'asset': asset, 'project': self._commit(project), 'warnings': warnings + render['warnings']}

    def export_version(self, version_id, image_format='png'):
        extension = {'png': '.png', 'jpeg': '.jpg', 'jpg': '.jpg', 'webp': '.webp', 'tiff': '.tiff'}.get(image_format.lower())
        if extension is None:
            raise StateError('unsupported export format')
        with self.store.lock():
            project = self._load()
            parent = self._version(project, version_id, readable=True)
            with tempfile.TemporaryDirectory(prefix='.export-', dir=self.store.root) as temporary:
                output = Path(temporary) / ('export' + extension)
                render = render_pipeline(self._asset_path(project, parent['asset_id']), [], output)
                asset = self._materialize(output, {'kind': 'export', 'parent_version_id': version_id},
                                          derived_from=parent['asset_id'], transform={'format': image_format})
            request = self._new_local_request(project, parent['work_id'], 'export', version_id,
                                               {'type': 'export', 'format': image_format})
            pipeline = None
            if extension != '.jpg' and parent.get('edit_pipeline'):
                pipeline = copy.deepcopy(parent['edit_pipeline'])
                pipeline['render_asset_id'] = asset['id']
            version = self._version_record(project, request, asset, 'export', pipeline=pipeline)
            version['checks']['encoding_check'] = {'status': 'pass', 'format': image_format, 'warnings': render['warnings']}
            self._register_version(project, version, asset)
            return {'version': version, 'asset': asset, 'project': self._commit(project), 'warnings': render['warnings']}

    def record_check(self, version_id, category, status, evidence, note=''):
        if category not in ('artifact_integrity', 'requirement_checks', 'visual_observations', 'encoding_check'):
            raise StateError('user choices must use select_version; unknown check category')
        if status not in ('pass', 'fail', 'unclear', 'not_applicable'):
            raise StateError('invalid check status')
        _evidence(evidence)
        with self.store.lock():
            project = self._load()
            self._version(project, version_id)
            record = {'id': new_id('check'), 'version_id': version_id, 'category': category, 'status': status,
                      'evidence': copy.deepcopy(evidence), 'note': note, 'created_at': utc_now()}
            project.setdefault('check_records', {})[record['id']] = record
            return {'check': record, 'project': self._commit(project)}
