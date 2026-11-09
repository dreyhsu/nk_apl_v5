"""Additive station history migration and the read-only dashboard interface."""

import json


def _object(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def migrate_history(db):
    """Run once, in one transaction. Never rewrite existing evidence files."""
    if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
            return
        for definition in (
            'attempt_no INTEGER', 'judged_at REAL',
            'judge_pass INTEGER CHECK (judge_pass IN (0, 1))',
            "capture_trigger TEXT NOT NULL DEFAULT 'UNKNOWN'",
        ):
            db.execute('ALTER TABLE attempts ADD COLUMN ' + definition)
        numbers = {}
        rows = db.execute('''SELECT attempt_id, cycle_id, visual_result, source_json
                             FROM attempts ORDER BY cycle_id, captured_at, rowid''').fetchall()
        for row in rows:
            attempt_id, cycle_id, visual_result, source_json = row
            numbers[cycle_id] = numbers.get(cycle_id, 0) + 1
            source = _object(source_json)
            trigger = ('AUTO' if source.get('auto_capture') is True else
                       'MANUAL' if source.get('auto_capture') is False else 'UNKNOWN')
            passed = {'OK': 1, 'TIMEOUT_PASS': 1, 'NOT_MATCHED': 0}.get(visual_result)
            db.execute('''UPDATE attempts SET attempt_no=?, judge_pass=?, capture_trigger=?
                          WHERE attempt_id=?''',
                       (numbers[cycle_id], passed, trigger, attempt_id))
        db.execute('CREATE UNIQUE INDEX idx_attempts_number ON attempts(cycle_id, attempt_no)')

        # InspectionStore can also be used alone, without the station journal.
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycles'").fetchone():
            for definition in ('mcio_sn TEXT', 'sn_scanned_at REAL',
                               'mcio_scanned_at REAL', 'completed_at REAL'):
                db.execute('ALTER TABLE cycles ADD COLUMN ' + definition)
            db.execute('CREATE INDEX IF NOT EXISTS idx_events_cycle_event_ts ON events(cycle_id, event, ts)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_cycles_sn_created ON cycles(sn, created_at)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_cycles_updated ON cycles(updated_at)')
            db.execute('CREATE INDEX IF NOT EXISTS idx_outbox_cycle_updated ON outbox(cycle_id, updated_at)')
            for cycle_id, created_at, detail_json in db.execute(
                    'SELECT cycle_id, created_at, detail_json FROM cycles').fetchall():
                detail = _object(detail_json)
                scan_times = dict(db.execute('''SELECT event, MIN(ts) FROM events
                    WHERE cycle_id=? AND event IN
                    ('SN_SCANNED', 'SN_SCANNED_AND_STARTED', 'MCIO_SN_SCANNED')
                    GROUP BY event''', (cycle_id,)).fetchall())
                sn_time = scan_times.get('SN_SCANNED_AND_STARTED',
                                         scan_times.get('SN_SCANNED', created_at))
                db.execute('''UPDATE cycles SET mcio_sn=?, sn_scanned_at=?,
                              mcio_scanned_at=?, completed_at=? WHERE cycle_id=?''',
                           (detail.get('mcio_sn') or None, sn_time,
                            scan_times.get('MCIO_SN_SCANNED'), detail.get('completed_at'), cycle_id))
            db.execute('''CREATE VIEW cycle_history AS
                SELECT c.cycle_id, c.sn, c.mcio_sn, c.sn_scanned_at, c.mcio_scanned_at,
                       c.state AS cycle_status, c.result, c.created_at, c.updated_at,
                       c.completed_at,
                       COALESCE(a.visual_result, 'NOT_INSPECTED') AS visual_result,
                       a.judge_pass, a.captured_at AS last_capture_at, a.judged_at,
                       a.source_mode, a.capture_trigger,
                       (SELECT COUNT(*) FROM attempts n WHERE n.cycle_id=c.cycle_id) AS attempt_count,
                       COALESCE((SELECT o.status FROM outbox o WHERE o.cycle_id=c.cycle_id
                                 ORDER BY o.updated_at DESC, o.rowid DESC LIMIT 1),
                                'NOT_SENT') AS upload_status
                FROM cycles c LEFT JOIN attempts a ON a.attempt_id=(
                    SELECT z.attempt_id FROM attempts z WHERE z.cycle_id=c.cycle_id
                    ORDER BY z.attempt_no DESC LIMIT 1)''')
        db.execute('PRAGMA user_version=1')
