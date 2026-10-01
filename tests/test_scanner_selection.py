"""PC-side checks for SmartCam scanner discovery and remembered identity."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import scanner_selection as selection


class FakeReader:
    instances = []
    connect_on_start = True

    def __init__(self, on_scan, config):
        self.on_scan = on_scan
        self.config = config
        self.thread = None
        self.started = False
        self.stopped = False
        self.connected = self.connect_on_start
        self.reconnect_calls = 0
        self.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def reconnect(self):
        self.reconnect_calls += 1

    def status(self):
        present = any(selection._same_identity(device, self.config) for device in selection.devices())
        return {'connected': self.started and not self.stopped and self.connected and present, 'scan_count': 0}


class ScannerSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = {'enabled': True, 'vendor_id': '0c2e', 'product_id': '0901', 'name': 'Honeywell'}
        self.first = {'path': '/dev/input/event6', 'name': 'Honeywell 1250', 'vendor_id': '0c2e',
                      'product_id': '0b41', 'serial': '', 'phys': 'usb-1'}
        self.second = {'path': '/dev/input/event7', 'name': 'New Scanner', 'vendor_id': '1234',
                       'product_id': '5678', 'serial': 'SERIAL-2', 'phys': 'usb-2'}
        FakeReader.instances.clear()
        FakeReader.connect_on_start = True

    def tearDown(self):
        self.temp.cleanup()

    def manager(self):
        manager = selection.ScannerSelection(self.root, self.config, Mock())
        manager.platform_supported = True
        return manager

    def test_unique_device_auto_selected_and_remembered(self):
        with patch.object(selection, 'devices', return_value=[self.first]), patch.object(selection, 'SelectedReader', FakeReader):
            manager = self.manager()
            manager._auto_select()
            self.assertTrue(manager.status()['connected'])
            self.assertEqual(manager.status()['selection_source'], 'automatic')
            saved = json.loads(manager.selection_file.read_text())
            self.assertEqual(saved['version'], 2)
            self.assertEqual(saved['active']['product_id'], '0b41')
            self.assertEqual(len(saved['known']), 1)
            restored = self.manager()
            self.assertEqual(restored.status()['selection_source'], 'saved')
            self.assertEqual(restored.status()['selected']['product_id'], '0b41')

    def test_multiple_candidates_require_selection_and_replacement(self):
        with patch.object(selection, 'devices', return_value=[self.first, self.second]), patch.object(selection, 'SelectedReader', FakeReader):
            manager = self.manager()
            manager._auto_select()
            self.assertFalse(manager.status()['configured'])
            manager.select(self.first['path'])
            self.assertEqual(manager.status()['selected']['product_id'], '0b41')
            manager.select(self.second['path'])
            self.assertTrue(FakeReader.instances[0].stopped)
            self.assertEqual(manager.status()['selected']['serial'], 'SERIAL-2')
            saved = json.loads(manager.selection_file.read_text())
            self.assertEqual(saved['active']['vendor_id'], '1234')
            self.assertEqual(len(saved['known']), 2)

    def test_missing_saved_device_is_not_replaced_automatically(self):
        with patch.object(selection, 'devices', return_value=[self.first]), patch.object(selection, 'SelectedReader', FakeReader):
            manager = self.manager()
            manager.select(self.first['path'])
        with patch.object(selection, 'devices', return_value=[self.second]):
            restored = self.manager()
            restored._auto_select()
            self.assertEqual(restored.status()['selected']['vendor_id'], '0c2e')

    def test_stale_path_cannot_replace_selection(self):
        with patch.object(selection, 'devices', return_value=[self.first]):
            manager = self.manager()
            with self.assertRaisesRegex(ValueError, 'no longer available'):
                manager.select('/dev/input/event99')
            self.assertFalse(manager.selection_file.exists())

    def test_same_serial_can_reconnect_on_another_port(self):
        selected = {key: self.second[key] for key in selection.IDENTITY_FIELDS}
        moved = dict(self.second, path='/dev/input/event9', phys='usb-3')
        self.assertTrue(selection._same_identity(moved, selected))
        self.assertFalse(selection._same_identity(dict(moved, serial='OTHER'), selected))

    def test_known_scanners_switch_without_find_and_survive_restart(self):
        with patch.object(selection, 'SelectedReader', FakeReader):
            with patch.object(selection, 'devices', return_value=[self.first]):
                manager = self.manager()
                manager._auto_select()
            with patch.object(selection, 'devices', return_value=[self.second]):
                manager.select(self.second['path'])
                restored = self.manager()
                restored.start()
                self.assertEqual(restored.status()['selected']['product_id'], '5678')
            with patch.object(selection, 'devices', return_value=[self.first]):
                restored._auto_select()
                self.assertTrue(restored.status()['connected'])
                self.assertEqual(restored.status()['selected']['product_id'], '0b41')
                self.assertEqual(restored.status()['known_count'], 2)
            restored.stop()

    def test_only_verified_connection_is_remembered(self):
        with patch.object(selection, 'devices', return_value=[self.first]), patch.object(selection, 'SelectedReader', FakeReader):
            manager = self.manager()
            manager._auto_select()
            self.assertEqual(manager.status()['known_count'], 1)
            FakeReader.instances[-1].connected = False
        with patch.object(selection, 'devices', return_value=[self.second]), patch.object(selection, 'SelectedReader', FakeReader):
            FakeReader.connect_on_start = False
            manager.select(self.second['path'])
            self.assertEqual(manager.status()['known_count'], 1)
            self.assertEqual(json.loads(manager.selection_file.read_text())['active']['product_id'], '0b41')

    def test_serialless_scanner_can_move_port_when_unique(self):
        moved = dict(self.first, path='/dev/input/event9', phys='usb-3')
        with patch.object(selection, 'SelectedReader', FakeReader):
            with patch.object(selection, 'devices', return_value=[self.first]):
                manager = self.manager()
                manager._auto_select()
            with patch.object(selection, 'devices', return_value=[moved]):
                manager._auto_select()
                self.assertEqual(manager.status()['selected']['phys'], 'usb-3')
                self.assertEqual(manager.status()['known_count'], 1)

    def test_manual_reselection_after_port_move_does_not_duplicate_known_device(self):
        moved = dict(self.first, path='/dev/input/event9', phys='usb-3')
        with patch.object(selection, 'SelectedReader', FakeReader):
            with patch.object(selection, 'devices', return_value=[self.first]):
                manager = self.manager()
                manager._auto_select()
            with patch.object(selection, 'devices', return_value=[moved]):
                manager.select(moved['path'])
                self.assertEqual(manager.status()['known_count'], 1)
                saved = json.loads(manager.selection_file.read_text())
                self.assertEqual(saved['known'][0]['phys'], 'usb-3')

    def test_identical_serialless_scanners_are_ambiguous_after_port_change(self):
        twin = dict(self.first, path='/dev/input/event8', phys='usb-2')
        moved = dict(self.first, path='/dev/input/event9', phys='usb-3')
        with patch.object(selection, 'SelectedReader', FakeReader):
            with patch.object(selection, 'devices', return_value=[self.first, twin]):
                manager = self.manager()
                manager.select(self.first['path'])
                manager.select(twin['path'])
            with patch.object(selection, 'devices', return_value=[moved]):
                manager._auto_select()
                self.assertFalse(manager.status()['connected'])
                self.assertEqual(manager.status()['selected']['phys'], 'usb-2')

    def test_does_not_auto_switch_during_inspection(self):
        allowed = [True]
        with patch.object(selection, 'SelectedReader', FakeReader):
            with patch.object(selection, 'devices', return_value=[self.first]):
                manager = selection.ScannerSelection(self.root, self.config, Mock(), can_switch=lambda: allowed[0])
                manager.platform_supported = True
                manager._auto_select()
            with patch.object(selection, 'devices', return_value=[self.second]):
                manager.select(self.second['path'])
            allowed[0] = False
            with patch.object(selection, 'devices', return_value=[self.first]):
                manager._auto_select()
                self.assertEqual(manager.status()['selected']['product_id'], '5678')
                with self.assertRaisesRegex(ValueError, 'Finish the current inspection'):
                    manager.select(self.first['path'])
            allowed[0] = True
            with patch.object(selection, 'devices', return_value=[self.first]):
                manager._auto_select()
                self.assertEqual(manager.status()['selected']['product_id'], '0b41')

    def test_legacy_selection_is_migrated_after_connection(self):
        self.root.joinpath('scanner_selection.json').write_text(json.dumps(selection._identity(self.first)), encoding='utf-8')
        with patch.object(selection, 'devices', return_value=[self.first]), patch.object(selection, 'SelectedReader', FakeReader):
            manager = self.manager()
            manager.start()
            self.assertTrue(manager.status()['connected'])
            self.assertEqual(json.loads(manager.selection_file.read_text())['version'], 2)
            manager.stop()


if __name__ == '__main__':
    unittest.main()
