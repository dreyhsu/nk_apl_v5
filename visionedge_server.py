#!/usr/bin/env python3
"""VisionEdge HTTPS entrypoint and Edge APIs.

The application reuses LIVE_FLOW_GRAPH_REVIEW's Flask app/data model and adds a
single-camera Edge runtime. HTTPS configuration intentionally mirrors the
SmartCam webserver mechanism: cert/key are loaded from the project config and
can be overridden for development from the environment/CLI.
"""
from __future__ import annotations

import argparse
import atexit
import configparser
import math
import os
import signal
import socket
import ssl
import json
import threading
import time
import copy
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from flask import Response, jsonify, request, send_from_directory, stream_with_context, redirect

import server as base
from edge_runtime import EdgeRuntime
from camera_lifecycle import CameraLifecycle
from system_metrics import TemperatureReader

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / 'config' / 'edge.ini'
BROWSER_FRAME_MAX_BYTES = 20 * 1024 * 1024


@dataclass
class ServerConfig:
    host: str = '0.0.0.0'
    port: int = 8080
    tls: bool = True
    certfile: str = 'config/certs/CRS0000000616.cert'
    keyfile: str = 'config/certs/CRS0000000616.key'
    threads: int = 12

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> 'ServerConfig':
        cfg = cls()
        cp = configparser.ConfigParser()
        if path.exists():
            cp.read(path, encoding='utf-8-sig')
        if cp.has_section('server'):
            sec = cp['server']
            cfg.host = sec.get('host', cfg.host).strip() or cfg.host
            try:
                cfg.port = sec.getint('port', fallback=cfg.port)
            except Exception:
                pass
            try:
                cfg.tls = sec.getboolean('tls', fallback=cfg.tls)
            except Exception:
                pass
            cfg.certfile = sec.get('certfile', cfg.certfile).strip() or cfg.certfile
            cfg.keyfile = sec.get('keyfile', cfg.keyfile).strip() or cfg.keyfile
            try:
                cfg.threads = max(4, sec.getint('threads', fallback=cfg.threads))
            except Exception:
                pass
        return cfg

    def resolve_cert(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (BASE_DIR / p)


def _load_video_test_enabled(path: Path = CONFIG_PATH) -> bool:
    cp = configparser.ConfigParser()
    if path.exists():
        cp.read(path, encoding='utf-8-sig')
    try:
        return cp.getboolean('features', 'video_test_enabled', fallback=True)
    except ValueError:
        return True


VIDEO_TEST_ENABLED = _load_video_test_enabled()


EDGE = EdgeRuntime(base)
app = base.app


class APLRequest(app.request_class):
    @property
    def max_content_length(self):
        limit = super().max_content_length
        if self.path == '/api/edge/video-test/upload':
            video_limit = 1024*1024*1024 + 1024*1024
            return min(limit, video_limit) if limit is not None else video_limit
        if self.path == '/api/apl/capture-inspect-frame':
            frame_limit = BROWSER_FRAME_MAX_BYTES + 1024*1024
            return min(limit, frame_limit) if limit is not None else frame_limit
        return limit


app.request_class = APLRequest
STATION = None
SCANNER = None
_station_lock = threading.RLock()
_instance_lock = None
_shutdown_requested = threading.Event()
_shutdown_lock = threading.Lock()
_shutdown_result = None
_http_server = None
INPUTS = None
TEMPERATURE = TemperatureReader()
DEMO = False
_camera_lifecycle = None
_camera_lifecycle_lock = threading.Lock()
_station_snapshot = None


def _camera_station_interrupted():
    # Do not initialize a station as a prerequisite for releasing hardware.
    target = STATION
    if target is not None:
        target.camera_fault('Camera stopped by operator')
        if getattr(target, 'storage_error', ''):
            raise RuntimeError(target.storage_error)


def camera_lifecycle():
    global _camera_lifecycle
    with _camera_lifecycle_lock:
        if _camera_lifecycle is None or _camera_lifecycle.edge is not EDGE:
            _camera_lifecycle = CameraLifecycle(EDGE, _camera_station_interrupted)
        return _camera_lifecycle


def responsive_station_status():
    global _station_snapshot
    acquired = _station_lock.acquire(blocking=False)
    target = STATION
    station_acquired = False
    try:
        if acquired and target is not None:
            station_acquired = target.lock.acquire(blocking=False)
            if station_acquired:
                _station_snapshot = target.status()
                if SCANNER is None:
                    _station_snapshot['scanner'] = {'connected': False, 'enabled': False}
                elif SCANNER.control.acquire(blocking=False):
                    try:
                        _station_snapshot['scanner'] = SCANNER.status()
                    finally:
                        SCANNER.control.release()
                state = copy.deepcopy(_station_snapshot)
                state['status_stale'] = False
                return state
        state = copy.deepcopy(_station_snapshot) if _station_snapshot else {'state': 'UNAVAILABLE', 'sop': {}}
        state['status_stale'] = True
        return state
    finally:
        if station_acquired:
            target.lock.release()
        if acquired:
            _station_lock.release()


def station_command(func):
    @wraps(func)
    def locked(*args, **kwargs):
        with _station_lock:
            if _shutdown_requested.is_set():
                return jsonify({'success': False, 'error': 'Station is stopping'}), 503
            if camera_lifecycle().notification_pending:
                return jsonify({'success': False, 'error': 'Station interruption update is pending'}), 409
            if camera_lifecycle().transition_lock.locked():
                return jsonify({'success': False, 'error': 'Camera transition is in progress'}), 409
            return func(*args, **kwargs)
    return locked


def video_test_required(func):
    @wraps(func)
    def checked(*args, **kwargs):
        if not VIDEO_TEST_ENABLED:
            return jsonify({'success': False, 'error': 'Video test mode is disabled by configuration'}), 403
        return func(*args, **kwargs)
    return checked


def inputs():
    global INPUTS
    with _station_lock:
        if INPUTS is None:
            from video_test import VideoTestInputs
            INPUTS = VideoTestInputs(EDGE, station(), BASE_DIR / 'runtime_data' / 'video_tests')
        return INPUTS


def scanner_scan(sn, source='scanner'):
    with _station_lock:
        if _shutdown_requested.is_set():
            return {'success': False, 'error': 'Station is stopping'}
        if camera_lifecycle().notification_pending or camera_lifecycle().transition_lock.locked():
            return {'success': False, 'error': 'Camera transition is in progress'}
        return station().scan(sn, source)


def station():
    global STATION
    with _station_lock:
        if STATION is None:
            from apl_station import APLStation
            STATION = APLStation(
                BASE_DIR / 'config' / 'apl_workflow.json',
                Path(os.environ.get('APL_DATA_DIR', str(BASE_DIR / 'runtime_data' / 'apl'))),
                test_mode=DEMO,
            )
            EDGE.station = STATION
        return STATION


def station_status():
    state = station().status()
    state['scanner'] = SCANNER.status() if SCANNER is not None else {
        'connected': False, 'enabled': False, 'message': 'Use the SN input on this page',
    }
    return state


def json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('A JSON object is required')
    return data


def station_response(result):
    return jsonify(result), 200 if result.get('success') else 409


def rearm_scanner_after_cycle(result):
    if (result.get('success') and result.get('station', {}).get('state') == 'WAITING_SN'
            and SCANNER is not None):
        SCANNER.reconnect()
    return result


@app.errorhandler(ValueError)
def invalid_request(exc):
    return jsonify({'success': False, 'error': str(exc)}), 400


@app.before_request
def apl_request_policy():
    if _shutdown_requested.is_set() and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        return jsonify({'success': False, 'error': 'Station is stopping'}), 503
    # Legacy PC runtimes must not open another owner of the physical camera.
    blocked = ('/api/stream-sources', '/api/runtime/', '/api/video/stream_infer',
               '/api/video/sop_stream_infer')
    if request.path.startswith(blocked):
        return jsonify({'success': False, 'error': 'Use the APL Edge runtime for this station'}), 409
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        from urllib.parse import urlsplit
        origin = request.headers.get('Origin')
        if (origin and urlsplit(origin).netloc != request.host) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify({'success': False, 'error': 'Cross-origin station commands are not allowed'}), 403


@app.route('/api/apl/status')
def apl_status():
    return jsonify({'success': True, 'station': station_status()})


@app.route('/api/apl/sfis-log')
def apl_sfis_log():
    # Read-only diagnostics for the dashboard console: the last Step1/Step4
    # attempts with the real URL, body and SFIS answer.
    try:
        after = int(request.args.get('after', 0))
    except ValueError:
        raise ValueError('SFIS log cursor must be an integer')
    return jsonify({'success': True, 'events': station().sfis_log(after)})


@app.route('/api/apl/scanner', methods=['GET', 'POST'])
def apl_scanner():
    from scanner_selection import devices
    if SCANNER is None:
        return jsonify({'success': False, 'error': 'Scanner selection is unavailable'}), 409
    if request.method == 'POST':
        path = json_object().get('path')
        if not isinstance(path, str) or not path:
            raise ValueError('Select a scanner from the current device list')
        try:
            SCANNER.select(path)
        except ValueError as exc:
            return jsonify({'success': False, 'error': str(exc)}), 409
        except OSError as exc:
            return jsonify({'success': False, 'error': 'Cannot save scanner selection: ' + str(exc)}), 500
    return jsonify({'success': True, 'scanner': SCANNER.status(), 'devices': devices()})


@app.route('/api/apl/scan', methods=['POST'])
@station_command
def apl_scan():
    data = json_object()
    expect = data.get('expect')
    if expect is not None:
        # The browser's view of the step can be a second stale while a HID scan
        # advances it underneath. Both paths take _station_lock, so checking the
        # state here is atomic with respect to the scanner thread.
        wanted = {'sn': 'WAITING_SN', 'mcio_sn': 'WAITING_MCIO_SN'}.get(expect)
        if wanted is None:
            raise ValueError('Scan field must be sn or mcio_sn')
        snapshot = station().status()
        # Snapshot mode publishes the field it will actually accept: 'sn' both at
        # WAITING_SN and in the Step1 retry state (SN_CONFIRM + last_error).
        # Legacy has no such key, so fall back to the raw state comparison.
        field = snapshot.get('active_scan_field')
        accepted = field == expect if field is not None else snapshot['state'] == wanted
        if not accepted:
            return jsonify({'success': False, 'station': station_status(),
                            'error': 'The station moved to a different step. Check the panel and scan again.'}), 409
    return station_response(station().scan(data.get('sn', ''), source='web'))


@app.route('/api/apl/operator', methods=['POST'])
@station_command
def apl_set_operator():
    return station_response(station().set_operator(json_object().get('emp_no', '')))


@app.route('/api/apl/operator/clear', methods=['POST'])
@station_command
def apl_clear_operator():
    return station_response(station().clear_operator())


@app.route('/api/apl/confirm-sn', methods=['POST'])
@station_command
def apl_confirm_sn():
    data = json_object()
    status = EDGE.status()
    last_frame = status.get('last_frame_at')
    if not status.get('running') or not isinstance(last_frame, (int, float)) or time.time() - last_frame > 3:
        return jsonify({'success': False, 'error': 'Start the camera and wait for fresh video before confirming SN'}), 409
    return station_response(station().confirm_sn(data.get('cycle_id')))


@app.route('/api/apl/confirm-result', methods=['POST'])
@station_command
def apl_confirm_result():
    result = station().confirm_result(json_object().get('cycle_id'))
    return station_response(rearm_scanner_after_cycle(result))


@app.route('/api/apl/reset', methods=['POST'])
@station_command
def apl_reset():
    return station_response(station().reset(json_object().get('cycle_id')))


@app.route('/api/apl/terminate', methods=['POST'])
@station_command
def apl_terminate():
    result = station().terminate(json_object().get('cycle_id'))
    return station_response(rearm_scanner_after_cycle(result))


@app.route('/api/apl/capture-inspect', methods=['POST'])
@station_command
def apl_capture_inspect():
    source_context = INPUTS.status() if INPUTS is not None else None
    return station_response(EDGE.capture_and_inspect(source_context))


@app.route('/api/apl/capture-inspect-frame', methods=['POST'])
@station_command
@video_test_required
def apl_capture_inspect_frame():
    current = inputs().status()
    if current.get('mode') != 'browser_video':
        return jsonify({'success': False, 'error': 'Start Browser Video Test before sending a frame'}), 409
    if request.form.get('test_id', '') != current.get('id'):
        return jsonify({'success': False, 'error': 'Browser video test session is stale'}), 409
    uploaded = request.files.get('frame')
    if uploaded is None:
        raise ValueError('A captured PNG frame is required')
    if uploaded.mimetype not in {'image/png', 'image/jpeg'}:
        raise ValueError('Captured frame must be PNG or JPEG')
    payload = uploaded.stream.read(BROWSER_FRAME_MAX_BYTES + 1)
    if len(payload) > BROWSER_FRAME_MAX_BYTES:
        raise ValueError('Captured frame exceeds the 20 MB limit')
    if not payload:
        raise ValueError('Captured frame is empty')
    try:
        frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    except cv2.error as exc:
        raise ValueError('Captured frame could not be decoded') from exc
    if frame is None:
        raise ValueError('Captured frame could not be decoded')
    expected_width, expected_height = station().config['reference_size']
    if frame.shape[:2] != (expected_height, expected_width):
        raise ValueError(
            f'Captured frame must be {expected_width} x {expected_height} to match the golden samples'
        )
    try:
        selected_seconds = float(request.form.get('selected_seconds', ''))
    except (TypeError, ValueError):
        raise ValueError('Selected video time is invalid')
    duration = float(current.get('duration_seconds') or 0.0)
    if not math.isfinite(selected_seconds) or selected_seconds < 0 or selected_seconds > duration + 0.1:
        raise ValueError('Selected video time is outside the video duration')
    source_context = {
        'mode': 'browser_video',
        'id': current.get('id'),
        'name': current.get('name'),
        'selected_seconds': selected_seconds,
        'duration_seconds': duration,
        'width': expected_width,
        'height': expected_height,
    }
    return station_response(EDGE.inspect_frame(frame, source_context))


@app.route('/api/apl/history')
def apl_history():
    try:
        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))
    except ValueError:
        raise ValueError('History limit and offset must be integers')
    rows = station().list_history(
        sn=request.args.get('sn', ''),
        visual_result=request.args.get('visual_result', ''),
        limit=limit, offset=offset,
    )
    return jsonify({'success': True, 'items': rows, 'limit': limit, 'offset': offset})


