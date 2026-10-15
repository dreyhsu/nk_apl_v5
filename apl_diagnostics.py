"""Bounded, opt-in telemetry. Never write files from streaming callbacks."""
from __future__ import annotations

import atexit
import collections
import contextlib
import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import threading
import time


def enabled():
    return os.environ.get('APL_DIAGNOSTICS') == '1'


def identity():
    return {'enabled': enabled(), 'run_id': os.environ.get('APL_DIAGNOSTIC_RUN', '')}


def sanitize_browser(payload):
    """Bound diagnostic values; never retain SDP or arbitrary request bodies."""
    allowed = {'event', 'at', 'reason', 'generation', 'connection', 'ice', 'message',
               'status', 'frame_seq', 'fps', 'api_age_ms', 'api_error', 'decoded_age_ms',
               'packetsReceived', 'bytesReceived', 'framesReceived', 'framesDecoded',
               'keyFramesDecoded', 'packetsLost', 'jitter', 'freezeCount', 'nackCount',
               'pliCount', 'readyState', 'paused', 'currentTime', 'visibility', 'run_id',
               'tab', 'protocol', 'candidateType', 'currentRoundTripTime', 'availableIncomingBitrate'}
    def clean(item):
        return {k: (v[:256] if isinstance(v, str) else v) for k, v in item.items()
                if k in allowed and (v is None or isinstance(v, (str, int, float, bool)))}
    result = clean(payload)
    result['events'] = [clean(v) for v in payload.get('events', [])[:20] if isinstance(v, dict)] if isinstance(payload.get('events', []), list) else []
    return result


class Recorder:
    def __init__(self, directory):
        self.items = queue.Queue(maxsize=2048)
        self.lock = threading.Lock()
        self.counters = {}
        self.timings = {}
        self.dropped = 0
        self.stop = threading.Event()
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.handler = RotatingFileHandler(
            Path(directory) / ('events-%s.jsonl' % os.getpid()),
            maxBytes=4 * 1024 * 1024, backupCount=3, encoding='utf-8')
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.thread = threading.Thread(target=self._write, daemon=True, name='apl-diagnostics')
        self.thread.start()
        atexit.register(self.close)

    def emit(self, event, **fields):
        entry = dict(fields, event=event, utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     monotonic=time.monotonic(), pid=os.getpid(),
                     run_id=os.environ.get('APL_DIAGNOSTIC_RUN', ''))
        try:
            self.items.put_nowait(entry)
        except queue.Full:
            self.dropped += 1

    def count(self, name, amount=1, size=0, pts=None):
        with self.lock:
            value = self.counters.setdefault(name, {'total': 0, 'bytes': 0})
            value.update(total=value['total'] + amount, bytes=value['bytes'] + size,
                         last_at=time.monotonic(), pts=pts)

    def observe(self, name, ms):
        with self.lock:
            value = self.timings.setdefault(name, collections.deque(maxlen=2048))
            value.append(ms)

    def _summary(self, previous, previous_at):
        now = time.monotonic()
        with self.lock:
            counters = {k: dict(v) for k, v in self.counters.items()}
            timings = {k: list(v) for k, v in self.timings.items()}
            self.timings.clear()
        for name, value in counters.items():
            value['rate'] = (value['total'] - previous.get(name, 0)) / max(.001, now - previous_at)
            value['age_seconds'] = now - value.pop('last_at')
        summary = {}
        for name, values in timings.items():
            if values:
                values.sort()
                summary[name] = {'count': len(values), 'mean_ms': sum(values) / len(values),
                                 'max_ms': values[-1], 'p95_ms': values[min(len(values)-1, int(len(values)*.95))],
                                 'over_200ms': sum(v > 200 for v in values)}
        self.emit('HEARTBEAT', counters=counters, timings=summary,
                  dropped_log_events=self.dropped)
        return {k: v['total'] for k, v in counters.items()}, now

    def _write(self):
        previous, at = {}, time.monotonic()
        while not self.stop.is_set() or not self.items.empty():
            try:
                entry = self.items.get(timeout=.25)
            except queue.Empty:
                entry = None
            if entry is not None:
                try:
                    message = json.dumps(entry, ensure_ascii=False, default=str)
                    self.handler.emit(logging.LogRecord('diagnostics', logging.INFO, '', 0, message, (), None))
                except Exception:
                    self.dropped += 1
            if not self.stop.is_set() and time.monotonic() - at >= 5:
                previous, at = self._summary(previous, at)

    def close(self):
        if self.stop.is_set():
            return
        self.emit('DIAGNOSTICS_END', dropped_log_events=self.dropped)
        self.stop.set()
        self.thread.join(timeout=2)
        if not self.thread.is_alive():
            self.handler.close()


_recorder = None
_init_lock = threading.Lock()
_failed = False


def recorder():
    global _recorder, _failed
    if not enabled() or _failed:
        return None
    if _recorder is None:
        with _init_lock:
            if _recorder is None:
                try:
                    directory = os.environ.get('APL_DIAGNOSTIC_DIR')
                    if not directory:
                        directory = Path(__file__).parent / 'runtime_data' / 'diagnostics' / 'manual'
                    _recorder = Recorder(directory)
                except Exception as exc:
                    _failed = True
                    logging.getLogger('camera.lifecycle').warning('Diagnostics unavailable: %s', exc)
    return _recorder


def emit(event, **fields):
    value = recorder()
    if value:
        value.emit(event, **fields)


def count(name, amount=1, size=0, pts=None):
    value = recorder()
    if value:
        value.count(name, amount, size, pts)


def observe(name, ms):
    value = recorder()
    if value:
        value.observe(name, ms)


@contextlib.contextmanager
def measure(event, budget_ms=200.0, **fields):
    if not enabled():
        yield
        return
    started = time.perf_counter()
    error = None
    try:
        yield
    except Exception as exc:
        error = '%s: %s' % (type(exc).__name__, exc)
        raise
    finally:
        ms = (time.perf_counter() - started) * 1000
        observe(event, ms)
        emit(event, elapsed_ms=ms, budget_ms=budget_ms, exceeds_budget=ms > budget_ms,
             exceeds_5fps_budget=ms > 200, error=error, **fields)


def attach_gst(Gst, elements, queues):
    """Counts at pads are observations, not proof of delivery to MediaMTX."""
    if not enabled():
        return
    recorder()
    for name, element in elements.items():
        try:
            pad = element.get_static_pad('src')
            def probe(pad, info, label=name):
                try:
                    if info.type & Gst.PadProbeType.BUFFER_LIST:
                        buffers = info.get_buffer_list()
                        count(label, buffers.length(), sum(buffers.get(i).get_size() for i in range(buffers.length())))
                    else:
                        buffer = info.get_buffer()
                        if buffer is not None:
                            count(label, size=buffer.get_size(), pts=int(buffer.pts))
                except Exception:
                    count('probe_errors')
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, probe)
            emit('PROBE_ATTACHED', stage=name, element=element.get_name())
        except Exception as exc:
            emit('PROBE_UNAVAILABLE', stage=name, error=str(exc))
    for element, _ in queues:
        try:
            label = element.get_name()
            element.connect('overrun', lambda elem, name=label: count('queue_overrun:' + name))
        except Exception as exc:
            emit('QUEUE_PROBE_UNAVAILABLE', error=str(exc))
