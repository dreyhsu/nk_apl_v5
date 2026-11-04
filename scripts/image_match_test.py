#!/usr/bin/env python3
"""Offline APL template matching on still images; writes annotated JPGs.

Runs the same APLMatcher and overlay renderer the production station uses, but
headless: no station, no SFIS, no upload. Every output image carries the ROI,
the matched bounding box, and the match state/score. Accepts a single image or
a directory of images.

In single_snapshot_cover_card mode, loads the target and two counterexamples
only, and reports the same card/cover score margins as Auto Capture. Each image
is an independent visual inspection; consecutive-hit counting is camera-only.

Creates image_match_labels.json beside the inputs: map each filename to true
(should PASS), false (should fail), or null (not labeled). Existing labels are
preserved and new images are added automatically. Every run lists mismatches
and writes image_match_results.json (override with --json).
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apl_matching import UNKNOWN, APLMatcher  # noqa: E402
from workflow_view import draw_opencv  # noqa: E402

# Same set APLMatcher accepts for golden samples.
SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
OVERLAY_TAG = "_overlay"
LABELS_NAME = "image_match_labels.json"
RESULTS_NAME = "image_match_results.json"


def _save_json(path, value):
    """Replace complete JSON atomically so reruns preserve manual answers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, default=float)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sync_labels(path, images):
    """Do not infer ground truth from predictions or remove old manual labels."""
    labels = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(labels, dict):
        raise ValueError("Labels must be an object mapping filenames to true, false, or null")
    for name, value in labels.items():
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"Invalid label for {name!r}: use true, false, or null")
    for image in images:
        labels.setdefault(image.name, None)
    _save_json(path, labels)
    return labels