@app.route('/api/apl/history/<cycle_id>')
def apl_history_detail(cycle_id):
    item = station().history_detail(cycle_id)
    if item is None:
        return jsonify({'success': False, 'error': 'Inspection cycle not found'}), 404
    return jsonify({'success': True, 'inspection': item})


@app.route('/api/apl/evidence/<path:rel>')
def apl_evidence(rel):
    try:
        path = station().resolve_evidence(rel)
    except ValueError as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    if not path.is_file():
        return jsonify({'success': False, 'error': 'Evidence file not found'}), 404
    return send_from_directory(str(path.parent), path.name,
                               as_attachment=request.args.get('download') == '1', max_age=0)


@app.after_request
def visionedge_no_cache(response):
    # Dynamic state/media APIs must never be served from the browser cache.
    # This is especially important after deleting a capture.
    if request.path.startswith(('/api/edge', '/api/apl', '/api/stream')):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.route('/edge')
@app.route('/visionedge')
def edge_page():
    return send_from_directory('static', 'edge_dashboard.html')


app.view_functions['index'] = lambda: redirect('edge')
app.view_functions['multi_stream_page'] = lambda: redirect('edge')


@app.route('/api/edge/config', methods=['GET'])
def edge_get_config():
    config = EDGE.cfg.public()
    config['product_id'] = 1
    pending = getattr(EDGE, '_pending_config', None)
    return jsonify({'ok': True, 'config': config, 'effective_config': config,
                    'pending_config': pending.public() if pending is not None else None})


