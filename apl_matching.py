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
        root = (Path(config_path).parent / config.get("template_root", "../templates")).resolve()
        self.samples = {}
        if self.inspection_mode == "single_snapshot_cover_card":
            # These two counterexamples are used by evaluate_snapshot below.
            # Reject an incomplete configuration rather than silently skipping
            # either the card or the cover comparison.
            required_rejects = {"50_COVER_CLOSED", "50_COVER_OPEN_CARD_PRESENT"}
            if not required_rejects.issubset(self.snapshot_reject_states):
                raise ValueError("Snapshot inspection requires both cover/card reject states")
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

        if self.inspection_mode == "single_snapshot_cover_card":
            required = [self.target_state] + self.snapshot_reject_states
            missing = [state for state in required if state not in self.samples]
            if missing:
                raise ValueError("Missing snapshot templates: " + ", ".join(missing))

    def _best_by_class(self, gray):
        cv2 = self.cv2
        x, y, _, _ = self.roi
        best_by_class = {}
        for state, samples in self.samples.items():
            best = None
            for name, template in samples:
                scores = cv2.matchTemplate(gray, template, self.method)
                mn, mx, mnloc, mxloc = cv2.minMaxLoc(scores)
                score, location = (1.0 - float(mn), mnloc) if self.method == cv2.TM_SQDIFF_NORMED else (float(mx), mxloc)
                if not math.isfinite(score):
                    score = -1.0
                item = {"score": score, "template": name,
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
        closed_without_card = scores.get("50_COVER_CLOSED")
        open_with_card = scores.get("50_COVER_OPEN_CARD_PRESENT")
        card_margin = target["score"] - (closed_without_card["score"] if closed_without_card else -1.0)
        cover_margin = target["score"] - (open_with_card["score"] if open_with_card else -1.0)
        card_present = card_margin >= self.min_gap
        cover_closed = cover_margin >= self.min_gap
        accepted = target["score"] >= self.threshold and card_present and cover_closed
        reason = (
            "accepted" if accepted else
            "below_threshold" if target["score"] < self.threshold else
            "card_missing_or_ambiguous" if not card_present else
            "cover_open_or_ambiguous"
        )
        return {
            "raw": self.target_state if accepted else UNKNOWN,
            "stable": self.target_state if accepted else UNKNOWN,
            "accepted": accepted,
            "reason": reason,
            "best_state": self.target_state,
            "score": target["score"],
            "gap": min(card_margin, cover_margin),
            "box": target["box"] if accepted else None,
            "roi": list(self.roi),
            "class_scores": {name: item["score"] for name, item in scores.items()},
            "template": target["template"],
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
