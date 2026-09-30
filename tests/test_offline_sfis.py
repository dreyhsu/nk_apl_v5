"""Offline station: dmz.sfis_enabled=false must skip Shopfloor and save locally."""

import json
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np

from apl_station import APLStation
from dmz_client import DMZClient, DMZError, sfis_enabled
from test_snapshot_inspection import SnapshotMatcher, Transport


def write_config(path, dmz):
    path.write_text(json.dumps({
        "station_id": "TEST", "inspection_mode": "single_snapshot_cover_card",
        "target_state": "50_COVER_CLOSED_CARD_PRESENT",
        "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                  "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
        "template_root": "templates", "station_mode": "remote",
        "dmz": dmz,
    }), encoding="utf-8")
    return path


# A reachable-looking endpoint and the production step2_enabled=false, so the
# no-network assertions mean something and the offline path is proven to be
# independent of the Step2 switch.
OFFLINE_DMZ = {"mode": "http", "api_style": "sfis",
               "base_url": "http://10.0.28.125:8097/api/SFISAPI",
               "line": "TEST", "station": "AI-CAM",
               "step2_enabled": False, "sfis_enabled": False}


class OfflineSFISStationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = write_config(self.root / "workflow.json", dict(OFFLINE_DMZ))
        self.station = APLStation(self.config, self.root / "data", matcher=SnapshotMatcher())

    def tearDown(self):
        self.station.close()
        self.temp.cleanup()

    @staticmethod
    def frame(accepted):
        return np.full((8, 8, 3), 1 if accepted else 0, dtype=np.uint8)

    def test_offline_station_never_builds_a_transport(self):
        with patch("apl_station.DMZClient",
                   side_effect=AssertionError("DMZClient must not be constructed")):
            station = APLStation(self.config, self.root / "data2", matcher=SnapshotMatcher())
        try:
            self.assertIsNone(station.transport)
            self.assertFalse(station.sfis_enabled)
            self.assertTrue(station.offline_mode)
            self.assertTrue(station.local_mode)
        finally:
            station.close()

    def test_offline_does_not_require_operator(self):
        self.assertFalse(self.station.status()["operator_required"])
        self.assertTrue(self.station.scan("OFFLINE-1")["success"])

    def test_offline_snapshot_cycle_completes_without_network(self):
        with patch("dmz_client.DMZClient.send",
                   side_effect=AssertionError("Network must not be called")), \
             patch.object(urllib.request.OpenerDirector, "open",
                          side_effect=AssertionError("No socket may be opened")):
            scanned = self.station.scan("OFFLINE-1")
            self.assertTrue(scanned["success"])
            # Step1 is skipped entirely, so the cycle goes straight to capture.
            self.assertEqual(scanned["station"]["state"], "WAITING_CAPTURE")
            cycle = scanned["station"]["cycle_id"]
            judged = self.station.inspect_snapshot(self.frame(True))
            self.assertEqual(judged["station"]["state"], "PASS_CONFIRM")
            self.assertTrue(judged["station"]["can_confirm_result"])
            self.assertTrue(self.station.confirm_result(cycle)["success"])
            status = self.station.status()
            self.assertEqual(status["state"], "WAITING_SN")
            self.assertFalse(status["last_result"]["reported"])
            self.assertEqual(status["last_result"]["mode"], "offline")
            self.assertEqual(status["last_result"]["upload_status"], "SKIPPED_SFIS_DISABLED")
            self.assertEqual(status["last_result"]["visual_result"], "OK")
            # The SN is unlocked, so the next unit can be scanned immediately.
            self.assertTrue(self.station.scan("OFFLINE-2")["success"])

    def test_offline_cycle_persists_evidence_and_leaves_no_pending_outbox(self):
        cycle = self.station.scan("OFFLINE-1")["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(False))
        self.station.inspect_snapshot(self.frame(True))
        self.assertTrue(self.station.confirm_result(cycle)["success"])
        row = self.station.db.execute(
            "SELECT state, result FROM cycles WHERE cycle_id=?", (cycle,)).fetchone()
        self.assertEqual((row["state"], row["result"]), ("COMPLETE", "PASS"))
        events = [r[0] for r in self.station.db.execute(
            "SELECT event FROM events WHERE cycle_id=?", (cycle,)).fetchall()]
        self.assertIn("PASS_SAVED_OFFLINE_NO_SFIS", events)
        self.assertNotIn("PASS_ACKNOWLEDGED", events)
        self.assertEqual(self.station.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        detail = self.station.history_detail(cycle)
        self.assertEqual([item["visual_result"] for item in detail["attempts"]],
                         ["NOT_MATCHED", "OK"])
        self.assertTrue(all((self.station.evidence.root / item["raw_path"]).is_file()
                            for item in detail["attempts"]))

    def test_offline_profile_enables_the_confirm_action(self):
        cycle = self.station.scan("OFFLINE-1")["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(True))
        status = self.station.status()
        self.assertEqual(status["dmz"]["mode"], "offline")
        self.assertTrue(status["dmz"]["configured"])
        self.assertTrue(status["dmz"]["step2_enabled"])
        self.assertFalse(status["dmz"]["sfis_enabled"])
        self.assertEqual(status["dmz"]["label"], "SFIS OFF - LOCAL RECORD ONLY")
        self.assertTrue(status["can_confirm_result"])
        self.assertTrue(self.station.confirm_result(cycle)["success"])

    def test_scan_event_records_the_transport_decision(self):
        cycle = self.station.scan("OFFLINE-1")["station"]["cycle_id"]
        payload = json.loads(self.station.db.execute(
            "SELECT payload_json FROM events WHERE cycle_id=? AND event=?",
            (cycle, "SN_SCANNED_AND_STARTED")).fetchone()[0])
        self.assertEqual(payload["transport_mode"], "offline")
        self.assertFalse(payload["sfis_enabled"])

    def test_step1_local_branch_lands_in_waiting_capture(self):
        """The local Step1 no-op is unreachable in snapshot mode through the public
        API (scan() skips it, confirm_sn() rejects it). This pins the next state so
        the legacy RUNNING value cannot leak back in."""
        cycle = self.station.scan("OFFLINE-1")["station"]["cycle_id"]
        self.station.state = "SN_CONFIRM"
        self.assertTrue(self.station._confirm(cycle, result=False)["success"])
        self.assertEqual(self.station.status()["state"], "WAITING_CAPTURE")

    def test_flag_absent_keeps_sfis_enabled(self):
        dmz = {key: value for key, value in OFFLINE_DMZ.items() if key != "sfis_enabled"}
        config = write_config(self.root / "online.json", dmz)
        station = APLStation(config, self.root / "data3", matcher=SnapshotMatcher())
        try:
            self.assertTrue(station.sfis_enabled)
            self.assertFalse(station.offline_mode)
            self.assertFalse(station.local_mode)
            self.assertIsInstance(station.transport, DMZClient)
            self.assertTrue(station.status()["operator_required"])
        finally:
            station.close()

    def test_flipping_the_flag_mid_cycle_is_rejected(self):
        cycle = self.station.scan("OFFLINE-1")["station"]["cycle_id"]
        self.station.inspect_snapshot(self.frame(True))
        transport = Transport()
        self.station.sfis_enabled, self.station.offline_mode = True, False
        self.station.transport = transport
        reply = self.station.confirm_result(cycle)
        self.assertFalse(reply["success"])
        self.assertIn("differs from this cycle", reply["error"])
        self.assertFalse(self.station.status()["dmz"]["configuration_matches_cycle"])
        self.assertEqual(transport.calls, [])


class OnlineCycleReopenedOfflineTests(unittest.TestCase):
    """A cycle that already has a PENDING upload must not be closed out locally
    just because the station was restarted with SFIS switched off."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.online_config = write_config(
            self.root / "online.json",
            {key: value for key, value in OFFLINE_DMZ.items() if key != "sfis_enabled"})
        self.offline_config = write_config(self.root / "offline.json", dict(OFFLINE_DMZ))

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def frame(accepted):
        return np.full((8, 8, 3), 1 if accepted else 0, dtype=np.uint8)

    def test_pending_upload_survives_a_restart_with_sfis_off(self):
        transport = Transport()
        station = APLStation(self.online_config, self.data,
                             transport=transport, matcher=SnapshotMatcher())
        station.set_operator("OP-TEST")
        cycle = station.scan("SN-001")["station"]["cycle_id"]
        station.inspect_snapshot(self.frame(True))
        transport.fail = True
        self.assertFalse(station.confirm_result(cycle)["success"])
        station.close()

        offline = APLStation(self.offline_config, self.data, matcher=SnapshotMatcher())
        try:
            reply = offline.confirm_result(cycle)
            self.assertFalse(reply["success"])
            self.assertIn("differs from this cycle", reply["error"])
            status = offline.db.execute(
                "SELECT status FROM outbox WHERE cycle_id=?", (cycle,)).fetchone()[0]
            self.assertEqual(status, "PENDING")
            # Terminating is still allowed, so the station is not wedged.
            self.assertTrue(offline.terminate(cycle)["success"])
            self.assertEqual(offline.status()["state"], "WAITING_SN")
            self.assertEqual(offline.db.execute(
                "SELECT status FROM outbox WHERE cycle_id=?", (cycle,)).fetchone()[0], "PENDING")
        finally:
            offline.close()


class SFISEnabledFlagTests(unittest.TestCase):
    def test_absent_key_means_enabled(self):
        self.assertTrue(sfis_enabled(None))
        self.assertTrue(sfis_enabled({}))
        self.assertTrue(sfis_enabled({"mode": "http"}))

    def test_booleans_and_common_strings_are_accepted(self):
        for value in (False, "false", "False", " off ", "0", "no"):
            self.assertFalse(sfis_enabled({"sfis_enabled": value}), value)
        for value in (True, "true", "ON", "1", "yes"):
            self.assertTrue(sfis_enabled({"sfis_enabled": value}), value)

    def test_unreadable_value_is_rejected_instead_of_defaulting(self):
        with self.assertRaisesRegex(ValueError, "dmz.sfis_enabled"):
            sfis_enabled({"sfis_enabled": "maybe"})

    def test_disabled_client_refuses_to_send(self):
        client = DMZClient(dict(OFFLINE_DMZ))
        self.assertFalse(client.enabled)
        with self.assertRaisesRegex(DMZError, "sfis_enabled=false"):
            client.send("/sn/check", {"sn": "SN-001", "cycle_id": "c", "request_id": "r"})


if __name__ == "__main__":
    unittest.main()
