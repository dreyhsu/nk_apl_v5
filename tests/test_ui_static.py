"""Static operator UI checks that do not require the Flask runtime."""

import unittest
from pathlib import Path


class OperatorInputUITests(unittest.TestCase):
    def test_sn_input_is_read_only_and_ime_mutations_are_blocked(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="snInput"', html)
        self.assertIn('readonly disabled', html)
        self.assertIn("'beforeinput','compositionstart','compositionupdate','compositionend','drop'", script)
        self.assertIn("attachFixedScanner('snInput')", script)
        self.assertIn("attachFixedScanner('mcioInput')", script)
        self.assertIn("/^[\\x21-\\x7e]+$/", script)

    def test_operator_employee_number_field_is_rendered_in_every_mode(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="operatorSignIn"', html)
        self.assertIn("$('operatorSignIn').classList.toggle('hidden',signedIn)", script)
        self.assertNotIn('showSignIn', script)
        self.assertIn("const sifiBypass=station?.dmz?.bypass===true", script)

    def test_video_ui_uses_local_browser_playback_and_canvas_capture(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('accept=".mp4,video/mp4"', html)
        self.assertIn('id="testVideo"', html)
        self.assertIn('id="testFrameCanvas"', html)
        self.assertIn('file stays on this PC', html)
        self.assertIn('URL.createObjectURL(file)', script)
        self.assertIn('context.drawImage(video', script)
        self.assertIn("canvas.toBlob(resolve,'image/png')", script)
        self.assertIn('/api/apl/capture-inspect-frame', script)
        self.assertIn('data.attempt?.result_image', script)
        self.assertNotIn('data.attempt?.result_path', script)
        self.assertIn('textValue(scanner.last_error)', script)
        self.assertNotIn("data.append('video',file)", script)
        self.assertNotIn('/video-test/control', script)
        self.assertIn('id="videoTestToolbar" class="inputToolbar hidden"', html)
        self.assertIn('apl_ui.js?v=20261002-1-mcio-two-scan', html)

    def test_scanner_search_is_collapsed_and_operator_gate_clears_sn(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('<details id="scannerSettings"', html)
        self.assertIn('id="scannerRefresh"', html)
        self.assertIn('id="scannerApply"', html)
        self.assertIn("if(operatorRequired&&!signedIn&&(fixedBuffers.snInput||fixedBuffers.mcioInput))clearScanBuffers()", script)
        self.assertIn('function refreshScannerDevices()', script)
        self.assertIn('function saveScannerSelection()', script)

    def test_active_browser_video_can_be_reattached_after_scanning(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn("const VIDEO_TEST_STATES=['WAITING_SN','WAITING_MCIO_SN','WAITING_CAPTURE','NOT_MATCHED']", script)
        self.assertIn('VIDEO_TEST_STATES.includes(stationState)', script)
        self.assertIn("$('videoFile').disabled=!canAttachVideo", script)
        self.assertIn("browserActive&&videoChoice?'Replace Test Video':'Choose Test Video'", script)
        self.assertIn("const replacing=browserVideoActive()", script)
        self.assertIn("A+'/browser-video/start'", script)
        self.assertIn('Test video attached to the current inspection.', script)
        self.assertIn('id="videoBack"', html)
        self.assertIn('id="videoForward"', html)
        self.assertIn('skipBrowserVideo(-5)', html)
        self.assertIn('skipBrowserVideo(5)', html)
        self.assertIn('function updateVideoProgress', script)

    def test_video_test_feature_gate_hides_ui_and_guards_active_sn_start(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn("lastStatus.features?.video_test_enabled===true", script)
        self.assertIn("$('videoTestToolbar').classList.toggle('hidden',!videoTestEnabled)", script)
        self.assertIn("const canStartVideo=videoTestEnabled", script)
        self.assertIn("Browser video test started for the current SN.", script)


    def test_mcio_card_serial_number_has_its_own_guarded_scan_field(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('id="mcioInput"', html)
        # The MCIO field must carry the same IME and keyboard-layout guards as the SN field.
        self.assertEqual(html.count('inputmode="none"'), 2)
        self.assertEqual(html.count('readonly disabled'), 2)
        self.assertIn("submitScan('mcioInput')", html)
        self.assertIn("mcioInput:{field:'mcio_sn'", script)
        self.assertIn("WAITING_MCIO_SN:'Scan MCIO Card SN'", script)
        self.assertIn("WAITING_MCIO_SN:'Product serial number accepted.", script)
        self.assertIn("expect:spec.field", script)

    def test_guided_steps_dim_without_disabling_terminate(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        for step in range(1, 6):
            self.assertIn('id="step%d"' % step, html)
        # Terminate sits outside the step list so a dimmed step cannot hide it.
        footer = html.split('<div class="stationFooterActions">')[1]
        self.assertIn('id="terminateInspection"', footer)
        # Dimming must stay visual: a pending step still has to accept clicks.
        self.assertNotIn('.stepRow.pending{opacity:.45;pointer-events:none}', html)
        self.assertIn('.stepRow.pending{opacity:.45}', html)
        self.assertIn('function stepStatuses(station,freshStation)', script)

    def test_scan_focus_follows_step_transitions_only(self):
        root = Path(__file__).resolve().parents[1]
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn('if(!force&&step===lastScanStep)return;', script)
        self.assertIn("if(currentPage!=='live'||$(target).disabled)return;", script)
        self.assertIn("function clearScanBuffers(){setFixedValue('snInput','');setFixedValue('mcioInput','')}", script)


if __name__ == '__main__':
    unittest.main()