@app.route('/api/edge/config', methods=['PUT', 'POST'])
def edge_set_config():
    def validate():
        with _station_lock:
            if INPUTS is not None and INPUTS.current:
                return 'Return to live camera before changing device settings'
            if station().status()['state'] != 'WAITING_SN':
                return 'Finish or safely reset the SN cycle before changing settings'
    data = json_object()
    if 'product_id' in data:
        data['product_id'] = 0
    restart = bool(data.pop('restart', False))
    running = bool(EDGE.thread and EDGE.thread.is_alive())
    return station_response(camera_lifecycle().configure(data, restart=restart and running, validate=validate))


@app.route('/api/edge/start', methods=['POST'])
@station_command
def edge_start():
    station()
    return jsonify(camera_lifecycle().start())


@app.route('/api/edge/stop', methods=['POST'])
def edge_stop():
    return jsonify(camera_lifecycle().stop())


@app.route('/api/edge/lifecycle', methods=['GET'])
def edge_lifecycle():
    return jsonify(camera_lifecycle().status())


@app.route('/api/edge/restart', methods=['POST'])
def edge_restart():
    def validate():
        with _station_lock:
            if station().status()['state'] != 'WAITING_SN':
                return 'Reset the interrupted cycle before restarting the camera'
    return station_response(camera_lifecycle().configure(restart=True, validate=validate))


