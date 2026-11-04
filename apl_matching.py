"""Fixed-view APL template classifier with an explicit UNKNOWN reject state."""

import math
from pathlib import Path


UNKNOWN = "UNKNOWN"


class StableStateFilter:
    def __init__(self, required_frames=2):
        self.required_frames = max(1, int(required_frames))
        self.reset()

    def reset(self):
        self.candidate = UNKNOWN
        self.count = 0
        self.stable = UNKNOWN

    def update(self, raw):
        if raw == self.candidate:
            self.count += 1
        else:
            self.candidate, self.count = raw, 1
        if self.count >= self.required_frames:
            self.stable = raw
        return self.stable


class APLMatcher:
    def __init__(self, config, config_path):
        import cv2
        import numpy as np
        self.cv2, self.np = cv2, np
        self.inspection_mode = str(config.get("inspection_mode", "legacy_workflow"))
        self.target_state = str(config.get("target_state", "50_COVER_CLOSED_CARD_PRESENT"))
        self.snapshot_reject_states = list(config.get(
            "snapshot_reject_states", ["50_COVER_CLOSED", "50_COVER_OPEN_CARD_PRESENT"]
        ))
        self.size = tuple(int(x) for x in config.get("reference_size", [1920, 1080]))
        self.roi = tuple(int(x) for x in config.get("search_roi", [751, 254, 431, 588]))
        if len(self.size) != 2 or min(self.size) <= 0 or len(self.roi) != 4:
            raise ValueError("Invalid reference size or search ROI")
        x, y, w, h = self.roi
        if x < 0 or y < 0 or min(w, h) <= 0 or x + w > self.size[0] or y + h > self.size[1]:
            raise ValueError("Search ROI is outside reference image")
        self.threshold = float(config.get("threshold", 0.8))
        self.min_gap = float(config.get("min_gap", 0.05))
        if not 0 <= self.threshold <= 1 or not 0 <= self.min_gap <= 2:
            raise ValueError("Invalid matching threshold or gap")
        method_name = config.get("method", "TM_CCOEFF_NORMED")
        if method_name not in {"TM_CCOEFF_NORMED", "TM_CCORR_NORMED", "TM_SQDIFF_NORMED"}:
            raise ValueError("Unsupported template matching method")
        self.method = getattr(cv2, method_name)
        angles = config.get("template_angles_deg", [0])
        if (not isinstance(angles, list) or not angles
                or any(isinstance(angle, bool) or not isinstance(angle, (int, float))
                       or not math.isfinite(angle) or not -180 <= angle <= 180
                       for angle in angles)):
            raise ValueError("template_angles_deg must be a non-empty list of finite angles from -180 to 180")
        self.template_angles_deg = list(dict.fromkeys(float(angle) for angle in angles))
        root = (Path(config_path).parent / config.get("template_root", "../templates")).resolve()
        self.samples = {}
        self.template_variants = {}
        if self.inspection_mode == "single_snapshot_cover_card":
            # These two counterexamples are used by evaluate_snapshot below.
            # Reject an incomplete configuration rather than silently skipping
            # either the card or the cover comparison.
            required_rejects = {"50_COVER_CLOSED", "50_COVER_OPEN_CARD_PRESENT"}
            if not required_rejects.issubset(self.snapshot_reject_states):
                raise ValueError("Snapshot inspection requires both cover/card reject states")
            if self.target_state in self.snapshot_reject_states:
                raise ValueError("Target state cannot also be a snapshot reject state")
            classes = [self.target_state] + self.snapshot_reject_states
        else:
            classes = list(config["steps"]) + list(config.get("ng_states", [])) + [config.get("removal_state", "EMPTY_FIXTURE")]
        for state in dict.fromkeys(classes):
            folder = root / (state + "_CROOP")
            samples = []
            for path in sorted(folder.glob("*")):
                if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}:
                    continue
                image = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                if image is None or not image.size:
                    raise ValueError("Unreadable template: " + str(path))
                th, tw = image.shape
                if tw > w or th > h:
                    raise ValueError("Template exceeds fixed search ROI: " + str(path))
                if float(image.std()) < 1e-6:
                    raise ValueError("Constant template cannot be matched reliably: " + str(path))
                samples.append((path.name, image))
            if not samples:
                raise ValueError("No valid golden sample for " + state)
            self.samples[state] = samples
            variants = []
            # Prefer the original template on exact score ties. Rotation is
            # performed once here, never in the per-frame matching loop.
            for name, image in samples:
                for angle in sorted(self.template_angles_deg, key=lambda value: value != 0):
                    if angle == 0:
                        rotated = image
                    else:
                        th, tw = image.shape
                        matrix = cv2.getRotationMatrix2D(((tw - 1) / 2, (th - 1) / 2), angle, 1.0)
                        rotated = cv2.warpAffine(image, matrix, (tw, th),
                                                 flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                    if float(rotated.std()) < 1e-6:
                        raise ValueError("Constant rotated template cannot be matched reliably: " + name)
                    variants.append((name, angle, rotated))
            self.template_variants[state] = variants

        if self.inspection_mode == "single_snapshot_cover_card":
            required = [self.target_state] + self.snapshot_reject_states
            missing = [state for state in required if state not in self.samples]
            if missing:
                raise ValueError("Missing snapshot templates: " + ", ".join(missing))

    def _best_by_class(self, gray):
        cv2 = self.cv2
        x, y, _, _ = self.roi
        best_by_class = {}
        for state, variants in self.template_variants.items():
            best = None
            for name, angle, template in variants:
                scores = cv2.matchTemplate(gray, template, self.method)
                mn, mx, mnloc, mxloc = cv2.minMaxLoc(scores)
                score, location = (1.0 - float(mn), mnloc) if self.method == cv2.TM_SQDIFF_NORMED else (float(mx), mxloc)
                if not math.isfinite(score):
                    score = -1.0
                item = {"score": score, "template": name, "angle_deg": angle,
                        "box": [x + location[0], y + location[1], template.shape[1], template.shape[0]]}
                if best is None or score > best["score"]:
                    best = item
            best_by_class[state] = best
        return best_by_class

    def evaluate(self, frame):
        cv2 = self.cv2
        if frame is None or len(frame.shape) != 3 or frame.shape[2] != 3:
            raise ValueError("APL requires a BGR camera frame")
        if (frame.shape[1], frame.shape[0]) != self.size:
            raise ValueError("Camera frame dimensions do not match golden sample reference")
        x, y, w, h = self.roi
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y:y+h, x:x+w]
        best_by_class = self._best_by_class(gray)
        ranking = sorted(best_by_class.items(), key=lambda item: item[1]["score"], reverse=True)
        state, best = ranking[0]
        gap = best["score"] - (ranking[1][1]["score"] if len(ranking) > 1 else -1)
        reason = "below_threshold" if best["score"] < self.threshold else "insufficient_gap" if gap < self.min_gap else "accepted"
        return {
            "raw": state if reason == "accepted" else UNKNOWN,
            "reason": reason, "best_state": state, "score": best["score"], "gap": gap,
            "box": best["box"] if reason == "accepted" else None,
            "roi": list(self.roi), "class_scores": {k: v["score"] for k, v in best_by_class.items()},
            "template": best["template"],
            "angle_deg": best.get("angle_deg", 0.0),
            "class_angles_deg": {name: item.get("angle_deg", 0.0) for name, item in best_by_class.items()},
        }

    def evaluate_snapshot(self, frame):
        cv2 = self.cv2
        if frame is None or len(frame.shape) != 3 or frame.shape[2] != 3:
            raise ValueError("APL requires a BGR camera frame")
        if (frame.shape[1], frame.shape[0]) != self.size:
            raise ValueError("Camera frame dimensions do not match golden sample reference")
        x, y, w, h = self.roi
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[y:y+h, x:x+w]
        scores = self._best_by_class(gray)
        target = scores[self.target_state]
        reject_margins = {
            state: target["score"] - scores[state]["score"]
            for state in self.snapshot_reject_states
        }
        failed_reject_states = [
            state for state, margin in reject_margins.items() if margin < self.min_gap
        ]
        # Retain the card/cover diagnostics used by the station and image tool.
        card_margin = reject_margins["50_COVER_CLOSED"]
        cover_margin = reject_margins["50_COVER_OPEN_CARD_PRESENT"]
        card_present = card_margin >= self.min_gap
        cover_closed = cover_margin >= self.min_gap
        accepted = target["score"] >= self.threshold and not failed_reject_states
        reason = (
            "accepted" if accepted else
            "below_threshold" if target["score"] < self.threshold else
            "card_missing_or_ambiguous" if not card_present else
            "cover_open_or_ambiguous" if not cover_closed else
            "reject_state_or_ambiguous"
        )
        return {
            "raw": self.target_state if accepted else UNKNOWN,
            "stable": self.target_state if accepted else UNKNOWN,
            "accepted": accepted,
            "reason": reason,
            "best_state": self.target_state,
            "score": target["score"],
            "gap": min(reject_margins.values()),
            "reject_margins": reject_margins,
            "failed_reject_states": failed_reject_states,
            "box": target["box"] if accepted else None,
            "roi": list(self.roi),
            "class_scores": {name: item["score"] for name, item in scores.items()},
            "template": target["template"],
            "angle_deg": target.get("angle_deg", 0.0),
            "class_angles_deg": {name: item.get("angle_deg", 0.0) for name, item in scores.items()},
            "conditions": {
                "cover_closed": cover_closed,
                "mcio_card_present": card_present,
                "cover_margin": cover_margin,
                "card_margin": card_margin,
            },
        }

    def draw(self, frame, station):
        from workflow_view import draw_opencv
        return draw_opencv(frame, station)
