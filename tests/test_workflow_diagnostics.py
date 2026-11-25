"""Winner confidence, operator messages, and the camera status header."""

import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from overlay_renderer import OverlayRenderer
from snapshot_diagnostics import (CLASS_DIAGNOSTICS, NG_DIAGNOSTICS,
                                  active_diagnostic, winner_diagnostic)
from workflow_view import (AMBER, GREEN, MUTED, RED, TIMEOUT_PASS_LABEL,
                           light_indicator, overlay_commands, workflow_view)


def snapshot(code=None, phase='WAITING_CAPTURE', armed=True):
    return {'inspection_mode': 'single_snapshot_cover_card', 'state': phase,
            'steps': [{'name': '50_COVER_CLOSED_CARD_PRESENT_CROOP'}],
            'matching': {'diagnostic_code': code}, 'auto_capture': {'armed': armed},
            'sn': 'K2V5_TEST', 'step_index': int(phase == 'PASS_CONFIRM')}


class WinnerConfidenceTests(unittest.TestCase):
    def test_every_class_and_folder_alias(self):
        for name, (code, _) in CLASS_DIAGNOSTICS.items():
            for suffix in ('', '_CROOP'):
                with self.subTest(name=name, suffix=suffix):
                    result = winner_diagnostic({name+suffix: 0.9, 'other': 0.7}, 0.77, 0.05)
                    self.assertEqual(result['winning_state'], name+suffix)
                    self.assertEqual(result['winning_score'], 0.9)
                    self.assertAlmostEqual(result['winning_gap'], 0.2)
                    self.assertEqual(result['diagnostic_code'], code)

    def test_boundaries_ties_and_missing_scores(self):
        name = '50_COVER_OPEN_CARD_PRESENT_CROOP'
        cases = [({name: 0.77, 'runner': 0.72}, 'COVER_OPEN'),
                 ({name: 0.82, 'runner': 0.77}, 'COVER_OPEN'),
                 ({name: 0.769, 'runner': 0.6}, None),
                 ({name: 0.9, 'runner': 0.851}, None),
                 ({name: 0.9, 'runner': 0.9}, None),
                 ({name: 0.9}, None), ({name: None}, None),
                 ({name: float('nan'), 'runner': float('inf')}, None), ({}, None),
                 ({'CUSTOM_CLASS': 0.99, name: 0.8}, None)]
        for scores, code in cases:
            with self.subTest(scores=scores):
                self.assertEqual(winner_diagnostic(scores, 0.77, 0.05)['diagnostic_code'], code)
        self.assertIsNone(winner_diagnostic({name: 1, 'runner': 1}, 0, 0)['diagnostic_code'])


class WorkflowGuidanceTests(unittest.TestCase):
    def test_ng_guidance_updates_during_probes_and_survives_final_failure(self):
        for code, message in CLASS_DIAGNOSTICS.values():
            if code not in NG_DIAGNOSTICS:
                continue
            for phase, armed in (('WAITING_CAPTURE', True), ('NOT_MATCHED', True),
                                 ('NOT_MATCHED', False)):
                with self.subTest(code=code, phase=phase, armed=armed):
                    view = workflow_view(snapshot(code, phase, armed))
                    self.assertEqual(view['phase'], message)
                    self.assertEqual(view['accent'], RED)

    def test_live_low_confidence_and_target_confirmation_are_amber(self):
        for code, message in ((None, 'Auto-inspecting'), ('TARGET_MATCH', 'Confirming match')):
            view = workflow_view(snapshot(code))
            self.assertEqual(view['phase'], message)
            self.assertEqual(view['accent'], AMBER)
        self.assertEqual(workflow_view(snapshot('TARGET_MATCH', 'PASS_CONFIRM'))['phase'], 'Pass')
        self.assertEqual(workflow_view(snapshot('TARGET_MATCH', 'PASS_CONFIRM'))['accent'], GREEN)
        timed_out = snapshot('TARGET_MATCH', 'PASS_CONFIRM')
        timed_out['matching']['timeout_auto_pass'] = True
        self.assertEqual(workflow_view(timed_out)['phase'], TIMEOUT_PASS_LABEL)
        self.assertEqual(workflow_view(timed_out)['accent'], AMBER)

    def test_warning_does_not_leak_across_operational_phases_or_stale_status(self):
        for phase in ('WAITING_SN', 'WAITING_MCIO_SN', 'JUDGING', 'RECOVERY_REQUIRED',
                      'SN_CONFIRM', 'UPLOADING', 'PASS_CONFIRM'):
            state = snapshot('COVER_OPEN', phase)
            self.assertIsNone(active_diagnostic(state))
            self.assertNotEqual(workflow_view(state)['phase'], 'Please close the cover')
        self.assertIsNone(active_diagnostic(snapshot('COVER_OPEN'), stale=True))
        tagged = {**snapshot('COVER_OPEN'), 'status_stale': True}
        self.assertEqual(workflow_view(tagged)['phase'], 'Status unavailable')
        self.assertEqual(light_indicator(tagged), ('UNAVAILABLE', MUTED))
        self.assertIsNone(active_diagnostic(snapshot('COVER_OPEN', armed=False)))
        self.assertIsNone(active_diagnostic({**snapshot('COVER_OPEN'), 'inspection_mode': 'legacy_workflow'}))

    def test_snapshot_step_has_no_number_or_raw_template_identifier(self):
        for phase in ('WAITING_SN', 'WAITING_CAPTURE', 'PASS_CONFIRM'):
            commands = overlay_commands(snapshot(phase=phase), 1920, 1080)
            texts = [c[1] for c in commands if c[0] == 'text']
            self.assertIn('Insert MCIO card', texts)
            self.assertNotIn('01', texts)
            self.assertNotIn('50_COVER_CLOSED_CARD_PRESENT_CROOP', texts)


