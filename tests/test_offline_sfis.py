"""Protect migration from the retired sfis_enabled switch to bypass_sfis."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apl_station import APLStation
from dmz_client import DMZClient
from scripts import sfis_api_test


class LegacySwitchMigrationTests(unittest.TestCase):
    def test_legacy_switch_is_rejected_for_both_values(self):
        for value in (False, True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "dmz.sfis_enabled is obsolete"):
                    DMZClient({"mode": "http", "sfis_enabled": value})

    def test_old_offline_station_cannot_silently_start_real_sfis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "workflow.json"
            config.write_text(json.dumps({
                "station_mode": "remote",
                "inspection_mode": "single_snapshot_cover_card",
                "template_root": "templates",
                "dmz": {"mode": "http", "sfis_enabled": False,
                        "base_url": "http://10.0.28.125:8097/api/SFISAPI"},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dmz.sfis_enabled is obsolete"):
                APLStation(config, root / "data", matcher=object())

    def test_legacy_switch_is_rejected_even_with_an_injected_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "workflow.json"
            config.write_text(json.dumps({
                "station_mode": "local", "inspection_mode": "single_snapshot_cover_card",
                "dmz": {"sfis_enabled": False},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dmz.sfis_enabled is obsolete"):
                APLStation(config, root / "data", transport=object(), matcher=object())


class ManualSFISProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name) / "workflow.json"
        self.config.write_text(json.dumps({
            "station_id": "AI-CAM",
            "dmz": {"mode": "http", "api_style": "sfis", "bypass_sfis": True,
                    "step4_enabled": False, "base_url": "http://10.0.28.125:8097/api/SFISAPI",
                    "line": "TEST", "station": "AI-CAM", "allow_insecure_http": True},
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_overrides_config_bypass_but_does_not_send_step4(self):
        observed = []

        def fake_send(client, path, payload):
            observed.append((client.bypass, client.step4_enabled, path))
            return {"sfis_response": {"success": True}}

        argv = ["sfis_api_test.py", "--sn", "SN-001", "--config", str(self.config)]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"APL_DMZ_BYPASS": "off"}), \
             patch.object(DMZClient, "send", fake_send):
            self.assertEqual(sfis_api_test.main(), 0)
        self.assertEqual(observed, [(False, False, "/sn/check")])

    def test_probe_rejects_environment_bypass_before_sending(self):
        argv = ["sfis_api_test.py", "--sn", "SN-001", "--config", str(self.config)]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"APL_DMZ_BYPASS": "on"}), \
             patch.object(DMZClient, "send") as send:
            with self.assertRaises(SystemExit) as caught:
                sfis_api_test.main()
        self.assertEqual(caught.exception.code, 2)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
