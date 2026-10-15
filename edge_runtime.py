#!/usr/bin/env python3
"""VisionEdge single-camera runtime based on LIVE_FLOW_GRAPH_REVIEW.

Goals
-----
* One physical camera owner per process.
* Preview, template/SOP inference, snapshot and recording can run together.
* Qualcomm qtiqmmfsrc/v4l2h264enc is preferred on-device; OpenCV is the
  portable development/fallback backend.
* Existing LIVE_FLOW_GRAPH_REVIEW product/template/SOP data model is reused by VisionEdge.

The QTI backend uses one camera pipeline with a tee. One branch produces JPEG
frames for CPU-side template matching; another branch encodes H.264 once and
feeds an on-demand MP4 recorder. This avoids the old capture-vs-inference camera
handoff used by smartcam_webserver.
"""
from __future__ import annotations

import configparser
import copy
import datetime as _dt
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
import apl_diagnostics as diag

try:  # Only present on the Qualcomm image / GStreamer-enabled Linux hosts.
    import gi  # type: ignore
    gi.require_version('Gst', '1.0')
    from gi.repository import Gst  # type: ignore
    GST_AVAILABLE = True
except Exception:
    Gst = None  # type: ignore
    GST_AVAILABLE = False


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / 'config' / 'edge.ini'
EDGE_RUNTIME_DIR = BASE_DIR / 'runtime_data' / 'edge'
EDGE_PHOTO_DIR = EDGE_RUNTIME_DIR / 'photos'
EDGE_RECORD_DIR = EDGE_RUNTIME_DIR / 'recordings'
EDGE_LOG_DIR = EDGE_RUNTIME_DIR / 'logs'
log = logging.getLogger('camera.lifecycle')
# Mirrors APLStation._validated_auto_capture, for a runtime with no station.
AUTO_CAPTURE_DEFAULTS = {'enabled': False, 'fps': 5.0, 'required_hits': 2, 'timeout_sec': 0.0,
                         'timeout_action': 'not_matched'}


def _new_auto_state() -> Dict[str, Any]:
    """Automatic Capture & Inspect probe state. The camera thread owns every field
    except 'committing', which the commit worker clears under the runtime lock."""
    return {
        'key': None,              # (camera_generation, cycle_id, attempt_count)
        'cycle': '',
        'armed': False,
        'reason': 'disabled',
        'hits': 0,
        'score': None,
        'best_score': None,
        'last_match': None,       # overlay preview only, never evidence
        'window_started': None,   # time.monotonic()
        'elapsed': 0.0,
        'suspended_cycle': None,  # the cycle whose probe gave up on timeout
        'next_probe': 0.0,
        'committing': False,
        'last_commit': None,
        'error': '',
    }


