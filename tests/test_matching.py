"""Golden-sample loading and fixed-view matching checks."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_reject_config_requires_nonempty_state_names(self):
        for states in ([], "REJECT", [None], [""], ["  "], [1]):
            with self.subTest(states=states):
                with self.assertRaisesRegex(ValueError, "non-empty list"):
                    self.matcher(snapshot_reject_states=states)

    def test_reject_names_are_configurable_and_absent_diagnostics_are_unknown(self):
        custom = "CUSTOM_REJECT"
        source = self.root / "templates" / (self.rejects[0] + "_CROOP")
        source.rename(source.with_name(custom))
        matcher = self.matcher(snapshot_reject_states=[custom], template_angles_deg=[-1, 0, 1])
        for state in (self.target, self.rejects[0]):
            frame = np.zeros((48, 48, 3), dtype=np.uint8)
            frame[4:20, 5:21] = cv2.cvtColor(self.images[state], cv2.COLOR_GRAY2BGR)
            with self.subTest(state=state):
                result = matcher.evaluate_snapshot(frame)
                self.assertEqual(result["accepted"], state == self.target)
                self.assertEqual(set(result["reject_margins"]), {custom})
                self.assertTrue(all(value is None for value in result["conditions"].values()))
                if state == self.target:
                    self.assertEqual(result["reason"], "accepted")
                else:
                    self.assertEqual(result["failed_reject_states"], [custom])
                    self.assertIn(result["reason"], {"below_threshold", "reject_state_or_ambiguous"})

    def test_state_named_folder_takes_precedence_over_legacy_folder(self):
        folder = self.root / "templates" / self.target
        folder.mkdir()
        cv2.imwrite(str(folder / "direct.png"), self.images[self.target])
        matcher = self.matcher()
        self.assertEqual(matcher.samples[self.target][0][0], "direct.png")
        self.assertEqual(matcher.sample_folders[self.target], folder.resolve())

    def test_legacy_mode_still_requires_its_configured_folders(self):
        with self.assertRaisesRegex(ValueError, "No valid golden sample for EMPTY_FIXTURE"):
            self.matcher(inspection_mode="legacy_workflow", steps=["EMPTY_FIXTURE"])

    def test_new_reject_folder_can_block_an_otherwise_accepted_target(self):
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[4:20, 5:21] = cv2.cvtColor(self.images[self.target], cv2.COLOR_GRAY2BGR)
        self.assertTrue(self.matcher().evaluate_snapshot(frame)["accepted"])
        state = "50_CARD_NOT_FULLY_INSERTED"
        folder = self.root / "templates" / (state + "_CROOP")
        folder.mkdir()
        # A visually indistinguishable reject must prevent a PASS.
        cv2.imwrite(str(folder / "sample.png"), self.images[self.target])
        matcher = self.matcher(snapshot_reject_states=self.rejects + [state])
        result = matcher.evaluate_snapshot(frame)
        self.assertFalse(result["accepted"])
        self.assertTrue(result["conditions"]["cover_closed"])
        self.assertTrue(result["conditions"]["mcio_card_present"])
        self.assertEqual(result["failed_reject_states"], [state])
        self.assertEqual(result["reason"], "reject_state_or_ambiguous")
        self.assertAlmostEqual(result["reject_margins"][state], 0.0)
        self.assertAlmostEqual(result["gap"], 0.0)
        self.assertIsNone(result["box"])
        self.assertEqual(result["raw"], UNKNOWN)

    def test_missing_additional_reject_template_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "No valid golden sample for EXTRA_REJECT"):
            self.matcher(snapshot_reject_states=self.rejects + ["EXTRA_REJECT"])

    def test_target_cannot_be_listed_as_a_reject(self):
        with self.assertRaisesRegex(ValueError, "Target state cannot"):
            self.matcher(snapshot_reject_states=self.rejects + [self.target])

    def test_threshold_and_every_reject_margin_include_the_boundary(self):
        matcher = self.matcher(threshold=0.875, min_gap=0.125)
        matcher.snapshot_reject_states += ["EXTRA_A", "EXTRA_B"]
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        cases = [
            (0.875, 0.75, 0.75, True, "accepted", []),
            (0.8125, 0.5, 0.5, False, "below_threshold", []),
            (0.875, 0.8125, 0.75, False, "reject_state_or_ambiguous", ["EXTRA_A"]),
            (0.875, 0.75, 0.875, False, "reject_state_or_ambiguous", ["EXTRA_B"]),
            (0.875, 0.9375, 1.0, False, "reject_state_or_ambiguous", ["EXTRA_A", "EXTRA_B"]),
        ]
        for target, extra_a, extra_b, accepted, reason, failed in cases:
            scores = {
                self.target: {"score": target, "box": [0, 0, 16, 16], "template": "sample.png"},
                **{state: {"score": 0.5} for state in self.rejects},
                "EXTRA_A": {"score": extra_a}, "EXTRA_B": {"score": extra_b},
            }
            with self.subTest(target=target, extra_a=extra_a, extra_b=extra_b):
                with patch.object(matcher, "_best_by_class", return_value=scores):
                    result = matcher.evaluate_snapshot(frame)
                self.assertEqual(result["accepted"], accepted)
                self.assertEqual(result["reason"], reason)
                self.assertEqual(result["failed_reject_states"], failed)
                self.assertEqual(result["gap"], min(result["reject_margins"].values()))

    def test_rotated_target_and_rejects_use_their_best_angle(self):
        matcher = self.matcher(template_angles_deg=[-1, 0, 1], threshold=0.9999)
        original = self.matcher(threshold=0.9999)
        for state, image in self.images.items():
            for angle in (-1, 1):
                # Build the input independently of the cached template arrays.
                matrix = cv2.getRotationMatrix2D((7.5, 7.5), angle, 1.0)
                rotated = cv2.warpAffine(image, matrix, (16, 16), flags=cv2.INTER_LINEAR,
                                         borderMode=cv2.BORDER_REPLICATE)
                frame = np.zeros((48, 48, 3), dtype=np.uint8)
                frame[4:20, 5:21] = cv2.cvtColor(rotated, cv2.COLOR_GRAY2BGR)
                with self.subTest(state=state, angle=angle):
                    result = matcher.evaluate_snapshot(frame)
                    self.assertEqual(result["class_angles_deg"][state], angle)
                    self.assertAlmostEqual(result["class_scores"][state], 1.0, places=5)
                    self.assertEqual(result["accepted"], state == self.target)
                    if state == self.target:
                        self.assertEqual(result["angle_deg"], angle)
                        self.assertEqual(result["box"], [5, 4, 16, 16])
                        self.assertFalse(original.evaluate_snapshot(frame)["accepted"])
                    else:
                        self.assertIn(state, result["failed_reject_states"])

    def test_rotation_is_cached_and_each_angle_is_matched_per_frame(self):
        matcher = self.matcher(template_angles_deg=[-1, 0, 1])
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[4:20, 5:21] = cv2.cvtColor(self.images[self.target], cv2.COLOR_GRAY2BGR)
        with patch.object(cv2, "warpAffine", side_effect=AssertionError("per-frame rotation")):
            with patch.object(cv2, "matchTemplate", wraps=cv2.matchTemplate) as matching:
                for _ in range(2):
                    self.assertTrue(matcher.evaluate_snapshot(frame)["accepted"])
                self.assertEqual(matching.call_count, 2 * 3 * len(self.images))
        self.assertEqual(len(matcher.samples[self.target]), 1)
        self.assertEqual(len(matcher.template_variants[self.target]), 3)

    def test_rotation_preserves_template_size_when_it_fills_the_roi(self):
        matcher = self.matcher(template_angles_deg=[-1, 0, 1],
                               reference_size=[16, 16], search_roi=[0, 0, 16, 16])
        frame = cv2.cvtColor(self.images[self.target], cv2.COLOR_GRAY2BGR)
        result = matcher.evaluate_snapshot(frame)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["angle_deg"], 0)
        self.assertEqual(result["box"], [0, 0, 16, 16])

    def test_rotation_settings_default_disable_and_remove_duplicates(self):
        for angles in (None, [0], [0, 0]):
            with self.subTest(angles=angles):
                matcher = self.matcher() if angles is None else self.matcher(template_angles_deg=angles)
                self.assertEqual(matcher.template_angles_deg, [0.0])
                self.assertEqual(len(matcher.template_variants[self.target]), 1)

    def test_invalid_rotation_settings_are_rejected(self):
        for angles in ([], "-1,0,1", [True], ["1"], [None], [float("nan")],
                       [float("inf")], [181], [-181]):
            with self.subTest(angles=angles):
                with self.assertRaisesRegex(ValueError, "template_angles_deg"):
                    self.matcher(template_angles_deg=angles)

    def test_additional_reject_uses_rotation_to_block_a_match(self):
        state = "EXTRA_REJECT"
        folder = self.root / "templates" / (state + "_CROOP")
        folder.mkdir()
        cv2.imwrite(str(folder / "sample.png"), self.images[self.target])
        matcher = self.matcher(template_angles_deg=[-1, 0, 1],
                               snapshot_reject_states=self.rejects + [state])
        matrix = cv2.getRotationMatrix2D((7.5, 7.5), 1, 1.0)
        rotated = cv2.warpAffine(self.images[self.target], matrix, (16, 16),
                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[4:20, 5:21] = cv2.cvtColor(rotated, cv2.COLOR_GRAY2BGR)
        result = matcher.evaluate_snapshot(frame)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["class_angles_deg"][state], 1)
        self.assertEqual(result["failed_reject_states"], [state])
        self.assertAlmostEqual(result["reject_margins"][state], 0.0)

    def test_legacy_workflow_uses_cached_rotations_too(self):
        classes = ["EMPTY_FIXTURE", "50_PRODUCT_LOADED", "30_PRODUCT_LOADED", "30_COVER_CLOSED"]
        rng = np.random.default_rng(94)
        for state in classes:
            folder = self.root / "templates" / (state + "_CROOP")
            folder.mkdir()
            cv2.imwrite(str(folder / "sample.png"), rng.integers(0, 256, (16, 16), dtype=np.uint8))
        matcher = self.matcher(inspection_mode="legacy_workflow", steps=list(self.images) + classes[:2],
                               ng_states=classes[2:], template_angles_deg=[-1, 0, 1])
        self.assertEqual(len(matcher.samples), 7)
        frame = np.zeros((48, 48, 3), dtype=np.uint8)
        frame[4:20, 5:21] = cv2.cvtColor(self.images[self.target], cv2.COLOR_GRAY2BGR)
        result = matcher.evaluate(frame)
        self.assertEqual(result["raw"], self.target)
        self.assertEqual(result["angle_deg"], 0)
        self.assertEqual(len(result["class_angles_deg"]), 7)


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
