"""PATLITE LR5-LAN signal tower over the PNS protocol (TCP, default port 10000).

The camera thread only ever calls PatliteTower.request(), which stores the wanted
pattern and returns. One daemon worker owns the socket, so a tower that is off the
network costs the run loop nothing."""

import logging
import socket
import struct
import threading

log = logging.getLogger('camera.light_tower')

PNS_PRODUCT_ID = b'AB'
PNS_RUN_CONTROL_COMMAND = b'S'
PNS_CLEAR_COMMAND = b'C'
PNS_ACK = 0x06
PNS_NAK = 0x15

# LED: off 0, on 1, blink slow/medium/high 2-4, flash single/double/triple 5-7, no change 9.
LED_PATTERNS = {0, 1, 2, 3, 4, 5, 6, 7, 9}
# Buzzer: stop 0, ring 1, no change 9.
BUZZER_MODES = {0, 1, 9}
CHANNELS = ('red', 'amber', 'green', 'blue', 'white', 'buzzer')

EVENTS = ('camera_on', 'auto_capture_start', 'judge_timeout', 'judge_pass')
# docs/plan/LIGHT_TOWER_SCHEMA_20261019_ZH.md
DEFAULT_EVENTS = {
    'camera_on':          (0, 0, 2, 0, 0, 0),
    'auto_capture_start': (0, 2, 0, 0, 0, 0),
    'judge_timeout':      (4, 0, 0, 0, 0, 0),
    'judge_pass':         (0, 0, 1, 0, 0, 0),
}
LIGHT_TOWER_DEFAULTS = {'enabled': False, 'host': '192.168.10.1', 'port': 10000,
                        'timeout_sec': 1.0, 'events': dict(DEFAULT_EVENTS)}


def run_control_packet(pattern):
    """PNS operation control: header + [red, amber, green, blue, white, buzzer]."""
    return struct.pack('>2ssxH', PNS_PRODUCT_ID, PNS_RUN_CONTROL_COMMAND, 6) + bytes(pattern)


def clear_packet():
    """PNS clear: every LED off and the buzzer stopped."""
    return struct.pack('>2ssxH', PNS_PRODUCT_ID, PNS_CLEAR_COMMAND, 0)


def _validated_event(name, raw):
    if not isinstance(raw, dict):
        raise ValueError('light_tower.events.%s must be an object' % name)
    unknown = set(raw) - set(CHANNELS) - {'comment'}
    if unknown:
        raise ValueError('light_tower.events.%s has unsupported keys: %s' % (name, ', '.join(sorted(unknown))))
    if not isinstance(raw.get('comment', ''), str):
        raise ValueError('light_tower.events.%s.comment must be text' % name)
    pattern = []
    for channel in CHANNELS:
        value = raw.get(channel, 0)
        allowed = BUZZER_MODES if channel == 'buzzer' else LED_PATTERNS
        if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
            raise ValueError('light_tower.events.%s.%s must be one of %s'
                             % (name, channel, ', '.join(str(v) for v in sorted(allowed))))
        pattern.append(value)
    return tuple(pattern)


def validated_light_tower(raw):
    """Normalize the station config's light_tower block. Everything defaults to off,
    so a config without the block never opens a socket."""
    raw = {} if raw is None else raw
    if not isinstance(raw, dict):
        raise ValueError('light_tower must be an object')
    unknown = set(raw) - {'enabled', 'host', 'port', 'timeout_sec', 'events'}
    if unknown:
        raise ValueError('light_tower has unsupported keys: ' + ', '.join(sorted(unknown)))
    enabled = raw.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('light_tower.enabled must be a boolean (true/false)')
    host = raw.get('host', LIGHT_TOWER_DEFAULTS['host'])
    if not isinstance(host, str) or not host.strip() or any(c.isspace() for c in host.strip()):
        raise ValueError('light_tower.host must be an IP address or host name')
    port = raw.get('port', LIGHT_TOWER_DEFAULTS['port'])
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('light_tower.port must be a whole number between 1 and 65535')
    timeout = raw.get('timeout_sec', LIGHT_TOWER_DEFAULTS['timeout_sec'])
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0.2 <= timeout <= 10.0:
        raise ValueError('light_tower.timeout_sec must be between 0.2 and 10 seconds')
    events_raw = raw.get('events') or {}
    if not isinstance(events_raw, dict):
        raise ValueError('light_tower.events must be an object')
    unknown = set(events_raw) - set(EVENTS)
    if unknown:
        raise ValueError('light_tower.events has unsupported events: ' + ', '.join(sorted(unknown)))
    events = dict(DEFAULT_EVENTS)
    for name, value in events_raw.items():
        events[name] = _validated_event(name, value)
    return {'enabled': enabled, 'host': host.strip(), 'port': port,
            'timeout_sec': float(timeout), 'events': events}


