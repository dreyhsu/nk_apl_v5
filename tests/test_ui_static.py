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
        self.assertIn('captureFixedSNKey,true', script)
        self.assertIn("/^[\\x21-\\x7e]+$/", script)

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
        self.assertIn('apl_ui.js?v=20260914-3-video-feature-gate', html)

    def test_active_browser_video_can_be_reattached_after_scanning(self):
        root = Path(__file__).resolve().parents[1]
        html = (root/'static'/'edge_dashboard.html').read_text(encoding='utf-8')
        script = (root/'static'/'apl_ui.js').read_text(encoding='utf-8')
        self.assertIn("['WAITING_SN','WAITING_CAPTURE','NOT_MATCHED'].includes(stationState)", script)
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


if __name__ == '__main__':
    unittest.main()
