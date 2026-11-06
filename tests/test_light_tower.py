"""PATLITE LR5-LAN tower: PNS packets, config validation, event choice and the
non-blocking sender."""

import socket
import socketserver
import threading
import time
import unittest
from types import SimpleNamespace

from light_tower import (DEFAULT_EVENTS, PatliteTower, clear_packet, run_control_packet,
                         select_event, validated_light_tower)


class FakeTower(socketserver.ThreadingTCPServer):
    """Records every PNS packet and answers ACK (or NAK when told to)."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.packets = []
        self.reply = b'\x06'
        self.connections = 0
        self.changed = threading.Condition()

        tower = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                tower.connections += 1
                while True:
                    header = self.request.recv(6)
                    if len(header) < 6:
                        return
                    size = int.from_bytes(header[4:6], 'big')
                    body = self.request.recv(size) if size else b''
                    with tower.changed:
                        tower.packets.append(header + body)
                        tower.changed.notify_all()
                    self.request.sendall(tower.reply)

        super().__init__(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def port(self):
        return self.server_address[1]

    def wait_for(self, count, timeout=3.0):
        with self.changed:
            self.changed.wait_for(lambda: len(self.packets) >= count, timeout)
        return list(self.packets)

    def stop(self):
        self.shutdown()
        self.server_close()


class PacketTests(unittest.TestCase):
    def test_run_control_matches_the_vendor_layout(self):
        # 'AB', 'S', pad byte, big-endian size 6, then red/amber/green/blue/white/buzzer.
        self.assertEqual(run_control_packet((0, 0, 2, 0, 0, 0)),
                         b'ABS\x00\x00\x06\x00\x00\x02\x00\x00\x00')

    def test_clear_has_no_data_area(self):
        self.assertEqual(clear_packet(), b'ABC\x00\x00\x00')


class ConfigTests(unittest.TestCase):
    def test_absent_block_is_disabled_with_schema_defaults(self):
        cfg = validated_light_tower(None)
        self.assertFalse(cfg['enabled'])
        self.assertEqual((cfg['host'], cfg['port']), ('192.168.10.1', 10000))
        self.assertEqual(cfg['events'], DEFAULT_EVENTS)

    def test_events_carry_a_comment_and_default_unlisted_channels_to_off(self):
        cfg = validated_light_tower({'enabled': True, 'host': '10.0.0.5', 'events': {
            'judge_pass': {'comment': 'green steady', 'green': 1}}})
        self.assertEqual(cfg['host'], '10.0.0.5')
        self.assertEqual(cfg['events']['judge_pass'], (0, 0, 1, 0, 0, 0))
        self.assertEqual(cfg['events']['camera_on'], DEFAULT_EVENTS['camera_on'])

    def test_bad_values_fail_at_startup(self):
        for bad in ({'enabled': 'yes'}, {'colour': 1}, {'port': 0}, {'port': '10000'},
                    {'host': ''}, {'timeout_sec': 60},
                    {'events': {'camera_off': {}}},
                    {'events': {'camera_on': {'green': 8}}},
                    {'events': {'camera_on': {'buzzer': 2}}},
                    {'events': {'camera_on': {'purple': 1}}},
                    {'events': {'camera_on': {'comment': 5}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validated_light_tower(bad)

    def test_the_shipped_station_config_is_valid_and_off(self):
        import json
        from pathlib import Path
        raw = json.loads((Path(__file__).resolve().parents[1] / 'config' / 'apl_workflow.json')
                         .read_text(encoding='utf-8'))
        cfg = validated_light_tower(raw['light_tower'])
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['events'], DEFAULT_EVENTS)


class SelectEventTests(unittest.TestCase):
    def test_phase_table(self):
        cases = [
            ((False, 'WAITING_CAPTURE', True, False), None),
            ((True, 'WAITING_SN', False, False), 'camera_on'),
            ((True, 'WAITING_MCIO_SN', False, False), 'camera_on'),
            ((True, 'WAITING_CAPTURE', True, False), 'auto_capture_start'),
            ((True, 'WAITING_CAPTURE', False, False), 'camera_on'),
            ((True, 'JUDGING', False, True), 'auto_capture_start'),
            ((True, 'NOT_MATCHED', False, True), 'judge_timeout'),
            # A failed manual capture is not a timeout.
            ((True, 'NOT_MATCHED', False, False), 'camera_on'),
            ((True, 'RECOVERY_REQUIRED', False, False), 'camera_on'),
            ((True, 'PASS_CONFIRM', False, True), 'judge_pass'),
            ((True, 'UPLOADING', False, False), 'judge_pass'),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(select_event(*args), expected)


class PatliteTowerTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTower()
        self.tower = PatliteTower('127.0.0.1', self.server.port, timeout_sec=0.5, retry_sec=0.1)

    def tearDown(self):
        self.tower.close()
        self.server.stop()

    def test_sends_once_per_change_on_one_connection(self):
        self.tower.request('camera_on', DEFAULT_EVENTS['camera_on'])
        self.tower.request('camera_on', DEFAULT_EVENTS['camera_on'])
        self.server.wait_for(1)
        self.tower.request('judge_pass', DEFAULT_EVENTS['judge_pass'])
        packets = self.server.wait_for(2)
        time.sleep(0.1)
        self.assertEqual(packets, [run_control_packet(DEFAULT_EVENTS['camera_on']),
                                   run_control_packet(DEFAULT_EVENTS['judge_pass'])])
        self.assertEqual(len(self.server.packets), 2)
        self.assertEqual(self.server.connections, 1)
        status = self.tower.status()
        self.assertTrue(status['connected'])
        self.assertEqual(status['event'], 'judge_pass')

    def test_close_turns_the_tower_dark(self):
        self.tower.request('camera_on', DEFAULT_EVENTS['camera_on'])
        self.server.wait_for(1)
        self.tower.close()
        self.assertEqual(self.server.wait_for(2)[-1], clear_packet())

    def test_a_nak_is_retried_until_acknowledged(self):
        self.server.reply = b'\x15'
        self.tower.request('judge_timeout', DEFAULT_EVENTS['judge_timeout'])
        self.server.wait_for(2)
        self.assertIn('negative acknowledge', self.tower.status()['error'])
        self.assertFalse(self.tower.status()['connected'])
        self.server.reply = b'\x06'
        deadline = time.time() + 3
        while not self.tower.status()['connected'] and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.tower.status()['connected'])
        self.assertEqual(self.tower.status()['error'], '')


class UnreachableTowerTests(unittest.TestCase):
    def test_request_never_blocks_the_caller(self):
        def hang(address, timeout=None):
            time.sleep(timeout)
            raise socket.timeout('timed out')
        tower = PatliteTower('192.0.2.1', 10000, timeout_sec=0.5, retry_sec=0.1, connect=hang)
        try:
            started = time.monotonic()
            for event in ('camera_on', 'auto_capture_start', 'judge_pass'):
                tower.request(event, DEFAULT_EVENTS[event])
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            tower.close(timeout=0.1)


class RuntimeTickTests(unittest.TestCase):
    """EdgeRuntime._light_tower_tick builds the tower from station config and maps
    the probe state onto events."""

    def setUp(self):
        try:
            from edge_runtime import EdgeRuntime
        except ImportError as exc:  # cv2 missing on a dev Mac
            self.skipTest(str(exc))
        self.requests = []
        runtime = EdgeRuntime.__new__(EdgeRuntime)
        runtime.station = SimpleNamespace(light_tower=validated_light_tower(
            {'enabled': True, 'host': '127.0.0.1', 'port': 1}))
        self.runtime = runtime
        self.towers = []
        test = self

        class Recorder:
            def __init__(self, host, port, timeout_sec):
                self.settings = (host, port, timeout_sec)
                self.closed = False
                test.towers.append(self)

            def request(self, event, pattern=None):
                test.requests.append(event)

            def close(self):
                self.closed = True

        import edge_runtime
        self._original = edge_runtime.PatliteTower
        edge_runtime.PatliteTower = Recorder

    def tearDown(self):
        import edge_runtime
        edge_runtime.PatliteTower = self._original

    def test_timeout_is_only_the_probe_window_of_this_cycle(self):
        self.runtime._auto.update(armed=True)
        self.runtime._light_tower_tick({'state': 'WAITING_CAPTURE', 'cycle_id': 'c1'})
        self.runtime._auto.update(armed=False, suspended_cycle='c1')
        self.runtime._light_tower_tick({'state': 'NOT_MATCHED', 'cycle_id': 'c1'})
        self.runtime._auto.update(suspended_cycle=None)
        self.runtime._light_tower_tick({'state': 'NOT_MATCHED', 'cycle_id': 'c1'})
        self.assertEqual(self.requests, ['auto_capture_start', 'judge_timeout', 'camera_on'])
        self.assertEqual(len(self.towers), 1)

    def test_disabling_or_moving_the_tower_closes_the_old_one(self):
        self.runtime._light_tower_tick({'state': 'WAITING_SN'})
        first = self.towers[0]
        self.runtime.station.light_tower = validated_light_tower(
            {'enabled': True, 'host': '127.0.0.2', 'port': 1})
        self.runtime._light_tower_tick({'state': 'WAITING_SN'})
        self.assertTrue(first.closed)
        self.assertEqual(self.towers[1].settings[0], '127.0.0.2')
        self.runtime.station.light_tower = validated_light_tower(None)
        self.runtime._light_tower_tick({'state': 'WAITING_SN'})
        self.assertTrue(self.towers[1].closed)
        self.assertIsNone(self.runtime._tower)


if __name__ == '__main__':
    unittest.main()
