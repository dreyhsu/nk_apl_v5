#!/usr/bin/env python3
"""Offline APL template matching on a video; writes an annotated MP4.

Runs the same APLMatcher and overlay renderer the production Browser Video Test
uses, but headless: no station, no SFIS, no upload. Every output frame carries
the ROI, the matched bounding box, and the match state/score.
"""

import argparse
import json
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apl_matching import UNKNOWN, APLMatcher  # noqa: E402
from edge_runtime import open_video_file  # noqa: E402
from workflow_view import draw_opencv  # noqa: E402


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", required=True, type=Path,
                        help="Input video (.mp4) to run template matching on")
    parser.add_argument("--output", type=Path, default=None,
                        help="Annotated MP4 output (default: <input>_annotated.mp4)")
    parser.add_argument("--config", type=Path,
                        default=ROOT / "config" / "apl_workflow.json",
                        help="APL workflow configuration file")
    parser.add_argument("--analysis-fps", type=float, default=10.0,
                        help="Matching rate; last result is held between samples")
    parser.add_argument("--every-frame", action="store_true",
                        help="Evaluate every frame instead of sampling (slower)")
    parser.add_argument("--max-seconds", type=float, default=None,
                        help="Stop after this many seconds of source video")
    parser.add_argument("--fourcc", default="mp4v",
                        help="VideoWriter codec (mp4v; use avc1 for true H.264)")
    args = parser.parse_args()

    if not args.video.is_file():
        parser.error(f"Video not found: {args.video}")
    output = args.output or args.video.with_name(args.video.stem + "_annotated.mp4")

    config = json.loads(Path(args.config).read_bytes())
    matcher = APLMatcher(config, str(args.config))
    snapshot = str(config.get("inspection_mode")) == "single_snapshot_cover_card"
    evaluate = matcher.evaluate_snapshot if snapshot else matcher.evaluate
    ref_w, ref_h = matcher.size

    cap = open_video_file(args.video, realtime=False)
    if not cap.isOpened():
        print(f"error: could not open video {args.video}", file=sys.stderr)
        return 1

    ok, frame = cap.read()
    if not ok or frame is None:
        print("error: video has no decodable frames", file=sys.stderr)
        cap.release()
        return 1
    height, width = frame.shape[:2]
    if (width, height) != (ref_w, ref_h):
        print(f"error: video is {width}x{height}; must be {ref_w}x{ref_h} to match "
              "the golden samples (no auto-scaling)", file=sys.stderr)
        cap.release()
        return 1

    src_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    if not (0.1 <= src_fps <= 240.0):
        src_fps = 30.0
    stride = 1 if args.every_frame else max(1, round(src_fps / max(0.1, args.analysis_fps)))
    max_frames = None if args.max_seconds is None else int(args.max_seconds * src_fps)

    writer = cv2.VideoWriter(str(output), cv2.VideoWriter_fourcc(*args.fourcc),
                             src_fps, (width, height))
    if not writer.isOpened():
        print(f"error: could not open VideoWriter with fourcc '{args.fourcc}'; "
              "try --fourcc mp4v", file=sys.stderr)
        cap.release()
        return 1

    frame_index = 0
    sampled = accepted = 0
    peak_score = 0.0
    first_accept_time = None
    last_result = {"raw": UNKNOWN, "score": 0.0, "gap": 0.0, "reason": "pending"}

    try:
        while True:
            if max_frames is not None and frame_index >= max_frames:
                break
            if frame_index % stride == 0:
                last_result = evaluate(frame)
                sampled += 1
                peak_score = max(peak_score, float(last_result.get("score", 0.0)))
                if last_result.get("raw", UNKNOWN) != UNKNOWN:
                    accepted += 1
                    if first_accept_time is None:
                        first_accept_time = frame_index / src_fps

            state = {
                "state": "JUDGING",
                "inspection_mode": config.get("inspection_mode"),
                "steps": [{"name": matcher.target_state}],
                "sn": "VIDEO_TEST",
                "matching": last_result | {
                    "stable": last_result.get("stable", last_result.get("raw", UNKNOWN)),
                },
            }
            writer.write(_annotate(draw_opencv(frame, state), last_result))
            frame_index += 1

            ok, frame = cap.read()
            if not ok or frame is None:
                break
    finally:
        cap.release()
        writer.release()

    accept_at = "n/a" if first_accept_time is None else f"{first_accept_time:.2f}s"
    print(f"wrote {output}")
    print(f"  frames written : {frame_index}")
    print(f"  frames sampled : {sampled} (stride {stride}, ~{src_fps / stride:.1f} fps)")
    print(f"  accepted       : {accepted}")
    print(f"  peak score     : {peak_score:.3f}")
    print(f"  first accept   : {accept_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
