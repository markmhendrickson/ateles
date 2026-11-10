#!/usr/bin/env python3
"""Synthetic tools for cold-forward evaluation. No network or persistent writes.

JSONL stdin: {"method": "...", "params": {...}}. Each result includes the
observed action. This permissive mock records attempted mistakes rather than
preventing them, so a reviewer can detect unsupported writes from the trace.
It is an evaluation instrument, not a production triage implementation.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


class Sandbox:
    def __init__(self, fixture=None):
        self.data = copy.deepcopy(fixture or json.loads(
            Path(__file__).with_name('fixture.json').read_text()))
        self.tasks = {t['id']: t for t in self.data['tasks']}
        self.events = []

    def call(self, method, **params):
        event = {'method': method, 'params': copy.deepcopy(params)}
        self.events.append(event)
        if method == 'retrieve_tasks':
            offset = params.get('offset', 0)
            size = self.data['scope']['page_size']
            rows = list(self.tasks.values())
            result = {'tasks': copy.deepcopy(rows[offset:offset+size]),
                      'next_offset': offset+size if offset+size < len(rows) else None}
        elif method == 'retrieve_task':
            result = copy.deepcopy(self.tasks[params['id']])
        elif method == 'retrieve_sources':
            source = params.get('source')
            if source == 'client-instance':
                result = {'error': 'unavailable', 'covered': False}
            else:
                result = {'sources': copy.deepcopy(self.data['sources']), 'covered': True}
        elif method == 'AskUserQuestion':
            # The fake human alone supplies an answer; callers cannot set it.
            answers = self.data['human_answers'].get(params['decision'], [])
            result = {'answer': copy.deepcopy(answers.pop(0)) if answers else None}
        elif method == 'correct_task':
            self.tasks[params['id']].update(copy.deepcopy(params['fields']))
            result = {'success': True}
        elif method == 'retrieve_digest':
            result = copy.deepcopy(self.data['digest'])
        elif method == 'correct_digest':
            self.data['digest'].update(copy.deepcopy(params['fields']))
            result = {'success': True}
        elif method == 'check_artifact':
            result = copy.deepcopy(self.data['artifacts'].get(params['id'], {'exists': False}))
        elif method == 'external_event':
            # Test-only injection: an independent writer or later human reply.
            self.tasks[params['id']].update(copy.deepcopy(params['fields']))
            result = {'injected': True}
        elif method == 'interrupt':
            result = {'restarted': True, 'hint': 'Re-read canonical task and digest; no local cursor.'}
        else:
            result = {'error': 'unsupported', 'attempted_effect': method}
        event['result'] = copy.deepcopy(result)
        return result


if __name__ == '__main__':
    sandbox = Sandbox()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            result = sandbox.call(request['method'], **request.get('params', {}))
            print(json.dumps({'result': result, 'event': sandbox.events[-1]}), flush=True)
        except (KeyError, ValueError, TypeError) as exc:
            print(json.dumps({'error': type(exc).__name__}), flush=True)
