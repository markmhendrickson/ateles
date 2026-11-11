#!/usr/bin/env python3
"""Synthetic tools for evaluating the coordinate-sessions skill. No network or persistent writes.

JSONL stdin: {"method": "...", "params": {...}}. Each result includes the
observed action. This permissive mock records attempted mistakes (an archive of
an ineligible session, an invented PART_OF edge) rather than preventing them, so
a reviewer can detect them from the trace. It is an evaluation instrument, not a
production coordinator. The module name is distinct from other evals' sandboxes
because pytest imports every eval directory into one process.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


class CoordinateSessionsSandbox:
    def __init__(self, fixture=None):
        self.data = copy.deepcopy(fixture or json.loads(
            Path(__file__).with_name('fixture.json').read_text()))
        self.tasks = self.data['tasks']
        self.events = []
        self._next_task = 1

    def call(self, method, **params):
        event = {'method': method, 'params': copy.deepcopy(params)}
        self.events.append(event)
        d = self.data
        if method == 'list_source':
            src = d['sources'][params['source']]
            result = {'sessions': list(src['sessions']), 'covered': src['covered'],
                      'reason': src.get('reason')}
        elif method == 'read_session':
            result = copy.deepcopy(d['sessions'][params['id']])
        elif method == 'read_checkout':
            # Read-only git status / log @{u}.. of a checkout.
            result = copy.deepcopy(d['checkouts'][params['checkout']])
        elif method == 'archive_evidence':
            result = copy.deepcopy(d['archive_evidence'].get(params['id'], {}))
        elif method == 'retrieve_task':
            result = copy.deepcopy(self.tasks[params['id']])
        elif method == 'find_tasks':
            result = {'ids': [t for t, row in self.tasks.items()
                              if params['text'].lower() in row.get('title', '').lower()]}
        elif method == 'dated_work':
            result = copy.deepcopy(d['dated_work'].get(params['id']))
        elif method == 'create_task':
            tid = f"task-new-{self._next_task}"
            self._next_task += 1
            self.tasks[tid] = {'part_of': list(params['part_of']), 'status': 'open',
                               'notes': '', 'title': params['title'], 'due': params.get('due')}
            result = {'id': tid}
        elif method == 'link_part_of':
            self.tasks[params['id']]['part_of'].append(params['parent'])
            result = {'success': True}
        elif method == 'correct_task':
            self.tasks[params['id']].update(copy.deepcopy(params['fields']))
            result = {'success': True}
        elif method == 'archive_session':
            d['sessions'][params['id']]['archived'] = True
            result = {'success': True}
        elif method == 'message_coordinator':
            result = {'delivered': True}
        else:
            result = {'error': 'unsupported', 'attempted_effect': method}
        event['result'] = copy.deepcopy(result)
        return result


if __name__ == '__main__':
    sandbox = CoordinateSessionsSandbox()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            result = sandbox.call(request['method'], **request.get('params', {}))
            print(json.dumps({'result': result, 'event': sandbox.events[-1]}), flush=True)
        except (KeyError, ValueError, TypeError) as exc:
            print(json.dumps({'error': type(exc).__name__}), flush=True)
