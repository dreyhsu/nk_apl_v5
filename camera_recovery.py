"""Evidence-gated reacquisition; never relabel failed release as safe."""
import hashlib
import json
from pathlib import Path
import time


def server_identity():
    identities = []
    for path in Path('/proc').glob('[0-9]*'):
        try:
            if (path / 'comm').read_text().strip() != 'cam-server':
                continue
            stat = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            if stat[0] not in ('Z', 'X'):
                identities.append([int(path.name), stat[19]])
        except (OSError, IndexError):
            continue
    return sorted(identities)


def release_eligible(release, reaped, forced, acknowledged, frames, server):
    elements = release.get('camera_elements') or {}
    source = elements.get('camsrc') or {}
    return bool(reaped and not forced and acknowledged and frames >= 2 and server
                and release.get('camera_null') is False
                and release.get('camera_eos') is True
                and release.get('callbacks_drained') is True
                and release.get('recorder_null') is not False
                and release.get('recorder_finalized') is not False
                and source.get('current') == 'ready'
                and source.get('pending') == 'null'
                and len(elements) > 1
                and all(v.get('current') == 'null' for k, v in elements.items() if k != 'camsrc'))


def marker_ticket(paths, boot, camera):
    records = []
    hashes = {}
    for path in paths:
        raw = path.read_bytes()
        value = json.loads(raw)
        release = value.get('release') or {}
        if (not boot or value.get('boot_id') != boot
                or release.get('reopen_schema') != 1
                or release.get('camera_id') != camera
                or release.get('reopen_eligible') is not True
                or release.get('worker_reaped') is not True
                or release.get('forced_exit') is not False
                or not release.get('server_before')
                or not release_eligible(release, release.get('worker_reaped'),
                                        release.get('forced_exit'), release.get('worker_acknowledged'),
                                        release.get('received_frames', 0), release.get('server_before'))
                or not isinstance(value.get('pid'), int)
                or Path('/proc', str(value['pid'])).exists()):
            raise RuntimeError('Camera recovery evidence is incomplete; inspect logs before retry')
        records.append(value)
        hashes[str(path)] = hashlib.sha256(raw).hexdigest()
    if len(records) != 2 or records[0].get('session') != records[1].get('session'):
        raise RuntimeError('Camera recovery markers do not identify the same session')
    if records[0]['release'] != records[1]['release']:
        raise RuntimeError('Camera recovery evidence differs between markers')
    return dict(hashes=hashes, server_before=records[0]['release']['server_before'],
                previous_session=records[0]['session'], boot_id=boot)


def verify_ticket(ticket, paths, boot):
    if not ticket or ticket.get('boot_id') != boot:
        raise RuntimeError('Invalid camera recovery ticket')
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != ticket.get('hashes', {}).get(str(path)):
            raise RuntimeError('Camera recovery marker changed; reacquisition cancelled')
    identity = server_identity()
    if not identity or identity == ticket['server_before']:
        raise RuntimeError('cam-server has not restarted after failed release')


def wait_replacement(previous, cancel, timeout=60, identity=server_identity,
                     clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    stable = None
    since = clock()
    while clock() < deadline:
        if cancel():
            raise RuntimeError('Camera startup cancelled')
        current = identity()
        if current != stable:
            stable, since = current, clock()
        if current and current != previous and clock() - since >= 3:
            return current
        sleep(.1)
    raise RuntimeError('cam-server recovery timed out; no service restart was requested')