def select_event(camera_live, state, auto_armed, timeout_fired):
    """The tower event for this moment, or None for a dark tower.

    judge_timeout is only the automatic-capture window running out: a failed manual
    capture, or RECOVERY_REQUIRED, falls back to camera_on."""
    if not camera_live:
        return None
    if state in {'PASS_CONFIRM', 'UPLOADING'}:
        return 'judge_pass'
    if state == 'NOT_MATCHED' and timeout_fired:
        return 'judge_timeout'
    if state == 'JUDGING' or (state == 'WAITING_CAPTURE' and auto_armed):
        return 'auto_capture_start'
    return 'camera_on'


class PatliteTower:
    """Latest-wins PNS sender. request() never touches the network."""

    def __init__(self, host, port, timeout_sec=1.0, retry_sec=2.0, connect=None):
        self.host, self.port = host, int(port)
        self.timeout_sec = float(timeout_sec)
        self.retry_sec = float(retry_sec)
        self._connect = connect or socket.create_connection
        self._cond = threading.Condition()
        self._wanted = None       # (event, packet); event None means clear
        self._sent = None
        self._stopping = False
        self._sock = None
        self.connected = False
        self.error = ''
        self._thread = threading.Thread(target=self._worker, daemon=True, name='patlite-tower')
        self._thread.start()

    @property
    def settings(self):
        return (self.host, self.port, self.timeout_sec)

    def request(self, event, pattern=None):
        """Ask for an event's pattern, or event=None for a dark tower."""
        wanted = (event, run_control_packet(pattern) if event is not None else clear_packet())
        with self._cond:
            if self._stopping or wanted == self._wanted:
                return
            self._wanted = wanted
            self._cond.notify_all()

    def status(self):
        with self._cond:
            return {'enabled': True, 'host': self.host, 'port': self.port,
                    'connected': self.connected,
                    'event': self._sent[0] if self._sent else None,
                    'error': self.error}

    def close(self, timeout=2.0):
        """Turn the tower dark (best effort) and stop the worker."""
        with self._cond:
            if not self._stopping:
                self._wanted = (None, clear_packet())
                self._stopping = True
                self._cond.notify_all()
        self._thread.join(timeout=timeout)

    def _worker(self):
        while True:
            with self._cond:
                while self._wanted == self._sent and not self._stopping:
                    self._cond.wait()
                if self._wanted == self._sent:
                    break
                wanted, stopping = self._wanted, self._stopping
            ok = self._send(wanted[1])
            with self._cond:
                if ok:
                    if self.error:
                        log.info('[PATLITE] %s:%s reachable again', self.host, self.port)
                    self._sent, self.connected, self.error = wanted, True, ''
                    if stopping:
                        break
                    continue
                if stopping:
                    break
                # Forget what the tower shows so the wanted pattern is resent once it is back.
                self._sent = None
                self._cond.wait(self.retry_sec)
        self._close_socket()

    def _send(self, packet):
        reused = self._sock is not None
        try:
            self._exchange(packet)
            return True
        except (OSError, ValueError) as exc:
            self._close_socket()
            if reused:
                # The tower may have dropped an idle connection; one fresh attempt.
                try:
                    self._exchange(packet)
                    return True
                except (OSError, ValueError) as retry_exc:
                    self._close_socket()
                    exc = retry_exc
            with self._cond:
                if not self.error:
                    log.warning('[PATLITE] %s:%s command failed: %s', self.host, self.port, exc)
                self.error, self.connected = str(exc) or exc.__class__.__name__, False
            return False

    def _exchange(self, packet):
        if self._sock is None:
            self._sock = self._connect((self.host, self.port), timeout=self.timeout_sec)
            self._sock.settimeout(self.timeout_sec)
        self._sock.sendall(packet)
        reply = self._sock.recv(1024)
        if not reply:
            raise ConnectionError('connection closed by tower')
        if reply[0] == PNS_NAK:
            raise ValueError('negative acknowledge')
        if reply[0] != PNS_ACK:
            raise ValueError('unexpected reply 0x%02x' % reply[0])

    def _close_socket(self):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
