"""Keep production streaming and SFIS configuration distinct from test mode."""

import configparser
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from dmz_client import DMZClient


ROOT = Path(__file__).resolve().parents[1]


class ProductionStreamConfigTests(unittest.TestCase):
    def test_real_sfis_and_bounded_auto_capture(self):
        config = json.loads((ROOT / 'config/apl_workflow.json').read_text(encoding='utf-8'))
        dmz = config['dmz']
        self.assertEqual(dmz['mode'], 'http')
        self.assertEqual(dmz['api_style'], 'sfis')
        self.assertFalse(dmz['bypass_sfis'])
        self.assertTrue(dmz['step4_enabled'])
        self.assertTrue(dmz['base_url'].startswith('http://'))
        self.assertEqual(dmz['step1_path'], '/Step1')
        self.assertEqual(dmz['step4_path'], '/Step4')
        self.assertEqual(config['auto_capture']['fps'], 3.0)
        self.assertEqual(config['auto_capture']['timeout_sec'], 20.0)
        self.assertEqual(config['auto_capture']['timeout_action'], 'not_matched')
        with patch.dict(os.environ, {'APL_DMZ_MODE': 'http', 'APL_DMZ_BYPASS': 'off'}):
            client = DMZClient(dmz)
            self.assertEqual(client.mode, 'http')
            self.assertFalse(client.bypass)
            self.assertTrue(client.status()['configured'])

    def test_stream_ports_and_disabled_video_test(self):
        edge = configparser.ConfigParser()
        edge.read(ROOT / 'config/edge.ini', encoding='utf-8')
        self.assertTrue(edge.getboolean('server', 'tls'))
        self.assertEqual(edge.getint('server', 'port'), 8765)
        self.assertEqual(edge.get('edge', 'stream_rtp_host'), '127.0.0.1')
        self.assertEqual(edge.getint('edge', 'stream_rtp_port'), 5004)
        self.assertEqual(edge.getint('stream', 'whep_port'), 8889)
        self.assertFalse(edge.getboolean('features', 'video_test_enabled'))
        media = (ROOT / 'mediamtx.yml').read_text(encoding='utf-8')
        self.assertIn('source: udp+rtp://127.0.0.1:5004', media)
        self.assertIn('webrtcAddress: :8889', media)
        self.assertIn('webrtcLocalTCPAddress: :5000', media)
        self.assertIn('udpReadBufferSize: 1048576', media)

    def test_service_uses_production_source_and_kernel_buffer(self):
        service = (ROOT / 'mediamtx.service').read_text(encoding='utf-8')
        self.assertIn('WorkingDirectory=/var/rootdirs/opt/VisionEdge_APL', service)
        self.assertIn('/var/rootdirs/opt/VisionEdge_APL/mediamtx.yml', service)
        self.assertIn('ExecStartPre=/usr/sbin/sysctl -w net.core.rmem_max=2097152', service)
        self.assertNotIn('nk_apl_v5_test', service)
        app_service = (ROOT / 'visionedge-apl.service').read_text(encoding='utf-8')
        self.assertIn('Environment=VISIONEDGE_TLS=on', app_service)
        self.assertIn('Environment=APL_DMZ_MODE=http', app_service)
        self.assertIn('Environment=APL_DMZ_BYPASS=off', app_service)
        self.assertNotIn('Environment=APL_DMZ_MODE=disabled', app_service)
        launcher = (ROOT / 'run_apl.sh').read_text(encoding='utf-8')
        self.assertIn('Production APL requires HTTPS', launcher)
        self.assertIn('Production APL requires APL_DMZ_MODE=http', launcher)
        self.assertIn('Production APL cannot bypass SFIS', launcher)


if __name__ == '__main__':
    unittest.main()
