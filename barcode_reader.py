"""Linux HID keyboard reader with dynamic discovery and Enter-delimited scans."""

import os
import select
import struct
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None


INPUT_EVENT = struct.Struct("@llHHi")
EVIOCGRAB = 0x40044590
KEY_ENTER = 28
LETTERS = {code: char for code, char in zip(
    [16,17,18,19,20,21,22,23,24,25,30,31,32,33,34,35,36,37,38,44,45,46,47,48,49,50],
    "qwertyuiopasdfghjklzxcvbnm")}
KEYS = {2:("1","!"),3:("2","@"),4:("3","#"),5:("4","$"),6:("5","%"),7:("6","^"),
        8:("7","&"),9:("8","*"),10:("9","("),11:("0",")"),12:("-","_"),13:("=","+"),
        26:("[","{"),27:("]","}"),39:(";",":"),40:("'",'"'),41:("`","~"),43:("\\","|"),
        51:(",","<"),52:(".",">"),53:("/","?"),57:(" "," "),
        71:("7","7"),72:("8","8"),73:("9","9"),75:("4","4"),76:("5","5"),77:("6","6"),
        79:("1","1"),80:("2","2"),81:("3","3"),82:("0","0"),83:(".",".")}


def normalize_hex_id(value):
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    number = int(text, 16)
    if not 0 <= number <= 0xFFFF:
        raise ValueError("USB ID is outside the 16-bit range")
    return f"{number:04x}"


def _device_identity(device):
    try:
        name = (device / "name").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        name = ""
    try:
        vendor = normalize_hex_id((device / "id" / "vendor").read_text())
        product = normalize_hex_id((device / "id" / "product").read_text())
        return vendor, product, name
    except (OSError, ValueError):
        pass

    try:
        resolved = device.resolve()
    except OSError:
        return "", "", name
    for parent in (resolved, *resolved.parents):
        try:
            vendor = normalize_hex_id((parent / "idVendor").read_text())
            product = normalize_hex_id((parent / "idProduct").read_text())
            return vendor, product, name
        except (OSError, ValueError):
            continue
    return "", "", name


def _supports_enter(device):
    try:
        words = (device / "capabilities" / "key").read_text().split()
        word_width = struct.calcsize("@L") * 2
        bits = int("".join(word.zfill(word_width) for word in words), 16)
        return bool(bits & (1 << KEY_ENTER))
    except (OSError, ValueError):
        return False


def discover_devices(vendor, product, name_contains="Honeywell",
                     sys_root=Path("/sys/class/input"), dev_root=Path("/dev/input")):
    vendor = normalize_hex_id(vendor)
    product = normalize_hex_id(product)
    name_filter = str(name_contains or "").strip().lower()

    def event_number(path):
        suffix = path.name[5:]
        return int(suffix) if suffix.isdigit() else 1 << 30

    for event in sorted(sys_root.glob("event*"), key=event_number):
        device = event / "device"
        found_vendor, found_product, found_name = _device_identity(device)
        if (found_vendor, found_product) != (vendor, product):
            continue
        if name_filter and name_filter not in found_name.lower():
            continue
        if not _supports_enter(device):
            continue
        yield {
            "path": dev_root / event.name,
            "name": found_name,
            "vendor_id": found_vendor,
            "product_id": found_product,
        }


class ScanDecoder:
    def __init__(self, timeout=1.0, max_length=128):
        self.timeout = max(0.05, float(timeout))
        self.max_length = max(1, int(max_length))
        self.reset()

    def reset(self):
        self.buffer = []
        self.shift = set()
        self.caps = False
        self.invalid = False
        self.last_key = None

    def feed(self, event_type, code, value, now=None):
        now = time.monotonic() if now is None else now
        if event_type == 0 and code == 3:
            self.reset()
            self.invalid = True
            return None
        if event_type != 1:
            return None
        if self.last_key is not None and now - self.last_key > self.timeout:
            incomplete = bool(self.buffer) or self.invalid
            self.reset()
            self.invalid = incomplete
        self.last_key = now
        if code in {42, 54}:
            if value == 1:
                self.shift.add(code)
            elif value == 0:
                self.shift.discard(code)
            return None
        if value != 1:
            return None
        if code == 58:
            self.caps = not self.caps
            return None
        if code in {28, 96}:
            text = "".join(self.buffer)
            accepted = text if text and not self.invalid else None
            self.buffer = []
            self.invalid = False
            return accepted
        if code == 14:
            if self.buffer:
                self.buffer.pop()
            return None
        if code in LETTERS:
            char = LETTERS[code]
            char = char.upper() if bool(self.shift) != self.caps else char
        elif code in KEYS:
            char = KEYS[code][int(bool(self.shift))]
        else:
            self.invalid = True
            return None
        if len(self.buffer) >= self.max_length:
            self.invalid = True
            return None
        self.buffer.append(char)
        return None


