"""Dashboard history, legacy migration, and capture transaction regressions."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from apl_station import APLStation
from station_schema import migrate_history


LEGACY_SCHEMA = '''
CREATE TABLE cycles (
 cycle_id TEXT PRIMARY KEY, sn TEXT NOT NULL, source TEXT NOT NULL,
 state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 sn_request_id TEXT NOT NULL, report_request_id TEXT NOT NULL,
 step_index INTEGER NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE attempts (
 attempt_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, sn TEXT NOT NULL,
 captured_at REAL NOT NULL, visual_result TEXT NOT NULL, source_mode TEXT NOT NULL,
 source_json TEXT NOT NULL, matching_json TEXT NOT NULL, raw_path TEXT NOT NULL,
 result_path TEXT NOT NULL, json_path TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL,
 ts REAL NOT NULL, event TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE outbox (request_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL,
 payload_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
 updated_at REAL NOT NULL, response_json TEXT);
'''


class MigrationTests(unittest.TestCase):
    def test_legacy_history_is_preserved_and_migration_is_repeatable(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(LEGACY_SCHEMA)
        for cycle_id in ('old', 'empty'):
            db.execute('INSERT INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (cycle_id, 'SAME-SN', 'scanner', 'TERMINATED', None, 10, 30,
                        'request1', 'request2', 0,
                        json.dumps({'mcio_sn': 'MCIO-1', 'completed_at': 25})))
        for attempt_id, stamp, result, source in (
                ('a2', 20, 'OK', {}), ('a1', 15, 'NOT_MATCHED', {'auto_capture': True})):
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (attempt_id, 'old', 'SAME-SN', stamp, result, 'camera',
                        json.dumps(source), '{}', 'raw.jpg', 'result.jpg', 'evidence.json'))
        db.execute("INSERT INTO events(cycle_id,ts,event,payload_json) VALUES ('old',12,'MCIO_SN_SCANNED','{}')")
        db.commit()
        migrate_history(db)
        before = [tuple(r) for r in db.execute('SELECT * FROM attempts ORDER BY attempt_no')]
        migrate_history(db)
        self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM attempts ORDER BY attempt_no')])
        rows = db.execute('SELECT * FROM attempts ORDER BY attempt_no').fetchall()
        self.assertEqual([r['attempt_id'] for r in rows], ['a1', 'a2'])
        self.assertEqual([r['capture_trigger'] for r in rows], ['AUTO', 'UNKNOWN'])
        self.assertTrue(all(r['judged_at'] is None for r in rows))
        self.assertEqual([r['raw_path'] for r in rows], ['raw.jpg', 'raw.jpg'])
        summary = db.execute("SELECT * FROM cycle_history WHERE cycle_id='old'").fetchone()
        self.assertEqual((summary['attempt_count'], summary['judge_pass']), (2, 1))
        self.assertEqual((summary['mcio_sn'], summary['mcio_scanned_at']), ('MCIO-1', 12))
        self.assertEqual(summary['sn_scanned_at'], 10)
        empty = db.execute("SELECT * FROM cycle_history WHERE cycle_id='empty'").fetchone()
        self.assertEqual((empty['attempt_count'], empty['visual_result'], empty['judge_pass']),
                         (0, 'NOT_INSPECTED', None))
        db.execute("INSERT INTO outbox VALUES ('upload', 'old', '{}', 'PENDING', 3, 40, NULL)")
        db.commit()
        summary = db.execute("SELECT * FROM cycle_history WHERE cycle_id='old'").fetchone()
        self.assertEqual((summary['upload_status'], summary['attempt_count']), ('PENDING', 2))
        db.execute("UPDATE outbox SET status='ACKED'")
        self.assertEqual(db.execute("SELECT upload_status FROM cycle_history WHERE cycle_id='old'").fetchone()[0],
                         'ACKED')

    def test_failed_migration_rolls_back_schema_and_can_be_retried(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.executescript(LEGACY_SCHEMA)
        db.execute('DROP TABLE events')
        with self.assertRaises(sqlite3.OperationalError):
            migrate_history(db)
        self.assertNotIn('attempt_no', [r[1] for r in db.execute('PRAGMA table_info(attempts)')])
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        db.execute('CREATE TABLE events(cycle_id TEXT, event TEXT, ts REAL)')
        migrate_history(db)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)


class Matcher:
    def evaluate_snapshot(self, frame):
        return {'accepted': bool(frame[0, 0, 0]), 'reason': 'test'}

    def draw(self, frame, snapshot):
        return frame.copy()


class HistoryWriteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'workflow.json'
        self.config.write_text(json.dumps({
            'inspection_mode': 'single_snapshot_cover_card', 'station_mode': 'local',
            'dmz': {'bypass_sfis': True}, 'template_root': 'templates',
        }))
        self.station = APLStation(self.config, self.root / 'data', matcher=Matcher())
        self.addCleanup(self.station.close)
        self.station.set_operator('OP-1')

    def start(self):
        self.station.scan('SN-1')
        cycle = self.station.cycle_id
        self.station.scan('MCIO-1')
        return cycle

    def capture(self, passed=False, automatic=False, timeout=False):
        return self.station.inspect_snapshot(
            np.full((8, 8, 3), int(passed), dtype=np.uint8),
            source={'mode': 'camera', 'auto_capture': automatic}, timeout_pass=timeout)

    def test_auto_then_manual_retries_and_repeat_sn_are_separate_cycles(self):
        cycle = self.start()
        self.capture(automatic=True)
        self.capture()
        self.capture(passed=True)
        detail = self.station.history_detail(cycle)
        attempts = detail['attempts']
        self.assertEqual([a['attempt_no'] for a in attempts], [1, 2, 3])
        self.assertEqual([a['capture_trigger'] for a in attempts], ['AUTO', 'MANUAL', 'MANUAL'])
        self.assertEqual([a['judge_pass'] for a in attempts], [0, 0, 1])
        self.assertTrue(all(a['captured_at'] <= a['judged_at'] for a in attempts))
        self.assertLessEqual(detail['sn_scanned_at'], detail['mcio_scanned_at'])
        self.assertEqual(detail['mcio_sn'], 'MCIO-1')
        self.assertEqual(detail['completed_at'], attempts[-1]['judged_at'])
        self.assertEqual(self.station.list_history(visual_result='OK')[0]['attempt_count'], 3)
        self.assertEqual(self.station.list_history(visual_result='NOT_MATCHED'), [])
        self.station.confirm_result(cycle)
        self.assertEqual(self.station.list_history()[0]['state'], 'COMPLETE')
        second_cycle = self.start()
        self.assertNotEqual(cycle, second_cycle)
        self.station.terminate(second_cycle)
        history = {r['cycle_id']: r for r in self.station.list_history()}
        self.assertEqual(history[cycle]['attempt_count'], 3)
        self.assertEqual(history[second_cycle]['attempt_count'], 0)
        self.assertEqual(history[second_cycle]['visual_result'], 'NOT_INSPECTED')
        self.assertIsNone(history[second_cycle]['judge_pass'])

    def test_dashboard_sees_attempt_and_cycle_result_together(self):
        cycle = self.start()
        reader = sqlite3.connect((self.root / 'data/station.sqlite3').as_uri() + '?mode=ro', uri=True)
        self.addCleanup(reader.close)
        original = self.station._write_save

        def observe(event, *args, **kwargs):
            result = original(event, *args, **kwargs)
            if event == 'INSPECTION_OK':
                # The station has written the attempt and PASS, but not committed.
                row = reader.execute('SELECT cycle_status, attempt_count FROM cycle_history WHERE cycle_id=?',
                                     (cycle,)).fetchone()
                self.assertEqual(row, ('JUDGING', 0))
            return result

        with patch.object(self.station, '_write_save', side_effect=observe):
            self.assertTrue(self.capture(passed=True)['success'])
        self.assertEqual(reader.execute('SELECT cycle_status, attempt_count, judge_pass FROM cycle_history').fetchone(),
                         ('PASS_CONFIRM', 1, 1))

    def test_journal_failure_rolls_back_attempt_and_removes_evidence(self):
        self.start()
        self.station.db.executescript('''CREATE TRIGGER fail_judgment BEFORE INSERT ON events
            WHEN NEW.event='INSPECTION_OK' BEGIN SELECT RAISE(ABORT, 'disk test'); END;''')
        self.assertFalse(self.capture(passed=True)['success'])
        self.assertEqual(self.station.db.execute('SELECT COUNT(*) FROM attempts').fetchone()[0], 0)
        self.assertEqual(self.station.list_history()[0]['result'], 'INTERRUPTED')
        self.assertIsNone(self.station.list_history()[0]['completed_at'])
        self.assertEqual(list(self.station.evidence.root.rglob('*.jpg')), [])
        self.assertEqual(list(self.station.evidence.root.rglob('*.json')), [])

    def test_timeout_pass_keeps_the_actual_visual_result(self):
        self.start()
        self.capture(automatic=True, timeout=True)
        row = self.station.list_history()[0]
        self.assertEqual((row['visual_result'], row['judge_pass'], row['attempt_count']),
                         ('TIMEOUT_PASS', 1, 1))

    def test_restart_preserves_scan_times_and_continues_attempt_numbers(self):
        cycle = self.start()
        self.capture(automatic=True)
        before = self.station.list_history()[0]
        self.station.close()
        self.station = APLStation(self.config, self.root / 'data', matcher=Matcher())
        self.addCleanup(self.station.close)
        self.assertEqual(self.station.cycle_id, cycle)
        self.capture(passed=True)
        after = self.station.list_history()[0]
        self.assertEqual((after['sn_scanned_at'], after['mcio_scanned_at']),
                         (before['sn_scanned_at'], before['mcio_scanned_at']))
        self.assertEqual(after['attempt_count'], 2)
        self.assertEqual([a['attempt_no'] for a in self.station.history_detail(cycle)['attempts']], [1, 2])


if __name__ == '__main__':
    unittest.main()
