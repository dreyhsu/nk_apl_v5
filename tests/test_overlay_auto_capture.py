"""The AI overlay has to show the automatic probe counting down, on both the
OpenCV draw path and the QTI/Cairo one, which share overlay_commands."""

import unittest

from workflow_view import (AUTO_PHASE_LABELS, PHASE_LABELS, auto_capture_line,
                           auto_capture_remaining, overlay_commands, workflow_view)


def state(**auto):
    base = {'state': 'WAITING_CAPTURE', 'sn': 'K2V5-001',
            'inspection_mode': 'single_snapshot_cover_card',
            'steps': [{'name': '50_COVER_CLOSED_CARD_PRESENT'}],
            'sop': {'done_count': 0},
            'matching': {'raw': 'UNKNOWN', 'stable': 'UNKNOWN',
                         'roi': [751, 254, 431, 588], 'box': None}}
    if auto:
        base['auto_capture'] = auto
    return base


def texts(payload, width=1920, height=1080, stale=False):
    return [c[1] for c in overlay_commands(payload, width, height, stale) if c[0] == 'text']


class AutoCaptureOverlayTests(unittest.TestCase):
    ARMED = dict(armed=True, elapsed=7.4, timeout_sec=20.0, hits=1,
                 required_hits=2, score=0.695, threshold=0.8)

    def test_the_countdown_line_reports_time_hits_and_score(self):
        self.assertEqual(auto_capture_line(self.ARMED),
                         'AUTO  13s left  -  hit 1/2  -  score 0.69 / 0.80')

    def test_the_countdown_never_goes_negative_past_the_timeout(self):
        line = auto_capture_line({**self.ARMED, 'elapsed': 26.0})
        self.assertIn('0s left', line)
        self.assertEqual(auto_capture_remaining({**self.ARMED, 'elapsed': 26.0}), 0.0)

    def test_without_a_timeout_the_line_counts_up_and_the_bar_stays_full(self):
        counting = {**self.ARMED, 'timeout_sec': 0.0, 'elapsed': 42.6}
        self.assertTrue(auto_capture_line(counting).startswith('AUTO  42s  -  '))
        self.assertNotIn('left', auto_capture_line(counting))
        self.assertEqual(auto_capture_remaining(counting), 1.0)

    def test_a_missing_score_is_left_out_rather_than_shown_as_zero(self):
        self.assertNotIn('score', auto_capture_line({**self.ARMED, 'score': None}))
        # No threshold to compare against: show the bare score, not 'score 0.69 / 0.00'.
        self.assertTrue(auto_capture_line({**self.ARMED, 'threshold': None})
                        .endswith('score 0.69'))

    def test_the_overlay_text_is_ascii_because_opencv_cannot_draw_anything_else(self):
        """cv2.putText with FONT_HERSHEY_SIMPLEX silently renders '?' otherwise."""
        for value in texts(state(**self.ARMED)):
            self.assertTrue(str(value).isascii(), value)

    def test_the_armed_overlay_draws_the_countdown_and_its_bar(self):
        armed = overlay_commands(state(**self.ARMED), 1920, 1080)
        idle = overlay_commands(state(), 1920, 1080)
        self.assertIn('AUTO  13s left  -  hit 1/2  -  score 0.69 / 0.80',
                      [c[1] for c in armed if c[0] == 'text'])
        # The panel grows by one line, and the bar is a track plus its fill.
        self.assertEqual(len(armed) - len(idle), 3)
        panel = [c for c in armed if c[0] == 'rect'][0]
        self.assertEqual(panel[4], 128)
        self.assertEqual([c for c in idle if c[0] == 'rect'][0][4], 82)

    def test_the_bar_fill_tracks_the_time_left(self):
        def fill(elapsed):
            commands = overlay_commands(state(**{**self.ARMED, 'elapsed': elapsed}), 1920, 1080)
            # rects: 0 panel, 1 countdown track, 2 countdown fill, 3 ROI, ...
            return [c for c in commands if c[0] == 'rect'][2][3]
        self.assertAlmostEqual(fill(0.0), 738.0)
        self.assertAlmostEqual(fill(10.0), 369.0)
        self.assertAlmostEqual(fill(20.0), 0.0)

    def test_an_armed_phase_never_tells_the_operator_to_press_the_button(self):
        self.assertIn('Capture & Inspect', PHASE_LABELS['WAITING_CAPTURE'])
        for phase in ('WAITING_CAPTURE', 'NOT_MATCHED'):
            with self.subTest(phase):
                payload = {**state(**self.ARMED), 'state': phase}
                self.assertEqual(workflow_view(payload)['phase'], AUTO_PHASE_LABELS[phase])
                self.assertNotIn('Capture & Inspect', workflow_view(payload)['phase'])
                # Disarmed, the original wording comes back.
                self.assertEqual(workflow_view({**payload, 'auto_capture': {'armed': False}})['phase'],
                                 PHASE_LABELS[phase])

    def test_an_overlay_with_no_auto_block_is_unchanged(self):
        """Every legacy caller - scripts/image_match_test.py, the offline video
        tools, a station with auto_capture off - passes no auto_capture key."""
        self.assertFalse(workflow_view(state())['armed'])
        self.assertEqual(workflow_view(state())['phase'], PHASE_LABELS['WAITING_CAPTURE'])
        self.assertNotIn('AUTO', ' '.join(str(v) for v in texts(state())))

    def test_a_stale_overlay_drops_the_countdown_with_everything_else(self):
        view = workflow_view(state(**self.ARMED), stale=True)
        self.assertFalse(view['armed'])
        self.assertEqual(view['phase'], PHASE_LABELS['STATE_UNAVAILABLE'])
        self.assertNotIn('AUTO', ' '.join(str(v) for v in texts(state(**self.ARMED), stale=True)))


if __name__ == '__main__':
    unittest.main()
