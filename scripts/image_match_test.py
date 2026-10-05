#!/usr/bin/env python3
"""Offline APL template matching on still images; writes annotated JPGs.

Runs the same APLMatcher and overlay renderer the production station uses, but
headless: no station, no SFIS, no upload. Every output image carries the ROI,
the matched bounding box, and the match state/score. Accepts a single image or
a directory of images.
"""

import argparse
import json
import sys
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


def main():
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
                        help="Also write the full per-image match results as JSON")
    parser.add_argument("--no-overlay", action="store_true",
                        help="Only report scores; skip writing annotated images")
    parser.add_argument("--quality", type=int, default=95,
                        help="Overlay JPEG quality (1-100)")
    args = parser.parse_args()

    if not 1 <= args.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    if not args.image.exists():
        parser.error(f"Image not found: {args.image}")
    images = _collect(args.image)
    if not images:
        parser.error(f"No image with a supported suffix in {args.image}")

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
    snapshot = str(config.get("inspection_mode")) == "single_snapshot_cover_card"
    evaluate = matcher.evaluate_snapshot if snapshot else matcher.evaluate
    ref_w, ref_h = matcher.size

    report = []
    accepted = failed = 0
    peak_score = 0.0

    for path in images:
        frame = _read(path)
        if frame is None or not frame.size:
            print(f"error: {path.name}: not a decodable image", file=sys.stderr)
            failed += 1
            continue
        height, width = frame.shape[:2]
        if (width, height) != (ref_w, ref_h):
            print(f"error: {path.name}: image is {width}x{height}; must be {ref_w}x{ref_h} "
                  "to match the golden samples (no auto-scaling)", file=sys.stderr)
            failed += 1
            continue

        result = evaluate(frame)
        raw = result.get("raw", UNKNOWN)
        peak_score = max(peak_score, float(result.get("score", 0.0)))
        if raw != UNKNOWN:
            accepted += 1

        state = {
            "state": "JUDGING",
            "inspection_mode": config.get("inspection_mode"),
            "steps": [{"name": matcher.target_state}],
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
        report.append({"image": str(path), "overlay": str(written) if written else None,
                       "result": result})

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=float),
                             encoding="utf-8")
        print(f"wrote {args.json}")

    evaluated = len(report)
    print(f"  images evaluated : {evaluated}")
    print(f"  accepted         : {accepted}")
    print(f"  peak score       : {peak_score:.3f} (threshold {matcher.threshold:.3f}, "
          f"min gap {matcher.min_gap:.3f})")
    if failed:
        print(f"  skipped          : {failed}", file=sys.stderr)
    return 1 if failed or not evaluated else 0


if __name__ == "__main__":
    raise SystemExit(main())
