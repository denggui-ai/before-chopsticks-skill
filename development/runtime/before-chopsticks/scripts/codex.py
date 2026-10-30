#!/usr/bin/env python3
"""Prepare/journal native Codex calls locally. Does not invoke a model."""
import argparse
import json
import sys
from pathlib import Path

from bc_core.codex_bridge import CodexBridge


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--input', required=True, help='JSON command file, or - for stdin')
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.read() if args.input == '-' else Path(args.input).read_text(encoding='utf-8')
        command = json.loads(text)
        if not isinstance(command, dict):
            raise ValueError('command must be a JSON object')
        action = command.pop('action', None)
        interface = command.pop('interface_evidence', None)
        if action not in ('prepare', 'preview', 'dispatch', 'capture_return', 'consume_receipt', 'recover'):
            raise ValueError('unsupported native bridge action')
        result = getattr(CodexBridge(args.project, interface), action)(**command)
        blocked = (result.get('status') in ('blocked', 'protocol_error', 'requires_review')
                   or result.get('recovery', {}).get('degraded', False)
                   or any(r['status'] == 'blocked' for r in result.get('native_receipt_recoveries', [])))
        print(json.dumps({'ok': not blocked, 'result': result}, ensure_ascii=False, indent=2))
        return 2 if blocked else 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc), 'error_type': type(exc).__name__}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