class HeaderTests(unittest.TestCase):
    def test_cairo_adapter_draws_shared_header_and_handles_stale_status(self):
        state = {**snapshot('COVER_OPEN'), 'overlay_timestamp': 1700000000,
                 'light_tower': {'enabled': True, 'connected': True, 'requested_event': 'judge_ng'}}
        renderer = OverlayRenderer(1920, 1080)
        renderer.update(state)
        context = Mock()
        renderer.draw(None, context, 0, 0)
        displayed = [call.args[0] for call in context.show_text.call_args_list]
        self.assertIn('Please close the cover', displayed)
        self.assertIn('LIGHT: NG', displayed)
        self.assertIn('TIME ' + time.strftime('%H:%M:%S', time.localtime(1700000000)), displayed)
        context.reset_mock()
        renderer._updated = time.monotonic() - 4
        renderer.draw(None, context, 0, 0)
        displayed = [call.args[0] for call in context.show_text.call_args_list]
        self.assertIn('Status unavailable', displayed)
        self.assertIn('LIGHT: UNAVAILABLE', displayed)
        self.assertNotIn('Please close the cover', displayed)

    def test_clock_uses_system_local_time_and_live_time_advances(self):
        state = snapshot()
        with patch('workflow_view.time.time', return_value=1700000000):
            commands = overlay_commands(state, 1920, 1080)
        expected = 'TIME ' + time.strftime('%H:%M:%S', time.localtime(1700000000))
        self.assertIn(expected, [c[1] for c in commands if c[0] == 'text'])
        with patch('workflow_view.time.time', return_value=1700000001):
            next_commands = overlay_commands(state, 1920, 1080)
        self.assertNotIn(expected, [c[1] for c in next_commands if c[0] == 'text'])

    def test_evidence_time_remains_frozen(self):
        state = {**snapshot(), 'overlay_timestamp': 1700000000}
        with patch('workflow_view.time.time', return_value=1700000999):
            commands = overlay_commands(state, 1920, 1080)
        self.assertIn('TIME ' + time.strftime('%H:%M:%S', time.localtime(1700000000)),
                      [c[1] for c in commands if c[0] == 'text'])

    def test_tower_command_connection_and_staleness(self):
        for event, expected, color in (('camera_on', 'READY', GREEN),
                                      ('auto_capture_start', 'INSPECTING', AMBER),
                                      ('judge_ng', 'NG', RED), ('judge_timeout', 'NG', RED),
                                      ('judge_pass', 'OK', GREEN)):
            state = {'light_tower': {'enabled': True, 'connected': True,
                                     'requested_event': event, 'event': 'camera_on'}}
            self.assertEqual(light_indicator(state), (expected, color))
            self.assertEqual(light_indicator(state, stale=True), ('UNAVAILABLE', MUTED))
        self.assertEqual(light_indicator({}), ('OFF', MUTED))
        self.assertEqual(light_indicator({'light_tower': {'enabled': True}}), ('CONNECTING', AMBER))
        self.assertEqual(light_indicator({'light_tower': {'enabled': True, 'error': 'offline'}}),
                         ('ERROR', RED))

    def test_scaled_header_text_stays_inside_panel(self):
        # Full pixel/font measurements are checked with OpenCV in the render check.
        for width, height in ((1920, 1080), (960, 540)):
            for code, _ in CLASS_DIAGNOSTICS.values():
                commands = overlay_commands(snapshot(code), width, height)
                for command in commands:
                    if command[0] == 'text':
                        self.assertTrue(command[1].isascii())
                        self.assertGreaterEqual(command[2], 0)
                        self.assertLess(command[2], width)
                        self.assertLess(command[3], height)


class WebInstructionsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js unavailable')
    def test_browser_uses_live_diagnostic_then_committed_failure(self):
        source = (Path(__file__).resolve().parents[1] / 'static' / 'apl_ui.js').read_text()
        functions = source.split('const INSPECTION_DIAGNOSTICS=', 1)[1].split('// SN_CONFIRM', 1)[0]
        script = "const assert=require('node:assert/strict');\nconst INSPECTION_DIAGNOSTICS=" + functions + '''
const s={inspection_mode:'single_snapshot_cover_card',state:'WAITING_CAPTURE',cycle_id:'c1'};
const a={armed:true,cycle_id:'c1',diagnostic_code:'COVER_OPEN'};
assert.equal(inspectionInstruction(s,a),'Please close the cover');
assert.equal(inspectionInstruction(s,{...a,diagnostic_code:null}),'Auto-inspecting');
assert.equal(inspectionInstruction(s,{...a,diagnostic_code:'TARGET_MATCH'}),'Confirming match');
assert.equal(inspectionInstruction(s,{...a,cycle_id:'old'}),'');
assert.equal(inspectionInstruction({...s,status_stale:true},a),'');
assert.equal(inspectionInstruction({...s,state:'WAITING_SN'},a),'');
assert.equal(inspectionInstruction({...s,state:'NOT_MATCHED',matching:{diagnostic_code:'COVER_OPEN'}},{}),
 'Please close the cover. Select Capture & Inspect to retry.');
assert.equal(inspectionInstruction({...s,state:'PASS_CONFIRM'},a),'Pass');
assert.match(inspectionInstruction({...s,state:'PASS_CONFIRM',matching:{timeout_auto_pass:true}},a),/NOT visually matched/);
'''
        result = subprocess.run([shutil.which('node')], input=script, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