def _ensure_dirs() -> None:
    for p in (EDGE_RUNTIME_DIR, EDGE_PHOTO_DIR, EDGE_RECORD_DIR, EDGE_LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _safe_int(v: Any, default: int, lo: Optional[int] = None, hi: Optional[int] = None) -> int:
    try:
        x = int(v)
    except Exception:
        x = int(default)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _safe_float(v: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        x = float(v)
    except Exception:
        x = float(default)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


def _timestamp() -> str:
    return _dt.datetime.now().strftime('%Y-%m-%d_%H%M%S_%f')[:-3]


def _jpeg(frame: np.ndarray, quality: int = 80) -> bytes:
    ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError('JPEG encode failed')
    return bytes(buf)


def _decode_jpeg(data: bytes) -> Optional[np.ndarray]:
    if not data:
        return None
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


class GstVideoFileCapture:
    """GStreamer file reader with native time seeking for SmartCam."""

    def __init__(self, path: Path, demux: str, *, realtime: bool = True):
        if not GST_AVAILABLE:
            raise RuntimeError('GStreamer unavailable')
        self.path = Path(path).resolve()
        self.demux = str(demux)
        self.realtime = bool(realtime)
        self.pipeline = None
        self.sink = None
        self.pace = None
        self.bus = None
        self.duration_seconds = 0.0
        self.fps = 0.0
        self.frame_index = -1
        self.playback_seconds = 0.0
        self.last_error = ''
        self._opened = False
        self._create_pipeline(paced=self.realtime, precise=False)

    def _pipeline_text(self, paced: bool, precise: bool) -> str:
        location = str(self.path).replace('\\', '\\\\').replace('"', '\\"')
        sync = 'true' if paced else 'false'
        drop = 'false' if precise else ('true' if self.realtime else 'false')
        buffers = 8 if precise else (1 if self.realtime else 4)
        return (f'filesrc location="{location}" ! {self.demux} name=demux '
                'demux.video_0 ! queue ! decodebin ! qtivtransform ! '
                f'video/x-raw,format=BGR ! identity name=video_pace sync={sync} ! '
                f'appsink name=frame_sink emit-signals=false sync=false '
                f'max-buffers={buffers} drop={drop}')

    def _create_pipeline(self, *, paced: bool, precise: bool) -> None:
        Gst.init(None)
        self.pipeline = Gst.parse_launch(self._pipeline_text(paced, precise))
        self.sink = self.pipeline.get_by_name('frame_sink')
        self.pace = self.pipeline.get_by_name('video_pace')
        self.bus = self.pipeline.get_bus()
        if self.sink is None or self.pace is None:
            self.release()
            raise RuntimeError('GStreamer video pipeline is missing appsink or pace control')
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.release()
            raise RuntimeError('GStreamer video pipeline failed to enter PLAYING')
        self._opened = True
        self._query_duration()

    def _query_duration(self) -> None:
        if self.pipeline is None:
            return
        try:
            ok, value = self.pipeline.query_duration(Gst.Format.TIME)
            if ok and value not in (None, Gst.CLOCK_TIME_NONE) and int(value) > 0:
                self.duration_seconds = float(value) / float(Gst.SECOND)
        except Exception:
            pass

    def _read_bus_error(self) -> str:
        if self.bus is None:
            return ''
        try:
            message = self.bus.pop_filtered(Gst.MessageType.ERROR)
            if message is not None:
                error, debug = message.parse_error()
                return f'{error}: {debug or ""}'.strip()
        except Exception:
            pass
        return ''

    def _update_fps(self, structure) -> None:
        if self.fps > 0 or structure is None:
            return
        try:
            value = structure.get_value('framerate')
            numerator = int(getattr(value, 'numerator', 0))
            denominator = int(getattr(value, 'denominator', 0))
            if denominator > 0:
                rate = float(numerator) / float(denominator)
                if 0.1 <= rate <= 240.0:
                    self.fps = rate
        except Exception:
            pass

    def _sample_to_frame(self, sample) -> Tuple[np.ndarray, float, int]:
        caps = sample.get_caps()
        structure = caps.get_structure(0) if caps is not None and caps.get_size() else None
        if structure is None:
            raise RuntimeError('Decoded video sample has no caps')
        width = int(structure.get_value('width'))
        height = int(structure.get_value('height'))
        self._update_fps(structure)
        buffer = sample.get_buffer()
        if buffer is None:
            raise RuntimeError('Decoded video sample has no buffer')
        mapped, info = buffer.map(Gst.MapFlags.READ)
        if not mapped:
            raise RuntimeError('Decoded video buffer cannot be mapped')
        try:
            pixels = np.frombuffer(info.data, dtype=np.uint8)
            row_bytes = width * 3
            if height <= 0 or pixels.size < row_bytes * height or pixels.size % height:
                raise RuntimeError('Decoded BGR buffer size is invalid')
            stride = pixels.size // height
            frame = pixels.reshape(height, stride)[:, :row_bytes].reshape(height, width, 3).copy()
        finally:
            buffer.unmap(info)
        pts = getattr(buffer, 'pts', Gst.CLOCK_TIME_NONE)
        if pts not in (None, Gst.CLOCK_TIME_NONE) and int(pts) >= 0:
            position = float(pts) / float(Gst.SECOND)
        else:
            position = (self.frame_index + 1) / self.fps if self.fps > 0 else self.playback_seconds
        if self.fps > 0:
            index = max(0, int(round(position * self.fps)))
        else:
            index = self.frame_index + 1
        self.playback_seconds = max(0.0, position)
        self.frame_index = max(0, index)
        self._query_duration()
        return frame, self.playback_seconds, self.frame_index

    def _pull_sample(self, timeout_seconds: float = 3.0):
        if self.sink is None:
            return None
        timeout_ns = max(1, int(float(timeout_seconds) * float(Gst.SECOND)))
        sample = self.sink.emit('try-pull-sample', timeout_ns)
        if sample is None:
            self.last_error = self._read_bus_error()
            return None
        return self._sample_to_frame(sample)

    def _decode_until(self, target_seconds: float, timeout_seconds: float = 15.0):
        target = max(0.0, float(target_seconds))
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        latest = None
        while time.monotonic() < deadline:
            remaining = max(0.05, min(2.0, deadline - time.monotonic()))
            item = self._pull_sample(remaining)
            if item is None:
                break
            latest = item
            if item[1] + 0.001 >= target:
                break
        return latest

    def _rebuild_from_start(self) -> None:
        self.release()
        self.duration_seconds = 0.0
        self.fps = 0.0
        self.frame_index = -1
        self.playback_seconds = 0.0
        self.last_error = ''
        self._create_pipeline(paced=False, precise=True)

    def _set_precise_mode(self, enabled: bool) -> None:
        if self.sink is None:
            return
        self.sink.set_property('drop', False if enabled else self.realtime)
        self.sink.set_property('max-buffers', 8 if enabled else (1 if self.realtime else 4))

    def seek_time(self, seconds: float) -> Dict[str, Any]:
        if not self.isOpened():
            raise RuntimeError('GStreamer video pipeline is not open')
        target = max(0.0, float(seconds))
        if self.duration_seconds > 0:
            target = min(target, max(0.0, self.duration_seconds - 0.001))
        target_ns = int(target * float(Gst.SECOND))
        item = None
        native_seek_ok = False
        try:
            self.pipeline.set_state(Gst.State.PAUSED)
            self.pace.set_property('sync', False)
            self._set_precise_mode(True)
            native_seek_ok = bool(self.pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                target_ns,
            ))
            if native_seek_ok:
                self.pipeline.set_state(Gst.State.PLAYING)
                item = self._decode_until(target)
        except Exception as exc:
            self.last_error = str(exc)
            native_seek_ok = False
        if not native_seek_ok or item is None:
            self._rebuild_from_start()
            item = self._decode_until(target)
        if item is None:
            detail = f': {self.last_error}' if self.last_error else ''
            raise RuntimeError(f'Video frame could not be decoded at the requested position{detail}')
        self.pipeline.set_state(Gst.State.PAUSED)
        self.pace.set_property('sync', self.realtime)
        self._set_precise_mode(False)
        frame, actual_seconds, frame_index = item
        return {
            'frame': frame,
            'playback_seconds': actual_seconds,
            'frame_index': frame_index,
        }

    def set_paused(self, paused: bool) -> bool:
        if not self.isOpened():
            return False
        state = Gst.State.PAUSED if paused else Gst.State.PLAYING
        return self.pipeline.set_state(state) != Gst.StateChangeReturn.FAILURE

    def isOpened(self) -> bool:
        return bool(self._opened and self.pipeline is not None)

    def getBackendName(self) -> str:
        return 'GSTREAMER'

    def read(self):
        if not self.isOpened():
            return False, None
        item = self._pull_sample()
        return (True, item[0]) if item is not None else (False, None)

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FPS:
            return self.fps
        if prop == cv2.CAP_PROP_FRAME_COUNT:
            return self.duration_seconds * self.fps if self.duration_seconds > 0 and self.fps > 0 else 0.0
        if prop == cv2.CAP_PROP_POS_MSEC:
            return self.playback_seconds * 1000.0
        if prop == cv2.CAP_PROP_POS_FRAMES:
            return float(self.frame_index + 1)
        return 0.0

    def set(self, prop: int, value: float) -> bool:
        del value
        return prop == cv2.CAP_PROP_BUFFERSIZE

    def release(self) -> None:
        pipeline = self.pipeline
        self._opened = False
        self.pipeline = None
        self.sink = None
        self.pace = None
        self.bus = None
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass


def open_video_file(path, *, realtime=True, native_seek=False):
    """Decode only the video track; offline mode preserves every source frame."""
    file_path = Path(path).resolve()
    demux = {'.mp4': 'qtdemux', '.mov': 'qtdemux', '.m4v': 'qtdemux',
             '.mkv': 'matroskademux', '.avi': 'avidemux'}.get(file_path.suffix.lower())
    if GST_AVAILABLE and demux:
        Gst.init(None)
        if Gst.ElementFactory.find('qtivtransform'):
            if native_seek:
                return GstVideoFileCapture(file_path, demux, realtime=realtime)
            location = str(file_path).replace('\\', '\\\\').replace('"', '\\"')
            sync = 'true' if realtime else 'false'
            drop = 'true' if realtime else 'false'
            buffers = 1 if realtime else 4
            pipeline = (f'filesrc location="{location}" ! {demux} name=demux '
                        'demux.video_0 ! queue ! decodebin ! qtivtransform ! '
                        f'video/x-raw,format=BGR ! identity sync={sync} ! '
                        f'appsink sync=false max-buffers={buffers} drop={drop}')
            return cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    return cv2.VideoCapture(str(file_path))


@dataclass
class EdgeConfig:
    backend: str = 'qti'                # auto | qti | opencv
    source: str = '0'                   # camera index / file / URI for opencv
    camera: int = 0                     # qti camera id
    width: int = 1920
    height: int = 1080
    framerate: str = '30/1'
    rotation: int = 180                 # 0 | 180
    ldc: bool = False
    product_id: int = 0                 # 0 = preview only
    infer_fps: float = 10.0
    preview_quality: int = 80
    snapshot_quality: int = 92
    # 0 = automatic by resolution (8 Mbps at 1080p, 16 Mbps at 4K).
    recording_bitrate: int = 0
    recording_fps: float = 30.0
    record_source: str = 'result'       # QTI records the shared overlaid stream
    min_free_mb: int = 10240
    reconnect_sec: float = 10.0
    loop_source: bool = True
    auto_start: bool = False
    method: str = 'TM_CCOEFF_NORMED'
    display_mode: str = 'headless'
    live_view_mode: str = 'webrtc'
    stream_enabled: bool = True
    stream_rtp_host: str = '127.0.0.1'
    stream_rtp_port: int = 5004

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> 'EdgeConfig':
        cfg = cls()
        cp = configparser.ConfigParser()
        if path.exists():
            cp.read(path, encoding='utf-8')
        sec = cp['edge'] if cp.has_section('edge') else {}
        cfg.backend = str(sec.get('backend', cfg.backend)).strip().lower()
        cfg.source = str(sec.get('source', cfg.source)).strip()
        cfg.camera = _safe_int(sec.get('camera', cfg.camera), cfg.camera, 0)
        cfg.width = _safe_int(sec.get('width', cfg.width), cfg.width, 64)
        cfg.height = _safe_int(sec.get('height', cfg.height), cfg.height, 64)
        cfg.framerate = str(sec.get('framerate', cfg.framerate)).strip() or cfg.framerate
        cfg.rotation = 180 if _safe_int(sec.get('rotation', cfg.rotation), cfg.rotation) == 180 else 0
        cfg.ldc = _truthy(sec.get('ldc', cfg.ldc))
        cfg.product_id = _safe_int(sec.get('product_id', cfg.product_id), cfg.product_id, 0)
        cfg.infer_fps = _safe_float(sec.get('infer_fps', cfg.infer_fps), cfg.infer_fps, 0.1, 30.0)
        cfg.preview_quality = _safe_int(sec.get('preview_quality', cfg.preview_quality), cfg.preview_quality, 30, 100)
        cfg.snapshot_quality = _safe_int(sec.get('snapshot_quality', cfg.snapshot_quality), cfg.snapshot_quality, 50, 100)
        cfg.recording_bitrate = _safe_int(sec.get('recording_bitrate', cfg.recording_bitrate), cfg.recording_bitrate, 0)
        cfg.recording_fps = _safe_float(sec.get('recording_fps', cfg.recording_fps), cfg.recording_fps, 1.0, 120.0)
        cfg.record_source = str(sec.get('record_source', cfg.record_source)).strip().lower()
        cfg.record_source = cfg.record_source if cfg.record_source in ('raw', 'result') else 'raw'
        cfg.min_free_mb = _safe_int(sec.get('min_free_mb', cfg.min_free_mb), cfg.min_free_mb, 0)
        cfg.reconnect_sec = _safe_float(sec.get('reconnect_sec', cfg.reconnect_sec), cfg.reconnect_sec, 0.2, 30.0)
        cfg.loop_source = _truthy(sec.get('loop_source', cfg.loop_source))
        cfg.auto_start = _truthy(sec.get('auto_start', cfg.auto_start))
        cfg.method = str(sec.get('method', cfg.method)).strip() or cfg.method
        cfg.display_mode = str(sec.get('display_mode', cfg.display_mode)).strip().lower()
        if cfg.display_mode not in ('headless', 'hdmi', 'auto'):
            cfg.display_mode = 'headless'
        cfg.live_view_mode = str(sec.get('live_view_mode', cfg.live_view_mode)).strip().lower()
        cfg.stream_enabled = _truthy(sec.get('stream_enabled', cfg.stream_enabled))
        cfg.stream_rtp_host = str(sec.get('stream_rtp_host', cfg.stream_rtp_host)).strip()
        cfg.stream_rtp_port = _safe_int(sec.get('stream_rtp_port', cfg.stream_rtp_port), 5004, 1, 65535)
        return cfg

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve deployment sections such as [server] (HTTPS cert/port).
        # The old implementation rewrote edge.ini with only [edge], which
        # could silently erase server/TLS settings after a UI Save.
        cp = configparser.ConfigParser()
        if path.exists():
            cp.read(path, encoding='utf-8-sig')
        if cp.has_section('edge'):
            cp.remove_section('edge')
        cp['edge'] = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in asdict(self).items()}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix=path.name + '.', suffix='.tmp', delete=False) as f:
                temporary = Path(f.name)
                cp.write(f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def public(self) -> Dict[str, Any]:
        return asdict(self)

    def update(self, data: Dict[str, Any]) -> None:
        # Central whitelist/normalization so HTTP cannot inject arbitrary INI keys.
        for key in ('backend', 'source', 'framerate', 'record_source', 'method', 'display_mode'):
            if key in data:
                setattr(self, key, str(data[key]).strip())
        if 'backend' in data:
            self.backend = self.backend.lower()
            if self.backend not in ('auto', 'qti', 'opencv'):
                self.backend = 'auto'
        if self.display_mode not in ('headless', 'hdmi', 'auto'):
            self.display_mode = 'headless'
        if 'record_source' in data:
            self.record_source = self.record_source.lower()
            if self.record_source not in ('raw', 'result'):
                self.record_source = 'raw'
        for key, default, lo, hi in (
            ('camera', self.camera, 0, None), ('width', self.width, 64, 8192),
            ('height', self.height, 64, 8192), ('product_id', self.product_id, 0, None),
            ('preview_quality', self.preview_quality, 30, 100),
            ('snapshot_quality', self.snapshot_quality, 50, 100),
            ('recording_bitrate', self.recording_bitrate, 0, 100_000_000),
            ('min_free_mb', self.min_free_mb, 0, None),
        ):
            if key in data:
                setattr(self, key, _safe_int(data[key], default, lo, hi))
        if 'rotation' in data:
            self.rotation = 180 if _safe_int(data['rotation'], self.rotation) == 180 else 0
        for key, default, lo, hi in (
            ('infer_fps', self.infer_fps, 0.1, 30.0),
            ('recording_fps', self.recording_fps, 1.0, 120.0),
            ('reconnect_sec', self.reconnect_sec, 0.2, 30.0),
        ):
            if key in data:
                setattr(self, key, _safe_float(data[key], default, lo, hi))
        for key in ('ldc', 'loop_source', 'auto_start'):
            if key in data:
                setattr(self, key, _truthy(data[key]))


class CameraBackend:
    name = 'base'
    hardware_recording = False

    def start(self) -> None:
        raise NotImplementedError

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def start_recording(self, path: Path) -> Tuple[bool, str]:
        return False, 'hardware recording unavailable'

    def stop_recording(self) -> Dict[str, Any]:
        return {'success': False, 'error': 'hardware recording unavailable'}

    def status(self) -> Dict[str, Any]:
        return {'backend': self.name, 'hardware_recording': self.hardware_recording}

    def set_overlay(self, state):
        pass


class OpenCVCameraBackend(CameraBackend):
    name = 'opencv'

    def __init__(self, cfg: EdgeConfig):
        self.cfg = cfg
        self._control_lock = threading.RLock()
        self.cap = None
        self._is_file = False
        self._file_fps = 0.0
        self._next_deadline = 0.0
        self._source_clocked = False
        self._playback_started = 0.0
        self.last_frame_monotonic = 0.0
        self.playback_seconds = 0.0
        self.duration_seconds = 0.0
        self.replay_count = 0
        self.paused = False
        self.last_frame = None
        self.frame_index = 0

    def _source(self):
        src = self.cfg.source.strip()
        if src.isdigit():
            return int(src)
        return src

    def start(self) -> None:
        src = self._source()
        self._is_file = isinstance(src, str) and os.path.isfile(src)
        self.cap = open_video_file(src, native_seek=True) if self._is_file else cv2.VideoCapture(src)
        if not self.cap.isOpened():
            self.cap.release()
            self.cap = None
            raise RuntimeError(f'OpenCV cannot open source: {src}')
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        if isinstance(src, int):
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.height)
        if self._is_file:
            try:
                self._file_fps = float(self.cap.get(cv2.CAP_PROP_FPS) or 0.0)
            except Exception:
                self._file_fps = 0.0
            if not (0.1 <= self._file_fps <= 240.0):
                self._file_fps = 25.0
            try:
                self._source_clocked = self.cap.getBackendName().upper() == 'GSTREAMER'
            except Exception:
                self._source_clocked = False
            self._next_deadline = time.monotonic()
            self._playback_started = self._next_deadline
            native_duration = float(getattr(self.cap, 'duration_seconds', 0.0) or 0.0)
            self.duration_seconds = native_duration or (
                float(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / self._file_fps
            )

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        del timeout
        with self._control_lock:
            if self.cap is None:
                return None
            if self._is_file and self.paused and self.last_frame is not None:
                frame = self.last_frame.copy()
                paused = True
                ok = True
            else:
                ok, frame = self.cap.read()
                paused = False
        if ok and frame is not None:
            if paused:
                time.sleep(0.03)
            elif self._source_clocked:
                native_position = getattr(self.cap, 'playback_seconds', None)
                if isinstance(native_position, (int, float)):
                    self.playback_seconds = max(0.0, float(native_position))
                    self.frame_index = max(0, int(getattr(self.cap, 'frame_index', self.frame_index)))
                    native_duration = getattr(self.cap, 'duration_seconds', None)
                    if isinstance(native_duration, (int, float)) and native_duration > 0:
                        self.duration_seconds = float(native_duration)
                else:
                    self.playback_seconds = max(0.0, time.monotonic() - self._playback_started)
            else:
                with self._control_lock:
                    self.playback_seconds = float(self.cap.get(cv2.CAP_PROP_POS_MSEC) or 0) / 1000
                    self.frame_index = max(0, int(self.cap.get(cv2.CAP_PROP_POS_FRAMES) or 1) - 1)
            if self._is_file and self._file_fps > 0 and not self._source_clocked and not paused:
                now = time.monotonic()
                wait = self._next_deadline - now
                if wait > 0:
                    time.sleep(min(wait, 1.0))
                self._next_deadline = max(self._next_deadline + 1.0 / self._file_fps, time.monotonic())
            self.last_frame_monotonic = time.monotonic()
            if not paused:
                self.last_frame = frame.copy()
            return frame
        if self._is_file and not self.cfg.loop_source and self.last_frame is not None:
            self.paused = True
            self.playback_seconds = self.duration_seconds or self.playback_seconds
            return self.last_frame.copy()
        if self._is_file and self.cfg.loop_source:
            self.replay_count += 1
            if not self.rewind():
                return None
            ok, frame = self.cap.read()
            if ok:
                self.last_frame_monotonic = time.monotonic()
                return frame
        return None

    def rewind(self) -> bool:
        if not self._is_file or self.cap is None:
            return False
        reset = False
        if hasattr(self.cap, 'seek_time'):
            try:
                self.cap.seek_time(0.0)
                reset = bool(self.cap.set_paused(False))
            except Exception:
                reset = False
        elif not self._source_clocked:
            try:
                reset = bool(self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0))
            except Exception:
                reset = False
        if not reset:
            old = self.cap
            self.cap = open_video_file(self._source(), native_seek=True)
            try:
                old.release()
            except Exception:
                pass
            if not self.cap.isOpened():
                self.cap.release()
                self.cap = None
                return False
        self._next_deadline = time.monotonic()
        self._playback_started = self._next_deadline
        self.playback_seconds = 0.0
        self.frame_index = 0
        self.paused = False
        return True

    def set_paused(self, paused: bool) -> bool:
        with self._control_lock:
            if not self._is_file or self.cap is None:
                return False
            if hasattr(self.cap, 'set_paused') and not self.cap.set_paused(bool(paused)):
                return False
            self.paused = bool(paused)
            if not self.paused:
                self._next_deadline = time.monotonic()
                self._playback_started = self._next_deadline - self.playback_seconds
            return True

    def seek(self, seconds: float) -> Dict[str, Any]:
        with self._control_lock:
            if not self._is_file or self.cap is None:
                raise ValueError('Seek is only available for a test video')
            value = max(0.0, float(seconds))
            if self.duration_seconds > 0:
                value = min(value, self.duration_seconds)
            frame_no = int(round(value * self._file_fps)) if self._file_fps > 0 else 0
            if hasattr(self.cap, 'seek_time'):
                result = self.cap.seek_time(value)
                frame = result['frame']
                self.frame_index = max(0, int(result.get('frame_index', frame_no)))
                self.playback_seconds = max(0.0, float(result.get('playback_seconds', value)))
            else:
                ok = bool(self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no))
                if not ok:
                    ok = bool(self.cap.set(cv2.CAP_PROP_POS_MSEC, value * 1000.0))
                if not ok:
                    raise RuntimeError('Video seek failed')
                decoded, frame = self.cap.read()
                if not decoded or frame is None:
                    raise RuntimeError('Video frame could not be decoded at the requested position')
                self.frame_index = max(0, int(self.cap.get(cv2.CAP_PROP_POS_FRAMES) or frame_no + 1) - 1)
                self.playback_seconds = float(self.cap.get(cv2.CAP_PROP_POS_MSEC) or value * 1000.0) / 1000.0
            self.last_frame = frame.copy()
            self.paused = True
            self._next_deadline = time.monotonic()
            return {'paused': True, 'playback_seconds': self.playback_seconds,
                    'duration_seconds': self.duration_seconds, 'frame_index': self.frame_index}

    def selected_frame(self):
        with self._control_lock:
            frame = self.last_frame.copy() if self.last_frame is not None else None
            return frame, {
                'mode': 'video_test', 'source': str(self.cfg.source),
                'playback_seconds': self.playback_seconds,
                'duration_seconds': self.duration_seconds,
                'frame_index': self.frame_index, 'paused': self.paused,
            }

    def stop(self) -> None:
        with self._control_lock:
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None

    def status(self) -> Dict[str, Any]:
        out = super().status()
        out.update({'source': self.cfg.source, 'opened': bool(self.cap is not None and self.cap.isOpened()),
                    'playback_seconds': self.playback_seconds, 'duration_seconds': self.duration_seconds,
                    'frame_index': self.frame_index, 'paused': self.paused,
                    'is_file': self._is_file, 'replay_count': self.replay_count})
        return out