@app.route('/api/edge/status', methods=['GET'])
def edge_status():
    state = responsive_station_status()
    status = EDGE.status()
    lifecycle = camera_lifecycle().status()
    if lifecycle.get('pending'):
        status['status'] = 'STOPPING'
    if lifecycle.get('station_update', {}).get('success') is False:
        status['warning'] = lifecycle['station_update'].get('error', 'Station cleanup failed')
    demo_path = Path(str(getattr(EDGE.cfg, 'source', '')))
    demo_source = bool(
        DEMO and getattr(EDGE.cfg, 'backend', '') == 'opencv'
        and demo_path.suffix.lower() in {'.mp4', '.mov', '.avi', '.mkv'}
        and demo_path.is_file()
    )
    status.update(
        station=state,
        lifecycle=lifecycle,
        product_id=1,
        product={'id': 1, 'serial': 'NK_APL_50', 'name': 'APL 50 MCIO'},
        sop=state['sop'],
        temperature=TEMPERATURE.status(),
        input=INPUTS.status() if INPUTS is not None and not state.get('status_stale') else {
            'mode': 'demo' if demo_source else 'camera',
            'name': demo_path.name if demo_source else 'Live camera',
        },
        stream={
            'enabled': bool(EDGE.cfg.stream_enabled),
            'mode': EDGE.cfg.live_view_mode,
            'whep_url': '/api/stream/whep',
            'media_transport': 'ICE/TCP 5000',
        },
        features={'video_test_enabled': VIDEO_TEST_ENABLED},
    )
    return jsonify(status)


