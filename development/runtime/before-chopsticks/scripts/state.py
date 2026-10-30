#!/usr/bin/env python3
"""JSON CLI for the local v6 state core; never invokes an external model.

Examples:
  python3 state.py --project ./artwork init --name Dinner
  python3 state.py --project ./artwork apply --input command.json
  python3 state.py --project ./artwork recover
"""
import argparse
import json
import sys
from pathlib import Path

from bc_core.engine import ProjectEngine


_ACTIONS = {
    'create_work', 'create_group', 'set_series_anchor', 'set_item_anchor', 'set_item_dependencies', 'add_requirements', 'authorize', 'extend_retry_budget',
    'import_image', 'import_asset', 'select_version', 'resolve_version_label', 'select_label',
    'set_edit_target', 'add_reference',
    'record_read_only', 'prepare', 'dispatch', 'cancel_prepared', 'retry', 'observe',
    'acknowledge_unknown', 'local_edit', 'export_version', 'record_check',
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True, help='local project directory')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('init', help='create an empty project without overwriting an existing project')
    create.add_argument('--name', default='Untitled')
    sub.add_parser('inspect', help='read current state without repairing or resubmitting')
    sub.add_parser('recover', help='recover local facts; no external query or resubmission')
    apply = sub.add_parser('apply', help='perform a named local state operation')
    apply.add_argument('--input', required=True, help='UTF-8 JSON file, or - for stdin')
    args = parser.parse_args(argv)
    engine = ProjectEngine(args.project)
    try:
        if args.command == 'init':
            result = engine.initialize(args.name)
        elif args.command == 'inspect':
            result = engine.inspect()
        elif args.command == 'recover':
            result = engine.recover()
        else:
            text = sys.stdin.read() if args.input == '-' else Path(args.input).read_text(encoding='utf-8')
            command = json.loads(text)
            if not isinstance(command, dict):
                raise ValueError('command must be an object with an action')
            command = dict(command)
            action = command.pop('action', None)
            if not isinstance(action, str) or action not in _ACTIONS:
                raise ValueError(f'unknown action; allowed actions: {", ".join(sorted(_ACTIONS))}')
            result = getattr(engine, action)(**command)
        blocked = (result.get('status') in ('blocked', 'protocol_error')
                   or result.get('recovery', {}).get('degraded', False))
        print(json.dumps({'ok': not blocked, 'result': result}, ensure_ascii=False, indent=2, allow_nan=False))
        return 2 if blocked else 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'error_type': type(exc).__name__},
                         ensure_ascii=False, indent=2))
        return 2


if __name__ == '__main__':
    sys.exit(main())
