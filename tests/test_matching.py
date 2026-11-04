"""Golden-sample loading and fixed-view matching checks."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apl_matching import APLMatcher, StableStateFilter, UNKNOWN


class MatchingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_path = ROOT / "config" / "apl_workflow.json"
        cls.config = json.loads(cls.config_path.read_text(encoding="utf-8"))
        cls.matcher = APLMatcher({
            **cls.config, "inspection_mode": "legacy_workflow",
            "steps": ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "50_COVER_CLOSED",
                      "50_COVER_CLOSED_CARD_PRESENT", "50_COVER_OPEN_CARD_PRESENT"],
            "ng_states": ["30_PRODUCT_LOADED", "30_COVER_CLOSED"],
            "removal_state": "EMPTY_FIXTURE",
        }, cls.config_path)

    def test_legacy_loads_all_seven_classes(self):
        self.assertEqual(len(self.matcher.samples), 7)
        self.assertTrue(all(samples for samples in self.matcher.samples.values()))

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


class SnapshotTemplateLoadingTests(unittest.TestCase):
    target = "50_COVER_CLOSED_CARD_PRESENT"
    rejects = ["50_COVER_CLOSED", "50_COVER_OPEN_CARD_PRESENT"]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {
            "inspection_mode": "single_snapshot_cover_card",
            "reference_size": [48, 48], "search_roi": [0, 0, 48, 48],
            "template_root": "templates", "target_state": self.target,
            "snapshot_reject_states": self.rejects,
        }
        rng = np.random.default_rng(42)
        self.images = {}
        for state in [self.target] + self.rejects:
            folder = self.root / "templates" / (state + "_CROOP")
            folder.mkdir(parents=True)
            image = rng.integers(0, 256, (16, 16), dtype=np.uint8)
            cv2.imwrite(str(folder / "sample.png"), image)
            self.images[state] = image

    def matcher(self, **overrides):
        return APLMatcher({**self.config, **overrides}, self.root / "workflow.json")

    def test_three_folders_suffice_without_legacy_config(self):
        matcher = self.matcher()
        self.assertEqual(set(matcher.samples), set(self.images))
        for state, image in self.images.items():
            frame = np.zeros((48, 48, 3), dtype=np.uint8)
            frame[4:20, 5:21] = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
            with self.subTest(state=state):
                result = matcher.evaluate_snapshot(frame)
                self.assertEqual(result["accepted"], state == self.target)
                self.assertEqual(set(result["class_scores"]), set(self.images))

    def test_missing_legacy_folders_are_ignored_even_when_configured(self):
        matcher = self.matcher(
            steps=["EMPTY_FIXTURE", "50_PRODUCT_LOADED"] + list(self.images),
            ng_states=["30_PRODUCT_LOADED", "30_COVER_CLOSED"],
            removal_state="EMPTY_FIXTURE",
        )
        self.assertEqual(set(matcher.samples), set(self.images))

    def test_each_required_folder_must_have_a_valid_sample(self):
        for state in self.images:
            path = self.root / "templates" / (state + "_CROOP") / "sample.png"
            content = path.read_bytes()
            path.unlink()
            with self.subTest(state=state):
                with self.assertRaisesRegex(ValueError, "No valid golden sample for " + state):
                    self.matcher()
            path.write_bytes(content)

    def test_reject_comparisons_cannot_be_disabled_by_omitting_states(self):
        for states in ([], self.rejects[:1], self.rejects[1:]):
            with self.subTest(states=states):
                with self.assertRaisesRegex(ValueError, "requires both"):
                    self.matcher(snapshot_reject_states=states)

    def test_legacy_mode_still_requires_its_configured_folders(self):
        with self.assertRaisesRegex(ValueError, "No valid golden sample for EMPTY_FIXTURE"):
            self.matcher(inspection_mode="legacy_workflow", steps=["EMPTY_FIXTURE"])


class StableStateFilterTests(unittest.TestCase):
    def test_stable_state_filter_latches_and_never_clears(self):
        """Why the automatic capture probe counts consecutive accepts itself: this
        filter only ever writes self.stable, so once it latches the target state a
        later UNKNOWN frame still reads as a match."""
        f = StableStateFilter(2)
        self.assertEqual(f.update("50_COVER_CLOSED_CARD_PRESENT"), UNKNOWN)
        self.assertEqual(f.update("50_COVER_CLOSED_CARD_PRESENT"), "50_COVER_CLOSED_CARD_PRESENT")
        self.assertEqual(f.update(UNKNOWN), "50_COVER_CLOSED_CARD_PRESENT")
        self.assertEqual(f.update(UNKNOWN), UNKNOWN)


if __name__ == "__main__":
    unittest.main()