class GstH264Mp4Recorder:
    """Small self-contained H264 appsink -> appsrc/mp4mux recorder.

    It intentionally mirrors the stable mechanism used by smartcam_smd_sdk:
    wait for a keyframe, rebase timestamps, push into a separate mux pipeline,
    EOS-finalize that pipeline without stopping the camera.
    """

    def __init__(self, path: Path, caps):
        if not GST_AVAILABLE:
            raise RuntimeError('GStreamer unavailable')
        self.path = Path(path)
        self.caps = caps
        self.pipeline = None
        self.src = None
        self.started = False
        self.base_pts = None
        self.base_dts = None
        self.frames = 0
        self.lock = threading.RLock()

        self._stop_lock = threading.Lock()
        self._stop_result = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pipeline = Gst.Pipeline.new('edge-mp4-recorder')
        self.src = Gst.ElementFactory.make('appsrc', 'rec_src')
        parser = Gst.ElementFactory.make('h264parse', 'rec_parse')
        mux = Gst.ElementFactory.make('mp4mux', 'rec_mux')
        sink = Gst.ElementFactory.make('filesink', 'rec_sink')
        if None in (self.pipeline, self.src, parser, mux, sink):
            raise RuntimeError('Missing appsrc/h264parse/mp4mux/filesink')
        self.src.set_property('is-live', True)
        self.src.set_property('format', Gst.Format.TIME)
        self.src.set_property('do-timestamp', False)
        if self.caps is not None:
            self.src.set_property('caps', self.caps)
        try:
            parser.set_property('config-interval', -1)
        except Exception:
            pass
        try:
            mux.set_property('faststart', True)
            mux.set_property('fragment-duration', 1000)
        except Exception:
            pass
        sink.set_property('location', str(self.path))
        sink.set_property('sync', False)
        for el in (self.src, parser, mux, sink):
            self.pipeline.add(el)
        if not self.src.link(parser) or not parser.link(mux) or not mux.link(sink):
            raise RuntimeError('Cannot link MP4 recorder pipeline')
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError('MP4 recorder failed to enter PLAYING')
        self.started = True

    def push(self, buf) -> None:
        with self.lock:
            if not self.started or self.src is None or buf is None:
                return
            try:
                # Do not begin a file on a delta frame; wait for an IDR.
                if self.frames == 0 and buf.has_flags(Gst.BufferFlags.DELTA_UNIT):
                    return
                out = buf.copy_deep()
                if self.frames == 0:
                    self.base_pts = out.pts if out.pts != Gst.CLOCK_TIME_NONE else 0
                    self.base_dts = out.dts if out.dts != Gst.CLOCK_TIME_NONE else self.base_pts
                if out.pts != Gst.CLOCK_TIME_NONE:
                    out.pts = max(0, out.pts - int(self.base_pts or 0))
                if out.dts != Gst.CLOCK_TIME_NONE:
                    out.dts = max(0, out.dts - int(self.base_dts or 0))
                flow = self.src.emit('push-buffer', out)
                if flow == Gst.FlowReturn.OK:
                    self.frames += 1
            except Exception:
                # Never let a mux failure destroy the camera streaming thread.
                return

    def stop(self, timeout=8.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        if not self._stop_lock.acquire(timeout=max(0.0, timeout)):
            return {'success': False, 'recorder_null': False, 'error': 'Recorder stop lock timed out'}
        try:
            if self._stop_result is not None:
                return dict(self._stop_result)
            if not self.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                return {'success': False, 'recorder_null': False, 'error': 'Recorder push drain timed out'}
            try:
                was_started = self.started
                self.started = False
                src, pipeline = self.src, self.pipeline
            finally:
                self.lock.release()
            errors = []
            finalized = not was_started
            null = pipeline is None
            try:
                if was_started and src is not None and pipeline is not None:
                    if src.emit('end-of-stream') != Gst.FlowReturn.OK:
                        raise RuntimeError('Recorder rejected EOS')
                    remaining = min(5.0, max(0.0, deadline - time.monotonic()))
                    msg = pipeline.get_bus().timed_pop_filtered(
                        int(remaining * Gst.SECOND), Gst.MessageType.EOS | Gst.MessageType.ERROR)
                    if msg is None:
                        raise RuntimeError('MP4 finalize timed out before EOS')
                    if msg.type == Gst.MessageType.ERROR:
                        err, dbg = msg.parse_error()
                        raise RuntimeError(f'{err}: {dbg}')
                    finalized = True
            except Exception as exc:
                errors.append(str(exc))
            try:
                if pipeline is not None:
                    ret = pipeline.set_state(Gst.State.NULL)
                    remaining = min(2.0, max(0.0, deadline - time.monotonic()))
                    result, state, _ = pipeline.get_state(int(remaining * Gst.SECOND))
                    null = (ret != Gst.StateChangeReturn.FAILURE
                            and result != Gst.StateChangeReturn.FAILURE and state == Gst.State.NULL)
                    if not null:
                        raise RuntimeError('Recorder did not reach NULL')
            except Exception as exc:
                errors.append(str(exc))
            if null:
                self.src = self.pipeline = None
            if self.frames == 0 and null:
                if was_started:
                    errors.append('Recording contains no encoded frames')
                try:
                    self.path.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(f'Empty recording cleanup failed: {exc}')
            self._stop_result = {'success': finalized and null and not errors,
                                 'error': '; '.join(errors), 'path': str(self.path), 'frames': self.frames,
                                 'recorder_finalized': finalized, 'recorder_null': null}
            return dict(self._stop_result)
        finally:
            self._stop_lock.release()


class QtiGstCameraBackend(CameraBackend):
    name = 'qti'
    hardware_recording = True

    def __init__(self, cfg: EdgeConfig):
        if not GST_AVAILABLE:
            raise RuntimeError('PyGObject/GStreamer unavailable')
        self.cfg = cfg
        self.pipeline = None
        self.frame_sink = None
        self.h264_sink = None
        self.encoder = None
        self._frames: 'queue.Queue[np.ndarray]' = queue.Queue(maxsize=1)
        self._recorder: Optional[GstH264Mp4Recorder] = None
        self._record_path = ''
        self._record_lock = threading.RLock()
        self._record_stop_lock = threading.Lock()
        self._record_stopping = False
        self._unreleased_recorder = None
        self._bus_stop = threading.Event()
        self._bus_thread = None
        self._last_error = ''
        self._frame_callback = None
        self._overlay = None
        self._sample_stopping = threading.Event()
        self._sample_lock = threading.Lock()
        self._h264_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._release_result = None
        self._recorder_release = None
        self.display_actual = 'headless'

    @staticmethod
    def available() -> bool:
        if not GST_AVAILABLE:
            return False
        try:
            Gst.init(None)
            return Gst.ElementFactory.find('qtiqmmfsrc') is not None
        except Exception:
            return False

    @staticmethod
    def _fps_text(v: str) -> str:
        text = str(v or '30/1').strip()
        if '/' not in text:
            try:
                return f'{max(1, int(float(text)))}/1'
            except Exception:
                return '30/1'
        return text

    def _frame_sample(self, sink):
        if self._sample_stopping.is_set() or not self._sample_lock.acquire(blocking=False):
            return Gst.FlowReturn.OK
        try:
            if self._sample_stopping.is_set():
                return Gst.FlowReturn.OK
            return self._frame_sample_impl(sink)
        finally:
            self._sample_lock.release()

    def _frame_sample_impl(self, sink):
        try:
            sample = sink.emit('pull-sample')
            if sample is None:
                return Gst.FlowReturn.OK
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK
            try:
                jpeg_data = bytes(info.data)
            finally:
                buf.unmap(info)
            if self._frame_callback is not None:
                self._frame_callback(jpeg_data, time.monotonic())
                return Gst.FlowReturn.OK
            frame = _decode_jpeg(jpeg_data)
            if frame is not None:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    pass
        except Exception as exc:
            self._last_error = f'frame callback: {exc}'
        return Gst.FlowReturn.OK

    def _h264_sample(self, sink):
        if self._sample_stopping.is_set() or not self._h264_lock.acquire(blocking=False):
            return Gst.FlowReturn.OK
        try:
            if self._sample_stopping.is_set():
                return Gst.FlowReturn.OK
            return self._h264_sample_impl(sink)
        finally:
            self._h264_lock.release()

    def _h264_sample_impl(self, sink):
        try:
            sample = sink.emit('pull-sample')
            if sample is None:
                return Gst.FlowReturn.OK
            rec = self._recorder
            if rec is not None:
                rec.push(sample.get_buffer())
        except Exception as exc:
            self._last_error = f'h264 callback: {exc}'
        return Gst.FlowReturn.OK

    def _bus_loop(self):
        bus = self.pipeline.get_bus() if self.pipeline is not None else None
        while bus is not None and not self._bus_stop.wait(0.1):
            msg = bus.timed_pop_filtered(
                100 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS,
            )
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                diag.emit('GST_ERROR', element=msg.src.get_name() if msg.src else '', error=str(err), debug=dbg)
                self._last_error = f'{err}: {dbg}'
                break
            if msg.type == Gst.MessageType.WARNING:
                warn, dbg = msg.parse_warning()
                diag.emit('GST_WARNING', element=msg.src.get_name() if msg.src else '', error=str(warn), debug=dbg)
                self._last_error = f'warning: {warn}: {dbg}'
            if msg.type == Gst.MessageType.EOS:
                diag.emit('GST_EOS', element=msg.src.get_name() if msg.src else '')
                self._last_error = 'camera pipeline EOS'
                break

    @staticmethod
    def _make_gst_element(factory: str, name: str):
        elem = Gst.ElementFactory.make(factory, name)
        if elem is None:
            raise RuntimeError(f'GStreamer element not found: {factory}')
        return elem

    @staticmethod
    def _set_if_property(elem, prop: str, value) -> bool:
        try:
            if elem is None or elem.find_property(prop) is None:
                return False
            elem.set_property(prop, value)
            return True
        except Exception:
            return False

    @staticmethod
    def _link_or_raise(src, dst, desc: str) -> None:
        if not src.link(dst):
            raise RuntimeError(f'GStreamer link failed: {desc}')

    @staticmethod
    def _request_preview_pad(camsrc):
        """Request qtiqmmfsrc video_0 and set PREVIEW before linking.

        Qualcomm's newer QIR/QIM camera service is sensitive to the stream
        usecase selected on the request pad.  The PC-era parse_launch form can
        request/link video_0 before Python gets a chance to set pad.type=0,
        allowing the service to select an unsuitable default usecase.  Match
        the known-good LIVE_FLOW_GRAPH_REVIEW/vendor path: request video_0,
        set type=PREVIEW (0), then link it to the capsfilter.
        """
        pad = None
        try:
            pad = camsrc.get_static_pad('video_0')
        except Exception:
            pad = None
        if pad is None and hasattr(camsrc, 'request_pad_simple'):
            try:
                pad = camsrc.request_pad_simple('video_0')
            except Exception:
                pad = None
        if pad is None and hasattr(camsrc, 'get_request_pad'):
            try:
                pad = camsrc.get_request_pad('video_0')
            except Exception:
                pad = None
        if pad is None:
            raise RuntimeError('qtiqmmfsrc video_0 request pad unavailable')
        try:
            if pad.find_property('type') is not None:
                pad.set_property('type', 0)  # PREVIEW
        except Exception as exc:
            raise RuntimeError(f'cannot set qtiqmmfsrc video_0 type=PREVIEW: {exc}') from exc
        return pad

    @staticmethod
    def _startup_bus_error(pipeline) -> str:
        try:
            bus = pipeline.get_bus()
            if bus is None:
                return ''
            msg = bus.timed_pop_filtered(
                250 * Gst.MSECOND,
                Gst.MessageType.ERROR | Gst.MessageType.WARNING,
            )
            if msg is None:
                return ''
            if msg.type == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                return f'{err}: {dbg}'
            warn, dbg = msg.parse_warning()
            return f'warning: {warn}: {dbg}'
        except Exception:
            return ''

    @staticmethod
    def _state_name(value):
        return str(getattr(value, 'value_nick', value))

    def _stop_camera_pipeline(self, deadline):
        """Drain the main stream before closing the QMMF camera."""
        pipeline = self.pipeline
        detail = {'camera_eos': None, 'camera_state_change': {}, 'camera_elements': {}}
        errors = []
        if pipeline is None:
            return True, detail, errors
        remaining = lambda cap: min(cap, max(0.0, deadline - time.monotonic()))
        # A single bus consumer owns the stop-time EOS/ERROR messages.
        self._bus_stop.set()
        if self._bus_thread and self._bus_thread.is_alive():
            self._bus_thread.join(timeout=remaining(1.0))
            if self._bus_thread.is_alive():
                errors.append('Camera bus thread did not stop before EOS')
                return False, detail, errors
        try:
            _, initial, _ = pipeline.get_state(0)
            if initial in (Gst.State.PLAYING, Gst.State.PAUSED):
                log.info('PIPELINE_EOS pid=%s state=%s', os.getpid(), self._state_name(initial))
                try:
                    if not pipeline.send_event(Gst.Event.new_eos()):
                        raise RuntimeError('Camera pipeline rejected EOS')
                    msg = pipeline.get_bus().timed_pop_filtered(
                        int(remaining(2.0) * Gst.SECOND), Gst.MessageType.EOS | Gst.MessageType.ERROR)
                    if msg is None:
                        raise RuntimeError('Camera pipeline EOS wait timed out')
                    if msg.type == Gst.MessageType.ERROR:
                        err, debug = msg.parse_error()
                        raise RuntimeError(f'{err}: {debug}')
                    detail['camera_eos'] = True
                except Exception as exc:
                    detail['camera_eos'] = False
                    detail['camera_eos_warning'] = str(exc)
                    log.warning('PIPELINE_EOS_FALLBACK pid=%s error=%s', os.getpid(), exc)
            log.info('PIPELINE_NULL pid=%s', os.getpid())
            started = time.monotonic()
            ret = pipeline.set_state(Gst.State.NULL)
            result, state, pending = pipeline.get_state(int(remaining(8.0) * Gst.SECOND))
            detail['camera_state_change'] = {
                'set_return': self._state_name(ret), 'wait_return': self._state_name(result),
                'current': self._state_name(state), 'pending': self._state_name(pending),
                'elapsed_seconds': round(time.monotonic() - started, 3),
            }
            log.info('PIPELINE_NULL_RESULT pid=%s state=%s', os.getpid(), detail['camera_state_change'])
            null = (ret != Gst.StateChangeReturn.FAILURE
                    and result == Gst.StateChangeReturn.SUCCESS and state == Gst.State.NULL)
            if not null:
                failed = ret == Gst.StateChangeReturn.FAILURE or result == Gst.StateChangeReturn.FAILURE
                reason = 'state change failed' if failed else 'did not reach NULL before deadline'
                errors.append(f'Camera release {reason}: {detail["camera_state_change"]}')
                bus_error = self._startup_bus_error(pipeline)
                if bus_error:
                    errors.append(f'Camera bus during release: {bus_error}')
                for name in ('camsrc', 'encoder', 'frame_sink', 'h264_sink', 'stream_udp', 'display'):
                    element = pipeline.get_by_name(name)
                    if element is not None:
                        ret, current, pending = element.get_state(0)
                        detail['camera_elements'][name] = {
                            'return': self._state_name(ret), 'current': self._state_name(current),
                            'pending': self._state_name(pending),
                        }
                log.error('PIPELINE_RELEASE_FAILED pid=%s elements=%s', os.getpid(), detail['camera_elements'])
            return null, detail, errors
        except Exception as exc:
            errors.append(f'Camera release failed: {exc}')
            return False, detail, errors

    def start(self) -> None:
        Gst.init(None)
        import cairo  # Registers the native context type used by cairooverlay.
        from overlay_renderer import OverlayRenderer
        fps = self._fps_text(self.cfg.framerate)
        pipeline = None
        try:
            # Build programmatically so qtiqmmfsrc.video_0 can be configured as
            # PREVIEW *before* the pad is linked.  This intentionally mirrors the
            # newer QIR camera builder already used by LIVE_FLOW_GRAPH_REVIEW.
            pipeline = Gst.Pipeline.new('visionedge_qti_camera')
            if pipeline is None:
                raise RuntimeError('cannot create GStreamer pipeline')
            self.pipeline = pipeline

            camsrc = self._make_gst_element('qtiqmmfsrc', 'camsrc')
            camcaps = self._make_gst_element('capsfilter', 'camcaps')
            tee = self._make_gst_element('tee', 'edge_tee')
            frame_q = self._make_gst_element('queue', 'frame_queue')
            frame_rate = self._make_gst_element('videorate', 'frame_rate')
            frame_caps = self._make_gst_element('capsfilter', 'frame_rate_caps')
            jpegenc = self._make_gst_element('jpegenc', 'jpeg_encoder')
            frame_sink = self._make_gst_element('appsink', 'frame_sink')
            h264_q = self._make_gst_element('queue', 'h264_queue')
            encoder = self._make_gst_element('v4l2h264enc', 'encoder')
            parser = self._make_gst_element('h264parse', 'h264_parser')
            h264_sink = self._make_gst_element('appsink', 'h264_sink')
            overlay_q = self._make_gst_element('queue', 'overlay_queue')
            convert_in = self._make_gst_element('videoconvert', 'overlay_convert_in')
            cairo_caps = self._make_gst_element('capsfilter', 'overlay_caps')
            cairo_element = self._make_gst_element('cairooverlay', 'apl_overlay')
            convert_out = self._make_gst_element('videoconvert', 'overlay_convert_out')
            output_caps = self._make_gst_element('capsfilter', 'output_caps')
            output_tee = self._make_gst_element('tee', 'output_tee')
            h264_tee = self._make_gst_element('tee', 'h264_tee')
            recorder_q = self._make_gst_element('queue', 'recorder_queue')
            display_q = self._make_gst_element('queue', 'display_queue')
            display_barrier = self._make_gst_element('identity', 'display_alloc_barrier')
            display_barrier.set_property('drop-allocation', True)
            display = None
            connected = False
            if self.cfg.display_mode != 'headless':
                try:
                    connected = Path('/sys/class/drm/card0-HDMI-A-1/status').read_text().strip() == 'connected'
                except OSError:
                    pass
            if connected:
                display = Gst.ElementFactory.make('waylandsink', 'display')
                if display is not None:
                    self._set_if_property(display, 'display', os.environ.get('WAYLAND_DISPLAY', 'wayland-1'))
                    self._set_if_property(display, 'fullscreen', True)
                    self._set_if_property(display, 'qos', False)
                    self._set_if_property(display, 'enable-last-sample', False)
                    self.display_actual = 'hdmi'
            if display is None:
                display = self._make_gst_element('fakesink', 'display')
                self.display_actual = 'headless'
            self._set_if_property(display, 'sync', False)
            self._set_if_property(display, 'async', False)
            self._set_if_property(display, 'enable-last-sample', False)
            cairo_caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=BGRx'))
            output_caps.set_property('caps', Gst.Caps.from_string('video/x-raw,format=NV12'))
            self._overlay = OverlayRenderer(self.cfg.width, self.cfg.height)
            self._overlay.attach(cairo_element)
            infer_rate = max(1, min(30, math.ceil(self.cfg.infer_fps)))
            frame_rate.set_property('drop-only', True)
            frame_rate.set_property('max-rate', infer_rate)
            frame_caps.set_property('caps', Gst.Caps.from_string(f'video/x-raw,framerate={infer_rate}/1'))
            for split in (output_tee, h264_tee):
                self._set_if_property(split, 'allow-not-linked', True)
            stream_elements = []
            if self.cfg.stream_enabled:
                stream_q = self._make_gst_element('queue', 'stream_queue')
                pay = self._make_gst_element('rtph264pay', 'rtppay')
                udp = self._make_gst_element('udpsink', 'stream_udp')
                pay.set_property('pt', 96)
                pay.set_property('config-interval', 1)
                udp.set_property('host', self.cfg.stream_rtp_host)
                udp.set_property('port', self.cfg.stream_rtp_port)
                udp.set_property('sync', False)
                udp.set_property('async', False)
                self._set_if_property(udp, 'enable-last-sample', False)
                stream_elements = [stream_q, pay, udp]

            self._set_if_property(camsrc, 'camera', int(self.cfg.camera))
            self._set_if_property(camsrc, 'ldc', bool(self.cfg.ldc))
            # Keep only the minimal safe control used by the QIR base path.
            self._set_if_property(camsrc, 'white-balance-mode', 0)
            preview_pad = self._request_preview_pad(camsrc)

            camcaps.set_property(
                'caps',
                Gst.Caps.from_string(
                    f'video/x-raw,format=NV12,width={self.cfg.width},height={self.cfg.height},framerate={fps}'
                ),
            )
            self._set_if_property(tee, 'allow-not-linked', True)

            queue_settings = [(frame_q, 2), (overlay_q, 3), (h264_q, 30),
                              (recorder_q, 30), (display_q, 3)]
            if stream_elements:
                queue_settings.append((stream_q, 30))
            for q, max_buffers in queue_settings:
                self._set_if_property(q, 'max-size-buffers', max_buffers)
                self._set_if_property(q, 'max-size-bytes', 0)
                self._set_if_property(q, 'max-size-time', 0)
                self._set_if_property(q, 'leaky', 2)  # downstream

            self._set_if_property(jpegenc, 'quality', int(self.cfg.preview_quality))
            for sink, max_buffers, drop in ((frame_sink, 1, True), (h264_sink, 16, True)):
                self._set_if_property(sink, 'emit-signals', True)
                self._set_if_property(sink, 'sync', False)
                self._set_if_property(sink, 'max-buffers', max_buffers)
                self._set_if_property(sink, 'drop', drop)
                self._set_if_property(sink, 'enable-last-sample', False)
                self._set_if_property(sink, 'wait-on-eos', False)

            self._set_if_property(encoder, 'capture-io-mode', 0)
            self._set_if_property(encoder, 'output-io-mode', 0)
            self._set_if_property(parser, 'config-interval', -1)

            controls = Gst.Structure.new_empty('controls')
            controls.set_value('h264_i_frame_period', 30)
            bitrate = int(self.cfg.recording_bitrate or 0)
            if bitrate <= 0:
                bitrate = 16_000_000 if int(self.cfg.width or 0) >= 3840 else 8_000_000
            controls.set_value('video_bitrate', bitrate)
            self._set_if_property(encoder, 'extra-controls', controls)

            elements = [camsrc, camcaps]
            transform = None
            postcaps = None
            if self.cfg.rotation == 180:
                transform = self._make_gst_element('qtivtransform', 'rotate180')
                postcaps = self._make_gst_element('capsfilter', 'post_rotate_caps')
                self._set_if_property(transform, 'rotate', 3)
                postcaps.set_property(
                    'caps',
                    Gst.Caps.from_string(
                        f'video/x-raw,format=NV12,width={self.cfg.width},height={self.cfg.height},framerate={fps}'
                    ),
                )
                elements.extend([transform, postcaps])
            elements.extend([tee, frame_q, frame_rate, frame_caps, jpegenc, frame_sink,
                             overlay_q, convert_in, cairo_caps, cairo_element, convert_out,
                             output_caps, output_tee, h264_q, encoder, parser, h264_tee,
                             recorder_q, h264_sink, display_q, display_barrier, display])
            elements.extend(stream_elements)
            for elem in elements:
                pipeline.add(elem)

            sink_pad = camcaps.get_static_pad('sink')
            if sink_pad is None:
                raise RuntimeError('camcaps sink pad unavailable')
            link_ret = preview_pad.link(sink_pad)
            if link_ret not in (Gst.PadLinkReturn.OK, Gst.PadLinkReturn.WAS_LINKED):
                raise RuntimeError(f'GStreamer pad link failed: camsrc.video_0 -> camcaps ({link_ret.value_nick})')

            if transform is not None:
                self._link_or_raise(camcaps, transform, 'camcaps -> qtivtransform')
                self._link_or_raise(transform, postcaps, 'qtivtransform -> post caps')
                self._link_or_raise(postcaps, tee, 'post caps -> tee')
            else:
                self._link_or_raise(camcaps, tee, 'camcaps -> tee')

            self._link_or_raise(tee, frame_q, 'tee -> frame queue')
            self._link_or_raise(frame_q, frame_rate, 'frame queue -> frame rate')
            self._link_or_raise(frame_rate, frame_caps, 'frame rate -> rate caps')
            self._link_or_raise(frame_caps, jpegenc, 'rate caps -> jpegenc')
            self._link_or_raise(jpegenc, frame_sink, 'jpegenc -> frame appsink')
            chain = [tee, overlay_q, convert_in, cairo_caps, cairo_element, convert_out, output_caps, output_tee]
            for left, right in zip(chain, chain[1:]):
                self._link_or_raise(left, right, f'{left.name} -> {right.name}')
            self._link_or_raise(output_tee, h264_q, 'output tee -> h264 queue')
            self._link_or_raise(h264_q, encoder, 'h264 queue -> encoder')
            self._link_or_raise(encoder, parser, 'encoder -> h264parse')
            self._link_or_raise(parser, h264_tee, 'h264parse -> h264 tee')
            self._link_or_raise(h264_tee, recorder_q, 'h264 tee -> recorder queue')
            self._link_or_raise(recorder_q, h264_sink, 'recorder queue -> h264 appsink')
            self._link_or_raise(output_tee, display_q, 'output tee -> display queue')
            self._link_or_raise(display_q, display_barrier, 'display queue -> allocation barrier')
            self._link_or_raise(display_barrier, display, 'allocation barrier -> display')
            if stream_elements:
                self._link_or_raise(h264_tee, stream_q, 'h264 tee -> stream queue')
                self._link_or_raise(stream_q, pay, 'stream queue -> RTP pay')
                self._link_or_raise(pay, udp, 'RTP pay -> UDP sink')

            self.pipeline = pipeline
            self.encoder = encoder
            self.frame_sink = frame_sink
            self.h264_sink = h264_sink
            self.frame_sink.connect('new-sample', self._frame_sample)
            self.h264_sink.connect('new-sample', self._h264_sample)
            self._bus_stop.clear()

            probes = {'camera_buffers': camcaps, 'jpeg_buffers': jpegenc,
                      'encoder_input_buffers': output_caps, 'h264_buffers': parser}
            if stream_elements:
                probes['rtp_buffers'] = pay
            diag.attach_gst(Gst, probes, queue_settings)
            diag.emit('PIPELINE_START', camera=self.cfg.camera, width=self.cfg.width,
                      height=self.cfg.height, framerate=fps, stream_enabled=self.cfg.stream_enabled,
                      rtp_host=self.cfg.stream_rtp_host, rtp_port=self.cfg.stream_rtp_port)
            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                detail = self._startup_bus_error(pipeline)
                raise RuntimeError('QTI camera pipeline failed to start' + (f': {detail}' if detail else ''))
            ret, state, pending = pipeline.get_state(5 * Gst.SECOND)
            if state != Gst.State.PLAYING:
                detail = self._startup_bus_error(pipeline)
                raise RuntimeError(
                    f'QTI camera did not reach PLAYING ({ret}, state={state}, pending={pending})'
                    + (f': {detail}' if detail else '')
                )

            self._bus_thread = threading.Thread(target=self._bus_loop, daemon=True, name='edge-qti-bus')
            self._bus_thread.start()
            diag.emit('PIPELINE_PLAYING', camera=self.cfg.camera)
        except Exception as exc:
            diag.emit('PIPELINE_START_FAILED', error=str(exc))
            result = self.stop()
            if not result.get('safe'):
                raise RuntimeError(f'{exc}; startup cleanup failed: {result.get("error")}') from exc
            raise

    def read(self, timeout: float = 1.0) -> Optional[np.ndarray]:
        if self._last_error.lower().startswith('gst-resource-error'):
            return None
        try:
            return self._frames.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return None

    def start_recording(self, path: Path) -> Tuple[bool, str]:
        with self._record_lock:
            if (self._sample_stopping.is_set() or self._record_stopping
                    or self._recorder_release and self._recorder_release.get('recorder_null') is False):
                return False, 'Camera or recorder release is pending'
            if self._recorder is not None:
                return False, 'recording already active'
            caps = None
            try:
                caps = self.h264_sink.get_static_pad('sink').get_current_caps() if self.h264_sink is not None else None
            except Exception:
                caps = None
            if caps is None:
                return False, 'H.264 encoder caps not ready yet; retry after live view is running'
            rec = GstH264Mp4Recorder(path, caps)
            self._recorder = rec
            self._recorder_release = None
            rec.start()
            self._record_path = str(path)
            # With a one-second GOP, the recorder begins on the next IDR even if
            # the vendor force-key-unit event is unavailable.
            return True, str(path)

    def stop_recording(self, timeout=8.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        if not self._record_stop_lock.acquire(timeout=max(0.0, timeout)):
            return {'success': False, 'recorder_null': False, 'error': 'Recorder stop is still in progress'}
        try:
            with self._record_lock:
                rec = self._recorder
                self._recorder = None
                self._record_path = ''
                self._record_stopping = True
                if rec is not None:
                    self._unreleased_recorder = rec
            if rec is None:
                return self._recorder_release or {'success': True, 'not_recording': True,
                                                 'recorder_null': None, 'recorder_finalized': None}
            result = rec.stop(timeout=max(0.0, deadline - time.monotonic()))
            self._recorder_release = result
            if result.get('recorder_null') is True:
                self._unreleased_recorder = None
            return result
        except Exception as exc:
            self._recorder_release = {'success': False, 'recorder_null': False,
                                      'recorder_finalized': False, 'error': str(exc)}
            return dict(self._recorder_release)
        finally:
            with self._record_lock:
                self._record_stopping = False
            self._record_stop_lock.release()

    def stop(self) -> Dict[str, Any]:
        with self._stop_lock:
            if self._release_result is not None:
                return dict(self._release_result)
            deadline = time.monotonic() + 20.0
            remaining = lambda cap: min(cap, max(0.0, deadline - time.monotonic()))
            errors = []
            drained = True
            null = self.pipeline is None
            self._sample_stopping.set()
            log.info('CALLBACK_DRAIN pid=%s', os.getpid())
            if self._overlay is not None:
                self._overlay.prevent_new_draws()
                try:
                    if not self._overlay.stop(timeout=remaining(3.0)):
                        raise RuntimeError('Cairo callback drain timeout')
                    self._overlay = None
                except Exception as exc:
                    drained = False
                    errors.append(str(exc))
            for label, lock in (('Camera sample', self._sample_lock), ('H264 sample', self._h264_lock)):
                if lock.acquire(timeout=remaining(3.0)):
                    lock.release()
                else:
                    drained = False
                    errors.append(f'{label} callback drain timeout')
            recorder = self._recorder_release or {}
            camera_detail = {}
            if drained:
                log.info('RECORDER_FINALIZE pid=%s', os.getpid())
                try:
                    recorder = self.stop_recording(timeout=remaining(8.0))
                    if not recorder.get('success'):
                        errors.append(recorder.get('error') or 'Recorder finalize failed')
                except Exception as exc:
                    recorder = {'recorder_null': False, 'recorder_finalized': False}
                    errors.append(str(exc))
                null, camera_detail, camera_errors = self._stop_camera_pipeline(deadline)
                errors.extend(camera_errors)
                if null:
                    self.pipeline = None
                    self.frame_sink = self.h264_sink = self.encoder = None
            self._bus_stop.set()
            if self._bus_thread and self._bus_thread.is_alive():
                self._bus_thread.join(timeout=remaining(1.0))
                if self._bus_thread.is_alive():
                    errors.append('Camera bus thread did not stop')
            safe = drained and null and recorder.get('recorder_null') is not False
            self._release_result = {**camera_detail, 'success': safe and not errors, 'safe': safe,
                                    'callbacks_drained': drained, 'camera_null': null,
                                    'recorder_null': recorder.get('recorder_null'),
                                    'recorder_finalized': recorder.get('recorder_finalized'),
                                    'error': '; '.join(errors), 'errors': errors}
            return dict(self._release_result)

    def set_overlay(self, state):
        if self._overlay is not None:
            self._overlay.update(state)

    def status(self) -> Dict[str, Any]:
        out = super().status()
        out.update({
            'camera': self.cfg.camera,
            'last_error': self._last_error,
            'recording': self._recorder is not None,
            'record_path': self._record_path,
            'display_actual': self.display_actual,
            'hardware_recording_mode': 'result',
            'stream_enabled': self.cfg.stream_enabled,
        })
        return out


class SoftwareRecorder:
    def __init__(self, cfg: EdgeConfig):
        self.cfg = cfg
        self.writer = None
        self.path: Optional[Path] = None
        self.frames = 0
        self.started_at = None
        self.mode = 'raw'
        self.size: Optional[Tuple[int, int]] = None
        self.fps: float = 0.0
        self.lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stop_result = None
        self.finalizing = False
        self._unreleased_writer = None

    def start(self, frame: np.ndarray, path: Path, mode: str, fps: Optional[float] = None) -> Tuple[bool, str]:
        with self.lock:
            if self.finalizing or self._unreleased_writer is not None:
                return False, 'Previous recording is still finalizing'
            if self.writer is not None:
                return False, 'recording already active'
            h, w = frame.shape[:2]
            path.parent.mkdir(parents=True, exist_ok=True)
            target_fps = float(fps or 0.0)
            if not (1.0 <= target_fps <= 120.0):
                target_fps = float(self.cfg.recording_fps)
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*'mp4v'),
                target_fps, (w, h),
            )
            if not writer.isOpened():
                writer.release()
                path = path.with_suffix('.avi')
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*'MJPG'),
                    target_fps, (w, h),
                )
            if not writer.isOpened():
                writer.release()
                return False, 'OpenCV VideoWriter cannot open mp4v or MJPG encoder'
            self.writer = writer
            self._stop_result = None
            self.path = path
            self.frames = 0
            self.started_at = time.time()
            self.mode = mode
            self.size = (w, h)
            self.fps = target_fps
            return True, str(path)

    def write(self, raw: np.ndarray, result: Optional[np.ndarray]) -> None:
        with self.lock:
            if self.writer is None:
                return
            frame = result if self.mode == 'result' and result is not None else raw
            try:
                if self.size is not None and (frame.shape[1], frame.shape[0]) != self.size:
                    frame = cv2.resize(frame, self.size, interpolation=cv2.INTER_AREA)
                self.writer.write(frame)
                self.frames += 1
            except Exception:
                pass

    def stop(self) -> Dict[str, Any]:
        if not self._stop_lock.acquire(timeout=8.0):
            return {'success': False, 'recorder_null': False, 'error': 'Software recorder finalization timed out'}
        self.finalizing = True
        try:
            if self._stop_result is None:
                self._stop_result = self._finish()
            return dict(self._stop_result)
        except Exception as exc:
            self._stop_result = {'success': False, 'recorder_null': False, 'error': str(exc)}
            return dict(self._stop_result)
        finally:
            self.finalizing = False
            self._stop_lock.release()

    def _finish(self) -> Dict[str, Any]:
        with self.lock:
            writer, path = self.writer, self.path
            frames = self.frames
            started = self.started_at
            self.writer = None
            self.path = None
            self.frames = 0
            self.started_at = None
            fps = self.fps
            self.size = None
            self.fps = 0.0
        if writer is None:
            return {'success': True, 'not_recording': True, 'recorder_null': None}
        self._unreleased_writer = writer
        writer.release()
        self._unreleased_writer = None
        return {
            'success': frames > 0,
            'error': '' if frames > 0 else 'Recording contains no frames',
            'recorder_null': True,
            'recorder_finalized': frames > 0,
            'path': str(path) if path else '',
            'frames': frames,
            'duration_sec': round(time.time() - started, 2) if started else 0,
            'fps': round(fps, 2),
        }

    @property
    def active(self) -> bool:
        return self.writer is not None