@app.route('/api/edge/video-test/upload', methods=['POST'])
@video_test_required
def video_test_upload():
    uploaded = request.files.get('video')
    if uploaded is None:
        raise ValueError('A video file is required')
    return jsonify({'success': True, 'video': inputs().upload(uploaded)})


@app.route('/api/edge/browser-video/start', methods=['POST'])
@station_command
@video_test_required
def browser_video_start():
    global STATION
    result = inputs().start_browser(json_object())
    STATION = INPUTS.station
    return station_response(result)


@app.route('/api/edge/browser-video/stop', methods=['POST'])
@station_command
@video_test_required
def browser_video_stop():
    global STATION
    result = inputs().restore()
    STATION = INPUTS.station
    return station_response(result)


@app.route('/api/edge/video-test/start', methods=['POST'])
@station_command
@video_test_required
def video_test_start():
    global STATION
    result = inputs().start(json_object().get('id'))
    STATION = INPUTS.station
    return station_response(result)


@app.route('/api/edge/video-test/camera', methods=['POST'])
@station_command
@video_test_required
def video_test_camera():
    global STATION
    result = inputs().restore()
    STATION = INPUTS.station
    return station_response(result)


@app.route('/api/edge/video-test/control', methods=['POST'])
@station_command
@video_test_required
def video_test_control():
    data = json_object()
    result = EDGE.video_control(data.get('action'), data.get('seconds'))
    return jsonify(result), 200 if result.get('success') else 409


