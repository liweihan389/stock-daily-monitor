#!/usr/bin/env python3
"""Record an AI-reviewed support candidate in monitor state."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--candidate-index", type=int, required=True, help="1-based index in support_candidates")
    parser.add_argument("--confidence", choices=["高", "中", "低"], required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()
    if not args.reason.strip():
        parser.error("reason is required")
    scan = json.loads(args.scan.read_text(encoding="utf-8"))
    if scan.get("dry_run"):
        parser.error("cannot record support from dry-run output")
    ticker = args.ticker.upper()
    match = next((item for item in scan["stocks"] if item["ticker"] == ticker), None)
    if not match or match.get("status") != "ok":
        parser.error("ticker has no valid scan data")
    candidates = match.get("support_candidates", [])
    if not 1 <= args.candidate_index <= len(candidates):
        parser.error("candidate index out of range")
    candidate = candidates[args.candidate_index - 1]
    state = json.loads(args.state.read_text(encoding="utf-8"))
    item = state.get("stocks", {}).get(ticker)
    if not item or item.get("last_scan_date") != match["daily"]["date"]:
        parser.error("state does not match scan date")
    item["support"] = {"low": candidate["low"], "high": candidate["high"],
                       "center": candidate["center"], "evidence": candidate["evidence"],
                       "confidence": args.confidence, "reason": args.reason.strip(),
                       "set_on": match["daily"]["date"]}
    item["below_dates"] = []
    item["low_alert"] = None
    args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ticker": ticker, "support": item["support"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
