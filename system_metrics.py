"""Read actual Linux thermal sensors with a bounded refresh interval."""

import copy
import math
import threading
import time
from pathlib import Path


class TemperatureReader:
    def __init__(self, root='/sys/class/thermal', interval=2.0):
        self.root = Path(root)
        self.interval = interval
        self.lock = threading.Lock()
        self.sample = {'available': False, 'celsius': None, 'sensor': '', 'sampled_at': None}
        self.next_read = 0.0

    def status(self):
        with self.lock:
            if time.monotonic() < self.next_read:
                return copy.deepcopy(self.sample)
            sensors = []
            for zone in self.root.glob('thermal_zone*'):
                try:
                    name = (zone / 'type').read_text().strip()
                    # Qualcomm also exposes voltage/current as thermal zones.
                    if not ('thermal' in name.lower() or name.lower().startswith(('cpu', 'soc'))):
                        continue
                    value = float((zone / 'temp').read_text()) / 1000.0
                    if math.isfinite(value) and -40 <= value <= 150:
                        sensors.append({'sensor': name, 'celsius': round(value, 1)})
                except (OSError, ValueError):
                    continue
            cpu = [s for s in sensors if s['sensor'].lower().startswith('cpu')]
            hottest = max(cpu or sensors, key=lambda s: s['celsius'], default=None)
            self.sample = {'available': bool(hottest), 'celsius': hottest['celsius'] if hottest else None,
                           'sensor': hottest['sensor'] if hottest else '',
                           'label': 'CPU (hottest)' if cpu else 'Device (hottest)',
                           'sampled_at': time.time(), 'sensors': sensors}
            self.next_read = time.monotonic() + self.interval
            return copy.deepcopy(self.sample)