def _annotate(vis, result):
    """Draw the score/gap/reason line the workflow panel omits."""
    raw = result.get("raw", UNKNOWN)
    color = (90, 210, 130) if raw != UNKNOWN else (100, 100, 255)  # BGR green / red
    line = (f"{raw}  score={result.get('score', 0.0):.3f} "
            f"gap={result.get('gap', 0.0):.3f}  {result.get('reason', '')}")
    y = vis.shape[0] - 28
    cv2.putText(vis, line, (24, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(vis, line, (24, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                color, 2, cv2.LINE_AA)
    if "conditions" in result:
        conditions = result["conditions"]
        line = (f"Card: {'OK' if conditions['mcio_card_present'] else 'NG'} "
                f"(margin {conditions['card_margin']:.3f})  "
                f"Cover: {'OK' if conditions['cover_closed'] else 'NG'} "
                f"(margin {conditions['cover_margin']:.3f})")
        for stroke, ink in ((4, (0, 0, 0)), (2, color)):
            cv2.putText(vis, line, (24, y - 36), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, ink, stroke, cv2.LINE_AA)
    return vis


def _collect(target):
    """One image, or every image in a directory excluding our own overlays."""
    if target.is_file():
        return [target]
    images = [path for path in sorted(target.iterdir())
              if path.is_file() and path.suffix.lower() in SUFFIXES
              and not path.stem.endswith(OVERLAY_TAG)]
    return images


def _read(path):
    """imdecode keeps non-ASCII paths working where cv2.imread does not."""
    return cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)


def _write(path, image, quality):
    ok, buffer = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise ValueError("could not encode overlay JPEG")
    path.write_bytes(buffer.tobytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path,
                        help="Input image, or a directory of images, to run matching on")
    parser.add_argument("--output", type=Path, default=None,
                        help="Annotated output file (single image) or directory "
                             "(default: <input>_overlay.jpg beside each input)")
    parser.add_argument("--config", type=Path,
                        default=ROOT / "config" / "apl_workflow.json",
                        help="APL workflow configuration file")
    parser.add_argument("--json", type=Path, default=None,
                        help=f"Per-image results JSON (default: {RESULTS_NAME} in the input folder)")
    parser.add_argument("--labels", type=Path, default=None,
                        help=f"Manual answers JSON (default: {LABELS_NAME} in the input folder); "
                             "filename -> true, false, or null")
    parser.add_argument("--no-overlay", action="store_true",
                        help="Only report scores; skip writing annotated images")
    parser.add_argument("--quality", type=int, default=95,
                        help="Overlay JPEG quality (1-100)")
    args = parser.parse_args(argv)

    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if not args.image.exists():
        parser.error(f"Image not found: {args.image}")
    images = _collect(args.image)
    if not images:
        parser.error(f"No image with a supported suffix in {args.image}")

    input_dir = args.image if args.image.is_dir() else args.image.parent
    labels_path = args.labels if args.labels is not None else input_dir / LABELS_NAME
    report_path = args.json if args.json is not None else input_dir / RESULTS_NAME
    protected_paths = {args.config.resolve(), *(path.resolve() for path in images)}
    if (labels_path.resolve() == report_path.resolve()
            or labels_path.resolve() in protected_paths
            or report_path.resolve() in protected_paths):
        parser.error("Labels, results, workflow config, and input images must use different paths")
    try:
        labels = _sync_labels(labels_path, images)
    except (OSError, ValueError) as exc:
        parser.error(f"Cannot load/save labels {labels_path}: {exc}")
    print(f"manual answers: {labels_path} (true=PASS, false=fail, null=unlabeled)")

    # --output as a file only makes sense for a single input image.
    out_file = out_dir = None
    if args.output is not None and not args.no_overlay:
        if len(images) == 1 and args.output.suffix.lower() in SUFFIXES:
            out_file = args.output
            out_file.parent.mkdir(parents=True, exist_ok=True)
        else:
            if len(images) > 1 and args.output.suffix.lower() in SUFFIXES:
                parser.error("--output must be a directory when --image is a directory")
            out_dir = args.output
            out_dir.mkdir(parents=True, exist_ok=True)

    config = json.loads(Path(args.config).read_bytes())
    matcher = APLMatcher(config, str(args.config))
    snapshot = matcher.inspection_mode == "single_snapshot_cover_card"
    evaluate = matcher.evaluate_snapshot if snapshot else matcher.evaluate
    ref_w, ref_h = matcher.size
    print(f"inspection mode: {matcher.inspection_mode}")
    for name, samples in matcher.samples.items():
        print(f"  golden sample: {name}_CROOP ({len(samples)} images)")
    if snapshot:
        print("PASS requires target score >= threshold and both card/cover margins >= min_gap.")
        print("Each image is evaluated independently; Auto Capture consecutive hits are not simulated.")

    report = []
    mismatches = []
    errors = []
    accepted = failed = 0
    peak_score = 0.0

    for path in images:
        expected = labels[path.name]
        try:
            frame = _read(path)
            if frame is None or not frame.size:
                raise ValueError("not a decodable image")
            height, width = frame.shape[:2]
            if (width, height) != (ref_w, ref_h):
                raise ValueError(f"image is {width}x{height}; must be {ref_w}x{ref_h} "
                                 "to match the golden samples (no auto-scaling)")
            result = evaluate(frame)
        except (OSError, ValueError, cv2.error) as exc:
            error = str(exc)
            print(f"error: {path.name}: {error}", file=sys.stderr)
            errors.append((path.name, error))
            report.append({"image": str(path), "overlay": None, "expected_pass": expected,
                           "actual_pass": None, "correct": None, "error": error, "result": None})
            failed += 1
            continue

        raw = result.get("raw", UNKNOWN)
        peak_score = max(peak_score, float(result.get("score", 0.0)))
        passed = bool(result["accepted"]) if snapshot else raw != UNKNOWN
        if passed:
            accepted += 1
        correct = None if expected is None else passed == expected
        if correct is False:
            mismatches.append((path.name, expected, passed, result))

        state = {
            "state": ("PASS_CONFIRM" if passed else "NOT_MATCHED") if snapshot else "JUDGING",
            "inspection_mode": matcher.inspection_mode,
            "step_index": int(passed) if snapshot else 0,
            "steps": [{"name": name} for name in
                      ([matcher.target_state] if snapshot else config["steps"])],
            "sn": "IMAGE_TEST",
            "matching": result | {"stable": result.get("stable", raw)},
        }
        written = None
        if not args.no_overlay:
            if out_file is not None:
                written = out_file
            elif out_dir is not None:
                written = out_dir / (path.stem + OVERLAY_TAG + ".jpg")
            else:
                written = path.with_name(path.stem + OVERLAY_TAG + ".jpg")
            _write(written, _annotate(draw_opencv(frame, state), result), args.quality)

        print(f"{path.name}: {raw}  score={result.get('score', 0.0):.3f} "
              f"gap={result.get('gap', 0.0):.3f}  {result.get('reason', '')}"
              + (f"  -> {written}" if written else ""))
        if snapshot:
            conditions = result["conditions"]
            verdict = "PASS" if passed else "NOT_MATCHED"
            print(f"  {verdict}: card={'OK' if conditions['mcio_card_present'] else 'NG'} "
                  f"(margin={conditions['card_margin']:.3f}), "
                  f"cover={'OK' if conditions['cover_closed'] else 'NG'} "
                  f"(margin={conditions['cover_margin']:.3f})")
            for name, score in result["class_scores"].items():
                print(f"  {name}: {score:.3f}")
        if expected is not None:
            print(f"  answer: {'PASS' if expected else 'FAIL'}; "
                  f"{'CORRECT' if correct else 'MISMATCH'}")
        report.append({"image": str(path), "overlay": str(written) if written else None,
                       "expected_pass": expected, "actual_pass": passed, "correct": correct,
                       "error": None, "result": result})

    _save_json(report_path, report)
    print(f"wrote {report_path}")

    evaluated = len(report) - failed
    compared = sum(item["correct"] is not None for item in report)
    unlabeled = sum(labels[path.name] is None for path in images)
    print(f"  images evaluated : {evaluated}")
    print(f"  accepted         : {accepted}")
    print(f"  labels compared  : {compared}")
    print(f"  unlabeled        : {unlabeled}")
    print(f"  mismatches       : {len(mismatches)}")
    if compared:
        print(f"  accuracy         : {(compared - len(mismatches)) / compared:.1%}")
    print(f"  peak score       : {peak_score:.3f} (threshold {matcher.threshold:.3f}, "
          f"min gap {matcher.min_gap:.3f})")
    if failed:
        print(f"  skipped          : {failed}", file=sys.stderr)
    if mismatches:
        print("\nMISMATCHES (manual answer vs template matching):")
        for name, expected, actual, result in mismatches:
            kind = "FALSE PASS" if actual else "FALSE REJECT"
            print(f"  {name}: {kind}; expected={'PASS' if expected else 'FAIL'}, "
                  f"actual={'PASS' if actual else 'FAIL'}, score={result['score']:.3f}, "
                  f"gap={result['gap']:.3f}, reason={result['reason']}")
    if errors:
        print("\nIMAGE ERRORS:", file=sys.stderr)
        for name, error in errors:
            print(f"  {name}: {error}", file=sys.stderr)
    return 1 if failed or mismatches or not evaluated else 0


if __name__ == "__main__":
    raise SystemExit(main())
