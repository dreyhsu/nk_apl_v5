#!/usr/bin/env python3
"""Host-side Edge runtime smoke test; Flask and a physical camera are not required."""
import tempfile
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from edge_runtime import EdgeRuntime, EDGE_PHOTO_DIR, EDGE_RECORD_DIR


BASE_PREVIEW_STUB = SimpleNamespace(
    METHOD_MAP_PC={'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED},
)


class FakeCache:
    last_rule_results = [{'id': 1, 'name': 'fake', 'pass': True}]


class FakeCacheManager:
    def __init__(self, *args, **kwargs):
        self.cache = FakeCache()
    def initial_load(self): return True
    def start_watcher(self): return None
    def stop_watcher(self): return None
    def get(self): return self.cache


class FakeSopEngine:
    def __init__(self, steps, cfg):
        self.n = 0
    def summary(self):
        return {'complete': False, 'progress_pct': 0, 'done_count': 0, 'total_required': 1, 'steps': []}
    def update(self, rules):
        self.n += 1
        done = self.n >= 2
        return {
            'complete': done, 'progress_pct': 100 if done else 50,
            'done_count': 1 if done else 0, 'total_required': 1,
            'current_step_id': 1, 'alarm': {'active': False},
            'steps': [{'id': 1, 'step_no': 1, 'name': 'Fake Step', 'required': True,
                       'status': 'DONE' if done else 'DETECTING', 'last_score': .99,
                       'consecutive_hits': min(2, self.n), 'min_consecutive_hits': 2}],
        }
    def reset(self, reason=''): self.n = 0; return self.summary()
    def finish(self): return self.update([])
    def acknowledge_alarm(self): return self.summary()
    def interrupt(self, reason=''): return self.summary()


def fake_run_inference(frame, cache, method=None, draw_vis=True):
    vis = frame.copy()
    cv2.putText(vis, 'FAKE PASS', (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    cache.last_rule_results = [{'id': 1, 'name': 'fake', 'pass': True}]
    return True, [{'label': 'fake', 'pass': True, 'score': .99}], vis


FAKE_VC = SimpleNamespace(
    CacheManager=FakeCacheManager,
    load_runtime_inspection_items=lambda db, pid: [{'id': 1}],
    load_product_sop_config=lambda db, pid: {'enabled': True},
    SopFlowEngine=FakeSopEngine,
    run_inference=fake_run_inference,
)
BASE_INFER_STUB = SimpleNamespace(
    METHOD_MAP_PC={'TM_CCOEFF_NORMED': cv2.TM_CCOEFF_NORMED},
    DB_PATH=':memory:', vc=FAKE_VC,
    _load_product_dict=lambda pid: {'id': pid, 'serial': 'FAKE-001', 'name': 'Fake', 'reference_width': 0, 'reference_height': 0},
    _prepare_frame=lambda frame, product: frame,
)


def make_video(path: Path):
    w, h, fps = 640, 360, 15.0
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError('Cannot create smoke-test video')
    for i in range(90):
        img = np.zeros((h, w, 3), dtype=np.uint8)
        x = 20 + (i * 5) % 500
        cv2.rectangle(img, (x, 90), (x + 90, 190), (255, 255, 255), -1)
        cv2.putText(img, f'EDGE {i:03d}', (30, 320), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 220, 255), 2)
        writer.write(img)
    writer.release()


def wait_for(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.03)
    return None


def run_preview_capture(video: Path, cfg: Path):
    rt = EdgeRuntime(BASE_PREVIEW_STUB, config_path=cfg)
    rt.update_config({
        'backend': 'opencv', 'source': str(video), 'rotation': 0,
        'product_id': 0, 'preview_quality': 70, 'recording_fps': 15,
        'loop_source': True, 'min_free_mb': 0,
    })
    assert rt.start()['success']
    assert wait_for(lambda: rt.status()['frame_seq'] >= 3), rt.status()
    st = rt.status()
    assert st.get('warning','') == '', st
    assert 'storage_free_mb' in st and 'storage_reserve_mb' in st, st
    assert rt.jpeg('raw'), 'raw preview missing'
    snap = rt.snapshot('raw')
    assert snap['success'], snap
    start_seq = rt.status()['frame_seq']
    rec = rt.start_recording('raw')
    assert rec['success'], rec
    assert wait_for(lambda: rt.status()['frame_seq'] >= start_seq + 12), rt.status()
    rec_stop = rt.stop_recording()
    assert rec_stop['success'], rec_stop
    media = rt.list_media()
    assert media['photos'] and media['recordings'], media
    assert rt.stop()['success']
    stopped = rt.status()
    assert stopped['source_fps'] == 0 and stopped['infer_fps'] == 0, stopped
    return snap, rec_stop


def run_fake_inference(video: Path, cfg: Path):
    rt = EdgeRuntime(BASE_INFER_STUB, config_path=cfg)
    rt.update_config({
        'backend': 'opencv', 'source': str(video), 'rotation': 0,
        'product_id': 1, 'infer_fps': 8, 'loop_source': True, 'min_free_mb': 0,
    })
    assert rt.start()['success']
    assert wait_for(lambda: rt.status()['infer_seq'] >= 2), rt.status()
    st = rt.status()
    assert st['product']['serial'] == 'FAKE-001', st
    assert st['verdict'] == 'COMPLETE', st
    assert st['sop']['complete'] is True, st
    assert rt.jpeg('result'), 'result preview missing'
    assert rt.reset_sop()['complete'] is False
    assert rt.stop()['success']


def main():
    before_photos = {p.name for p in EDGE_PHOTO_DIR.glob('*') if p.is_file()}
    before_recs = {p.name for p in EDGE_RECORD_DIR.glob('*') if p.is_file()}
    try:
        with tempfile.TemporaryDirectory(prefix='edge-smoke-') as td:
            td = Path(td)
            video = td / 'source.avi'
            make_video(video)
            snap, rec_stop = run_preview_capture(video, td / 'preview.ini')
            run_fake_inference(video, td / 'infer.ini')
            print('EDGE_SMOKE_TEST PASS')
            print('snapshot:', snap['path'])
            print('recording:', rec_stop.get('relative_path') or rec_stop.get('path'))
            print('fake inference + SOP: PASS')
    finally:
        # Remove only files created by this smoke run so the deliverable starts clean.
        for p in EDGE_PHOTO_DIR.glob('*'):
            if p.is_file() and p.name not in before_photos:
                p.unlink()
        for p in EDGE_RECORD_DIR.glob('*'):
            if p.is_file() and p.name not in before_recs:
                p.unlink()


if __name__ == '__main__':
    main()
