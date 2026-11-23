#!/usr/bin/env python3
"""Synthetic tools for evaluating the migrate-harness skill. No network or persistent writes.

JSONL stdin: {"method": "...", "params": {...}}. This permissive mock records
attempted mistakes (an archive before the new side confirmed, a deleted goal, a
closed task with no artifact) rather than preventing them, so a reviewer can
detect them from the trace. It is an evaluation instrument, not a production
migrator. The module name is distinct from other evals' sandboxes because
pytest imports every eval directory into one process.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys


class MigrateHarnessSandbox:
    def __init__(self, fixture=None):
        self.data = copy.deepcopy(fixture or json.loads(
            Path(__file__).with_name('fixture.json').read_text()))
        self.tasks = self.data['tasks']
        self.plans = self.data['plans']
        self.projects = self.data['projects']
        self.old = self.data['old_side']
        self.events = []

    def call(self, method, **params):
        event = {'method': method, 'params': copy.deepcopy(params)}
        self.events.append(event)
        d = self.data
        if method == 'retrieve_task':
            result = copy.deepcopy(self.tasks[params['id']])
        elif method == 'retrieve_plan':
            result = copy.deepcopy(self.plans[params['id']])
        elif method == 'retrieve_project':
            result = copy.deepcopy(self.projects[params['id']])
        elif method == 'retrieve_lead':
            result = copy.deepcopy(d['leads'][params['id']])
        elif method == 'correct_task':
            self.tasks[params['id']].update(copy.deepcopy(params['fields']))
            result = {'success': True}
        elif method == 'correct_plan':
            self.plans[params['id']].update(copy.deepcopy(params['fields']))
            result = {'success': True}
        elif method == 'read_old_side':
            result = copy.deepcopy(self.old[params['session']])
        elif method == 'pause_goal':
            self.old[params['session']]['goal']['status'] = 'paused'
            result = {'success': True}
        elif method == 'resume_goal':
            self.old[params['session']]['goal']['status'] = params['status']
            result = {'success': True}
        elif method == 'pause_watcher':
            for w in self.old[params['session']]['watchers']:
                if w['id'] == params['id']:
                    w['status'] = 'paused'
            result = {'success': True}
        elif method == 'resume_watcher':
            for w in self.old[params['session']]['watchers']:
                if w['id'] == params['id']:
                    w['status'] = params['status']
            result = {'success': True}
        elif method == 'delete_goal':
            self.old[params['session']]['goal'] = None
            result = {'success': True}
        elif method == 'delete_watcher':
            self.old[params['session']]['watchers'] = [
                w for w in self.old[params['session']]['watchers'] if w['id'] != params['id']]
            result = {'success': True}
        elif method == 'new_side_confirmation':
            result = copy.deepcopy(d['new_side'][params['session']])
        elif method == 'archive_session':
            self.old[params['session']]['archived'] = True
            result = {'success': True}
        elif method == 'message_coordinator':
            result = {'delivered': True}
        else:
            result = {'error': 'unsupported', 'attempted_effect': method}
        event['result'] = copy.deepcopy(result)
        return result


if __name__ == '__main__':
    sandbox = MigrateHarnessSandbox()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            result = sandbox.call(request['method'], **request.get('params', {}))
            print(json.dumps({'result': result, 'event': sandbox.events[-1]}), flush=True)
        except (KeyError, ValueError, TypeError) as exc:
            print(json.dumps({'error': type(exc).__name__}), flush=True)
