"""Remember verified SmartCam USB scanners without changing the station SN flow."""

import json
import os
import sys
import threading
from pathlib import Path

from barcode_reader import BarcodeReader, _device_identity, _supports_enter, normalize_hex_id


IDENTITY_FIELDS = ('vendor_id', 'product_id', 'name', 'serial', 'phys')


def _read_text(path):
    try:
        return path.read_text(encoding='utf-8', errors='replace').strip()
    except OSError:
        return ''


def devices(sys_root=Path('/sys/class/input'), dev_root=Path('/dev/input')):
    """Return USB keyboard candidates; a candidate is not yet a scanner."""
    found = []
    for event in sorted(sys_root.glob('event*')):
        node = event / 'device'
        if _read_text(node / 'id/bustype') != '0003' or not _supports_enter(node):
            continue
        vendor, product, name = _device_identity(node)
        if not vendor or not product:
            continue
        try:
            resolved = node.resolve()
        except OSError:
            continue
        serial = ''
        for parent in (resolved, *resolved.parents):
            if (parent / 'idVendor').exists():
                serial = _read_text(parent / 'serial')
                break
        found.append({
            'path': str(dev_root / event.name), 'name': name,
            'vendor_id': vendor, 'product_id': product,
            'serial': serial, 'phys': _read_text(node / 'phys'),
        })
    return found


def _identity(device):
    return {key: device[key] for key in IDENTITY_FIELDS}


def _valid_identity(value):
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str) for key in IDENTITY_FIELDS):
        raise ValueError('Invalid scanner identity')
    identity = _identity(value)
    identity['vendor_id'] = normalize_hex_id(identity['vendor_id'])
    identity['product_id'] = normalize_hex_id(identity['product_id'])
    return identity


def _same_model(device, selected):
    return all(device.get(key, '') == selected.get(key, '') for key in ('vendor_id', 'product_id', 'name', 'serial'))


def _same_identity(device, selected):
    return _same_model(device, selected) and (bool(selected.get('serial')) or device.get('phys', '') == selected.get('phys', ''))


def _candidate_for(identity, candidates, known):
    """Resolve a known scanner without guessing between identical serial-less units."""
    matches = [candidate for candidate in candidates if _same_model(candidate, identity)]
    if identity['serial']:
        return matches[0] if len(matches) == 1 else None
    exact = [candidate for candidate in matches if candidate['phys'] == identity['phys']]
    if len(exact) == 1:
        return exact[0]
    same_known = [item for item in known if _same_model(item, identity)]
    return matches[0] if len(matches) == 1 and len(same_known) == 1 else None


class SelectedReader(BarcodeReader):
    def _discover(self):
        matches = [device for device in devices() if _same_identity(device, self.config)]
        if len(matches) == 1:
            yield matches[0]

    def _update(self, **fields):
        if str(fields.get('last_error', '')).startswith('Honeywell keyboard'):
            fields['last_error'] = 'Waiting for the remembered scanner; known scanners reconnect automatically.'
        super()._update(**fields)


