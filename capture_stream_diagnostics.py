#!/usr/bin/env python3
"""Read-only capture of running camera and MediaMTX service evidence."""

import argparse
import datetime
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time


ROOT = Path(__file__).resolve().parent


def read_text(path, limit=16000):
    try:
        return Path(path).read_text(errors='replace')[:limit]
    except OSError as exc:
        return 'UNAVAILABLE: ' + str(exc)


def sample_system(diag):
    payload = {name: read_text('/proc/' + name)
               for name in ('loadavg', 'meminfo', 'stat', 'net/snmp', 'net/dev')}
    payload['thermal'] = {str(path): read_text(path)
                          for path in Path('/sys/class/thermal').glob('thermal_zone*/temp')}
    processes = []
    for entry in Path('/proc').glob('[0-9]*'):
        name = read_text(entry / 'comm', 80).strip()
        if name in {'mediamtx', 'cam-server', 'python3'}:
            processes.append({'pid': entry.name, 'name': name,
                              'stat': read_text(entry / 'stat', 2000),
                              'status': read_text(entry / 'status', 4000)})
    payload['processes'] = processes[:100]
    try:
        result = subprocess.run(
            ['systemctl', 'show', 'mediamtx.service', 'cam-server.service',
             '-p', 'Id', '-p', 'MainPID', '-p', 'ActiveState', '-p', 'NRestarts',
             '-p', 'ExecMainStatus'], capture_output=True, text=True, timeout=2)
        payload['services'] = (result.stdout + result.stderr)[:6000]
    except (OSError, subprocess.TimeoutExpired) as exc:
        payload['services'] = str(exc)
    diag.emit('SYSTEM_SAMPLE', **payload)


def capture_journal(process, diag, source):
    try:
        for line in process.stdout:
            diag.emit('JOURNAL', source=source, text=line.rstrip()[:12000])
    except Exception as exc:
        diag.emit('JOURNAL_CAPTURE_ERROR', source=source, error=str(exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=300)
    parser.add_argument('--run-id', default=datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error('--seconds must be between 1 and 3600')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run_id):
        parser.error('--run-id must contain only letters, numbers, _ or -')
    directory = ROOT / 'runtime_data' / 'diagnostics' / args.run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name == 'posix':
        directory.chmod(0o700)
    os.environ.update(APL_DIAGNOSTICS='1', APL_DIAGNOSTIC_RUN=args.run_id,
                      APL_DIAGNOSTIC_DIR=str(directory))
    import apl_diagnostics as diag
    recorder = diag.recorder()
    print('DIAGNOSTIC_DIRECTORY=' + str(directory), flush=True)
    diag.emit('RUN_START', mode='read_only_services', seconds=args.seconds,
              boot_id=read_text('/proc/sys/kernel/random/boot_id'))
    stop = threading.Event()
    journal_processes = []
    for source, extra in (('services', ['-u', 'mediamtx.service', '-u', 'cam-server.service']),
                          ('kernel', ['-k'])):
        try:
            process = subprocess.Popen(
                ['journalctl', '-b', '-f', '-n', '0', '-o', 'short-iso-precise'] + extra,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors='replace')
            journal_processes.append(process)
            threading.Thread(target=capture_journal, args=(process, diag, source),
                             daemon=True).start()
        except OSError as exc:
            diag.emit('JOURNAL_UNAVAILABLE', source=source, error=str(exc))
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    try:
        deadline = time.monotonic() + args.seconds
        while not stop.is_set() and time.monotonic() < deadline:
            sample_system(diag)
            if stop.wait(min(5.0, max(0.0, deadline - time.monotonic()))):
                break
        diag.emit('RUN_END', interrupted=stop.is_set())
    finally:
        for process in journal_processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        recorder.close()


if __name__ == '__main__':
    main()