@app.route('/api/edge/frame.jpg', methods=['GET'])
def edge_frame():
    mode = request.args.get('mode', 'result')
    data = EDGE.jpeg(mode)
    if not data:
        return Response(status=204)
    return Response(data, mimetype='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.route('/api/edge/live.mjpg', methods=['GET'])
def edge_mjpeg():
    mode = request.args.get('mode', 'result')

    def generate():
        seq = -1
        while True:
            seq, data = EDGE.wait_jpeg(seq, mode=mode, timeout=2.0)
            if not data:
                continue
            yield (
                b'--frame\r\nContent-Type: image/jpeg\r\n'
                b'Cache-Control: no-store\r\n\r\n' + data + b'\r\n'
            )

    return Response(
        stream_with_context(generate()),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/edge/snapshot', methods=['POST'])
def edge_snapshot():
    data = request.get_json(silent=True) or {}
    return jsonify(EDGE.snapshot(data.get('mode', 'result')))


@app.route('/api/edge/record/start', methods=['POST'])
def edge_record_start():
    data = request.get_json(silent=True) or {}
    result = EDGE.start_recording(data.get('mode'))
    if result.get('success'):
        camera_lifecycle().reset_recording()
    return jsonify(result)


@app.route('/api/edge/record/stop', methods=['POST'])
def edge_record_stop():
    return jsonify(camera_lifecycle().stop_recording())


@app.route('/api/edge/media', methods=['GET'])
def edge_media():
    return jsonify(EDGE.list_media())


@app.route('/api/edge/media/delete', methods=['POST'])
def edge_media_delete():
    data = request.get_json(silent=True) or {}
    result = EDGE.delete_media(data.get('path', ''))
    if result.get('success'):
        return jsonify(result)
    code = result.get('code')
    status = 404 if code == 'not_found' else 409 if code == 'recording_active' else 400
    return jsonify(result), status


@app.route('/api/edge/media/<path:rel>', methods=['GET', 'DELETE'])
def edge_media_file(rel):
    if request.method == 'DELETE':
        # Backward-compatible route. New UI uses POST /media/delete to avoid
        # proxy/path-encoding ambiguity and mirrors smartcam_webserver behavior.
        result = EDGE.delete_media(rel)
        if result.get('success'):
            return jsonify(result)
        code = result.get('code')
        status = 404 if code == 'not_found' else 409 if code == 'recording_active' else 400
        return jsonify(result), status
    try:
        path = EDGE.resolve_media(rel)
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    if not path.exists() or not path.is_file():
        return jsonify({'error': 'not found'}), 404
    return send_from_directory(
        str(path.parent), path.name,
        as_attachment=request.args.get('download') == '1',
        max_age=0,
    )


@app.route('/api/edge/sop/reset', methods=['POST'])
def edge_sop_reset():
    return jsonify({'success': False, 'error': 'Use the SN-cycle reset action'}), 409


@app.route('/api/edge/sop/finish', methods=['POST'])
def edge_sop_finish():
    return jsonify({'success': False, 'error': 'All four visual steps and operator confirmation are required'}), 409


@app.route('/api/edge/sop/ack', methods=['POST'])
def edge_sop_ack():
    return jsonify({'success': False, 'error': 'Remove the 30 product; stable empty fixture evidence clears this alarm'}), 409


def _shutdown():
    global _shutdown_result
    _shutdown_requested.set()
    camera_lifecycle().closing = True
    with _shutdown_lock:
        if _shutdown_result is not None:
            return dict(_shutdown_result)
        errors = []
        safe = False
        try:
            result = camera_lifecycle().stop()
            safe = result.get('safe', result.get('success', False))
            if not result.get('success'):
                errors.append(result.get('error') or 'Camera shutdown failed')
        except Exception as exc:
            errors.append(f'Camera shutdown failed: {exc}')
        if SCANNER is not None:
            scanner_done = threading.Event()
            def stop_scanner():
                try:
                    SCANNER.stop()
                except Exception as exc:
                    errors.append(f'Scanner shutdown failed: {exc}')
                finally:
                    scanner_done.set()
            threading.Thread(target=stop_scanner, daemon=True, name='scanner-shutdown').start()
            if not scanner_done.wait(2.0):
                errors.append('Scanner shutdown timed out')
        # Do not close station storage while its runtime is still producing data.
        if not (EDGE.thread and EDGE.thread.is_alive()) and not camera_lifecycle().notification_pending:
            if _station_lock.acquire(timeout=3.0):
                try:
                    # VideoTestInputs owns both production and test station storage.
                    target = INPUTS if INPUTS is not None else STATION
                    if target is not None:
                        try:
                            close_done = threading.Event()
                            def close_station():
                                try:
                                    target.close()
                                except Exception as exc:
                                    errors.append(f'Station shutdown failed: {exc}')
                                finally:
                                    close_done.set()
                            threading.Thread(target=close_station, daemon=True, name='station-close').start()
                            if not close_done.wait(2.0):
                                errors.append('Station close timed out; storage is still owned by its writer')
                        except Exception as exc:
                            errors.append(f'Station shutdown failed: {exc}')
                finally:
                    _station_lock.release()
            else:
                errors.append('Station storage retained: request is still in progress')
        else:
            errors.append('Station storage retained: runtime is still alive')
        _shutdown_result = {'success': safe and not errors, 'safe': safe, 'error': '; '.join(errors)}
        print(f'[VisionEdge] shutdown result: {_shutdown_result}', flush=True)
        return dict(_shutdown_result)


def _request_shutdown(signum, frame):
    if _shutdown_requested.is_set():
        return
    _shutdown_requested.set()
    EDGE.stop_event.set()
    camera_lifecycle().closing = True
    if _http_server is not None:
        threading.Thread(target=_http_server.shutdown, daemon=True, name='apl-http-stop').start()


atexit.register(_shutdown)


def _env_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name, '').strip().lower()
    if raw in ('1', 'on', 'true', 'yes'):
        return True
    if raw in ('0', 'off', 'false', 'no'):
        return False
    return None


def _build_tls_context(enabled: bool, cfg: ServerConfig):
    if not enabled:
        return None
    cert = cfg.resolve_cert(cfg.certfile)
    key = cfg.resolve_cert(cfg.keyfile)
    missing = [str(p) for p in (cert, key) if not p.exists()]
    if missing:
        raise RuntimeError('TLS is enabled but certificate/key is missing: ' + ', '.join(missing))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return ctx


def _assert_port_available(host: str, port: int) -> None:
    # Do this before touching qtiqmmfsrc. It prevents the half-open Qualcomm
    # camera teardown we saw when Flask discovered a busy port after auto-start.
    test_host = host if host not in ('', '*') else '0.0.0.0'
    family = socket.AF_INET6 if ':' in test_host and test_host != '0.0.0.0' else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((test_host, int(port)))
    except OSError as exc:
        raise RuntimeError(f'Port {port} is not available on {host}: {exc}') from exc
    finally:
        sock.close()


def _lock_instance(data_dir):
    global _instance_lock
    data_dir.mkdir(parents=True, exist_ok=True)
    handle = (data_dir / 'station.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('Another station process already owns this data directory')
    _instance_lock = handle


def _serve(host, port, tls_context, threads):
    global _http_server
    from werkzeug.serving import ThreadedWSGIServer, WSGIRequestHandler

    class StationRequestHandler(WSGIRequestHandler):
        def log_request(self, code='-', size='-'):
            try:
                is_error = int(code) >= 400
            except (TypeError, ValueError):
                is_error = True
            if is_error:
                super().log_request(code, size)

    class StationHTTPServer(ThreadedWSGIServer):
        request_queue_size = 64
        daemon_threads = True

        def __init__(self):
            self.slots = threading.BoundedSemaphore(max(8, threads))
            super().__init__(host, port, app, handler=StationRequestHandler, ssl_context=None)

        def process_request(self, conn, address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(conn)
                return
            try:
                super().process_request(conn, address)
            except BaseException:
                self.slots.release()
                raise

        def process_request_thread(self, conn, address):
            try:
                conn.settimeout(5)
                if tls_context is not None:
                    conn = tls_context.wrap_socket(conn, server_side=True, do_handshake_on_connect=False)
                    conn.do_handshake()
                conn.settimeout(30)
                super().process_request_thread(conn, address)
            except (OSError, ssl.SSLError):
                conn.close()
            finally:
                self.slots.release()

    if _shutdown_requested.is_set():
        return
    httpd = StationHTTPServer()
    _http_server = httpd
    try:
        if _shutdown_requested.is_set():
            return
        httpd.serve_forever(poll_interval=0.25)
    finally:
        _shutdown()
        httpd.server_close()
        _http_server = None


def _main():
    global SCANNER, DEMO
    cfg = ServerConfig.load()
    parser = argparse.ArgumentParser(description='VisionEdge AI Camera server')
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--threads', type=int, default=None)
    tls_group = parser.add_mutually_exclusive_group()
    tls_group.add_argument('--tls', dest='tls', action='store_true', help='force HTTPS on')
    tls_group.add_argument('--no-tls', dest='tls', action='store_false', help='force plain HTTP (development only)')
    parser.set_defaults(tls=None)
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--demo', action='store_true', help='Loopback-only local SN demo with separate test data')
    parser.add_argument('--demo-video', type=Path, help='Existing video for the PC demo; never opens a local camera')
    args = parser.parse_args()
    DEMO = args.demo

    if args.demo and not VIDEO_TEST_ENABLED:
        raise SystemExit('Video test mode is disabled in config/edge.ini')

    # Config -> legacy env -> VisionEdge env -> CLI. New names win.
    host = os.environ.get('TM_EDGE_HOST', cfg.host)
    host = os.environ.get('VISIONEDGE_HOST', host)
    port = int(os.environ.get('TM_EDGE_PORT', cfg.port))
    port = int(os.environ.get('VISIONEDGE_PORT', port))
    threads = int(os.environ.get('TM_EDGE_THREADS', cfg.threads))
    threads = int(os.environ.get('VISIONEDGE_THREADS', threads))
    tls_enabled = cfg.tls
    env_tls = _env_bool('VISIONEDGE_TLS')
    if env_tls is not None:
        tls_enabled = env_tls
    if args.host is not None:
        host = args.host
    if args.port is not None:
        port = args.port
    if args.threads is not None:
        threads = args.threads
    if args.tls is not None:
        tls_enabled = args.tls
    if args.demo:
        host = args.host or '127.0.0.1'
        if host not in ('127.0.0.1', '::1', 'localhost'):
            raise SystemExit('Demo is restricted to loopback')
        os.environ.setdefault('APL_DATA_DIR', str(BASE_DIR / 'runtime_data' / 'demo'))
        EDGE.cfg.auto_start = bool(args.demo_video)
        if args.demo_video:
            path = args.demo_video.resolve(strict=True)
            if not path.is_file():
                raise SystemExit('Demo source must be an existing file')
            EDGE.cfg.backend = 'opencv'
            EDGE.cfg.source = str(path)
            EDGE.cfg.rotation = 0
            EDGE.cfg.loop_source = False
    elif args.demo_video:
        raise SystemExit('--demo-video requires --demo')

    try:
        tls_context = _build_tls_context(tls_enabled, cfg)
        _assert_port_available(host, port)
        _lock_instance(Path(os.environ.get('APL_DATA_DIR', str(BASE_DIR / 'runtime_data' / 'apl'))))
        station()
        from stream_proxy import register_stream_routes
        register_stream_routes(app, CONFIG_PATH)
    except Exception as exc:
        print(f'[VisionEdge] startup preflight failed: {exc}')
        raise SystemExit(2)

    scanner_cfg = json.loads((BASE_DIR / 'config' / 'apl_workflow.json').read_text(encoding='utf-8'))['scanner']
    from scanner_selection import ScannerSelection
    SCANNER = ScannerSelection(
        BASE_DIR / 'runtime_data' / 'barcode', scanner_cfg, scanner_scan,
        can_switch=lambda: station().status()['state'] == 'WAITING_SN',
    )
    SCANNER.start()

    # Camera auto-start happens only after TLS + port preflight succeeds.
    if EDGE.cfg.auto_start and not _shutdown_requested.is_set():
        result = EDGE.start()
        if not result.get('success'):
            print(f"[VisionEdge] camera auto-start failed: {result.get('error', 'unknown error')}")

    scheme = 'https' if tls_context is not None else 'http'
    print(f'VisionEdge: {scheme}://{host}:{port}/edge')
    print(f'Backend requested={EDGE.cfg.backend}; product_id={EDGE.cfg.product_id}')
    if tls_context is not None:
        print(f'[VisionEdge] TLS enabled (cert={Path(cfg.certfile).name}, TLS >= 1.2)')
    else:
        print('[VisionEdge] WARNING: TLS disabled; use only for development')

    print('APL DMZ mode: ' + station().status()['dmz']['label'])
    _serve(host, port, tls_context, threads)


def main():
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _request_shutdown)
    try:
        _main()
    finally:
        result = _shutdown()
        if not result.get('success'):
            raise SystemExit(70)


if __name__ == '__main__':
    main()