class ScannerSelection:
    def __init__(self, root, config, on_scan, can_switch=None):
        self.root = Path(root)
        self.config = dict(config)
        self.on_scan = on_scan
        self.can_switch = can_switch or (lambda: True)
        self.enabled = bool(config.get('enabled', True))
        self.platform_supported = sys.platform.startswith('linux')
        self.selection_file = self.root / 'scanner_selection.json'
        self.control = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.reader = None
        self.selected = None
        self.known = []
        self.pending_known = None
        self.saved_active = None
        self.persisted_version = 0
        self.missing_checks = 0
        self.selection_source = ''
        self.error = ''
        self.invalid_selection = False
        try:
            selection = json.loads(self.selection_file.read_text(encoding='utf-8'))
            if not isinstance(selection, dict):
                raise ValueError('Invalid scanner selection')
            if selection.get('version') == 2:
                self.persisted_version = 2
                entries = selection.get('known')
                if not isinstance(entries, list) or len(entries) > 32:
                    raise ValueError('Invalid known scanner list')
                self.known = [_valid_identity(entry) for entry in entries]
                if selection.get('active') is not None:
                    self.selected = _valid_identity(selection['active'])
            elif 'version' not in selection:
                self.persisted_version = 1
                self.selected = _valid_identity(selection)
                self.known = [dict(self.selected)]
            else:
                raise ValueError('Unsupported scanner selection version')
            if self.selected:
                self.saved_active = dict(self.selected)
                self.selection_source = 'saved'
                self.pending_known = next((item for item in self.known if _same_identity(item, self.selected)), None)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            self.invalid_selection = True
            self.error = 'Cannot load scanner selection: ' + str(exc)

    def _start_reader(self):
        if self.enabled and self.platform_supported and self.selected:
            self.reader = SelectedReader(self.on_scan, dict(self.config, **self.selected, enabled=True, exclusive=True))
            self.reader.start()

    def _save(self, known, active):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.selection_file.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump({'version': 2, 'known': known, 'active': active}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.selection_file)

    def _remember_connected(self, candidates):
        if not self.selected or not self.reader or not self.reader.status().get('connected'):
            return
        resolved = _candidate_for(self.selected, candidates, self.known or [self.selected])
        if resolved is None or not _same_identity(resolved, self.selected):
            return
        active = _identity(resolved)
        known = [item for item in self.known if item != self.pending_known]
        if active not in known:
            known.append(active)
        if len(known) > 32:
            self.error = 'Scanner connected but the remembered scanner list is full'
            return
        if known != self.known or active != self.saved_active or self.persisted_version != 2:
            try:
                self._save(known, active)
            except OSError as exc:
                self.error = 'Scanner connected but cannot save its identity: ' + str(exc)
                return
        self.known = known
        self.selected = active
        self.pending_known = active
        self.saved_active = active
        self.persisted_version = 2
        self.error = ''

    def _activate(self, device, source, known_identity=None):
        selection = _identity(device)
        if self.selected == selection and self.reader is not None:
            if not self.reader.status().get('connected'):
                self.reader.reconnect()
            return
        if self.reader:
            self.reader.stop()
            if self.reader.thread and self.reader.thread.is_alive():
                raise ValueError('Previous scanner is still stopping; retry shortly')
        self.selected = selection
        self.pending_known = known_identity
        self.selection_source = source
        self.invalid_selection = False
        self.error = ''
        self.missing_checks = 0
        self._start_reader()
        self._remember_connected(devices())

    def _auto_select(self):
        if self.invalid_selection or not self.enabled or not self.platform_supported:
            return
        candidates = devices()
        self._remember_connected(candidates)
        if self.reader and self.reader.status().get('connected'):
            exact = [item for item in candidates if _same_identity(item, self.selected)]
            self.missing_checks = 0 if len(exact) == 1 else self.missing_checks + 1
            if self.missing_checks >= 2:
                self.reader.reconnect()
            return
        self.missing_checks = 0
        if self.selected:
            same = _candidate_for(self.selected, candidates, self.known or [self.selected])
            if same is not None:
                if _identity(same) != self.selected and self.can_switch():
                    self._activate(same, 'reconnected', self.pending_known)
                return
        matches = []
        for item in self.known:
            candidate = _candidate_for(item, candidates, self.known)
            if candidate and not any(existing[0]['path'] == candidate['path'] for existing in matches):
                matches.append((candidate, item))
        if matches:
            if len(matches) == 1 and self.can_switch():
                self._activate(matches[0][0], 'remembered', matches[0][1])
            elif len(matches) > 1:
                self.error = 'Several remembered scanners are present; select the intended scanner.'
            return
        if self.selected or self.known:
            return
        try:
            vendor = normalize_hex_id(self.config.get('vendor_id', '0c2e'))
            product = normalize_hex_id(self.config.get('product_id', '0901'))
        except ValueError:
            vendor = product = ''
        name = str(self.config.get('name', '')).lower()
        configured = [device for device in candidates if device['vendor_id'] == vendor
                      and device['product_id'] == product and name in device['name'].lower()]
        chosen = configured[0] if len(configured) == 1 else candidates[0] if len(candidates) == 1 else None
        if chosen and self.can_switch():
            self._activate(chosen, 'automatic')
        elif len(candidates) > 1:
            self.error = 'Multiple USB keyboard devices found; open scanner settings and choose one.'

    def _monitor(self):
        while not self.stop_event.wait(1):
            with self.control:
                try:
                    self._auto_select()
                except (OSError, ValueError) as exc:
                    self.error = 'Scanner reconnect failed: ' + str(exc)

    def start(self):
        with self.control:
            if not self.enabled or not self.platform_supported:
                return
            if self.selected:
                self._start_reader()
            try:
                self._auto_select()
            except (OSError, ValueError) as exc:
                self.error = 'Scanner reconnect pending: ' + str(exc)
            if self.thread is None or not self.thread.is_alive():
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._monitor, name='apl-scanner-discovery', daemon=True)
                self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        with self.control:
            if self.reader:
                self.reader.stop()

    def select(self, path):
        with self.control:
            if not self.enabled or not self.platform_supported:
                raise ValueError('SmartCam USB scanner selection is unavailable on this host')
            candidates = devices()
            chosen = next((device for device in candidates if device['path'] == path), None)
            if chosen is None:
                raise ValueError('Device is no longer available; use Find Devices again')
            if self.selected != _identity(chosen) and not self.can_switch():
                raise ValueError('Finish the current inspection before changing scanners')
            known = next((item for item in self.known
                          if (resolved := _candidate_for(item, candidates, self.known)) is not None
                          and resolved['path'] == path), None)
            self._activate(chosen, 'manual', known)

    def reconnect(self):
        with self.control:
            if self.reader:
                self.reader.reconnect()

    def status(self):
        with self.control:
            state = self.reader.status() if self.reader else {}
            return {
                'enabled': self.enabled, 'platform_supported': self.platform_supported,
                'configured': self.selected is not None, 'selected': dict(self.selected or {}),
                'known_count': len(self.known), 'selection_source': self.selection_source,
                'connected': state.get('connected', False), 'device': state.get('device', ''),
                'scan_count': state.get('scan_count', 0), 'last_scan_at': state.get('last_scan_at'),
                'last_error': self.error or state.get('last_error', ''),
            }