class EdgeRuntime:
    """Single-camera orchestrator. `base` is the imported server.py module."""

    def __init__(self, base_module, config_path: Path = DEFAULT_CONFIG_PATH):
        _ensure_dirs()
        self.base = base_module
        self.config_path = Path(config_path)
        self.cfg = EdgeConfig.load(self.config_path)
        self.lock = threading.RLock()
        self.frame_cond = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.thread = None
        self.backend: Optional[CameraBackend] = None
        self._pending_backend = None
        self._camera_generation = 0
        self._pending_config = None
        self._configuring = False
        self._record_result = None
        self._record_finalizing = False
        self._record_command_lock = threading.Lock()
        self._station_fault_thread = None
        self._probe_process = None
        self._stop_lock = threading.Lock()
        self._stopping = False
        self._release_failed = False
        self._cleanup_result = None
        self.soft_rec = SoftwareRecorder(self.cfg)
        self.cache_mgr = None
        self.engine = None
        self.product = None
        self.latest_raw: Optional[np.ndarray] = None
        self.latest_result: Optional[np.ndarray] = None
        self.latest_raw_jpeg: bytes = b''
        self.latest_result_jpeg: bytes = b''
        self.frame_seq = 0
        self.infer_seq = 0
        self.started_at = None
        self.last_frame_at = None
        self.last_infer_at = None
        self.last_error = ''
        self.last_warning = ''
        self.status_text = 'STOPPED'
        self.verdict = 'WAIT'
        self.frame_pass = False
        self.results = []
        self.rules = []
        self.sop = None
        self.actual_backend = ''
        self._fps_frames = 0
        self._fps_mark = time.monotonic()
        self.source_fps = 0.0
        self.infer_actual_fps = 0.0
        self._infer_frames = 0
        self._infer_mark = time.monotonic()
        self._last_storage_check = 0.0
        # Media catalogue revision. Bumped whenever a photo/video is created or
        # deleted so the browser can refresh only when the filesystem changes.
        self.media_version = 0
        self.station = None
        self.video_trace = None
        self._video_trace_cursor = 0
        self._video_cycle_id = ''

    def _bump_media_version(self) -> int:
        with self.lock:
            self.media_version += 1
            return self.media_version

    def set_video_trace(self, trace) -> None:
        """Install an offline 10 FPS decision trace while the runtime is stopped."""
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise RuntimeError('Stop the runtime before changing the video decision trace')
            self.video_trace = list(trace) if trace is not None else None
            self._video_trace_cursor = 0
            self._video_cycle_id = ''

    def _sync_video_cycle(self, backend) -> None:
        if self.video_trace is None or self.station is None or not isinstance(backend, OpenCVCameraBackend):
            return
        status = self.station.status()
        cycle_id = status.get('cycle_id') if status.get('state') == 'RUNNING' else ''
        if cycle_id and cycle_id != self._video_cycle_id:
            if not backend.rewind():
                raise RuntimeError('Video could not restart from the first frame for this inspection')
            self._video_trace_cursor = 0
            self._video_cycle_id = cycle_id

    def _video_matches_due(self, backend):
        if self.video_trace is None or self.station is None or not self._video_cycle_id:
            return []
        state = self.station.status().get('state')
        if state not in {'RUNNING', 'NG'}:
            return []
        position = max(0.0, float(getattr(backend, 'playback_seconds', 0.0)))
        due = []
        while self._video_trace_cursor < len(self.video_trace):
            sample = self.video_trace[self._video_trace_cursor]
            if float(sample['time_seconds']) > position + 1e-6:
                break
            due.append(sample['match'])
            self._video_trace_cursor += 1
        return due

    def _method(self):
        return getattr(self.base, 'METHOD_MAP_PC', {}).get(
            self.cfg.method, cv2.TM_CCOEFF_NORMED
        )

    def _prepare_inference(self) -> None:
        self._release_inference()
        if int(self.cfg.product_id or 0) <= 0:
            # Camera-only mode is a normal operating mode, not a warning.
            self.last_warning = ''
            return
        product = self.base._load_product_dict(int(self.cfg.product_id))
        if not product:
            self.last_warning = f'product_id={self.cfg.product_id} not found; preview continues'
            return
        mgr = self.base.vc.CacheManager(self.base.DB_PATH, int(self.cfg.product_id), reload_interval=5.0)
        if not mgr.initial_load():
            self.last_warning = 'No valid template/SOP for selected product; preview continues'
            return
        mgr.start_watcher()
        steps = self.base.vc.load_runtime_inspection_items(self.base.DB_PATH, int(self.cfg.product_id))
        sop_cfg = self.base.vc.load_product_sop_config(self.base.DB_PATH, int(self.cfg.product_id))
        self.cache_mgr = mgr
        self.engine = self.base.vc.SopFlowEngine(steps, sop_cfg) if sop_cfg.get('enabled') and steps else None
        self.product = product
        self.sop = self.engine.summary() if self.engine else None
        self.last_warning = ''

    def _release_inference(self) -> None:
        if self.cache_mgr is not None:
            try:
                self.cache_mgr.stop_watcher()
            except Exception:
                pass
        self.cache_mgr = None
        self.engine = None
        self.product = None
        self.sop = None
        self.results = []
        self.rules = []
        self.frame_pass = False

    def _probe_qti(self) -> bool:
        if not GST_AVAILABLE:
            return False
        proc = subprocess.Popen([sys.executable, '-B', str(BASE_DIR / 'camera_worker.py'), '--probe-qti'],
                                start_new_session=(os.name == 'posix'))
        self._probe_process = proc
        deadline = time.monotonic() + 10.0
        try:
            while proc.poll() is None:
                if self.stop_event.wait(0.1):
                    raise RuntimeError('Camera startup cancelled during plugin discovery')
                if time.monotonic() >= deadline:
                    raise RuntimeError('QTI plugin discovery timed out')
            if proc.returncode not in (0, 1):
                raise RuntimeError(f'QTI plugin discovery failed ({proc.returncode})')
            return proc.returncode == 0
        finally:
            if proc.poll() is None:
                if os.name == 'posix':
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            proc.wait(timeout=2.0)
            self._probe_process = None

    def _make_backend(self) -> CameraBackend:
        requested = self.cfg.backend.lower()
        if self.stop_event.is_set():
            raise RuntimeError('Camera startup cancelled')
        qti_available = requested == 'qti' or (requested == 'auto' and self._probe_qti())

        # On Qualcomm camera images, the presence of qtiqmmfsrc means QTI is the
        # authoritative camera path.  Falling back to OpenCV after QTI *starts*
        # but cannot acquire the camera is misleading: these devices normally do
        # not expose the sensor as /dev/video0, and the fallback only produces a
        # flood of V4L2 warnings while hiding the real cause (most commonly that
        # another app already owns camera 0).  Auto therefore falls back to
        # OpenCV only when the QTI plugin is not present at all (PC/dev hosts).
        if requested in ('auto', 'qti') and qti_available:
            try:
                # Keep Qualcomm GStreamer and its GLib loop in a child process.
                # A wedged native teardown must never be followed by a second
                # camera owner in this web process.
                from camera_worker import WorkerCameraBackend
                b = WorkerCameraBackend(self.cfg)
                with self.lock:
                    self._pending_backend = b
                    if self.stop_event.is_set():
                        b.request_stop()
                b.start()
                self.actual_backend = 'qti'
                return b
            except Exception as exc:
                self.actual_backend = 'qti'
                hint = f'QTI camera failed to start: {exc}'
                self.last_warning = ''
                raise RuntimeError(hint) from exc

        if requested == 'qti' and not qti_available:
            raise RuntimeError('QTI backend requested but qtiqmmfsrc is unavailable')

        b = OpenCVCameraBackend(self.cfg)
        with self.lock:
            self._pending_backend = b
        if self.stop_event.is_set():
            raise RuntimeError('Camera startup cancelled')
        b.start()
        self.actual_backend = 'opencv'
        return b

    def start(self) -> Dict[str, Any]:
        with self.lock:
            if self._station_fault_thread and self._station_fault_thread.is_alive():
                return {'success': False, 'error': 'Station interruption update is still pending'}
            reopen = bool((self._cleanup_result or {}).get('reopen_eligible'))
            if self._stopping or (self._release_failed and not reopen) or self._configuring or self._record_finalizing:
                return {'success': False, 'error': self.last_error or 'Camera release is pending'}
            if self.thread and self.thread.is_alive():
                return {'success': True, 'already_running': True, 'status': self.status()}
            probe = self._auto_probe
            if probe is not None and probe['thread'].is_alive():
                return {'success': False, 'error': 'Previous automatic inspection is still finishing'}
            self.stop_event.clear()
            self._release_failed = False
            self._camera_generation += 1
            if self._pending_config is not None:
                self.cfg = self._pending_config
                self.soft_rec.cfg = self.cfg
                self._pending_config = None
            self._cleanup_result = None
            self.status_text = 'STARTING'
            self.last_error = ''
            self.last_warning = ''
            if reopen:
                self.last_warning = 'Previous release failed; camera recovery may take about 40 seconds'
            # Do not present stale frames/FPS from the previous camera session as live.
            self.latest_raw = None
            self.latest_result = None
            self.latest_raw_jpeg = b''
            self.latest_result_jpeg = b''
            self.source_fps = 0.0
            self.infer_actual_fps = 0.0
            self._fps_frames = 0
            self._infer_frames = 0
            self._fps_mark = time.monotonic()
            self._infer_mark = time.monotonic()
            self.last_frame_at = None
            self.last_infer_at = None
            self.verdict = 'WAIT'
            self._video_trace_cursor = 0
            self._video_cycle_id = ''
            self.started_at = time.time()
            self.thread = threading.Thread(target=self._run, daemon=True, name='edge-camera-runtime')
            self.thread.start()
        return {'success': True}

    def stop(self) -> Dict[str, Any]:
        with self._stop_lock:
            deadline = time.monotonic() + 32.0
            with self.lock:
                self._stopping = True
                self.stop_event.set()
                t = self.thread
                pending = self._pending_backend or self.backend
                self.status_text = 'STOPPING'
            supervision_error = ''
            if pending is not None and hasattr(pending, 'request_stop'):
                try:
                    pending.request_stop()
                    # Supervise QTI even if the runtime is blocked in matching.
                    pending.stop()
                except Exception as exc:
                    supervision_error = f'Worker supervision failed: {exc}'
            if t and t.is_alive():
                t.join(timeout=max(0.0, deadline - time.monotonic()))
            with self.lock:
                if t and t.is_alive():
                    self.last_error = 'Runtime thread did not stop within timeout'
                    self._release_failed = True
                    self.status_text = 'RELEASE_FAILED'
                    return {'success': False, 'safe': False, 'error': self.last_error}
                self.thread = None
                self._stopping = False
                result = self._cleanup_result or {'success': not self._release_failed,
                                                  'safe': not self._release_failed, 'error': self.last_error}
                if supervision_error:
                    self._release_failed = True
                    self.last_error = supervision_error
                    result = {**result, 'success': False, 'safe': False, 'error': supervision_error}
                    self._cleanup_result = result
                self.status_text = 'RELEASE_FAILED' if not result.get('safe', True) else 'STOPPED'
                self.source_fps = self.infer_actual_fps = 0.0
                self.latest_raw = self.latest_result = None
                self.latest_raw_jpeg = self.latest_result_jpeg = b''
                self.last_frame_at = None
                return dict(result)

    def restart(self) -> Dict[str, Any]:
        result = self.stop()
        if not result.get('success'):
            return result
        return self.start()

    def update_config(self, data: Dict[str, Any], restart: bool = False) -> Dict[str, Any]:
        with self.lock:
            if self._configuring or self._stopping or self._release_failed:
                return {'success': False, 'error': 'Camera transition is in progress'}
            was_running = bool(self.thread and self.thread.is_alive())
            candidate = copy.deepcopy(self._pending_config or self.cfg)
            candidate.update(data)
            self._configuring = True
        try:
            if restart and was_running:
                result = self.stop()
                if not result.get('success'):
                    return {**result, 'config': self.cfg.public(), 'restart_required': True}
            candidate.save(self.config_path)
            with self.lock:
                if was_running and not restart:
                    self._pending_config = candidate
                else:
                    self.cfg = candidate
                    self.soft_rec.cfg = candidate
                    self._pending_config = None
            result = {'success': True, 'config': candidate.public(),
                      'effective_config': self.cfg.public(), 'restart_required': bool(was_running and not restart)}
        finally:
            with self.lock:
                self._configuring = False
        if restart and was_running:
            return self.start()
        return result

    def _rotate_if_needed(self, frame: np.ndarray) -> np.ndarray:
        # QTI applies rotation in hardware. OpenCV fallback mirrors the same setting.
        if self.actual_backend == 'opencv' and self.cfg.rotation == 180:
            return cv2.rotate(frame, cv2.ROTATE_180)
        return frame

    def _process_inference(self, frame: np.ndarray) -> Tuple[np.ndarray, bool, list, list, Any]:
        cache = self.cache_mgr.get() if self.cache_mgr is not None else None
        if cache is None:
            return frame, False, [], [], self.sop
        prepared = self.base._prepare_frame(frame, self.product or {})
        frame_pass, results, vis = self.base.vc.run_inference(
            prepared, cache, method=self._method(), draw_vis=True,
        )
        rules = getattr(cache, 'last_rule_results', []) or []
        sop = self.engine.update(rules) if self.engine else None
        if vis is None:
            vis = prepared.copy()
        return vis, bool(frame_pass), results, rules, sop

    def _storage_ok(self) -> bool:
        if self.cfg.min_free_mb <= 0:
            return True
        try:
            free = shutil.disk_usage(EDGE_RUNTIME_DIR).free // (1024 * 1024)
            return free >= self.cfg.min_free_mb
        except Exception:
            return False

    def _run(self) -> None:
        backend = None
        try:
            self._prepare_inference()
            backend = self._make_backend()
            with self.lock:
                self.backend = backend
                self._pending_backend = None
                if self.stop_event.is_set():
                    return
                self.status_text = 'LIVE'
            next_infer = 0.0
            replay_count = 0
            while not self.stop_event.is_set():
                diagnostic_loop_started = time.perf_counter()
                station_state = None
                self._sync_video_cycle(backend)
                frame = backend.read(timeout=1.0)
                if frame is None:
                    if self.stop_event.is_set():
                        break
                    recent = self.last_frame_at and time.time() - self.last_frame_at <= 3.0
                    starting = self.last_frame_at is None and time.time() - self.started_at <= 3.0
                    if self.actual_backend == 'qti' and (recent or starting):
                        continue
                    self.last_error = 'camera frame timeout / EOF'
                    diag.emit('CAMERA_READ_TIMEOUT', backend=self.actual_backend, last_frame_at=self.last_frame_at)
                    self.status_text = 'RECONNECTING'
                    if self.station is not None:
                        self.station.camera_fault(self.last_error)
                    if self.engine:
                        try:
                            self.engine.interrupt(reason='SOURCE_INTERRUPTED')
                        except Exception:
                            pass
                    # File/USB OpenCV EOF may recover by reopen; QTI errors also get a clean restart.
                    released = backend.stop()
                    if isinstance(released, dict) and not released.get('safe', released.get('success', True)):
                        raise RuntimeError(released.get('error') or 'Camera release failed during reconnect')
                    if self.stop_event.wait(self.cfg.reconnect_sec):
                        break
                    backend = self._make_backend()
                    with self.lock:
                        self.backend = backend
                        self._pending_backend = None
                        if self.stop_event.is_set():
                            break
                        self.status_text = 'LIVE'
                        self.last_error = ''
                    continue

                if self.actual_backend == 'opencv' and backend.replay_count != replay_count:
                    replay_count = backend.replay_count
                    if self.station is not None and self.video_trace is None:
                        self.station.camera_fault('Video restarted before this inspection completed; reset and scan again')
                frame = self._rotate_if_needed(frame)
                now = time.monotonic()
                self._fps_frames += 1
                if now - self._fps_mark >= 2.0:
                    self.source_fps = self._fps_frames / max(0.001, now - self._fps_mark)
                    self._fps_frames = 0
                    self._fps_mark = now

                # Raw preview is updated for every received frame.
                raw_jpg = _jpeg(frame, self.cfg.preview_quality)
                result_frame = self.latest_result
                did_infer = False
                video_matches = self._video_matches_due(backend)
                inference_due = now >= next_infer
                if (self.station is not None or self.cache_mgr is not None) and (inference_due or video_matches):
                    next_infer = now + (1.0 / max(0.1, self.cfg.infer_fps))
                    if self.station is not None:
                        if self.video_trace is not None:
                            result_frame, frame_pass, results, rules, sop = self.station.process_matches(frame, video_matches)
                        else:
                            result_frame, frame_pass, results, rules, sop = self.station.process_frame(frame)
                        station_state = self.station.status()
                        # The hardware overlay reads this snapshot directly, so the
                        # probe's live scores have to be injected here too. status()
                        # already returned a deep copy; self.matching is untouched.
                        if (self._auto['last_match'] is not None
                                and station_state.get('can_capture') is True):
                            station_state['matching'] = self._auto['last_match']
                            station_state['auto_capture'] = self._auto_overlay()
                        backend.set_overlay(station_state)
                        phase = station_state.get('state')
                        verdict = (
                            'COMPLETE' if phase == 'RESULT_CONFIRM'
                            else 'ALARM' if phase in ('NG', 'RECOVERY_REQUIRED')
                            else phase or 'RUNNING'
                        )
                    else:
                        result_frame, frame_pass, results, rules, sop = self._process_inference(frame)
                        verdict = (
                            'COMPLETE' if sop and sop.get('complete')
                            else 'ALARM' if sop and (sop.get('alarm') or {}).get('active')
                            else 'PASS' if frame_pass else 'RUNNING'
                        )
                    result_jpg = _jpeg(result_frame, self.cfg.preview_quality)
                    self._infer_frames += max(1, len(video_matches))
                    if now - self._infer_mark >= 2.0:
                        self.infer_actual_fps = self._infer_frames / max(0.001, now - self._infer_mark)
                        self._infer_frames = 0
                        self._infer_mark = now
                    with self.lock:
                        self.latest_result = result_frame
                        self.latest_result_jpeg = result_jpg
                        self.frame_pass = frame_pass
                        self.results = results
                        self.rules = rules
                        self.sop = sop
                        self.verdict = verdict
                        self.infer_seq += 1
                        self.last_infer_at = time.time()
                    did_infer = True
                elif self.station is None and self.cache_mgr is None:
                    result_frame = frame
                    with self.lock:
                        self.latest_result = frame
                        self.latest_result_jpeg = raw_jpg
                        self.verdict = 'PREVIEW'

                self.soft_rec.write(frame, result_frame)
                backend_status = backend.status()
                if (self.soft_rec.active or backend_status.get('recording')) and now - self._last_storage_check >= 2.0:
                    self._last_storage_check = now
                    if not self._storage_ok():
                        self.stop_recording()
                        self.last_warning = 'Recording auto-stopped: low/unavailable storage'

                with self.frame_cond:
                    if self.stop_event.is_set():
                        break
                    self.latest_raw = frame
                    self.latest_raw_jpeg = raw_jpg
                    self.frame_seq += 1
                    diag.count('app_frames')
                    self.last_frame_at = time.time()
                    self.status_text = 'LIVE'
                    self.last_error = ''
                    self.frame_cond.notify_all()

                # Last in the body on purpose: the result publish above must not
                # overwrite a commit, and frame_seq is now the published frame.
                if self.station is not None:
                    self._auto_capture_tick(frame, self.station.status(), backend_status,
                                            time.monotonic())
                diag.observe('FRAME_LOOP', (time.perf_counter() - diagnostic_loop_started) * 1000)
        except Exception as exc:
            diag.emit('CAMERA_RUN_ERROR', error=str(exc))
            with self.lock:
                self.status_text = 'ERROR'
                self.last_error = str(exc)
            # Hardware cleanup must precede station storage work.
        finally:
            backend = self._pending_backend or backend
            had_recording = self.soft_rec.active
            if backend is not None:
                try:
                    had_recording = had_recording or bool(backend.status().get('recording'))
                except Exception:
                    pass
            errors = []
            release = {'safe': True, 'success': True}
            software_release = {}
            try:
                software_release = self.soft_rec.stop()
                if not software_release.get('success'):
                    errors.append(software_release.get('error') or 'Software recorder finalization failed')
            except Exception as exc:
                errors.append(f'Software recorder stop failed: {exc}')
            if backend is not None:
                try:
                    result = backend.stop()
                    if isinstance(result, dict):
                        release = result
                        if not result.get('success', True):
                            errors.append(result.get('error') or 'Camera cleanup failed')
                except Exception as exc:
                    release = {'safe': False, 'success': False}
                    errors.append(str(exc))
            auto_thread = self._auto_thread
            if auto_thread is not None and auto_thread.is_alive():
                auto_thread.join(timeout=10.0)
                if auto_thread.is_alive():
                    errors.append('Automatic capture did not finish')
            probe = self._auto_probe
            if probe is not None:
                probe['cancelled'] = True
                probe['thread'].join(timeout=2.0)
                if probe['thread'].is_alive():
                    errors.append('Automatic probe did not finish')
                else:
                    self._auto_probe = None
            if self._probe_process is not None and self._probe_process.poll() is None:
                errors.append('QTI discovery process did not exit')
                release = {**release, 'safe': False, 'success': False,
                           'probe_reaped': False, 'probe_pid': self._probe_process.pid}
                try:
                    from camera_worker import _persist_recovery
                    _persist_recovery(errors[-1], f'probe-{self._probe_process.pid}',
                                      self._probe_process.pid, self.cfg.camera, release)
                except OSError as exc:
                    errors.append(f'Recovery marker write failed: {exc}')
            recovery_was_eligible = release.get('reopen_eligible', False)
            if not software_release.get('success', False) or software_release.get('recorder_null') is False:
                release = {**release, 'safe': False, 'software_recorder_null': False, 'reopen_eligible': False}
            if (self._probe_process is not None or (auto_thread is not None and auto_thread.is_alive())
                    or (probe is not None and probe['thread'].is_alive())):
                release = {**release, 'reopen_eligible': False}
            if recovery_was_eligible and not release.get('reopen_eligible'):
                try:
                    from camera_worker import _persist_recovery
                    _persist_recovery('Runtime cleanup incomplete; reacquisition blocked',
                                      release.get('session', 'runtime'), os.getpid(), self.cfg.camera, release)
                except OSError as exc:
                    errors.append(f'Recovery marker write failed: {exc}')
            if had_recording:
                self._bump_media_version()
            self._release_inference()
            with self.lock:
                self._release_failed = not release.get('safe', release.get('success', True))
                self._cleanup_result = {**release, 'success': not errors and release.get('success', True),
                                        'error': '; '.join(errors)}
                if errors:
                    self.last_error = '; '.join(filter(None, [self.last_error, *errors]))
                if self._release_failed:
                    self.status_text = 'RELEASE_FAILED'
                else:
                    self.backend = self._pending_backend = None
                if self.status_text not in ('ERROR', 'RELEASE_FAILED'):
                    self.status_text = 'STOPPED'
            if self.station is not None and self.status_text == 'ERROR' and not self.stop_event.is_set():
                target = self.station
                def notify_fault():
                    try:
                        target.camera_fault('Camera runtime stopped after source error')
                    except Exception:
                        log.exception('Station interruption update failed after camera cleanup')
                self._station_fault_thread = threading.Thread(target=notify_fault, daemon=True,
                                                             name='camera-fault-notify')
                self._station_fault_thread.start()

    def _storage_status(self) -> Dict[str, Any]:
        try:
            usage = shutil.disk_usage(EDGE_RUNTIME_DIR)
            free_mb = usage.free // (1024 * 1024)
            total_mb = usage.total // (1024 * 1024)
            reserve_mb = int(self.cfg.min_free_mb)
            return {
                'storage_free_mb': int(free_mb),
                'storage_total_mb': int(total_mb),
                'storage_reserve_mb': reserve_mb,
                'storage_ok': bool(reserve_mb <= 0 or free_mb >= reserve_mb),
            }
        except Exception:
            return {
                'storage_free_mb': None,
                'storage_total_mb': None,
                'storage_reserve_mb': int(self.cfg.min_free_mb),
                'storage_ok': False,
            }

    def status(self) -> Dict[str, Any]:
        storage = self._storage_status()
        with self.lock:
            running = bool(self.thread and self.thread.is_alive())
            observed_backend = self._pending_backend or self.backend
            backend_status = observed_backend.status() if observed_backend is not None else {}
            recording = self.soft_rec.active or bool(backend_status.get('recording'))
            return {
                'running': running,
                'status': self.status_text,
                'error': self.last_error,
                'warning': self.last_warning,
                'backend_requested': self.cfg.backend,
                'backend': self.actual_backend or backend_status.get('backend', ''),
                'backend_status': backend_status,
                'release': self._cleanup_result,
                'reopen_available': bool(not running and not self._stopping
                                         and (self._cleanup_result or {}).get('reopen_eligible')),
                'frame_seq': self.frame_seq,
                'infer_seq': self.infer_seq,
                'source_fps': round(self.source_fps, 2),
                'infer_fps': round(self.infer_actual_fps, 2),
                'last_frame_at': self.last_frame_at,
                'last_infer_at': self.last_infer_at,
                'uptime_sec': round(time.time() - self.started_at, 1) if running and self.started_at else 0,
                'product': self.product,
                'product_id': self.cfg.product_id,
                'verdict': self.verdict,
                'frame_pass': self.frame_pass,
                'results': self.results,
                'rules': self.rules,
                'sop': self.sop,
                'recording': recording,
                'recording_state': 'FINALIZING' if self._record_finalizing else 'RECORDING' if recording else 'FAILED' if self._record_result and not self._record_result.get('success') else 'IDLE',
                'recording_result': self._record_result,
                'record_path': (str(self.soft_rec.path) if self.soft_rec.active and self.soft_rec.path else str(backend_status.get('record_path') or '')),
                'media_version': self.media_version,
                'has_raw': bool(self.latest_raw_jpeg),
                'has_result': bool(self.latest_result_jpeg),
                'auto_capture': self._auto_capture_status(),
                **storage,
            }

    def _auto_capture_status(self) -> Dict[str, Any]:
        """Live probe telemetry for the dashboard. Deliberately on the runtime
        status rather than the station snapshot, so the station payload and the
        matching it reports as evidence stay exactly as they were."""
        station = self.station
        cfg = self._auto_capture_settings(station)
        threshold = None
        config = getattr(station, 'config', None)
        if isinstance(config, dict):
            try:
                threshold = float(config.get('threshold', 0.8))
            except (TypeError, ValueError):
                threshold = None
        return {
            'enabled': bool(cfg.get('enabled')),
            'armed': bool(self._auto['armed']),
            'reason': self._auto['reason'],
            'fps': float(cfg.get('fps', 5.0)),
            'required_hits': int(cfg.get('required_hits', 2)),
            'timeout_sec': float(cfg.get('timeout_sec', 0.0)),
            'timeout_action': str(cfg.get('timeout_action', 'not_matched')),
            'threshold': threshold,
            'hits': int(self._auto['hits']),
            'score': self._auto['score'],
            'best_score': self._auto['best_score'],
            'elapsed': round(float(self._auto['elapsed'] or 0.0), 2),
            'suspended': self._auto['suspended_cycle'] is not None,
            'committing': bool(self._auto['committing']),
            'probe_running': self._auto_probe_running(),
            'last_commit': self._auto['last_commit'],
            'error': self._auto['error'],
        }

    def jpeg(self, mode: str = 'result') -> bytes:
        with self.lock:
            if str(mode).lower() == 'raw':
                return bytes(self.latest_raw_jpeg)
            return bytes(self.latest_result_jpeg or self.latest_raw_jpeg)

    def wait_jpeg(self, after_seq: int, mode: str = 'result', timeout: float = 2.0) -> Tuple[int, bytes]:
        with self.frame_cond:
            if self.frame_seq <= after_seq:
                self.frame_cond.wait(timeout=timeout)
            seq = self.frame_seq
            data = self.latest_raw_jpeg if str(mode).lower() == 'raw' else (self.latest_result_jpeg or self.latest_raw_jpeg)
            return seq, bytes(data)

    def snapshot(self, mode: str = 'result') -> Dict[str, Any]:
        mode = str(mode or 'result').lower()
        with self.lock:
            if self._stopping or self._release_failed:
                return {'success': False, 'error': 'Camera release is pending'}
            if mode == 'raw':
                frame = self.latest_raw.copy() if self.latest_raw is not None else None
            else:
                src = self.latest_result if self.latest_result is not None else self.latest_raw
                frame = src.copy() if src is not None else None
        if frame is None:
            return {'success': False, 'error': 'no frame available'}
        data = _jpeg(frame, self.cfg.snapshot_quality)
        name = f'{mode}_{_timestamp()}.jpg'
        path = EDGE_PHOTO_DIR / name
        path.write_bytes(data)
        version = self._bump_media_version()
        return {'success': True, 'path': f'photos/{name}', 'size': path.stat().st_size, 'media_version': version}

    def video_control(self, action: str, seconds=None) -> Dict[str, Any]:
        with self.lock:
            backend = self.backend
        if not isinstance(backend, OpenCVCameraBackend) or not backend.status().get('is_file'):
            return {'success': False, 'error': 'No test video is active'}
        action = str(action or '').lower()
        try:
            if action == 'pause':
                if not backend.set_paused(True):
                    raise RuntimeError('Video could not be paused')
            elif action == 'play':
                if not backend.set_paused(False):
                    raise RuntimeError('Video could not be resumed')
            elif action == 'seek':
                backend.seek(float(seconds))
            else:
                raise ValueError('Unsupported video action')
            return {'success': True, 'video': backend.status()}
        except (TypeError, ValueError, RuntimeError) as exc:
            return {'success': False, 'error': str(exc)}

    # Immutable default, so an instance built without __init__ reads None safely.
    _auto_thread = None
    _auto_probe = None

    @property
    def _auto(self) -> Dict[str, Any]:
        """Probe state, created on first touch. The camera runtime is also built
        with __new__ in the unit tests, which never runs __init__."""
        state = self.__dict__.get('_auto_state')
        if state is None:
            state = self.__dict__['_auto_state'] = _new_auto_state()
        return state

    def _auto_capture_settings(self, station=None) -> Dict[str, Any]:
        station = self.station if station is None else station
        settings = getattr(station, 'auto_capture', None)
        return dict(settings) if isinstance(settings, dict) else dict(AUTO_CAPTURE_DEFAULTS)

    def _auto_capture_blocked(self, station, station_state, backend_status, cfg, cycle_id) -> str:
        """The reason the probe must stay disarmed, cheapest check first, or ''."""
        if not cfg.get('enabled'):
            return 'disabled'
        if station is None:
            return 'no_station'
        if station_state.get('inspection_mode') != 'single_snapshot_cover_card':
            return 'not_snapshot_mode'
        if self.video_trace is not None:
            return 'video_trace'
        if (backend_status or {}).get('is_file'):
            return 'file_source'
        if station_state.get('test_mode') is True:
            return 'test_mode'
        if station_state.get('storage_error'):
            return 'storage_error'
        if self.stop_event.is_set() or self._stopping or self._release_failed:
            return 'stopping'
        if station_state.get('can_capture') is not True:
            return 'state:' + str(station_state.get('state') or '')
        if self._auto['suspended_cycle'] == cycle_id:
            return 'timeout_stopped'
        # process_frame builds the matcher lazily before its snapshot-mode
        # short-circuit, so this only skips the very first inference slot.
        if station.matcher is None:
            return 'matcher_not_ready'
        thread = self._auto_thread
        if thread is not None and thread.is_alive():
            return 'committing'
        return ''

    def _auto_overlay(self) -> Dict[str, Any]:
        """Just what the overlay countdown needs, so neither draw path has to reach
        into the full status block."""
        cfg = self._auto_capture_settings()
        config = getattr(self.station, 'config', None)
        threshold = config.get('threshold', 0.8) if isinstance(config, dict) else None
        return {'armed': True, 'elapsed': float(self._auto['elapsed'] or 0.0),
                'timeout_sec': float(cfg.get('timeout_sec', 0.0)),
                'timeout_action': str(cfg.get('timeout_action', 'not_matched')),
                'hits': int(self._auto['hits']),
                'required_hits': int(cfg.get('required_hits', 2)),
                'score': self._auto['score'], 'threshold': threshold}

    def _auto_disarm(self, reason: str, station=None) -> None:
        if self._auto['armed'] or self._auto['reason'] != reason:
            diag.emit('AUTO_DISARM', cycle_id=self._auto['cycle'], reason=reason,
                      elapsed_seconds=self._auto['elapsed'])
        self._auto.update(armed=False, reason=reason, hits=0)
        if self._auto['last_match'] is not None:
            self._auto['last_match'] = None
            self._clear_preview_matching(station)

    @staticmethod
    def _clear_preview_matching(station) -> None:
        if station is None:
            return
        try:
            station.set_preview_matching(None)
        except Exception:
            log.debug('Could not clear the overlay preview', exc_info=True)

    def _auto_probe_running(self) -> bool:
        probe = self._auto_probe
        return bool(probe and not probe['done'].is_set())

    def _auto_capture_tick(self, frame, station_state, backend_status, now) -> None:
        """Poll one background match; never queue frames behind slow matching."""
        station = self.station
        try:
            cfg = self._auto_capture_settings(station)
            cycle_id = str(station_state.get('cycle_id') or '')
            if cycle_id != self._auto['cycle']:
                self._auto.update(cycle=cycle_id, suspended_cycle=None, last_commit=None)
            # A manual capture bumps attempt_count, which restarts the window too.
            key = (self._camera_generation, cycle_id, station_state.get('attempt_count', 0))
            if key != self._auto['key']:
                self._auto.update(key=key, hits=0, score=None, best_score=None,
                                  window_started=None, elapsed=0.0, next_probe=0.0, error='')
            blocked = self._auto_capture_blocked(station, station_state, backend_status,
                                                 cfg, cycle_id)
            probe = self._auto_probe
            stale = bool(probe and (probe['cancelled'] or probe['key'] != key
                                   or probe['station'] is not station or probe['matcher'] is not station.matcher))
            if probe is not None and (blocked or stale):
                probe['cancelled'] = True
                if probe['done'].is_set():
                    self._auto_probe = None
                    diag.emit('AUTO_PROBE_DISCARDED', cycle_id=probe['key'][1], reason=blocked or 'stale')
            if blocked:
                self._auto_disarm(blocked, station)
                return
            # The timeout window opens when the probe actually arms, not when the
            # key changed: cycle_id is stamped at the product SN scan, so a window
            # started there would already have burnt the SFIS Step1 round trip and
            # the operator's MCIO scan before capture even opened.
            started = self._auto['window_started']
            if started is None:
                started = self._auto['window_started'] = now
                diag.emit('AUTO_WINDOW_START', cycle_id=cycle_id, timeout_sec=cfg['timeout_sec'],
                          target_fps=cfg['fps'], required_hits=cfg['required_hits'],
                          timeout_action=cfg['timeout_action'])
            self._auto.update(armed=True, elapsed=now - started)
            timeout = float(cfg['timeout_sec'])
            if stale:
                return
            if probe is not None:
                if not probe['done'].is_set():
                    diag.count('auto_probe_busy_frames')
                    return
                self._auto_probe = None
                period = 1.0 / max(0.1, float(cfg['fps']))
                # Leave recovery time after overruns; never replay missed sampling slots.
                self._auto['next_probe'] = max(probe['started'] + period,
                                               probe['finished'] + min(0.05, period * 0.1))
                self._auto['elapsed'] = max(now, probe['finished']) - started
                if probe.get('error'):
                    raise RuntimeError(probe['error'])
                match = probe['match']
                diag.count('auto_probes')
                diag.emit('AUTO_PROBE_RESULT', cycle_id=cycle_id, accepted=bool(match.get('accepted')),
                          score=match.get('score'), reason=match.get('reason'),
                          frame_seq=probe['frame_seq'], elapsed_seconds=self._auto['elapsed'],
                          scheduler_elapsed_seconds=self._auto['elapsed'],
                          match_ms=probe['duration'] * 1000, budget_ms=period * 1000,
                          over_budget=probe['duration'] > period)
                score = match.get('score')
                if isinstance(score, (int, float)):
                    best = self._auto['best_score']
                    self._auto['best_score'] = score if not isinstance(best, (int, float)) else max(best, score)
                late = timeout > 0 and probe['finished'] - started > timeout
                self._auto.update(score=score, reason=str(match.get('reason') or ''),
                                  hits=self._auto['hits'] + 1 if match.get('accepted') and not late else 0,
                                  last_match=match)
                station.set_preview_matching(match, self._auto_overlay())
                if self._auto['hits'] >= int(cfg['required_hits']):
                    self._start_auto_commit(probe['frame'], station, cycle_id, 'accepted',
                                            frame_seq=probe['frame_seq'])
                elif timeout > 0 and self._auto['elapsed'] >= timeout:
                    self._auto['suspended_cycle'] = cycle_id
                    self._start_auto_commit(probe['frame'], station, cycle_id, 'timeout',
                                            timeout_pass=cfg.get('timeout_action') == 'pass',
                                            frame_seq=probe['frame_seq'])
                return
            # Expiration takes precedence over launching another expensive match.
            if timeout > 0 and self._auto['elapsed'] >= timeout:
                self._auto['suspended_cycle'] = cycle_id
                self._start_auto_commit(frame, station, cycle_id, 'timeout',
                                        timeout_pass=cfg.get('timeout_action') == 'pass')
                return
            if now < self._auto['next_probe']:
                return
            submitted_at = time.monotonic()
            probe = {'frame': frame.copy(), 'frame_seq': self.frame_seq, 'key': key,
                     'station': station, 'matcher': station.matcher, 'started': now,
                     'done': threading.Event(), 'cancelled': False}

            def evaluate():
                clock_start = time.perf_counter()
                try:
                    probe['match'] = probe['matcher'].evaluate_snapshot(probe['frame'])
                except Exception as exc:
                    probe['error'] = str(exc)
                finally:
                    probe['duration'] = time.perf_counter() - clock_start
                    probe['finished'] = probe['started'] + time.monotonic() - submitted_at
                    probe['done'].set()

            probe['thread'] = threading.Thread(target=evaluate, name='apl-auto-probe', daemon=True)
            self._auto_probe = probe
            try:
                probe['thread'].start()
            except Exception:
                self._auto_probe = None
                raise
        except Exception as exc:
            diag.emit('AUTO_PROBE_ERROR', error=str(exc))
            log.exception('Automatic capture probe failed')
            self._auto.update(armed=False, hits=0, reason='probe_error', error=str(exc))

    def _start_auto_commit(self, frame, station, cycle_id, trigger: str,
                           timeout_pass: bool = False, frame_seq=None) -> None:
        """Hand the probed frame to a short-lived worker. The evidence write holds
        station.frame_lock and fsyncs SQLite, and inspect_snapshot may raise, so
        neither belongs on the camera thread."""
        source = {'mode': 'camera',
                  'captured_from_frame_seq': self.frame_seq if frame_seq is None else frame_seq,
                  'camera_generation': self._camera_generation,
                  'auto_capture_cycle_id': cycle_id,
                  'auto_capture_attempt_count': self._auto['key'][2],
                  'auto_capture': True,
                  'auto_capture_trigger': trigger,
                  'auto_capture_hits': int(self._auto['hits']),
                  'auto_capture_elapsed': round(float(self._auto['elapsed']), 3)}
        if timeout_pass:
            # Spelled out in the evidence: this unit closed on the clock, not on a match.
            source['auto_capture_timeout_action'] = 'pass'
        snapshot = frame.copy()
        diag.emit('AUTO_COMMIT_START', cycle_id=cycle_id, trigger=trigger,
                  elapsed_seconds=self._auto['elapsed'], timeout_pass=timeout_pass)

        def commit():
            commit_started = time.perf_counter()
            try:
                result = self.inspect_frame(snapshot, source, timeout_pass=timeout_pass)
                diag.emit('AUTO_COMMIT_RESULT', cycle_id=cycle_id, success=result.get('success'),
                          state=(result.get('station') or {}).get('state'),
                          result=(result.get('station') or {}).get('result'),
                          error=result.get('error'), elapsed_ms=(time.perf_counter()-commit_started)*1000)
                if result.get('success'):
                    state = result.get('station') or {}
                    self._auto['last_commit'] = {
                        'trigger': trigger,
                        'result': state.get('result'),
                        'state': state.get('state'),
                        'attempt_id': (result.get('attempt') or {}).get('attempt_id'),
                        'timeout_pass': timeout_pass,
                        'at': time.time()}
                    self._auto['error'] = ''
                else:
                    self._auto['error'] = str(result.get('error') or '')
            except Exception as exc:
                log.exception('Automatic capture commit failed')
                diag.emit('AUTO_COMMIT_ERROR', cycle_id=cycle_id, error=str(exc),
                          elapsed_ms=(time.perf_counter()-commit_started)*1000)
                self._auto['error'] = str(exc)
            finally:
                # Hand the overlay back to the match that was actually saved.
                self._auto['last_match'] = None
                self._clear_preview_matching(station)
                with self.lock:
                    self._auto['committing'] = False
                    self._auto_thread = None

        self._auto.update(committing=True, hits=0)
        thread = threading.Thread(target=commit, name='apl-auto-capture', daemon=True)
        self._auto_thread = thread
        thread.start()

    def inspect_frame(self, frame, source_context=None, timeout_pass=False) -> Dict[str, Any]:
        with self.lock:
            station = self.station
        if station is None:
            return {'success': False, 'error': 'APL station is unavailable'}
        if frame is None:
            return {'success': False, 'error': 'No decoded frame is available for inspection'}
        source = dict(source_context or {})
        generation = source.get('camera_generation', getattr(self, '_camera_generation', 0))
        cancelled = (lambda: self.stop_event.is_set() or generation != self._camera_generation) if source.get('mode') == 'camera' else None
        expected = ({'expected_cycle_id': source.get('auto_capture_cycle_id'),
                     'expected_attempt_count': source.get('auto_capture_attempt_count')}
                    if source.get('auto_capture') else {})
        result = station.inspect_snapshot(frame, source=source, quality=self.cfg.snapshot_quality,
                                          cancelled=cancelled, timeout_pass=timeout_pass, **expected)
        if cancelled is not None and cancelled():
            return {'success': False, 'error': 'Camera stopped during inspection'}
        if result.get('success'):
            snapshot = station.status()
            result_frame = station.matcher.draw(frame, snapshot) if station.matcher is not None else frame
            with self.lock:
                self.latest_result = result_frame
                self.latest_result_jpeg = _jpeg(result_frame, self.cfg.preview_quality)
                self.frame_pass = snapshot.get('result') == 'PASS'
                self.verdict = 'OK' if self.frame_pass else 'NOT_MATCHED'
                self.last_infer_at = time.time()
                self.infer_seq += 1
            if not source.get('auto_capture'):
                # Pressing the button is how the operator restarts automatic
                # inspection after the probe timed out on this cycle.
                self._auto['suspended_cycle'] = None
        return result

    def capture_and_inspect(self, source_context=None) -> Dict[str, Any]:
        with self.lock:
            if self.stop_event.is_set() or self._stopping or self._release_failed:
                return {'success': False, 'error': 'Camera release is pending'}
            station = self.station
            backend = self.backend
            last_frame_at = self.last_frame_at
        if station is None:
            return {'success': False, 'error': 'APL station is unavailable'}
        if isinstance(backend, OpenCVCameraBackend) and backend.status().get('is_file'):
            backend.set_paused(True)
            frame, source = backend.selected_frame()
            context = dict(source_context or {})
            source.update({key: context[key] for key in ('id', 'name') if context.get(key)})
            source.pop('source', None)
        else:
            with self.lock:
                frame = self.latest_raw.copy() if self.latest_raw is not None else None
            if frame is None or not isinstance(last_frame_at, (int, float)) or time.time() - last_frame_at > 3.0:
                return {'success': False, 'error': 'Wait for a fresh camera frame before inspecting'}
            source = {'mode': 'camera', 'captured_from_frame_seq': self.frame_seq}
        return self.inspect_frame(frame, source)

    def start_recording(self, mode: Optional[str] = None) -> Dict[str, Any]:
        if not self._record_command_lock.acquire(blocking=False):
            return {'success': False, 'error': 'Recording transition is in progress'}
        try:
            result = self._start_recording(mode)
            if result.get('success'):
                self._record_result = None
            return result
        finally:
            self._record_command_lock.release()

    def _start_recording(self, mode: Optional[str] = None) -> Dict[str, Any]:
        mode = str(mode or self.cfg.record_source).lower()
        if mode not in ('raw', 'result'):
            mode = 'raw'
        if not self._storage_ok():
            return {'success': False, 'error': f'free space below min_free_mb={self.cfg.min_free_mb} or unavailable'}
        with self.lock:
            backend = self.backend
            if self.stop_event.is_set() or self._stopping or self._release_failed:
                return {'success': False, 'error': 'Camera release is pending'}
            if mode == 'result':
                src = self.latest_result if self.latest_result is not None else self.latest_raw
            else:
                src = self.latest_raw
            frame = src.copy() if src is not None else None
        if backend is None:
            return {'success': False, 'error': 'runtime not running'}
        path = EDGE_RECORD_DIR / f'rec_{_timestamp()}.mp4'
        # QTI shares the Cairo-overlaid H.264 branch with MediaMTX and recording.
        if backend.hardware_recording and mode == 'result':
            ok, msg = backend.start_recording(path)
            version = self._bump_media_version() if ok else self.media_version
            return {'success': ok, 'path': f'recordings/{path.name}' if ok else '', 'mode': mode, 'hardware': True, 'error': '' if ok else msg, 'media_version': version}
        if frame is None:
            return {'success': False, 'error': 'no frame available'}
        # Software overlay recording must use the measured received-frame rate,
        # not the sensor target rate. Otherwise a ~24 FPS appsink stream written
        # into a 30 FPS container plays back too fast.
        measured_fps = float(self.source_fps or 0.0)
        if not (1.0 <= measured_fps <= 120.0):
            measured_fps = float(self.cfg.recording_fps)
        ok, actual = self.soft_rec.start(frame, path, mode, fps=measured_fps)
        actual_path = Path(actual) if ok else None
        version = self._bump_media_version() if ok else self.media_version
        return {
            'success': ok,
            'path': f'recordings/{actual_path.name}' if actual_path else '',
            'mode': mode,
            'hardware': False,
            'error': '' if ok else actual,
            'media_version': version,
        }

    def stop_recording(self) -> Dict[str, Any]:
        if not self._record_command_lock.acquire(timeout=10.0):
            self.stop_event.set()
            return {'success': False, 'requires_camera_stop': True, 'error': 'Recording command did not finish'}
        self._record_finalizing = True
        try:
            result = self._stop_recording()
            self._record_result = result
            if result.get('recorder_null') is False or result.get('requires_camera_stop'):
                self.stop_event.set()
                result['requires_camera_stop'] = True
            return result
        finally:
            self._record_finalizing = False
            self._record_command_lock.release()

    def _stop_recording(self) -> Dict[str, Any]:
        with self.lock:
            backend = self.backend
        if self.soft_rec.active:
            result = self.soft_rec.stop()
        elif backend is not None and backend.hardware_recording:
            result = backend.stop_recording()
        else:
            result = self._record_result or {'success': True, 'not_recording': True}
        if result.get('path'):
            try:
                p = Path(result['path'])
                result['relative_path'] = f'recordings/{p.name}'
            except Exception:
                pass
        if result.get('success'):
            result['media_version'] = self._bump_media_version()
        else:
            result['media_version'] = self.media_version
        return result

    def reset_sop(self):
        with self.lock:
            if not self.engine:
                return None
            self.sop = self.engine.reset(reason='EDGE_API_RESET')
            return self.sop

    def finish_sop(self):
        with self.lock:
            if not self.engine:
                return None
            self.sop = self.engine.finish()
            return self.sop

    def acknowledge_sop(self):
        with self.lock:
            if not self.engine:
                return None
            self.sop = self.engine.acknowledge_alarm()
            return self.sop

    def list_media(self) -> Dict[str, Any]:
        with self.lock:
            backend_status = self.backend.status() if self.backend is not None else {}
            active_path = (
                str(self.soft_rec.path) if self.soft_rec.active and self.soft_rec.path
                else str(backend_status.get('record_path') or '')
            )
            active_resolved = str(Path(active_path).resolve()) if active_path else ''
            version = self.media_version

        def items(root: Path, prefix: str):
            out = []
            if not root.exists():
                return out
            for p in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True):
                if not p.is_file():
                    continue
                try:
                    st = p.stat()
                except FileNotFoundError:
                    continue
                out.append({
                    'path': f'{prefix}/{p.name}', 'name': p.name,
                    'size': st.st_size, 'mtime': st.st_mtime,
                    'active': bool(active_resolved and str(p.resolve()) == active_resolved),
                    'kind': 'photo' if prefix == 'photos' else 'recording',
                })
            return out
        return {
            'version': version,
            'generated_at': time.time(),
            'photos': items(EDGE_PHOTO_DIR, 'photos'),
            'recordings': items(EDGE_RECORD_DIR, 'recordings'),
        }

    def delete_media(self, rel: str) -> Dict[str, Any]:
        try:
            path = self.resolve_media(rel)
        except ValueError as exc:
            return {'success': False, 'error': str(exc), 'code': 'invalid_path'}

        with self.lock:
            backend_status = self.backend.status() if self.backend is not None else {}
            active = self.soft_rec.active or bool(backend_status.get('recording'))
            active_path = (
                str(self.soft_rec.path) if self.soft_rec.active and self.soft_rec.path
                else str(backend_status.get('record_path') or '')
            )
        if active and active_path:
            try:
                if path.resolve() == Path(active_path).resolve():
                    return {
                        'success': False,
                        'error': 'cannot delete the file that is currently being recorded',
                        'code': 'recording_active',
                    }
            except Exception:
                pass

        if not path.exists() or not path.is_file():
            return {'success': False, 'error': 'not found', 'code': 'not_found'}
        try:
            name = path.name
            path.unlink()
            # Verify the directory entry is actually gone before reporting success.
            if path.exists():
                return {'success': False, 'error': 'delete verification failed', 'code': 'delete_failed'}
        except OSError as exc:
            return {'success': False, 'error': f'delete failed: {exc}', 'code': 'delete_failed'}
        version = self._bump_media_version()
        return {'success': True, 'deleted': str(rel), 'name': name, 'media_version': version}

    @staticmethod
    def resolve_media(rel: str) -> Path:
        rel = str(rel or '').replace('\\', '/').lstrip('/')
        root = EDGE_RUNTIME_DIR.resolve()
        target = (EDGE_RUNTIME_DIR / rel).resolve()
        if target == root or root not in target.parents:
            raise ValueError('invalid media path')
        if target.parent not in (EDGE_PHOTO_DIR.resolve(), EDGE_RECORD_DIR.resolve()):
            raise ValueError('media path must be directly under photos/ or recordings/')
        return target
