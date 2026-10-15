#!/usr/bin/env python3
"""Summarize an APL diagnostic run without changing any running process."""
import argparse
import collections
import json
from pathlib import Path
import statistics


def records(directory):
    for path in sorted(Path(directory).glob('events-*.jsonl*')):
        try:
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            yield event
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue


def summarize(events):
    rows = sorted(events, key=lambda row: row.get('utc', ''))
    totals = collections.Counter(row.get('event', '') for row in rows)
    matches = [float(row['elapsed_ms']) for row in rows
               if row.get('event') == 'MATCH_SAMPLE' and isinstance(row.get('elapsed_ms'), (int, float))]
    windows = {}
    for row in rows:
        if row.get('event', '').startswith('AUTO_'):
            key = (row.get('run_id'), row.get('cycle_id'))
            window = windows.setdefault(key, {'start': None, 'probes': 0, 'commits': [], 'last_probe_elapsed': None})
            if row['event'] == 'AUTO_WINDOW_START':
                window['start'] = row.get('utc')
                window['_start_monotonic'] = row.get('monotonic')
                window['target_fps'] = row.get('target_fps')
                window['timeout_sec'] = row.get('timeout_sec')
            elif row['event'] == 'AUTO_PROBE_RESULT':
                window['probes'] += 1
                window['last_probe_elapsed'] = row.get('scheduler_elapsed_seconds')
            elif row['event'] in ('AUTO_COMMIT_START', 'AUTO_COMMIT_RESULT', 'AUTO_COMMIT_ERROR'):
                commit = {k: row.get(k) for k in ('event', 'utc', 'trigger', 'result', 'state', 'success', 'elapsed_seconds', 'elapsed_ms', 'error')}
                if row['event'] == 'AUTO_COMMIT_START' and isinstance(window.get('_start_monotonic'), (int, float)):
                    commit['actual_window_seconds'] = row.get('monotonic', 0) - window['_start_monotonic']
                window['commits'].append(commit)
    for window in windows.values():
        window.pop('_start_monotonic', None)
    def failed(row):
        if row.get('event') in ('GST_ERROR', 'CAMERA_RUN_ERROR', 'PIPELINE_START_FAILED',
                                'AUTO_PROBE_ERROR', 'AUTO_COMMIT_ERROR'):
            return True
        if row.get('event') in ('WHEP_REQUEST', 'HTTP_REQUEST'):
            return bool(row.get('error')) or any(isinstance(row.get(k), int) and row[k] >= 400
                                                  for k in ('status', 'upstream_status'))
        return False
    failures = [{k: row.get(k) for k in ('utc', 'event', 'error', 'reason', 'text', 'status', 'upstream_status')}
                for row in rows if failed(row)]
    for row in rows:
        if row.get('event') == 'BROWSER':
            for event in row.get('sample', {}).get('events', []):
                if event.get('event') == 'STREAM_FAILURE':
                    failures.append({k: event.get(k) for k in ('at', 'event', 'reason', 'status', 'connection', 'ice')})
    latest = {}
    browser = {}
    udp = []
    for row in rows:
        if row.get('event') == 'HEARTBEAT':
            latest[row.get('pid')] = row.get('counters', {})
        elif row.get('event') == 'BROWSER':
            for sample in row.get('sample', {}).get('events', []):
                if sample.get('event') == 'VIDEO_SAMPLE':
                    tab = sample.get('tab', 'unknown')
                    group = browser.setdefault(tab, {'first': sample, 'last': sample, 'samples': 0})
                    group['last'] = sample
                    group['samples'] += 1
        elif row.get('event') == 'SYSTEM_SAMPLE':
            lines = row.get('net/snmp', '').splitlines()
            for header, data in zip(lines, lines[1:]):
                if header.startswith('Udp:') and data.startswith('Udp:'):
                    values = dict(zip(header.split()[1:], data.split()[1:]))
                    if 'RcvbufErrors' in values:
                        udp.append({'utc': row.get('utc'), 'RcvbufErrors': values['RcvbufErrors'],
                                    'InErrors': values.get('InErrors')})
                    break
    for group in browser.values():
        first, last = group['first'], group['last']
        group['last'] = {k: last.get(k) for k in ('at', 'connection', 'ice', 'status',
            'frame_seq', 'framesReceived', 'framesDecoded', 'keyFramesDecoded',
            'packetsReceived', 'packetsLost', 'protocol', 'candidateType', 'decoded_age_ms')}
        group['deltas'] = {key: last[key] - first[key] for key in
                           ('framesReceived', 'framesDecoded', 'packetsReceived')
                           if isinstance(last.get(key), (int, float)) and isinstance(first.get(key), (int, float))}
        group.pop('first')
    return {'events': dict(totals),
            'matching': {'count': len(matches), 'mean_ms': statistics.mean(matches) if matches else None,
                         'max_ms': max(matches) if matches else None,
                         'over_200ms': sum(t > 200 for t in matches),
                         'over_configured_budget': sum(row.get('exceeds_budget') is True for row in rows
                                                       if row.get('event') == 'MATCH_SAMPLE')},
            'windows': {str(key): value for key, value in windows.items()},
            'latest_pipeline_counters_by_pid': latest,
            'browser_by_tab': browser,
            'udp_rcvbuf_errors': udp[-20:],
            'failures': failures[-100:]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error('Diagnostic directory does not exist')
    print(json.dumps(summarize(records(args.directory)), ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
