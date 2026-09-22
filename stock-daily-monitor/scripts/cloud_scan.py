#!/usr/bin/env python3
"""Run a Tiingo scan and publish only complete, current US trading-day data."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
STATE = DATA / "stock-monitor-state.json"
LATEST = DATA / "latest-scan.json"
SCANNER = Path(__file__).with_name("scan.py")


def validate_scan(report, trading_day):
    expected = trading_day.isoformat()
    if report.get("provider") != "tiingo":
        raise ValueError("scan provider is not Tiingo")
    benchmarks = report.get("benchmarks") or {}
    market = benchmarks.get("SPY")
    if not market or market.get("date") != expected:
        return False
    if report.get("errors"):
        raise ValueError("scan contains data errors")
    stocks = report.get("stocks") or []
    if not stocks or any(row.get("status") != "ok" or row.get("daily", {}).get("date") != expected
                         for row in stocks):
        raise ValueError("stock data dates are incomplete or inconsistent")
    if any(item.get("date") != expected for item in benchmarks.values()):
        raise ValueError("benchmark data dates are inconsistent")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace an existing scan for today's US date")
    args = parser.parse_args()
    if not os.environ.get("TIINGO_API_KEY", "").strip():
        parser.error("TIINGO_API_KEY secret is missing or empty")

    trading_day = datetime.now(ZoneInfo("America/New_York")).date()
    if trading_day.weekday() >= 5:
        print("US calendar date is a weekend; no scan published")
        return 0
    if LATEST.exists() and not args.force:
        previous = json.loads(LATEST.read_text(encoding="utf-8"))
        if (previous.get("benchmarks") or {}).get("SPY", {}).get("date") == trading_day.isoformat():
            print("A scan for this US trading date already exists; no duplicate published")
            return 0

    with TemporaryDirectory() as temp:
        temp_dir = Path(temp)
        temporary_state = temp_dir / "state.json"
        temporary_report = temp_dir / "scan.json"
        if STATE.exists():
            shutil.copyfile(STATE, temporary_state)
        command = [sys.executable, str(SCANNER), "--provider", "tiingo",
                   "--list", str(ROOT / "stock.txt"), "--state", str(temporary_state),
                   "--output", str(temporary_report)]
        subprocess.run(command, cwd=ROOT, check=True)
        report = json.loads(temporary_report.read_text(encoding="utf-8"))
        if not validate_scan(report, trading_day):
            print("Tiingo has not published the expected US trading date; no scan published")
            return 0
        (DATA / "scans").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(temporary_report, DATA / "scans" / f"{trading_day.isoformat()}.json")
        shutil.copyfile(temporary_report, LATEST)
        shutil.copyfile(temporary_state, STATE)
    print(f"Published complete Tiingo scan for {trading_day.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
