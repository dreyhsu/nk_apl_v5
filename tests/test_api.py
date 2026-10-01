"""HTTP-level checks for operator gates and legacy bypass prevention."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import visionedge_server as web


class StationAPITests(unittest.TestCase):
    def setUp(self):
        self.station = Mock()
        self.station.status.return_value = {
            'state': 'SN_CONFIRM', 'sn': 'TEST-SN', 'cycle_id': 'cycle-1', 'sop': {},
        }
        self.station.scan.return_value = {'success': True, 'station': self.station.status()}
        self.station.confirm_result.return_value = {'success': False, 'error': 'not completed'}
        self.edge = Mock()
        self.edge.cfg = SimpleNamespace(stream_enabled=True, live_view_mode='webrtc')
        self.edge.status.return_value = {'running': False, 'last_frame_at': None}
        self.scanner = Mock()
        self.patches = [patch.object(web, 'STATION', self.station), patch.object(web, 'EDGE', self.edge)]
        for item in self.patches:
            item.start()
        self.client = web.app.test_client()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_scan_only_produces_candidate(self):
        response = self.client.post('/api/apl/scan', json={'sn': 'TEST-SN'})
        self.assertEqual(response.status_code, 200)
        self.station.scan.assert_called_once_with('TEST-SN', source='web')
        self.station.confirm_sn.assert_not_called()

    def test_scan_expect_field_blocks_a_step_that_already_advanced(self):
        self.station.status.return_value = {
            'state': 'WAITING_SN', 'sn': '', 'cycle_id': '', 'sop': {},
        }
        stale = self.client.post('/api/apl/scan', json={'sn': 'APL-001', 'expect': 'mcio_sn'})
        self.assertEqual(stale.status_code, 409)
        self.assertIn('different step', stale.json['error'])
        self.station.scan.assert_not_called()
        matched = self.client.post('/api/apl/scan', json={'sn': 'K2V5-001', 'expect': 'sn'})
        self.assertEqual(matched.status_code, 200)
        self.station.scan.assert_called_once_with('K2V5-001', source='web')

    def test_scan_expect_field_must_name_a_known_scan_step(self):
        response = self.client.post('/api/apl/scan', json={'sn': 'K2V5-001', 'expect': 'operator'})
        self.assertEqual(response.status_code, 400)
        self.station.scan.assert_not_called()

    def test_scanner_candidates_and_selection(self):
        device = {'path': '/dev/input/event6', 'name': 'USB scanner', 'vendor_id': '0c2e',
                  'product_id': '0b41', 'serial': '', 'phys': 'usb-1'}
        self.scanner.status.return_value = {'enabled': True, 'configured': True, 'selected': device}
        with patch.object(web, 'SCANNER', self.scanner), patch('scanner_selection.devices', return_value=[device]):
            found = self.client.get('/api/apl/scanner')
            saved = self.client.post('/api/apl/scanner', json={'path': device['path']})
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json['devices'][0]['product_id'], '0b41')
        self.assertEqual(saved.status_code, 200)
        self.scanner.select.assert_called_once_with(device['path'])

    def test_scanner_selection_rejects_stale_or_invalid_path(self):
        self.scanner.select.side_effect = ValueError('Device is no longer available')
        with patch.object(web, 'SCANNER', self.scanner):
            self.assertEqual(self.client.post('/api/apl/scanner', json={'path': ''}).status_code, 400)
            self.assertEqual(self.client.post('/api/apl/scanner', json={'path': '/dev/input/event99'}).status_code, 409)

    def test_confirm_sn_requires_fresh_camera(self):
        for state in ({'running': False, 'last_frame_at': None}, {'running': True, 'last_frame_at': 1}):
            self.edge.status.return_value = state
            self.assertEqual(self.client.post('/api/apl/confirm-sn', json={'cycle_id': 'cycle-1'}).status_code, 409)
        self.station.confirm_sn.assert_not_called()

    def test_pass_cannot_bypass_visual_completion(self):
        response = self.client.post('/api/apl/confirm-result', json={'cycle_id': 'cycle-1'})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json['success'])
        self.scanner.reconnect.assert_not_called()

    def test_terminate_reopens_scanner_after_unlock(self):
        self.station.terminate.return_value = {
            'success': True,
            'station': {'state': 'WAITING_SN', 'sn': '', 'cycle_id': ''},
        }
        with patch.object(web, 'SCANNER', self.scanner):
            response = self.client.post('/api/apl/terminate', json={'cycle_id': 'cycle-1'})
        self.assertEqual(response.status_code, 200)
        self.scanner.reconnect.assert_called_once_with()

    def test_successful_upload_reopens_scanner_after_unlock(self):
        self.station.confirm_result.return_value = {
            'success': True,
            'station': {'state': 'WAITING_SN', 'sn': '', 'cycle_id': ''},
        }
        with patch.object(web, 'SCANNER', self.scanner):
            response = self.client.post('/api/apl/confirm-result', json={'cycle_id': 'cycle-1'})
        self.assertEqual(response.status_code, 200)
        self.scanner.reconnect.assert_called_once_with()

    def test_legacy_actions_cannot_bypass_station(self):
        for path in ('/api/edge/sop/finish', '/api/edge/sop/reset', '/api/edge/sop/ack',
                     '/api/runtime/reload', '/api/stream-sources'):
            self.assertEqual(self.client.post(path, json={}).status_code, 409, path)

    def test_active_sn_blocks_camera_settings(self):
        self.assertEqual(self.client.post('/api/edge/config', json={'rotation': 0}).status_code, 409)
        self.edge.update_config.assert_not_called()

    def test_invalid_json_is_reported(self):
        for body in ([], None, 'sn'):
            self.assertEqual(self.client.post('/api/apl/scan', json=body).status_code, 400)

    def test_cross_origin_operator_command_is_blocked(self):
        response = self.client.post('/api/apl/scan', json={'sn': 'TEST-SN'}, headers={'Origin': 'https://unrelated.invalid'})
        self.assertEqual(response.status_code, 403)
        self.station.scan.assert_not_called()

    def test_status_contains_station_and_workflow(self):
        response = self.client.get('/api/edge/status')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['station']['cycle_id'], 'cycle-1')
        self.assertEqual(response.json['product_id'], 1)
        self.assertEqual(response.headers['Cache-Control'].split(',')[0], 'no-store')


if __name__ == '__main__':
    unittest.main()
