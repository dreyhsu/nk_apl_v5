"""Immutable evidence storage for operator-triggered APL inspections."""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class InspectionStore:
    def __init__(self, root: Path, db):
        self.root = Path(root).resolve()
        self.db = db
        self.root.mkdir(parents=True, exist_ok=True)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id TEXT PRIMARY KEY,
                cycle_id TEXT NOT NULL,
                sn TEXT NOT NULL,
                captured_at REAL NOT NULL,
                visual_result TEXT NOT NULL,
                source_mode TEXT NOT NULL,
                source_json TEXT NOT NULL,
                matching_json TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                result_path TEXT NOT NULL,
                json_path TEXT NOT NULL
            )
        """)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_attempts_cycle ON attempts(cycle_id, captured_at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_attempts_sn ON attempts(sn, captured_at)")
        self.db.commit()

    @staticmethod
    def _safe_sn(sn: str) -> str:
        value = re.sub(r'[^A-Za-z0-9._-]+', '_', str(sn)).strip('._-')
        return (value or 'SN')[:80]

    @staticmethod
    def _encode_jpeg(frame, quality: int = 92) -> bytes:
        import cv2

        ok, data = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        if not ok:
            raise RuntimeError('Inspection image could not be encoded')
        return data.tobytes()

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_bytes(data)
        temporary.replace(path)

    def save_attempt(self, *, cycle_id, sn, visual_result, raw_frame, result_frame,
                     matching, source=None, quality=92):
        captured_at = time.time()
        attempt_id = uuid.uuid4().hex
        stamp = datetime.fromtimestamp(captured_at, timezone.utc)
        folder = self.root / stamp.strftime('%Y-%m-%d')
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{stamp.strftime('%Y%m%dT%H%M%S')}{stamp.microsecond // 1000:03d}Z_{self._safe_sn(sn)}_{visual_result}_{cycle_id[:12]}_{attempt_id[:12]}"
        raw_path = folder / f'{stem}_raw.jpg'
        result_path = folder / f'{stem}_result.jpg'
        json_path = folder / f'{stem}.json'
        source = dict(source or {})
        payload = {
            'schema_version': 1,
            'attempt_id': attempt_id,
            'cycle_id': cycle_id,
            'sn': sn,
            'captured_at_utc': stamp.isoformat().replace('+00:00', 'Z'),
            'captured_at_epoch': captured_at,
            'visual_result': visual_result,
            'source': source,
            'matching': matching,
            'raw_image': str(raw_path.relative_to(self.root)).replace('\\', '/'),
            'result_image': str(result_path.relative_to(self.root)).replace('\\', '/'),
        }
        written = []
        try:
            self._write_atomic(raw_path, self._encode_jpeg(raw_frame, quality))
            written.append(raw_path)
            self._write_atomic(result_path, self._encode_jpeg(result_frame, quality))
            written.append(result_path)
            self._write_atomic(json_path, json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))
            written.append(json_path)
            with self.db:
                self.db.execute(
                    "INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (attempt_id, cycle_id, sn, captured_at, visual_result,
                     str(source.get('mode') or 'camera'), json.dumps(source, ensure_ascii=False),
                     json.dumps(matching, ensure_ascii=False), payload['raw_image'],
                     payload['result_image'], str(json_path.relative_to(self.root)).replace('\\', '/')),
                )
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise
        return payload

    def list_cycles(self, *, sn='', visual_result='', limit=100, offset=0):
        clauses, values = [], []
        if sn:
            clauses.append('c.sn LIKE ?')
            values.append('%' + str(sn) + '%')
        if visual_result:
            clauses.append('a.visual_result = ?')
            values.append(str(visual_result))
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        sql = f"""
            SELECT c.cycle_id,c.sn,c.state,c.result,c.created_at,c.updated_at,
                   COUNT(a.attempt_id) AS attempt_count,
                   MAX(a.captured_at) AS last_capture_at,
                   (SELECT visual_result FROM attempts z WHERE z.cycle_id=c.cycle_id
                    ORDER BY z.captured_at DESC LIMIT 1) AS visual_result,
                   (SELECT source_mode FROM attempts z WHERE z.cycle_id=c.cycle_id
                    ORDER BY z.captured_at DESC LIMIT 1) AS source_mode
            FROM cycles c LEFT JOIN attempts a ON a.cycle_id=c.cycle_id
            {where} GROUP BY c.cycle_id ORDER BY c.updated_at DESC LIMIT ? OFFSET ?
        """
        rows = self.db.execute(sql, values + [max(1, min(int(limit), 500)), max(0, int(offset))]).fetchall()
        return [dict(row) for row in rows]

    def cycle_detail(self, cycle_id: str):
        cycle = self.db.execute('SELECT * FROM cycles WHERE cycle_id=?', (cycle_id,)).fetchone()
        if cycle is None:
            return None
        attempts = self.db.execute(
            'SELECT * FROM attempts WHERE cycle_id=? ORDER BY captured_at', (cycle_id,)
        ).fetchall()
        result = dict(cycle)
        result['detail'] = json.loads(result.pop('detail_json') or '{}')
        result['attempts'] = []
        for row in attempts:
            item = dict(row)
            item['source'] = json.loads(item.pop('source_json') or '{}')
            item['matching'] = json.loads(item.pop('matching_json') or '{}')
            result['attempts'].append(item)
        return result

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / str(relative_path)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError('Evidence path is outside the inspection store')
        return candidate
