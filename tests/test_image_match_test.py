"""Offline image inspection follows the production three-template decision."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from apl_matching import APLMatcher
from scripts.image_match_test import main


class ImageMatchTestTests(unittest.TestCase):
    def test_three_class_config_reports_conditions_and_writes_overlays(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            production_path = Path(__file__).resolve().parents[1] / "config" / "apl_workflow.json"
            config = json.loads(production_path.read_text())
            for key in ("steps", "ng_states", "removal_state"):
                self.assertNotIn(key, config)
            config.update(template_root="templates", reference_size=[1920, 1080],
                          search_roi=[751, 254, 48, 48])
            config_path = root / "workflow.json"
            config_path.write_text(json.dumps(config))
            inputs = root / "inputs"
            inputs.mkdir()
            rng = np.random.default_rng(100)
            classes = [config["target_state"]] + config["snapshot_reject_states"]
            for state in classes:
                folder = root / "templates" / (state + "_CROOP")
                folder.mkdir(parents=True)
                sample = rng.integers(0, 256, (16, 16), dtype=np.uint8)
                cv2.imwrite(str(folder / "golden.png"), sample)
                frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
                frame[258:274, 756:772] = cv2.cvtColor(sample, cv2.COLOR_GRAY2BGR)
                cv2.imwrite(str(inputs / (state + ".png")), frame)

            report_path = root / "report.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(["--image", str(inputs), "--config", str(config_path),
                             "--output", str(root / "overlays"), "--json", str(report_path)])
            self.assertEqual(code, 0)
            report = json.loads(report_path.read_text())
            self.assertEqual(len(report), 3)
            labels = json.loads((inputs / "image_match_labels.json").read_text())
            self.assertEqual(labels, {state + ".png": None for state in classes})
            matcher = APLMatcher(config, config_path)
            for item in report:
                state = Path(item["image"]).stem
                with self.subTest(state=state):
                    result = item["result"]
                    self.assertEqual(set(result["class_scores"]), set(classes))
                    self.assertEqual(result["accepted"], state == config["target_state"])
                    if state == config["target_state"]:
                        self.assertTrue(result["conditions"]["mcio_card_present"])
                        self.assertTrue(result["conditions"]["cover_closed"])
                    elif state == "50_COVER_CLOSED":
                        self.assertFalse(result["conditions"]["mcio_card_present"])
                    else:
                        self.assertFalse(result["conditions"]["cover_closed"])
                    frame = cv2.imread(item["image"])
                    self.assertEqual(result, matcher.evaluate_snapshot(frame))
                    overlay = cv2.imread(item["overlay"])
                    self.assertEqual(overlay.shape, frame.shape)
                    self.assertFalse(np.array_equal(overlay, frame))
            text = output.getvalue()
            self.assertIn("PASS: card=OK", text)
            self.assertIn("NOT_MATCHED:", text)
            self.assertIn("accepted         : 1", text)
            self.assertNotIn("EMPTY_FIXTURE", text)


class ManualAnswerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "images"
        self.inputs.mkdir()
        self.labels = self.inputs / "image_match_labels.json"
        self.results = self.inputs / "image_match_results.json"
        self.config = self.root / "workflow.json"
        config = {
            "inspection_mode": "single_snapshot_cover_card",
            "template_root": "templates", "reference_size": [64, 64],
            "search_roi": [0, 0, 48, 48],
        }
        self.config.write_text(json.dumps(config))
        rng = np.random.default_rng(77)
        for name, state in (
            ("pass.png", "50_COVER_CLOSED_CARD_PRESENT"),
            ("no_card.png", "50_COVER_CLOSED"),
            ("open.png", "50_COVER_OPEN_CARD_PRESENT"),
        ):
            folder = self.root / "templates" / (state + "_CROOP")
            folder.mkdir(parents=True)
            sample = rng.integers(0, 256, (16, 16), dtype=np.uint8)
            cv2.imwrite(str(folder / "sample.png"), sample)
            frame = np.zeros((64, 64, 3), dtype=np.uint8)
            frame[5:21, 7:23] = cv2.cvtColor(sample, cv2.COLOR_GRAY2BGR)
            cv2.imwrite(str(self.inputs / name), frame)

    def run_images(self, *extra):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main(["--image", str(self.inputs), "--config", str(self.config), *extra])
        return code, output.getvalue(), errors.getvalue()

    def test_reruns_preserve_answers_add_new_images_and_list_both_mismatch_types(self):
        self.assertEqual(self.run_images()[0], 0)
        self.assertEqual(json.loads(self.labels.read_text()), {
            "no_card.png": None, "open.png": None, "pass.png": None,
        })
        # Deliberately wrong answers exercise both false pass and false reject.
        answers = {"pass.png": False, "no_card.png": True, "open.png": None,
                   "removed.png": True}
        self.labels.write_text(json.dumps(answers))
        (self.inputs / "new.png").write_bytes((self.inputs / "pass.png").read_bytes())
        code, output, errors = self.run_images("--no-overlay")
        self.assertEqual(code, 1)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(self.labels.read_text()), {**answers, "new.png": None})
        self.assertIn("pass.png: FALSE PASS", output)
        self.assertIn("no_card.png: FALSE REJECT", output)
        self.assertIn("mismatches       : 2", output)
        self.assertIn("labels compared  : 2", output)
        self.assertIn("unlabeled        : 2", output)
        report = json.loads(self.results.read_text())
        # Generated overlays and old answers without a corresponding image are excluded.
        self.assertEqual(len(report), 4)
        self.assertEqual(sum(row["correct"] is False for row in report), 2)
        self.assertEqual(sum(row["correct"] is None for row in report), 2)

        answers.update({"pass.png": True, "no_card.png": False, "open.png": False})
        self.labels.write_text(json.dumps(answers))
        code, output, _ = self.run_images("--no-overlay")
        self.assertEqual(code, 0)
        self.assertIn("mismatches       : 0", output)
        self.assertIn("accuracy         : 100.0%", output)

    def test_bad_labels_are_reported_without_overwriting_manual_work(self):
        for content in ('{broken', '[]', '{"pass.png": "true"}', '{"pass.png": 1}'):
            self.labels.write_text(content)
            with self.subTest(content=content):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        self.run_images("--no-overlay")
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(self.labels.read_text(), content)

    def test_invalid_images_are_listed_in_answers_and_report_without_stopping_batch(self):
        (self.inputs / "broken.jpg").write_bytes(b"not a photograph")
        cv2.imwrite(str(self.inputs / "wrong_size.png"), np.zeros((8, 8, 3), np.uint8))
        code, output, errors = self.run_images("--no-overlay")
        self.assertEqual(code, 1)
        self.assertIn("images evaluated : 3", output)
        self.assertIn("IMAGE ERRORS:", errors)
        self.assertIn("broken.jpg", errors)
        self.assertIn("wrong_size.png", errors)
        labels = json.loads(self.labels.read_text())
        self.assertIn("broken.jpg", labels)
        report = json.loads(self.results.read_text())
        self.assertEqual(len(report), 5)
        self.assertEqual(sum(row["error"] is not None for row in report), 2)

    def test_custom_answer_path_and_result_path(self):
        labels = self.root / "answers" / "manual.json"
        results = self.root / "reports" / "results.json"
        code, _, _ = self.run_images("--no-overlay", "--labels", str(labels),
                                     "--json", str(results))
        self.assertEqual(code, 0)
        self.assertTrue(labels.exists())
        self.assertTrue(results.exists())
        self.assertFalse(self.labels.exists())
        self.assertFalse(self.results.exists())
        self.assertEqual(list(self.inputs.glob("*_overlay.jpg")), [])

    def test_answers_and_results_cannot_share_a_path(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_images("--json", str(self.labels), "--no-overlay")
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.labels.exists())


if __name__ == "__main__":
    unittest.main()
