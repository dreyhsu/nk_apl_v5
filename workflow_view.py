"""Shared workflow labels and drawing commands for PC and SmartCam."""

STEP_LABELS = {
    'EMPTY_FIXTURE': 'Empty fixture',
    '50_PRODUCT_LOADED': 'Load 50-series product',
    '50_COVER_CLOSED': 'Close guide fixture',
    '50_COVER_CLOSED_CARD_PRESENT': 'Insert MCIO card',
    '50_COVER_OPEN_CARD_PRESENT': 'Open fixture - card installed',
}
PHASE_LABELS = {
    'WAITING_SN': 'Waiting for SN', 'SN_CONFIRM': 'Validating SN with SFIS',
    'WAITING_MCIO_SN': 'Waiting for MCIO card SN',
    'RUNNING': 'Inspecting', 'RESULT_CONFIRM': 'PASS - complete',
    'NG': 'NG - remove 30-series product', 'RECOVERY_REQUIRED': 'Reset required',
    'WAITING_DMZ': 'Validating SN', 'REPORTING': 'Saving result',
    'STATE_UNAVAILABLE': 'Status unavailable',
    'WAITING_CAPTURE': 'Ready for Capture & Inspect',
    'JUDGING': 'Inspecting captured frame',
    'NOT_MATCHED': 'Not matched - adjust and capture again',
    'PASS_CONFIRM': 'OK - confirm upload or terminate',
    'UPLOADING': 'Uploading to SIFI',
    'TERMINATED': 'Inspection terminated',
}
# While the automatic probe is running these phases must not tell the operator to
# press a button. Keyed the same way as STATION_HINTS_AUTO in static/apl_ui.js.
AUTO_PHASE_LABELS = {
    'WAITING_CAPTURE': 'Auto-inspecting - no button needed',
    'NOT_MATCHED': 'Not matched - auto-inspecting, adjust the product',
}
BG = (0.035, 0.06, 0.10, 0.90)
WHITE = (0.92, 0.95, 1.0, 1.0)
MUTED = (0.57, 0.64, 0.74, 1.0)
GREEN = (0.21, 0.83, 0.54, 1.0)
AMBER = (0.96, 0.77, 0.33, 1.0)
RED = (1.0, 0.40, 0.46, 1.0)
BLUE = (0.40, 0.72, 1.0, 1.0)


def auto_capture_line(auto):
    """One compact ASCII status line for the automatic probe. ASCII only: the
    OpenCV overlay draws with FONT_HERSHEY_SIMPLEX, which has no glyph for
    anything else and silently renders '?'."""
    timeout = _number(auto.get('timeout_sec'), 0.0)
    elapsed = max(0.0, _number(auto.get('elapsed'), 0.0))
    # No timeout configured means the probe never gives up, so count up instead.
    parts = ['%ds left' % max(0, int(round(timeout - elapsed)))] if timeout > 0 \
        else ['%ds' % int(elapsed)]
    parts.append('hit %d/%d' % (int(_number(auto.get('hits'), 0)),
                                max(1, int(_number(auto.get('required_hits'), 1)))))
    score, threshold = auto.get('score'), auto.get('threshold')
    if isinstance(score, (int, float)):
        target = ' / %.2f' % threshold if isinstance(threshold, (int, float)) else ''
        parts.append('score %.2f%s' % (score, target))
    return 'AUTO  ' + '  -  '.join(parts)


def auto_capture_remaining(auto):
    """Fraction of the timeout window still left, for the countdown bar."""
    timeout = _number(auto.get('timeout_sec'), 0.0)
    if timeout <= 0:
        return 1.0
    return max(0.0, min(1.0, (timeout - max(0.0, _number(auto.get('elapsed'), 0.0))) / timeout))


def _number(value, default):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def workflow_view(state, stale=False):
    sop = state.get('sop') or {}
    phase = 'STATE_UNAVAILABLE' if stale else state.get('state', 'WAITING_SN')
    auto = {} if stale else (state.get('auto_capture') or {})
    armed = auto.get('armed') is True
    steps = state.get('steps') or sop.get('steps') or []
    done = int(sop.get('done_count', state.get('step_index', 0)))
    rows = []
    for i, step in enumerate(steps):
        current_phases = {'RUNNING', 'SN_CONFIRM', 'WAITING_MCIO_SN', 'WAITING_CAPTURE', 'JUDGING', 'NOT_MATCHED'}
        status = 'PENDING' if stale else 'DONE' if i < done else 'CURRENT' if phase in current_phases and i == done else 'PENDING'
        if state.get('inspection_mode') == 'single_snapshot_cover_card' and step['name'] == '50_COVER_CLOSED_CARD_PRESENT':
            label = 'Closed cover + MCIO card'
        else:
            label = STEP_LABELS.get(step['name'], step['name'])
        rows.append({'label': label, 'status': status})
    accent = RED if phase in {'NG', 'RECOVERY_REQUIRED', 'NOT_MATCHED'} else GREEN if phase in {'RESULT_CONFIRM', 'PASS_CONFIRM'} else AMBER if phase in {'RUNNING', 'SN_CONFIRM', 'WAITING_MCIO_SN', 'WAITING_CAPTURE', 'JUDGING', 'UPLOADING'} else BLUE
    label = (armed and AUTO_PHASE_LABELS.get(phase)) or PHASE_LABELS.get(phase, phase)
    return {'phase': label, 'rows': rows, 'done': 0 if stale else done,
            'accent': accent, 'auto': auto, 'armed': armed}


