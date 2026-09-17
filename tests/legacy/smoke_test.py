#!/usr/bin/env python3
"""Core logic smoke test. Does not start Flask or require camera/network input."""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import vision_core as vc


def step(i, required=True, min_hits=2, hold_ms=400, allow=False):
    return {
        'id': i,
        'name': f'Step {i}',
        'step_no': i,
        'sort_order': i - 1,
        'required': required,
        'min_consecutive_hits': min_hits,
        'hold_ms': hold_ms,
        'timeout_sec': 0,
        'allow_out_of_order': allow,
        'latch_when_done': True,
        'alarm_if_missing': True,
        'samples': [],
    }


def rule(i, matched=True):
    return {
        'id': i,
        'name': f'Step {i}',
        'pass': matched,
        'matched': f'Step {i} OK' if matched else None,
        'items': [{'sample_role': 'OK', 'matched': matched, 'score': 0.99}],
    }


def main():
    cfg = dict(vc.SOP_DEFAULT_CONFIG)
    cfg.update(enabled=True, strict_order=True, alarm_latch=True)

    engine = vc.SopFlowEngine([step(1), step(2)], cfg)
    t0 = engine.created_ts
    engine.update([rule(1)], now_ts=t0)
    engine.update([rule(1)], now_ts=t0 + 0.5)
    engine.update([rule(2)], now_ts=t0 + 1.0)
    result = engine.update([rule(2)], now_ts=t0 + 1.5)
    assert result['complete'] and result['elapsed_sec'] == 1.5

    engine = vc.SopFlowEngine([step(1, min_hits=3, hold_ms=1000), step(2, min_hits=3, hold_ms=1000)], cfg)
    t0 = engine.created_ts
    assert not engine.update([rule(2)], now_ts=t0)['alarm']['active']
    assert not engine.update([rule(2)], now_ts=t0 + 0.5)['alarm']['active']
    assert engine.update([rule(2)], now_ts=t0 + 1.0)['alarm']['active']

    engine = vc.SopFlowEngine([step(1, min_hits=1, hold_ms=0)], cfg)
    ng_only = {
        'id': 1,
        'name': 'NG only',
        'pass': True,
        'matched': None,
        'items': [{'sample_role': 'NG', 'matched': False, 'pass': True, 'score': 0.1}],
    }
    assert engine.update([ng_only], now_ts=engine.created_ts + 0.1)['state'] == 'WAITING'

    print('TM-Inspect core smoke test: PASS')


if __name__ == '__main__':
    main()
