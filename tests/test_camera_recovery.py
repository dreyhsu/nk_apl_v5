"""Camera recovery markers remain safe without surviving a completed reboot."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import camera_worker


class CameraRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.marker = self.root / 'camera_recovery_required.json'
        self.archive = self.root / 'recovery_archive'
        self.boot_id = self.root / 'boot_id'
        self.patches = [
            patch.object(camera_worker, 'RECOVERY_FILE', self.marker),
            patch.object(camera_worker, 'RECOVERY_ARCHIVE_DIR', self.archive),
            patch.object(camera_worker, 'BOOT_ID_FILE', self.boot_id),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def write_marker(self, value):
        self.marker.write_text(json.dumps(value), encoding='utf-8')

    def test_same_boot_remains_blocked(self):
        self.boot_id.write_text('boot-a', encoding='ascii')
        self.write_marker({'reason': 'cleanup failed', 'boot_id': 'boot-a'})
        with self.assertRaisesRegex(RuntimeError, 'during this boot'):
            camera_worker._enforce_recovery_guard()
        self.assertTrue(self.marker.exists())

    def test_completed_reboot_archives_marker_and_allows_start(self):
        self.boot_id.write_text('boot-b', encoding='ascii')
        self.write_marker({'reason': 'cleanup failed', 'boot_id': 'boot-a'})
        archived = camera_worker._enforce_recovery_guard()
        self.assertFalse(self.marker.exists())
        self.assertTrue(archived.is_file())
        self.assertEqual(json.loads(archived.read_text(encoding='utf-8'))['boot_id'], 'boot-a')

    def test_legacy_marker_is_not_silently_ignored(self):
        self.boot_id.write_text('boot-b', encoding='ascii')
        self.write_marker({'reason': 'old cleanup failure'})
        with self.assertRaisesRegex(RuntimeError, 'Legacy camera recovery marker'):
            camera_worker._enforce_recovery_guard()

    def test_invalid_marker_is_not_silently_ignored(self):
        self.boot_id.write_text('boot-b', encoding='ascii')
        self.marker.write_text('{broken', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'unreadable'):
            camera_worker._enforce_recovery_guard()

    def test_missing_boot_id_source_keeps_guard(self):
        self.write_marker({'reason': 'cleanup failed', 'boot_id': 'boot-a'})
        with self.assertRaisesRegex(RuntimeError, 'cannot verify the current boot'):
            camera_worker._enforce_recovery_guard()


if __name__ == '__main__':
    unittest.main()
