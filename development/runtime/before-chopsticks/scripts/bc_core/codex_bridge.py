"""Local bridge to the conversation's native image tool; never calls it itself.

Only the host assistant can invoke image_gen.imagegen. The bridge returns exact
arguments after persisting dispatch, then journals a host-observed tool return.
Local invocation IDs are correlation IDs, not invented remote job handles.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .engine import ProjectEngine
from .storage import StateError, atomic_json, digest, new_id, sha256_file, utc_now


ADAPTER_ID = 'codex-native-imagegen-v1'
TOOL_NAME = 'image_gen.imagegen'


def _nonempty(value, label):
    if not isinstance(value, dict) or not value:
        raise StateError(f'{label} requires nonempty evidence')
    digest(value)  # Reject non-JSON values and nonfinite floats.
    return copy.deepcopy(value)


def native_capabilities(interface_evidence):
    return {
        'schema_version': 1, 'adapter_id': ADAPTER_ID, 'observed_at': utc_now(),
        'operations': {'generate': 'supported', 'generative_edit': 'supported'},
        'reference_input': {'carriers': {'status': 'known', 'value': ['local_file']},
                            'max_count': {'status': 'known', 'value': 3}, 'ordered': 'supported'},
        'edit_modes': {'status': 'known', 'value': ['semantic_reference_edit']},
        'size_limits': {'status': 'unknown'},
        'output_limits': {'status': 'known', 'value': {'max_count': 1}},
        'cost_info': {'status': 'unknown'}, 'query': 'unsupported', 'cancel': 'unsupported',
        'interface_evidence': _nonempty(interface_evidence, 'current interface'),
        'adapter_scope_version': 2,
        'limitations': [
            'At most three edit inputs and one output are adapter limits, not discovered native tool maxima.',
            'Ordered means the submitted path array and role indices are recorded; visual role separation is not guaranteed.',
            'Brand-new generation omits image reference fields; multiple references are supported only for editing.',
            'The current exposed interface provides no remote query/cancel operation or job handle.',
            'Semantic editing has no pixel-preservation or region-mask guarantee.',
            'Requested framing is prompt guidance; exact dimensions and pricing are unknown.',
        ],
    }


class CodexBridge:
    def __init__(self, root, interface_evidence=None):
        self.engine = ProjectEngine(root)
        self.interface_evidence = interface_evidence

    def _path(self, attempt_id, filename):
        self.engine.store.record_path('attempts', attempt_id)  # Validate ID.
        return self.engine.store.project_path(f'host/codex-native/{attempt_id}/{filename}')

    def _read(self, path):
        envelope = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(envelope, dict) or digest(envelope.get('data')) != envelope.get('sha256'):
            raise StateError('native bridge record digest mismatch')
        return envelope['data']

    def _write(self, path, data):
        if path.exists():
            if self._read(path) != data:
                raise StateError('native bridge record is immutable')
            return
        atomic_json(path, {'data': data, 'sha256': digest(data)}, immutable=True)

    def _verify_call(self, attempt_id, call):
        attempt = self.engine.store.read_record('attempts', attempt_id)
        request = self.engine.store.read_record('requests', attempt['request_id'])
        if (call.get('attempt_id') != attempt_id or call.get('request_id') != request['id']
                or call.get('tool_name') != TOOL_NAME or attempt['adapter'] != ADAPTER_ID
                or request['adapter'] != ADAPTER_ID):
            raise StateError('native call does not belong to the current Attempt/Request')
        if call.get('payload_sha256') != digest(call.get('arguments')):
            raise StateError('native argument digest mismatch')
        refs = request['references']
        inputs = call.get('inputs')
        if not isinstance(inputs, list) or len(inputs) != len(refs):
            raise StateError('native input count differs from the fixed Request')
        expected_map = []
        for index, (reference, entry) in enumerate(zip(refs, inputs)):
            if (entry.get('asset_id') != reference['asset_id']
                    or entry.get('roles') != reference['roles']
                    or (entry.get('reference_id') is not None and entry['reference_id'] != reference['id'])
                    or reference['asset_id'] != 'asset_' + entry.get('sha256', '')):
                raise StateError('native input identity or roles differ from the fixed Request')
            # Binding is checked without opening input files, which may be lost
            # after a completed remote call. Recovery still uses its fixed plan.
            expected_map.append({'tool_index': index, 'reference_id': reference['id'],
                                 'asset_id': reference['asset_id']})
        if call.get('input_reference_map') != expected_map:
            raise StateError('native input order differs from the fixed Request')
        expected = self._arguments(request, call['inputs'])
        if call['arguments'] != expected:
            raise StateError('native payload differs from the fixed Request')
        mapping = attempt['dispatch'].get('input_reference_map')
        if mapping is not None and mapping != call['input_reference_map']:
            raise StateError('native input map differs from the recorded dispatch')
        return attempt, request

    def _arguments(self, request, inputs):
        version = request['design'].get('native_payload_version', 1)
        if version not in (1, 2):
            raise StateError('unknown native payload version')
        effective = [{'key': r['key'], 'value': r['value']} for r in request['effective_requirements']]
        prompt = ('Create exactly one image.\n' + request['design']['native_prompt'] +
                  '\nCurrent resolved requirements (authoritative over older design wording):\n' +
                  json.dumps(effective, ensure_ascii=False, sort_keys=True))
        arguments = {'prompt': prompt, 'transparent_background': request['design']['transparent_background']}
        if request['operation']['type'] == 'generative_edit':
            arguments['referenced_image_paths'] = [entry['path'] for entry in inputs]
            if version == 1:
                arguments['prompt'] += '\nThe single attached image is the edit target; preserve its content except for the requested changes.'
            else:
                lines = ['Image roles in the exact attached order. Borrow only the named attributes; exclusions must not be carried into the output.']
                for index, reference in enumerate(request['references'], 1):
                    base = any(role['role'] == 'base_image' for role in reference['roles'])
                    lines.append(f'Image {index}: ' + ('main edit target; preserve content except the requested changes.'
                                                       if base else 'supporting reference, not another edit target.'))
                    for role in reference['roles']:
                        description = {'role': role['role'], 'borrow': role['borrow'],
                                       'exclude': role['exclude'], 'strength': role['strength']}
                        lines.append(json.dumps(description, ensure_ascii=False, sort_keys=True))
                arguments['prompt'] += '\n' + '\n'.join(lines)
        return arguments

    def prepare(self, work_id, authorization_id, prompt, parent_version_id=None,
                changes=(), transparent_background=None, references=(), group_id=None,
                item_id=None, begin_context=True, operation_type=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise StateError('native prompt is required')
        if transparent_background is not None and type(transparent_background) is not bool:
            raise StateError('transparent_background must be boolean or unspecified')
        kind = operation_type or ('generative_edit' if parent_version_id else 'generate')
        if kind not in ('generate', 'generative_edit'):
            raise StateError('unsupported native operation')
        if not isinstance(references, (list, tuple)) or not all(isinstance(r, str) for r in references):
            raise StateError('references must be an ordered list of Reference IDs')
        if len(set(references)) != len(references):
            raise StateError('a reference cannot be submitted twice under the same ID')
        if kind == 'generate' and (references or parent_version_id):
            raise StateError('brand-new generation must omit reference images and parent version')
        # Selection, alpha inference, and Request creation share one state lock.
        # An implicit target must never become an explicit stale parent between reads.
        with self.engine.store.lock():
            state = self.engine._load()
            alpha = False
            if kind == 'generative_edit':
                if parent_version_id is None:
                    new_group = state['groups'].get(group_id)
                    if (state['target_resolution'] == 'ambiguous' or
                            (begin_context and new_group and len(new_group['item_ids']) > 1)):
                        raise StateError('ambiguous editing target; select a specific version')
                    parent_version_id = self.engine._work(state, work_id).get('edit_target_version_id')
                    if parent_version_id is None:
                        raise StateError('editing requires a selected or explicit parent version')
                parent = self.engine._version(state, parent_version_id, readable=True)
                if parent['work_id'] != work_id:
                    raise StateError('parent version belongs to another work')
                alpha = state['assets'][parent['asset_id']]['media_info']['has_alpha']
            transparent = alpha if transparent_background is None else transparent_background
            return self.engine._prepare_locked(
                state, work_id, authorization_id, ADAPTER_ID, native_capabilities(self.interface_evidence),
                operation={'type': kind, 'expected_outputs': 1,
                           'edit_mode': 'semantic_reference_edit' if kind == 'generative_edit' else None},
                parent_version_id=parent_version_id, changes=changes,
                references=references, group_id=group_id, item_id=item_id, begin_context=begin_context,
                design={'native_prompt': prompt, 'transparent_background': transparent, 'native_payload_version': 2},
            )

    def _plan(self, project, attempt, request):
        if request['adapter'] != ADAPTER_ID or attempt['adapter'] != ADAPTER_ID:
            raise StateError('attempt belongs to another adapter')
        if request['units'] != 1 or request['operation'].get('expected_outputs') != 1:
            raise StateError('native adapter supports one image per call')
        prompt = request.get('design', {}).get('native_prompt')
        transparent = request.get('design', {}).get('transparent_background')
        if not isinstance(prompt, str) or not prompt.strip() or type(transparent) is not bool:
            raise StateError('request is missing its fixed native arguments')
        refs = request['references']
        editing = request['operation']['type'] == 'generative_edit'
        version = request['design'].get('native_payload_version', 1)
        maximum = 1 if version == 1 else 3
        if (editing and not 1 <= len(refs) <= maximum) or (not editing and refs):
            raise StateError('native edit reference count exceeds the supported scope')
        bases = [reference for reference in refs if any(role['role'] == 'base_image' for role in reference['roles'])]
        if editing and len(bases) != 1:
            raise StateError('editing requires exactly one primary base-image reference')
        inputs, mapping = [], []
        for index, reference in enumerate(refs):
            if any(role['role'] == 'base_image' for role in reference['roles']):
                parent = self.engine._version(project, request['parent_version_id'], readable=True)
                if reference['asset_id'] != parent['asset_id']:
                    raise StateError('native base image is not the fixed parent version')
            asset = project['assets'][reference['asset_id']]
            path = self.engine._asset_path(project, asset['id'])
            entry = {'asset_id': asset['id'], 'path': str(path), 'sha256': asset['sha256'],
                     'roles': copy.deepcopy(reference['roles'])}
            if version == 2:
                entry['reference_id'] = reference['id']
            inputs.append(entry)
            mapping.append({'tool_index': index, 'reference_id': reference['id'], 'asset_id': asset['id']})
        arguments = self._arguments(request, inputs)
        return {'schema_version': 1, 'tool_name': TOOL_NAME, 'attempt_id': attempt['id'],
                'request_id': request['id'], 'arguments': arguments, 'inputs': inputs,
                'input_reference_map': mapping, 'payload_sha256': digest(arguments)}

    def preview(self, attempt_id):
        with self.engine.store.lock():
            project = self.engine._load()
            attempt = self.engine.store.read_record('attempts', attempt_id)
            request = self.engine.store.read_record('requests', attempt['request_id'])
            return self._plan(project, attempt, request)

    def dispatch(self, attempt_id, inspections=()):
        plan = self.preview(attempt_id)
        if not isinstance(inspections, (list, tuple)):
            raise StateError('inspections must be an explicit list')
        for entry in plan['inputs']:
            matches = [record for record in inspections if isinstance(record, dict)
                       and record.get('asset_id') == entry['asset_id'] and record.get('sha256') == entry['sha256']]
            if len(matches) != 1:
                raise StateError('view_image inspection of the exact input asset is required')
            _nonempty(matches[0].get('evidence'), 'view_image inspection')
        path = self._path(attempt_id, 'call.json')
        with self.engine.store.lock():
            attempt = self.engine.store.read_record('attempts', attempt_id)
            if attempt['state'] != 'prepared' or attempt['submission'] != 'not_submitted':
                raise StateError('native call is not prepared; never submit it again')
            if path.exists():
                call = self._read(path)
                if any(call.get(key) != value for key, value in plan.items()):
                    raise StateError('persisted native call no longer matches this request')
            else:
                call = {**plan, 'invocation_id': new_id('native_invocation'),
                        'created_at': utc_now(), 'inspections': copy.deepcopy(list(inspections))}
                self._write(path, call)
        result = self.engine.dispatch(attempt_id, input_reference_map=plan['input_reference_map'])
        if result['status'] == 'dispatching':
            result['call'] = call
        return result

    def capture_return(self, attempt_id, invocation_id, output_paths, tool_return):
        """Host attests to the actual return; arbitrary files alone are insufficient.

        Persist copied bytes and the receipt before Version adoption. This is a
        local evidence boundary, not remote cryptographic provenance verification.
        """
        observed = _nonempty(tool_return, 'native tool return')
        if (observed.get('tool_name') != TOOL_NAME or observed.get('kind') != 'native_tool_return'
                or observed.get('status') != 'completed'):
            raise StateError('capture requires an observed completed native tool return')
        _nonempty(observed.get('metadata'), 'native return metadata')
        if not isinstance(output_paths, list) or not output_paths:
            raise StateError('native completed return must identify its actual output files')
        with self.engine.store.lock():
            call = self._read(self._path(attempt_id, 'call.json'))
            if call['invocation_id'] != invocation_id or call['attempt_id'] != attempt_id:
                raise StateError('return invocation does not match the dispatched call')
            attempt, _ = self._verify_call(attempt_id, call)
            if attempt['submission'] == 'not_submitted':
                raise StateError('cannot attach a tool return before dispatch')
            raw = {'attempt_id': attempt_id, 'request_id': call['request_id'], 'invocation_id': invocation_id,
                   'payload_sha256': call['payload_sha256'], 'tool_return': observed,
                   'output_paths': [str(Path(path).expanduser().resolve()) for path in output_paths]}
            self._write(self._path(attempt_id, 'return.json'), raw)
            self.engine.store.hit('native_return_saved')
            outputs, errors = [], []
            for index, path in enumerate(output_paths):
                try:
                    asset = self.engine._materialize(path, {'kind': 'native_tool_return',
                                                           'attempt_id': attempt_id, 'invocation_id': invocation_id})
                    outputs.append({'output_index': index, 'asset_id': asset['id'], 'locator': asset['locator'],
                                    'sha256': asset['sha256'], 'source_path': str(Path(path).expanduser().resolve())})
                except (ValueError, OSError) as exc:
                    errors.append({'output_index': index, 'source_path': str(path), 'error': str(exc)})
            content = {'schema_version': 1, 'attempt_id': attempt_id, 'request_id': call['request_id'],
                       'invocation_id': invocation_id, 'payload_sha256': call['payload_sha256'],
                       'tool_return': observed, 'outputs': outputs, 'output_errors': errors,
                       'status': 'ready' if len(output_paths) == 1 and len(outputs) == 1 and not errors else 'requires_review'}
            receipt_path = self._path(attempt_id, 'receipt.json')
            if receipt_path.exists():
                receipt = self._read(receipt_path)
                if any(receipt.get(key) != value for key, value in content.items()):
                    raise StateError('conflicting native return for an existing receipt')
            else:
                receipt = {**content, 'received_at': utc_now()}
                self._write(receipt_path, receipt)
            self.engine.store.hit('native_receipt_saved')
            return receipt

    def consume_receipt(self, attempt_id, recovery=False):
        call = self._read(self._path(attempt_id, 'call.json'))
        self._verify_call(attempt_id, call)
        receipt = self._read(self._path(attempt_id, 'receipt.json'))
        for field in ('attempt_id', 'request_id', 'invocation_id', 'payload_sha256'):
            if receipt[field] != call[field]:
                raise StateError('native receipt and call identity mismatch')
        evidence = {'kind': 'persisted_native_tool_return', 'receipt_sha256': digest(receipt),
                    'tool_return': receipt['tool_return'], 'invocation_id': receipt['invocation_id'],
                    'payload_sha256': receipt['payload_sha256'], 'recovery': bool(recovery)}
        if receipt['status'] != 'ready' or len(receipt['outputs']) != 1:
            return self.engine.observe(attempt_id, 'unknown', 'submitted',
                                       {**evidence, 'reason': 'output count or saving requires review',
                                        'output_errors': receipt.get('output_errors', [])})
        outputs = []
        for entry in receipt['outputs']:
            path = self.engine.store.project_path(entry['locator'])
            if not path.is_file() or sha256_file(path) != entry['sha256']:
                raise StateError('saved native output is missing or changed')
            outputs.append({'output_index': entry['output_index'], 'path': str(path),
                            'provenance': {'attempt_id': attempt_id, 'verified': True,
                                           'source': TOOL_NAME, 'native_receipt_sha256': digest(receipt),
                                           'invocation_id': receipt['invocation_id']}})
        return self.engine.observe(attempt_id, 'succeeded', 'submitted', evidence,
                                   outputs=outputs, auto_target=not recovery)

    def recover(self):
        report = self.engine.recover()
        consumed = []
        if not report['recovery']['degraded']:
            for attempt in report['attempts']:
                if attempt['adapter'] != ADAPTER_ID:
                    continue
                if attempt['state'] == 'succeeded' and attempt['outputs']:
                    continue  # Core recovery already handles persisted adoption records.
                try:
                    if self._path(attempt['id'], 'receipt.json').exists():
                        result = self.consume_receipt(attempt['id'], recovery=True)
                    elif self._path(attempt['id'], 'return.json').exists():
                        call = self._read(self._path(attempt['id'], 'call.json'))
                        self._verify_call(attempt['id'], call)
                        raw = self._read(self._path(attempt['id'], 'return.json'))
                        if any(raw[field] != call[field] for field in ('attempt_id', 'request_id', 'invocation_id', 'payload_sha256')):
                            raise StateError('native return does not match the fixed call')
                        result = self.engine.observe(attempt['id'], 'unknown', 'submitted',
                                                     {'kind': 'native_return_saved_without_verified_copy', 'return': raw})
                    else:
                        continue
                    consumed.append({'attempt_id': attempt['id'], 'status': result['status']})
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    consumed.append({'attempt_id': attempt['id'], 'status': 'blocked', 'error': str(exc)})
        result = self.engine.recover()
        result['native_receipt_recoveries'] = consumed
        return result
