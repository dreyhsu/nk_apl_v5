"""Small thread-safe Cairo overlay for the APL camera output."""
from __future__ import annotations

import copy
import math
import threading
import time


def rectangle(value):
    """Normalize an APL x/y/width/height rectangle."""
    if isinstance(value, dict):
        value = [value.get('x', 0), value.get('y', 0),
                 value.get('width', value.get('w', 0)),
                 value.get('height', value.get('h', 0))]
    try:
        x, y, width, height = [float(v) for v in value]
        if not all(math.isfinite(v) for v in (x, y, width, height)) or width <= 0 or height <= 0:
            return None
        return x, y, width, height
    except (TypeError, ValueError):
        return None


def overlay_content(state, stale=False):
    """Build display content without importing Cairo or GStreamer."""
    match = state.get('matching') or {}
    sop = state.get('sop') or {}
    phase = str(state.get('state', state.get('phase', 'WAITING_SN')))
    stable = str(match.get('stable', state.get('stable', 'UNKNOWN')))
    raw = str(match.get('raw', state.get('raw', 'UNKNOWN')))
    if stale:
        phase, stable, raw = 'STATE_UNAVAILABLE', 'UNKNOWN', 'UNKNOWN'
    box = rectangle(match.get('box', state.get('match_box')))
    if stale or raw in ('UNKNOWN', '', 'None'):
        box = None
    alarm = sop.get('alarm') or {}
    color = (1.0, 0.2, 0.2) if alarm.get('active') or 'ALARM' in phase else (0.2, 1.0, 0.45)
    steps = sop.get('steps') or []
    lines = []
    for index, step in enumerate(steps[:8]):
        if not isinstance(step, dict):
            continue
        label = step.get('name') or step.get('label') or step.get('id') or f'Step {index + 1}'
        done = step.get('status') == 'DONE' or step.get('complete', step.get('done', False))
        lines.append(f'{index + 1}. {"OK" if done else "--"} {label}')
    if not lines:
        lines = [f'Workflow: {sop.get("done_count", 0)} / {sop.get("total_required", 4)}']
    dmz = state.get('dmz') or {}
    dmz_label = str(dmz.get('label') or dmz.get('mode') or 'unavailable')
    return {'stable': stable, 'raw': raw, 'phase': phase,
            'sn': str(state.get('sn') or '-'), 'box': box,
            'roi': rectangle(match.get('roi', state.get('roi'))),
            'color': color, 'steps': lines, 'dmz': dmz_label}


class OverlayRenderer:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self._state = {}
        self._updated = 0.0
        self._state_lock = threading.Lock()
        self._draw_lock = threading.Lock()
        self._stopping = threading.Event()
        self._element = None
        self._handler = None

    def update(self, state):
        with self._state_lock:
            self._state = copy.deepcopy(state or {})
            self._updated = time.monotonic()

    def attach(self, element):
        self._element = element
        self._handler = element.connect('draw', self.draw)

    def prevent_new_draws(self):
        self._stopping.set()

    def stop(self, timeout=3.0):
        self.prevent_new_draws()
        if not self._draw_lock.acquire(timeout=timeout):
            return False
        try:
            if self._element is not None and self._handler is not None:
                self._element.disconnect(self._handler)
            self._element, self._handler = None, None
            return True
        finally:
            self._draw_lock.release()

    @staticmethod
    def _text(context, text, x, y, size=24):
        context.set_font_size(size)
        context.move_to(x, y)
        context.show_text(str(text)[:100])

    def draw(self, element, context, timestamp, duration):
        if self._stopping.is_set() or not self._draw_lock.acquire(blocking=False):
            return
        saved = False
        try:
            if self._stopping.is_set():
                return
            with self._state_lock:
                state = self._state
                stale = not self._updated or time.monotonic() - self._updated > 3.0
            from workflow_view import overlay_commands
            context.save()
            saved = True
            context.select_font_face('DejaVu Sans', 0, 0)
            for command in overlay_commands(state, self.width, self.height, stale):
                if command[0] == 'rect':
                    _, x, y, w, h, color, stroke = command
                    context.set_source_rgba(*color)
                    context.rectangle(x, y, w, h)
                    if stroke:
                        context.set_line_width(stroke)
                        context.stroke()
                    else:
                        context.fill()
                else:
                    _, text, x, y, size, color = command
                    context.set_source_rgba(*color)
                    self._text(context, text, x, y, size)
        except Exception:
            # Native streaming callbacks must never propagate Python exceptions.
            pass
        finally:
            if saved:
                context.restore()
            self._draw_lock.release()
