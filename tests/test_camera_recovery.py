"""Camera recovery markers remain safe without surviving a completed reboot."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import camera_worker
import camera_recovery as recovery


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

class RecoveryTests(unittest.TestCase):
    def evidence(self):
        return dict(camera_null=False, camera_eos=True, callbacks_drained=True,
                    recorder_null=None, recorder_finalized=None,
                    camera_elements={'camsrc': {'current': 'ready', 'pending': 'null'},
                                     'sink': {'current': 'null'}})

    def test_only_confirmed_source_close_failure_is_eligible(self):
        release = self.evidence()
        self.assertTrue(recovery.release_eligible(release, True, False, True, 2, [[12, '1']]))
        for key, value in [('camera_eos', False), ('callbacks_drained', False),
                           ('recorder_null', False), ('recorder_finalized', False)]:
            self.assertFalse(recovery.release_eligible({**release, key: value}, True, False, True, 2, [[12, '1']]))
        self.assertFalse(recovery.release_eligible(release, True, True, True, 2, [[12, '1']]))
        self.assertFalse(recovery.release_eligible(release, True, False, False, 2, [[12, '1']]))
        self.assertFalse(recovery.release_eligible(release, True, False, True, 1, [[12, '1']]))

    def test_marker_pair_requires_same_boot_session_and_unchanged_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / 'local.json', Path(directory) / 'shared.json']
            record = dict(boot_id='boot', session='previous', pid=999999999,
                          release=dict(self.evidence(), reopen_schema=1, camera_id=0, reopen_eligible=True,
                                       worker_reaped=True, forced_exit=False, server_before=[[12, '1']],
                                       worker_acknowledged=True, received_frames=2))
            for path in paths:
                path.write_text(json.dumps(record))
            ticket = recovery.marker_ticket(paths, 'boot', 0)
            with patch.object(recovery, 'server_identity', return_value=[[13, '2']]):
                recovery.verify_ticket(ticket, paths, 'boot')
                paths[1].write_text(json.dumps({**record, 'session': 'other'}))
                with self.assertRaises(RuntimeError):
                    recovery.verify_ticket(ticket, paths, 'boot')
            with self.assertRaises(RuntimeError):
                recovery.marker_ticket(paths, 'wrong-boot', 0)
            with self.assertRaises(RuntimeError):
                recovery.marker_ticket(paths, 'boot', 0)

    def test_wait_is_condition_based_and_cancellable(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        result = recovery.wait_replacement([[12, '1']], lambda: False, timeout=5,
                   identity=lambda: [[13, '2']], clock=lambda: now[0], sleep=sleep)
        self.assertEqual(result, [[13, '2']])
        self.assertLess(now[0], 4)
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            recovery.wait_replacement([], lambda: True)

    def test_same_server_does_not_unlock_recovery(self):
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            recovery.wait_replacement([[12, '1']], lambda: False, timeout=1,
                identity=lambda: [[12, '1']], clock=lambda: now[0], sleep=sleep)
