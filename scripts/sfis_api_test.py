#!/usr/bin/env python3
"""Manually test SFIS Step1; Step4 requires the explicit --step4-test switch."""

import argparse
import copy
import json
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dmz_client import DMZClient, DMZError  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sn", required=True, help="Product serial number to send to SFIS")
    parser.add_argument(
        "--mcio-sn",
        default="",
        help="MCIO card serial number; required with --step4-test because Step4 carries it",
    )
    parser.add_argument(
        "--step4-test",
        action="store_true",
        help="After Step1 succeeds, explicitly allow one Step4 test request",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "apl_workflow.json",
        help="APL workflow configuration file",
    )
    args = parser.parse_args()
    if args.step4_test and not args.mcio_sn.strip():
        parser.error("--step4-test also needs --mcio-sn; SFIS Step4 carries the MCIO card serial number")

    config = json.loads(args.config.read_text(encoding="utf-8"))
    client_config = copy.deepcopy(config.get("dmz") or {})
    # This harness must reach SFIS even when the station config is in bypass mode.
    client_config["bypass_sfis"] = False
    client_config["step4_enabled"] = bool(args.step4_test)
    client = DMZClient(client_config)
    if client.bypass:
        parser.error("APL_DMZ_BYPASS is active; unset it before testing the real SFIS API")
    if client.mode != "http" or client.api_style != "sfis":
        parser.error("The SFIS probe requires dmz.mode=http and dmz.api_style=sfis")
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
        if not args.step4_test:
            print("Step4 skipped. Use --step4-test only when an SFIS station update is intended.")
            return 0
        step4_payload = {
            **step1_payload,
            "request_id": uuid.uuid4().hex,
            "mcio_sn": args.mcio_sn.strip(),
            "result": "PASS",
        }
        step4 = client.send("/result", step4_payload)
        print(json.dumps({"step": 4, "accepted": True, "response": step4.get("sfis_response")}, ensure_ascii=False))
        return 0
    except DMZError as exc:
        print(json.dumps({"accepted": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