class BarcodeReader:
    def __init__(self, on_scan, config=None):
        self.on_scan = on_scan
        self.config = dict(config or {})
        self.decoder = ScanDecoder(self.config.get("inter_key_timeout", 1.0), self.config.get("max_length", 128))
        self.stop_event = threading.Event()
        self.reconnect_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = None
        self.current = {"enabled": bool(self.config.get("enabled", True)), "connected": False,
                        "running": False, "device": "", "last_error": "", "scan_count": 0, "last_scan_at": None}

    def status(self):
        with self.lock:
            return dict(self.current)

    def _update(self, **fields):
        with self.lock:
            self.current.update(fields)

    def start(self):
        if not self.current["enabled"]:
            return self.status()
        if not sys.platform.startswith("linux"):
            self._update(last_error="Direct HID input is supported on Linux SmartCam; use the web SN field on PC.")
            return self.status()
        if self.thread and self.thread.is_alive():
            return self.status()
        self.stop_event.clear()
        self.reconnect_event.clear()
        self.thread = threading.Thread(target=self._run, name="apl-barcode", daemon=True)
        self.thread.start()
        return self.status()

    def stop(self):
        self.stop_event.set()
        self.reconnect_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        return self.status()

    def reconnect(self):
        """Close and reopen the HID input after an inspection cycle ends."""
        if self.thread and self.thread.is_alive():
            self.reconnect_event.set()
        return self.status()

    def _discover(self):
        yield from discover_devices(
            self.config.get("vendor_id", "0c2e"),
            self.config.get("product_id", "0901"),
            self.config.get("name", "Honeywell"),
        )

    def _run(self):
        self._update(running=True)
        try:
            while not self.stop_event.is_set():
                device = next(self._discover(), None)
                if device is None:
                    vendor = str(self.config.get("vendor_id", "0c2e"))
                    product = str(self.config.get("product_id", "0901"))
                    self._update(
                        connected=False,
                        device="",
                        last_error=f"Honeywell keyboard {vendor}:{product} not found; check powered Hub and USB enumeration.",
                    )
                    self.stop_event.wait(1)
                    continue
                path = device["path"]
                fd = None
                grabbed = False
                reconnect_requested = False
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    if self.config.get("exclusive", True):
                        if fcntl is None:
                            raise OSError("Exclusive HID access is unavailable on this platform")
                        fcntl.ioctl(fd, EVIOCGRAB, 1)
                        grabbed = True
                    self.decoder.reset()
                    pending = bytearray()
                    label = device.get("name") or "HID keyboard"
                    self._update(connected=True, device=f"{path} ({label})", last_error="")
                    while not self.stop_event.is_set():
                        if self.reconnect_event.is_set():
                            reconnect_requested = True
                            break
                        ready, _, _ = select.select([fd], [], [], 0.25)
                        if not ready:
                            continue
                        raw = os.read(fd, INPUT_EVENT.size * 64)
                        if not raw:
                            raise OSError("HID device reached EOF")
                        pending.extend(raw)
                        while len(pending) >= INPUT_EVENT.size:
                            _, _, kind, code, value = INPUT_EVENT.unpack_from(pending)
                            del pending[:INPUT_EVENT.size]
                            sn = self.decoder.feed(kind, code, value)
                            if sn is not None:
                                try:
                                    reply = self.on_scan(sn, source="scanner")
                                    if isinstance(reply, dict) and reply.get("success") is False:
                                        self._update(last_error=str(reply.get("error", "Scan rejected")))
                                    else:
                                        with self.lock:
                                            self.current["scan_count"] += 1
                                            self.current["last_scan_at"] = time.time()
                                            self.current["last_error"] = ""
                                except Exception as exc:
                                    self._update(last_error="Scan rejected: " + str(exc)[:200])
                except PermissionError as exc:
                    self._update(connected=False, device=str(path), last_error="Permission denied reading " + str(path) + ": " + str(exc)[:160])
                except OSError as exc:
                    self._update(connected=False, device="", last_error="HID connection error: " + str(exc)[:200])
                finally:
                    self.decoder.reset()
                    if fd is not None:
                        if grabbed:
                            try:
                                fcntl.ioctl(fd, EVIOCGRAB, 0)
                            except OSError:
                                pass
                        os.close(fd)
                if reconnect_requested:
                    self.reconnect_event.clear()
                else:
                    self.stop_event.wait(1)
        finally:
            self._update(running=False, connected=False, device="")
