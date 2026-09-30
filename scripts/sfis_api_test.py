#!/usr/bin/env python3
"""Manually test SFIS Step1; Step2 requires the explicit --step2-test switch."""

import argparse
import copy
import json
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dmz_client import DMZClient, DMZError  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sn", required=True, help="Serial number to send to SFIS")
    parser.add_argument(
        "--step2-test",
        action="store_true",
        help="After Step1 succeeds, explicitly allow one Step2 test request",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "apl_workflow.json",
        help="APL workflow configuration file",
    )
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    client_config = copy.deepcopy(config.get("dmz") or {})
    # The production setting remains untouched. This process-local override is
    # possible only when the operator explicitly supplied --step2-test.
    client_config["step2_enabled"] = bool(args.step2_test)
    # This harness exists to reach the real server, so the offline switch in
    # the configuration file must not silence it.
    client_config["sfis_enabled"] = True
    client = DMZClient(client_config)
    cycle_id = uuid.uuid4().hex

    step1_payload = {
        "sn": args.sn,
        "cycle_id": cycle_id,
        "request_id": uuid.uuid4().hex,
        "station_id": str(config.get("station_id", "AI-CAM")),
    }
    try:
        step1 = client.send("/sn/check", step1_payload)
        print(json.dumps({"step": 1, "accepted": True, "response": step1.get("sfis_response")}, ensure_ascii=False))
        if not args.step2_test:
            print("Step2 skipped. Use --step2-test only when an SFIS station update is intended.")
            return 0
        step2_payload = {
            **step1_payload,
            "request_id": uuid.uuid4().hex,
            "result": "PASS",
        }
        step2 = client.send("/result", step2_payload)
        print(json.dumps({"step": 2, "accepted": True, "response": step2.get("sfis_response")}, ensure_ascii=False))
        return 0
    except DMZError as exc:
        print(json.dumps({"accepted": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
