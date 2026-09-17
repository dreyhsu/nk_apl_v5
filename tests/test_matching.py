"""Golden-sample loading and fixed-view matching checks."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apl_matching import APLMatcher, UNKNOWN


class MatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = ROOT / "config" / "apl_workflow.json"
        cls.config = json.loads(cls.config_path.read_text(encoding="utf-8"))
        cls.matcher = APLMatcher(cls.config, cls.config_path)

    def test_all_seven_classes_have_two_samples(self):
        self.assertEqual(len(self.matcher.samples), 7)
        self.assertTrue(all(len(samples) == 2 for samples in self.matcher.samples.values()))

    def test_each_golden_sample_recognizes_its_class(self):
        width, height = self.matcher.size
        roi_x, roi_y, _, _ = self.matcher.roi
        for expected, samples in self.matcher.samples.items():
            for name, gray in samples:
                frame = np.zeros((height, width, 3), dtype=np.uint8)
                color = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
                h, w = gray.shape
                frame[roi_y:roi_y + h, roi_x:roi_x + w] = color
                with self.subTest(state=expected, sample=name):
                    result = self.matcher.evaluate(frame)
                    self.assertEqual(result["raw"], expected, result)
                    self.assertGreaterEqual(result["score"], self.matcher.threshold)

    def test_unknown_never_has_a_match_box(self):
        width, height = self.matcher.size
        rng = np.random.default_rng(20260907)
        frame = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
        result = self.matcher.evaluate(frame)
        self.assertEqual(result["raw"], UNKNOWN)
        self.assertIsNone(result["box"])

    def test_wrong_resolution_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "dimensions"):
            self.matcher.evaluate(np.zeros((720, 1280, 3), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
