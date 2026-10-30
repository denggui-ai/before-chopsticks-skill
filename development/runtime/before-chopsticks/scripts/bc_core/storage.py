"""Atomic local project snapshots and execution records. No external calls.

The immutable revision chain is authoritative for user state; project.json is
only a mirror. Attempts are independent execution facts and are never removed
because the project mirror or a revision is damaged. OS advisory locks serialize
writers without treating an unrelated process as a running image job.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class StateError(ValueError):
    """An operation cannot be performed without losing known state."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f'{prefix}_{uuid.uuid4().hex}'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def sync_dir(path):
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, data, immutable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical(data) + b'\n'
    descriptor, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        if immutable:
            try:
                os.link(tmp, path)
            except FileExistsError as exc:
                raise StateError(f'immutable record already exists: {path.name}') from exc
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ProjectStore:
    def __init__(self, root, failpoint=None):
        self.root = Path(root).expanduser().resolve()
        self.failpoint = failpoint
        self.recovery_report = {'degraded': False, 'issues': []}
        self._digest = None
        self._locked = False

    def hit(self, stage):
        if self.failpoint is not None:
            self.failpoint(stage)

    def project_path(self, relative):
        relative = Path(relative)
        if relative.is_absolute() or '..' in relative.parts:
            raise StateError('project path must be relative and cannot escape the project')
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise StateError('project path resolves outside the project')
        return path

    def record_path(self, kind, record_id):
        if kind not in ('requests', 'attempts'):
            raise StateError('invalid record kind')
        if not isinstance(record_id, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,127}', record_id):
            raise StateError('invalid record id')
        return self.project_path(f'{kind}/{record_id}.json')

    @contextmanager
    def lock(self):
        if self._locked:
            raise StateError('nested project lock is not supported')
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.project_path('state/write.lock')
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a+', encoding='utf-8') as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateError('project is locked by another writer') from exc
            self._locked = True
            try:
                f.seek(0)
                f.truncate()
                json.dump({'pid': os.getpid(), 'task_id': new_id('writer'), 'started_at': utc_now()}, f)
                f.flush()
                os.fsync(f.fileno())
                yield
            finally:
                self._locked = False
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def initialize(self, name='Untitled'):
        with self.lock():
            revisions = self.project_path('state/revisions')
            if (self.root / 'project.json').exists() or (revisions.exists() and any(revisions.iterdir())):
                raise StateError('project already exists; initialization cannot overwrite it')
            for directory in ('requests', 'attempts', 'assets', 'state/revisions'):
                self.project_path(directory).mkdir(parents=True, exist_ok=True)
            project = {
                'id': new_id('project'), 'name': name, 'schema_version': 1, 'revision': 0,
                'created_at': utc_now(), 'interaction_epoch': 0,
                'active_work_id': None, 'active_group_id': None, 'target_resolution': 'none',
                'works': {}, 'groups': {}, 'items': {}, 'requirements': {}, 'references': {},
                'assets': {}, 'versions': {}, 'authorizations': {}, 'adoptions': {},
                'display_labels': {}, 'request_ids': [], 'attempt_ids': [], 'notices': [],
            }
            self._save_snapshot(project, previous=None)
            return copy.deepcopy(project)

    def _save_snapshot(self, project, previous):
        revision = project['revision']
        payload = {'schema_version': 1, 'revision': revision, 'previous_sha256': previous, 'project': project}
        envelope = {**payload, 'sha256': digest(payload)}
        atomic_json(self.project_path(f'state/revisions/{revision:010d}.json'), envelope, immutable=True)
        self.hit('snapshot_committed')
        atomic_json(self.project_path('project.json'), project)
        self.hit('mirror_updated')
        self._digest = envelope['sha256']

    def load(self):
        revisions = self.project_path('state/revisions')
        if not revisions.is_dir():
            raise StateError('no project snapshots; initialize a project first')
        files = sorted(revisions.glob('*.json'))
        previous = None
        project = None
        issues = []
        expected = 0
        for path in files:
            try:
                if not path.resolve().is_relative_to(self.root):
                    raise StateError('revision file resolves outside the project')
                if not re.fullmatch(r'\d{10}\.json', path.name) or int(path.stem) != expected:
                    raise StateError(f'revision chain gap or invalid file at {path.name}')
                raw = json.loads(path.read_text(encoding='utf-8'))
                supplied = raw.pop('sha256')
                if digest(raw) != supplied or raw['previous_sha256'] != previous:
                    raise StateError(f'revision checksum/parent mismatch: {path.name}')
                candidate = raw['project']
                if (raw.get('schema_version') != 1 or raw.get('revision') != expected
                        or candidate.get('schema_version') != 1 or candidate.get('revision') != expected
                        or (project is not None and candidate.get('id') != project.get('id'))):
                    raise StateError(f'invalid revision identity: {path.name}')
                project = candidate
                previous = supplied
                expected += 1
            except (OSError, ValueError, KeyError, TypeError) as exc:
                issues.append(str(exc))
                break
        # The mirror is never a source of recovered user state. It can, however,
        # reveal that a newer committed revision has disappeared. Do not silently
        # call such a rollback a full recovery or overwrite its diagnostic mirror.
        if project is not None:
            try:
                mirror = json.loads(self.project_path('project.json').read_text(encoding='utf-8'))
                if (mirror.get('id') == project['id'] and type(mirror.get('revision')) is int
                        and mirror['revision'] > project['revision']):
                    issues.append('formal revision missing: mirror refers to a newer project state')
            except (OSError, ValueError, AttributeError):
                pass
        self.recovery_report = {'degraded': bool(issues), 'issues': issues, 'last_valid_revision': expected - 1}
        if project is None:
            raise StateError('no valid project snapshot; cannot infer user state')
        self._digest = previous
        return copy.deepcopy(project)

    def commit(self, project, expected_revision):
        if not self._locked:
            raise StateError('project write requires the project lock')
        current = self.load()
        if self.recovery_report['degraded']:
            raise StateError('degraded snapshot chain; confirm recovery before changing project state')
        if current['revision'] != expected_revision:
            raise StateError('stale project revision')
        if project.get('id') != current['id']:
            raise StateError('cannot change project identity')
        updated = copy.deepcopy(project)
        updated['revision'] = expected_revision + 1
        updated['updated_at'] = utc_now()
        self._save_snapshot(updated, previous=self._digest)
        return updated

    def require_healthy(self):
        if self.recovery_report.get('degraded'):
            raise StateError('degraded project state; affected submission is blocked')

    def write_record(self, kind, record_id, record, immutable=False):
        if record.get('id') != record_id:
            raise StateError('record identity mismatch')
        payload = {'record': record, 'sha256': digest(record)}
        atomic_json(self.record_path(kind, record_id), payload, immutable=immutable)

    def read_record(self, kind, record_id):
        try:
            raw = json.loads(self.record_path(kind, record_id).read_text(encoding='utf-8'))
            record = raw['record']
            if record.get('id') != record_id or digest(record) != raw['sha256']:
                raise StateError('record checksum or identity mismatch')
            return record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise StateError(f'cannot trust {kind}/{record_id}: {exc}') from exc

    def list_records(self, kind):
        folder = self.project_path(kind)
        if kind not in ('requests', 'attempts'):
            raise StateError('invalid record kind')
        return [self.read_record(kind, p.stem) for p in sorted(folder.glob('*.json'))]

    def repair_mirror(self):
        if not self._locked:
            raise StateError('mirror repair requires the project lock')
        project = self.load()
        self.require_healthy()
        atomic_json(self.project_path('project.json'), project)
        return project