def overlay_commands(state, width, height, stale=False):
    view = workflow_view(state, stale)
    match = state.get('matching') or {}
    commands = []
    scale = min(width / 1920.0, height / 1080.0)

    def rect(x, y, w, h, color, stroke=0):
        commands.append(('rect', x, y, w, h, color, stroke))

    def text(value, x, y, size, color=WHITE):
        commands.append(('text', str(value), x, y, size, color))

    stable = 'UNKNOWN' if stale else match.get('stable', 'UNKNOWN')
    raw = 'UNKNOWN' if stale else match.get('raw', 'UNKNOWN')
    rect(18*scale, 18*scale, 770*scale, (128 if view['armed'] else 82)*scale, BG)
    text('Stable: ' + stable, 34*scale, 50*scale, 24*scale)
    text('Raw: ' + raw, 34*scale, 83*scale, 22*scale, MUTED)
    if view['armed']:
        text(auto_capture_line(view['auto']), 34*scale, 114*scale, 22*scale, AMBER)
        bar = 738*scale
        rect(34*scale, 122*scale, bar, 7*scale, (0.20, 0.17, 0.10, 0.95))
        rect(34*scale, 122*scale, bar*auto_capture_remaining(view['auto']), 7*scale, AMBER)
    for key, color, stroke in [('roi', AMBER, 2), ('box', RED if raw.startswith('30_') else GREEN, 3)]:
        box = match.get(key)
        if box and len(box) == 4 and (key == 'roi' or not stale and raw != 'UNKNOWN'):
            rect(*box, color, stroke)

    w, row_h = 600*scale, 54*scale
    x, y = width - w - 18*scale, 18*scale
    h = (148 + 54*len(view['rows']))*scale
    rect(x, y, w, h, BG)
    rect(x, y, 4*scale, h, view['accent'])
    text('WORKFLOW', x+24*scale, y+36*scale, 25*scale)
    text(f"{view['done']} / {len(view['rows'])}", x+w-92*scale, y+36*scale, 23*scale, view['accent'])
    text(view['phase'], x+24*scale, y+74*scale, 25*scale, view['accent'])
    sn = str(state.get('sn') or '-')
    text('SN: ' + (sn if len(sn) <= 35 else sn[:32]+'...'), x+24*scale, y+110*scale, 22*scale, MUTED)
    for i, row in enumerate(view['rows']):
        top = y+(130+i*54)*scale
        current, done = row['status'] == 'CURRENT', row['status'] == 'DONE'
        color = GREEN if done else AMBER if current else MUTED
        if current:
            rect(x+12*scale, top, w-24*scale, row_h, (0.20, 0.17, 0.10, 0.95))
        text('OK' if done else f'{i+1:02}', x+24*scale, top+35*scale, 23*scale, color)
        text(row['label'], x+84*scale, top+35*scale, 23*scale, color)
    return commands


def draw_opencv(frame, state):
    import cv2
    vis = frame.copy()
    for command in overlay_commands(state, frame.shape[1], frame.shape[0]):
        if command[0] == 'rect':
            _, x, y, w, h, color, stroke = command
            x1, y1 = max(0, int(x)), max(0, int(y))
            x2, y2 = min(vis.shape[1], int(x+w)), min(vis.shape[0], int(y+h))
            bgr = tuple(int(c*255) for c in reversed(color[:3]))
            if stroke:
                cv2.rectangle(vis, (x1,y1), (x2,y2), bgr, stroke)
            elif x2 > x1 and y2 > y1:
                region = vis[y1:y2, x1:x2]
                fill = region.copy()
                fill[:] = bgr
                cv2.addWeighted(fill, color[3], region, 1-color[3], 0, region)
        else:
            _, value, x, y, size, color = command
            cv2.putText(vis, value, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX,
                        size/32, tuple(int(c*255) for c in reversed(color[:3])),
                        max(1, round(size/16)), cv2.LINE_AA)
    return vis
