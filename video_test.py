"""Exclusive video replay using the station matcher and a separate journal."""

import copy
import json
import math
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path

import cv2

from apl_matching import APLMatcher
from apl_station import APLStation
from edge_runtime import open_video_file


class VideoTestInputs:
    max_bytes = 1024 * 1024 * 1024
    extensions = {'.mp4'}
    def __init__(self, runtime, station, root):
        self.runtime = runtime
        self.production_station = station
        self.station = station
        self.root = Path(root)
        self.files = {}
        self.saved_config = None
        self.current = None
        self.browser_active = False
        self.production_test_mode = bool(station.test_mode)

    @staticmethod
    def _display_name(value):
        name = unicodedata.normalize('NFC', str(value or '')).replace('\\', '/').rsplit('/', 1)[-1].strip()
        if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError('Video filename is empty or contains unsupported characters')
        return name

    @staticmethod
    def _discover(path):
        metadata = {}
        ffprobe = shutil.which('ffprobe')
        if ffprobe:
            try:
                result = subprocess.run(
                    [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                     '-show_entries', 'stream=codec_name,width,height,avg_frame_rate:format=duration',
                     '-of', 'json', str(path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding='utf-8', errors='replace', timeout=15, check=False,
                )
                payload = json.loads(result.stdout or '{}') if result.returncode == 0 else {}
                stream = (payload.get('streams') or [{}])[0]
                rate = str(stream.get('avg_frame_rate') or '0/1').split('/', 1)
                fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 0.0
                duration = float((payload.get('format') or {}).get('duration') or 0.0)
                if math.isfinite(fps) and fps > 0:
                    metadata['fps'] = fps
                if math.isfinite(duration) and duration > 0:
                    metadata['duration_seconds'] = duration
                codec = str(stream.get('codec_name') or '').strip().lower()
                if codec:
                    metadata['codec'] = codec
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        if 'duration_seconds' not in metadata:
            discoverer = shutil.which('gst-discoverer-1.0')
            if discoverer:
                try:
                    result = subprocess.run(
                        [discoverer, str(path)], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                        errors='replace', timeout=15, check=False,
                    )
                    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stdout or '')
                    if match:
                        metadata['duration_seconds'] = int(match.group(1))*3600 + int(match.group(2))*60 + float(match.group(3))
                    if re.search(r'video/x-h264|H\.264|AVC', result.stdout or '', re.IGNORECASE):
                        metadata['codec'] = 'h264'
                except (OSError, subprocess.SubprocessError):
                    pass
        return metadata

    def upload(self, uploaded):
        name = self._display_name(uploaded.filename)
        if Path(name).suffix.lower() not in self.extensions:
            raise ValueError('Choose an H.264 MP4 video')
        self.root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        folder = self.root / token
        folder.mkdir()
        target = folder / ('source' + Path(name).suffix.lower())
        size = 0
        reserve = int(self.runtime.cfg.min_free_mb) * 1024 * 1024
        try:
            with target.open('xb') as output:
                while True:
                    chunk = uploaded.stream.read(1024*1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError('Video exceeds the 1 GB upload limit')
                    if shutil.disk_usage(folder).free - len(chunk) < reserve:
                        raise ValueError('Insufficient free space for this video')
                    output.write(chunk)
            meta = self._probe(target)
            trace = None if self.production_station.snapshot_mode else self._build_trace(target, meta)
        except Exception:
            target.unlink(missing_ok=True)
            folder.rmdir()
            raise
        self.files[token] = {'id': token, 'name': name, 'path': str(target),
                             'trace': trace, **meta}
        return self._public(self.files[token])

    @staticmethod
    def _public(item):
        return {key: value for key, value in item.items() if key not in {'path', 'trace'}}

    def _probe(self, path):
        cap = open_video_file(path)
        try:
            ok, frame = cap.read()
            if not ok or frame is None:
                raise ValueError('This video cannot be decoded on this device')
            width, height = self.production_station.config['reference_size']
            if frame.shape[:2] != (height, width):
                raise ValueError(f'Video must be {width} x {height} to match the golden samples')
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            discovered = self._discover(path)
            codec = str(discovered.get('codec') or '').lower()
            if codec and codec not in {'h264', 'avc', 'avc1'}:
                raise ValueError('Video must use H.264 encoding in an MP4 container')
            return {'width': width, 'height': height,
                    'fps': discovered.get('fps', fps),
                    'duration_seconds': discovered.get(
                        'duration_seconds', count/fps if fps > 0 else None,
                    ), 'codec': codec or 'verified-by-decoder'}
        finally:
            cap.release()

    def _build_trace(self, path, metadata):
        cap = open_video_file(path, realtime=False)
        matcher = APLMatcher(self.production_station.config,
                             self.production_station.config_path)
        trace = []
        try:
            source_fps = float(metadata.get('fps') or cap.get(cv2.CAP_PROP_FPS) or 0.0)
            if not (0.1 <= source_fps <= 240.0):
                raise ValueError('Video frame rate is unavailable or invalid')
            stride = max(1, round(source_fps / 10.0))
            frame_index = 0
            while True:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                if frame_index % stride == 0:
                    trace.append({'time_seconds': frame_index / source_fps,
                                  'match': matcher.evaluate(frame)})
                frame_index += 1
            if not trace:
                raise ValueError('Video contains no frames for template matching')
            metadata['analysis_fps'] = source_fps / stride
            metadata['analysis_samples'] = len(trace)
            return trace
        finally:
            cap.release()

    def _require_idle(self):
        if self.station.status()['state'] != 'WAITING_SN':
            raise ValueError('Finish or reset this inspection before switching inputs')
        if self.runtime.status().get('recording'):
            raise ValueError('Stop recording before switching inputs')

    def _browser_metadata(self, metadata):
        if not isinstance(metadata, dict):
            raise ValueError('Browser video metadata is required')
        name = self._display_name(metadata.get('name'))
        try:
            width = int(metadata.get('width'))
            height = int(metadata.get('height'))
            duration = float(metadata.get('duration_seconds'))
        except (TypeError, ValueError):
            raise ValueError('Browser video metadata is invalid')
        expected_width, expected_height = self.production_station.config['reference_size']
        if (width, height) != (expected_width, expected_height):
            raise ValueError(
                f'Video must be {expected_width} x {expected_height} to match the golden samples'
            )
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('Browser video duration is unavailable or invalid')
        return {
            'name': name,
            'width': width,
            'height': height,
            'duration_seconds': duration,
        }

    def start_browser(self, metadata):
        browser_metadata = self._browser_metadata(metadata)
        if self.browser_active and self.current:
            state = self.station.status()['state']
            if state not in {'WAITING_SN', 'WAITING_MCIO_SN', 'WAITING_CAPTURE', 'NOT_MATCHED'}:
                raise ValueError('The test video cannot be changed while inspection is busy or complete')
            if self.runtime.status().get('recording'):
                raise ValueError('Stop recording before changing the test video')
            self.current.update(browser_metadata)
            return {'success': True, 'station': self.station.status(), 'input': self.status()}

        state = self.station.status()['state']
        if state in {'WAITING_MCIO_SN', 'WAITING_CAPTURE', 'NOT_MATCHED'}:
            if self.runtime.status().get('recording'):
                raise ValueError('Stop recording before starting the test video')
            self.station.set_test_mode(True, source='browser_video')
            self.current = {'id': uuid.uuid4().hex, **browser_metadata}
            self.browser_active = True
            return {'success': True, 'station': self.station.status(), 'input': self.status()}

        self._require_idle()
        if self.current or self.saved_config is not None:
            raise ValueError('Return to the live camera before starting a browser video test')
        next_station = APLStation(
            self.production_station.config_path,
            self.production_station.data_dir,
            local_mode=True,
            test_mode=True,
        )
        if self.station is not self.production_station:
            self.station.close()
        self.station = next_station
        self.runtime.station = next_station
        self.current = {
            'id': uuid.uuid4().hex,
            **browser_metadata,
        }
        self.browser_active = True
        return {'success': True, 'station': self.station.status(), 'input': self.status()}

    def _stop(self):
        backend = self.runtime.backend
        result = self.runtime.stop()
        if not result.get('success'):
            raise ValueError(result.get('error', 'Input did not stop'))
        if backend is not None and hasattr(backend, '_safe_stop') and not backend._safe_stop:
            raise ValueError('Camera release was not confirmed; input switch cancelled')

    def start(self, token):
        self._require_idle()
        item = self.files.get(token)
        if not item:
            raise ValueError('Upload a video before starting the test')
        self._probe(item['path'])
        next_station = APLStation(
            self.production_station.config_path,
            self.production_station.data_dir,
            local_mode=True,
            test_mode=True,
        )
        try:
            self._stop()
        except Exception:
            next_station.close()
            raise
        if self.saved_config is None:
            self.saved_config = copy.deepcopy(self.runtime.cfg)
        if self.station is not self.production_station:
            self.station.close()
        self.station = next_station
        self.runtime.station = self.station
        self.runtime.set_video_trace(item.get('trace'))
        cfg = copy.deepcopy(self.saved_config)
        cfg.backend, cfg.source, cfg.rotation = 'opencv', item['path'], 0
        cfg.product_id, cfg.display_mode = 0, 'headless'
        cfg.loop_source = not self.production_station.snapshot_mode
        self.runtime.cfg = cfg
        self.runtime.soft_rec.cfg = cfg
        self.current = item
        return self.runtime.start()

    def restore(self):
        self._require_idle()
        if self.browser_active:
            if self.station is not self.production_station:
                self.station.close()
                self.station = self.production_station
                self.runtime.station = self.production_station
            else:
                self.production_station.set_test_mode(self.production_test_mode)
            self.production_station.camera_fault('Input changed')
            self.current = None
            self.browser_active = False
            return {'success': True, 'station': self.station.status(), 'input': self.status()}
        if self.saved_config is None:
            return {'success': True}
        self._stop()
        self.runtime.set_video_trace(None)
        self.station.close()
        self.station = self.production_station
        self.runtime.station = self.station
        self.runtime.cfg = self.saved_config
        self.runtime.soft_rec.cfg = self.saved_config
        self.station.camera_fault('Input changed')
        self.current = self.saved_config = None
        return self.runtime.start()

    def status(self):
        demo_path = Path(str(self.runtime.cfg.source))
        if (not self.current and self.production_station.test_mode
                and self.runtime.cfg.backend == 'opencv'
                and demo_path.suffix.lower() in {'.mp4', '.mov', '.avi', '.mkv'}
                and demo_path.is_file()):
            return {'mode': 'demo', 'name': demo_path.name, 'id': None}
        if self.current:
            return self._public(self.current) | {
                'mode': 'browser_video' if self.browser_active else 'video'
            }
        return {'mode': 'camera', 'name': 'Live camera', 'id': None}

    def close(self):
        if self.station is not self.production_station:
            self.station.close()
        self.production_station.close()
