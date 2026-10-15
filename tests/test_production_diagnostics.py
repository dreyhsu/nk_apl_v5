"""Production diagnostics remain opt-in and preserve application errors."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import apl_diagnostics as diag
from summarize_diagnostics import summarize


class ProductionDiagnosticsTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {'APL_DIAGNOSTICS': '0'}), \
                patch.object(diag, '_recorder', None), patch.object(diag, 'Recorder') as recorder:
            with diag.measure('MATCH_SAMPLE'):
                diag.emit('TEST')
        recorder.assert_not_called()

    def test_measure_keeps_original_exception_and_writes_bounded_log(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = diag.Recorder(directory)
            with patch.dict(os.environ, {'APL_DIAGNOSTICS': '1', 'APL_DIAGNOSTIC_RUN': 'test'}), \
                    patch.object(diag, '_recorder', recorder):
                with self.assertRaisesRegex(ValueError, 'original'):
                    with diag.measure('MATCH_SAMPLE', budget_ms=1000 / 3):
                        raise ValueError('original')
                recorder.count('rtp_buffers', size=1200)
                recorder._summary({}, time.monotonic() - 5)
                recorder.close()
            rows = [json.loads(line) for line in next(Path(directory).glob('events-*.jsonl'))
                    .read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(row['event'] == 'MATCH_SAMPLE' and row['error'] == 'ValueError: original'
                                for row in rows))
            self.assertTrue(any(row['event'] == 'HEARTBEAT' and row['counters']['rtp_buffers']['total'] == 1
                                for row in rows))
            self.assertEqual(summarize(rows)['matching']['count'], 1)

    def test_browser_payload_drops_sdp(self):
        clean = diag.sanitize_browser({'event': 'VIDEO_SAMPLE', 'sdp': 'private',
                                       'events': [{'event': 'STREAM_FAILURE', 'reason': 'x' * 300,
                                                   'sdp': 'private'}]})
        self.assertNotIn('sdp', clean)
        self.assertNotIn('sdp', clean['events'][0])
        self.assertEqual(len(clean['events'][0]['reason']), 256)

    def test_browser_payload_keeps_bounded_station_stale_fields(self):
        clean = diag.sanitize_browser({
            'events': [{'event': 'VIDEO_SAMPLE', 'station_status_stale': True,
                        'station_status_stale_reason': 'global_lock',
                        'station_snapshot_age_ms': 125.5}]})
        self.assertTrue(clean['events'][0]['station_status_stale'])
        self.assertEqual(clean['events'][0]['station_status_stale_reason'], 'global_lock')
        self.assertEqual(clean['events'][0]['station_snapshot_age_ms'], 125.5)


if __name__ == '__main__':
    unittest.main()
