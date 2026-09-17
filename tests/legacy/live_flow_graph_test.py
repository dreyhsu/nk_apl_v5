#!/usr/bin/env python3
"""Contract test for v1.3 real-time flow graph payload and pages."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import vision_core as vc

steps = [
    {
        'id': 1, 'step_no': 1, 'sort_order': 0, 'name': 'Open Camera',
        'required': True, 'logic_mode': 'ANY', 'min_consecutive_hits': 2,
        'hold_ms': 400, 'timeout_sec': 10, 'allow_out_of_order': False,
        'latch_when_done': True, 'alarm_if_missing': True,
        'samples': [
            {'sample_role': 'OK'}, {'sample_role': 'OK'}, {'sample_role': 'NG'},
        ],
    },
    {
        'id': 2, 'step_no': 2, 'sort_order': 1, 'name': 'Audio Test',
        'required': True, 'logic_mode': 'ANY', 'min_consecutive_hits': 1,
        'hold_ms': 0, 'timeout_sec': 10, 'allow_out_of_order': False,
        'latch_when_done': True, 'alarm_if_missing': True,
        'samples': [{'sample_role': 'OK'}],
    },
]
engine = vc.SopFlowEngine(steps, {'enabled': True, 'strict_order': True})
s = engine.summary(now_ts=100.0)
first = s['steps'][0]
assert s['current_step_id'] == 1
assert first['min_consecutive_hits'] == 2
assert first['hold_target_ms'] == 400
assert first['ok_sample_count'] == 2
assert first['ng_sample_count'] == 1
assert first['logic_mode'] == 'ANY'

# Two detections with enough media-time hold complete Step 1 and advance the graph.
rule1 = {'id': 1, 'name': 'Open Camera', 'pass': True, 'matched': 'camera-main', 'items': [
    {'sample_role': 'OK', 'matched': True, 'score': 0.93}
]}
engine.update([rule1], now_ts=100.0)
s = engine.update([rule1], now_ts=100.5)
assert s['steps'][0]['status'] == 'DONE'
assert s['current_step_id'] == 2
assert s['progress_pct'] == 50.0

# All three inference pages must include explicit flow graph contracts.
contracts = {
    'static/offline_infer.html': ['LIVE FLOW MAP', 'flow-row', 'current_step_id', 'connector-done'],
    'static/multi_stream.html': ['renderFlow', 'flow-row', 'current_step_id', 'progress-fill'],
    'static/sop_monitor.html': ['v1.3 live flow graph', 'flow-row', 'connectorClass', 'CURRENT'],
}
for rel, needles in contracts.items():
    text = (ROOT / rel).read_text(encoding='utf-8')
    for needle in needles:
        assert needle in text, f'{needle!r} missing from {rel}'

print('Live Flow Graph tests: PASS')
