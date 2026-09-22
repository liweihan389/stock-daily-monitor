#!/usr/bin/env python3
"""Deterministic AKShare/Yahoo daily screen; attribution and email stay with the skill."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys

import pandas as pd

TICKER = re.compile(r"[A-Z0-9][A-Z0-9.\-^=]{0,19}")
GROUPS = {"持仓股票": "holding", "候选池股票": "watchlist"}


def load_list(path):
    rows, group = [], None
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            title = line.lstrip("# ").strip()
            if title in GROUPS:
                group = GROUPS[title]
            continue
        if group is None:
            raise ValueError(f"line {number}: place ticker under a supported group heading")
        parts = line.split()
        ticker = parts[0].upper()
        if not TICKER.fullmatch(ticker):
            raise ValueError(f"line {number}: invalid ticker")
        settings = {}
        for part in parts[1:]:
            if part.startswith("sector="):
                sector = part.split("=", 1)[1].upper()
                if not TICKER.fullmatch(sector):
                    raise ValueError(f"line {number}: invalid sector ticker")
                settings["sector"] = sector
            elif part.startswith("peers="):
                peers = [ticker.upper() for ticker in part.split("=", 1)[1].split(",")]
                if not 2 <= len(peers) <= 4 or len(set(peers)) != len(peers) or ticker in peers or any(
                    not TICKER.fullmatch(peer) for peer in peers
                ):
                    raise ValueError(f"line {number}: peers must be 2–4 unique ticker symbols other than the stock")
                settings["peers"] = peers
            else:
                raise ValueError(f"line {number}: unknown option {part}")
        if "sector" in settings and "peers" in settings:
            raise ValueError(f"line {number}: choose sector or peers, not both")
        if any(row["ticker"] == ticker for row in rows):
            raise ValueError(f"line {number}: duplicate ticker {ticker}")
        rows.append({"ticker": ticker, "group": group, **settings})
    if not rows:
        raise ValueError("stock list is empty")
    return rows


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def rounded(value, digits=2):
    value = finite(value)
    return round(value, digits) if value is not None else None


def read_tiingo_key(path):
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Tiingo key file not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Tiingo key file cannot be read as valid JSON") from exc
    if not isinstance(credentials, dict) or not isinstance(credentials.get("api_key"), str) or not credentials["api_key"].strip():
        raise ValueError("Tiingo key file must contain a nonempty api_key string")
    return credentials["api_key"].strip()


def fetch(ticker, provider, tiingo_token=None):
    if provider == "akshare":
        import akshare as ak
        frame = ak.stock_us_daily(symbol=ticker, adjust="qfq").rename(columns={
            "open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
        frame = frame.set_index("date")
        frame["Adj Close"] = frame["Close"]  # Already forward-adjusted by AKShare/Sina.
        frame = frame.tail(520)
    elif provider == "tiingo":
        import requests
        if not tiingo_token:
            raise ValueError("Tiingo API key is not loaded")
        tiingo_ticker = ticker.replace(".", "-")
        start = (datetime.now(timezone.utc).date() - timedelta(days=800)).isoformat()
        response = requests.get(
            f"https://api.tiingo.com/tiingo/daily/{tiingo_ticker}/prices",
            params={"startDate": start},
            headers={"Authorization": f"Token {tiingo_token}", "Content-Type": "application/json"},
            timeout=25,
        )
        if response.status_code != 200:
            # Never include response bodies or request headers: either may expose the API token.
            raise ValueError(f"Tiingo HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise ValueError("Tiingo returned no daily bars")
        frame = pd.DataFrame(payload).rename(columns={
            "open": "Open", "high": "High", "low": "Low", "close": "Close",
            "adjOpen": "Adj Open", "adjHigh": "Adj High", "adjLow": "Adj Low",
            "adjClose": "Adj Close", "volume": "Raw Volume", "adjVolume": "Volume"})
        required = {"date", "Open", "High", "Low", "Close", "Adj Open", "Adj High",
                    "Adj Low", "Adj Close", "Volume", "Raw Volume"}
        if not required.issubset(frame.columns):
            raise ValueError("Tiingo response lacks required adjusted OHLCV fields")
        frame = frame.set_index("date").tail(520)
    else:
        import yfinance as yf
        frame = yf.download(ticker, period="2y", interval="1d", auto_adjust=False,
                            actions=True, progress=False, threads=False, timeout=25,
                            multi_level_index=False)
    if frame.empty:
        raise ValueError(f"{provider} returned no daily bars")
    frame = frame.sort_index()
    frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
    frame = frame[~frame.index.duplicated(keep="last")]
    for col in ("Open", "High", "Low", "Close", "Adj Close", "Volume"):
        if col not in frame:
            raise ValueError(f"missing {col}")
    frame = frame.dropna(subset=["Close", "Adj Close", "High", "Low"])
    frame = frame[(frame["Close"] > 0) & (frame["Adj Close"] > 0)]
    if len(frame) < 30:
        raise ValueError("fewer than 30 valid daily bars")
    factor = frame["Adj Close"] / frame["Close"]
    # Latest quote dollars are the display unit. Historical adjusted bars remain comparable.
    display_factor = float(factor.iloc[-1])
    for col in ("Open", "High", "Low", "Close"):
        adjusted_col = f"Adj {col}"
        frame[f"P_{col}"] = (frame[adjusted_col] if provider == "tiingo" else frame[col] * factor) / display_factor
    return frame


def support_candidates(frame):
    close = frame["P_Close"]
    high, low, volume = frame["P_High"], frame["P_Low"], frame["Volume"].fillna(0)
    prior = close.shift(1)
    tr = pd.concat([(high - low), (high - prior).abs(), (low - prior).abs()], axis=1).max(axis=1)
    atr = finite(tr.tail(14).mean()) or float(close.iloc[-1]) * .02
    last = float(close.iloc[-1])
    width = max(atr * .5, last * .0075)
    candidates = []
    for days in (20, 50, 200):
        if len(close) >= days:
            ma = finite(close.tail(days).mean())
            earlier = finite(close.iloc[-days-5:-5].mean()) if len(close) >= days + 5 else None
            if ma and ma <= last * 1.05:
                candidates.append({"kind": f"ma{days}", "center": ma,
                                   "rising_5d": bool(earlier is not None and ma > earlier)})
    tail = frame.tail(120)
    typical = (tail["P_High"] + tail["P_Low"] + tail["P_Close"]) / 3
    # Daily volume assigned to a typical-price bucket is an approximation, not a true chip profile.
    bucket_size = max(atr * .5, last * .01)
    buckets = ((typical / bucket_size).round() * bucket_size)
    clusters = tail.groupby(buckets)["Volume"].sum().sort_values(ascending=False).head(4)
    total_volume = finite(tail["Volume"].sum()) or 0
    for center, vol in clusters.items():
        center = finite(center)
        if center and center <= last * 1.05:
            candidates.append({"kind": "estimated_volume_cluster", "center": center,
                               "share_of_120d_volume": rounded(vol / total_volume, 3) if total_volume else None})
    candidates.sort(key=lambda item: item["center"], reverse=True)
    zones = []
    for item in candidates:
        matched = next((zone for zone in zones if abs(zone["center"] - item["center"]) <= width), None)
        if matched:
            matched["evidence"].append(item)
            matched["center"] = sum(x["center"] for x in matched["evidence"]) / len(matched["evidence"])
        else:
            zones.append({"center": item["center"], "evidence": [item]})
    result = []
    for zone in zones:
        center = zone["center"]
        if center < last * .7:
            continue
        kinds = {x["kind"] for x in zone["evidence"]}
        score = min(3, len(kinds)) + int("estimated_volume_cluster" in kinds)
        result.append({"low": rounded(center - width), "high": rounded(center + width),
                       "center": rounded(center), "evidence": zone["evidence"],
                       "program_rank": score, "atr14": rounded(atr)})
    result.sort(key=lambda item: (item["low"] > last, -item["center"], -item["program_rank"]))
    return result[:5]


def summary(frame):
    last, previous = frame.iloc[-1], frame.iloc[-2]
    current = float(last["P_Close"])
    return {"date": frame.index[-1].date().isoformat(), "previous_date": frame.index[-2].date().isoformat(),
            "close": rounded(current), "previous_close": rounded(previous["P_Close"]),
            "return_pct": rounded((current / float(previous["P_Close"]) - 1) * 100),
            "high": rounded(last["P_High"]), "low": rounded(last["P_Low"]),
            "volume": rounded(last["Volume"], 0),
            "volume_ratio_20d": rounded(last["Volume"] / frame["Volume"].iloc[-21:-1].mean())
            if len(frame) >= 21 and frame["Volume"].iloc[-21:-1].mean() > 0 else None,
            "history_bars": len(frame)}


def support_status(s, support):
    if not support:
        return "uninitialized"
    low, high = support["low"], support["high"]
    if s["close"] < low:
        return "close_below"
    if s["low"] < low:
        return "intraday_below_recovered"
    if s["low"] <= high:
        return "touched"
    if s["close"] <= high * 1.02:
        return "approaching"
    return "above"


def stage_low_signal(frame, previous_alert=None):
    """Return a material 120/252-session closing low, excluding today's bar from baselines."""
    close = frame["P_Close"]
    if len(close) < 121:
        return {"status": "insufficient_history", "required_prior_sessions": 120,
                "available_prior_sessions": len(close) - 1, "notify": False}
    current = float(close.iloc[-1])
    prior_close = close.iloc[:-1]
    high, low = frame["P_High"], frame["P_Low"]
    true_range = pd.concat([(high - low), (high - close.shift(1)).abs(),
                            (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr14 = finite(true_range.tail(14).mean()) or current * .02
    thresholds = {}
    for sessions in (120, 252):
        if len(prior_close) < sessions:
            continue
        reference = float(prior_close.tail(sessions).min())
        buffer = max(reference * .005, atr14 * .25)
        thresholds[sessions] = {"previous_low": reference, "buffer": buffer,
                                "broken": current < reference - buffer}
    horizon = 252 if thresholds.get(252, {}).get("broken") else (120 if thresholds[120]["broken"] else None)
    if horizon is None:
        return {"status": "no_new_low", "notify": False,
                "previous_120d_low": rounded(thresholds[120]["previous_low"], 4),
                "previous_252d_low": rounded(thresholds[252]["previous_low"], 4) if 252 in thresholds else None}
    previous_alert = previous_alert or {}
    previous_date = previous_alert.get("date")
    recent_dates = {date.date().isoformat() for date in frame.index[-121:-1]}
    still_recent = previous_date in recent_dates
    same_day = previous_date == frame.index[-1].date().isoformat()
    upgrade = still_recent and horizon > previous_alert.get("horizon", 0)
    last_close = finite(previous_alert.get("close")) if still_recent else None
    further_drop = last_close is not None and current < last_close - max(last_close * .01, atr14 * .5)
    notify = not same_day and (not still_recent or upgrade or further_drop)
    return {"status": f"new_{horizon}d_closing_low", "notify": notify,
            "horizon": horizon, "date": frame.index[-1].date().isoformat(),
            "close": rounded(current, 4),
            "previous_low": rounded(thresholds[horizon]["previous_low"], 4),
            "required_break": rounded(thresholds[horizon]["buffer"], 4),
            "atr14": rounded(atr14, 4),
            "notification_kind": "deduplicated" if not notify else
            "first_or_upgrade" if not still_recent or upgrade else "further_drop"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=["akshare", "yahoo", "tiingo"], default="akshare")
    parser.add_argument("--tiingo-key-file", type=Path, default=Path("tiingo.json"),
                        help="Local JSON containing api_key; default ./tiingo.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a new filename")
    rows = load_list(args.list)
    tiingo_token = None
    if args.provider == "tiingo":
        try:
            tiingo_token = read_tiingo_key(args.tiingo_key_file)
        except ValueError as exc:
            parser.error(str(exc))
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {"version": 1, "stocks": {}}
    if state.get("version") != 1 or not isinstance(state.get("stocks"), dict):
        parser.error("unsupported state format")
    if args.provider == "yahoo":
        import yfinance as yf
        cache = args.output.parent / ".yfinance-cache"
        cache.mkdir(parents=True, exist_ok=True)
        yf.set_tz_cache_location(str(cache))
    names = sorted({item["ticker"] for item in rows} |
                   {item["sector"] for item in rows if item.get("sector")} |
                   {peer for item in rows for peer in item.get("peers", [])} | {"SPY"})
    data, errors = {}, {}
    for name in names:
        try:
            data[name] = fetch(name, args.provider, tiingo_token)
        except Exception as exc:
            errors[name] = type(exc).__name__ + ": " + str(exc)
    summaries = {name: summary(frame) for name, frame in data.items()}
    source_names = {"akshare": "AKShare stock_us_daily qfq (Sina)",
                    "yahoo": "Yahoo Finance via yfinance",
                    "tiingo": "Tiingo End-of-Day adjusted OHLCV"}
    result = {"version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "source": source_names[args.provider], "provider": args.provider,
              "list": str(args.list.resolve()),
              "dry_run": args.dry_run, "benchmarks": summaries, "errors": errors, "stocks": []}
    for row in rows:
        ticker, group = row["ticker"], row["group"]
        if ticker not in data:
            result["stocks"].append({**row, "status": "data_missing", "error": errors.get(ticker)})
            continue
        s = summaries[ticker]
        prior = state["stocks"].get(ticker, {})
        support = prior.get("support")
        low_alert = prior.get("low_alert") if prior.get("provider", "akshare") == args.provider else None
        provider_changed = bool(support and prior.get("provider", "akshare") != args.provider)
        if provider_changed:
            support = None
            low_alert = None
        # Existing support is preserved until two distinct daily closes below the same zone.
        below_dates = list(prior.get("below_dates", []))
        if support and s["close"] < support["low"]:
            if prior.get("last_scan_date") != s["previous_date"]:
                below_dates = []
            if s["date"] not in below_dates:
                below_dates = (below_dates + [s["date"]])[-2:]
        else:
            below_dates = []
        broken_support = bool(support and len(below_dates) >= 2)
        if broken_support:
            support = None
            below_dates = []
        recalc = not support
        candidates = support_candidates(data[ticker]) if recalc else []
        status = support_status(s, support)
        if broken_support:
            status = "invalidated_after_two_closes"
        stage_low = stage_low_signal(data[ticker], low_alert) if support is None else None
        if stage_low and stage_low.get("notify"):
            low_alert = {"date": stage_low["date"], "horizon": stage_low["horizon"],
                         "close": stage_low["close"], "provider": args.provider}
        sector = row.get("sector")
        market = summaries.get("SPY")
        comparable = lambda x: x and x["date"] == s["date"] and x["previous_date"] == s["previous_date"]
        market_gap = rounded(s["return_pct"] - market["return_pct"]) if comparable(market) else None
        benchmark_type, benchmark_symbols, benchmark_return, benchmark_components = None, [], None, []
        if sector:
            peer = summaries.get(sector)
            if comparable(peer):
                benchmark_type, benchmark_symbols = "sector_etf", [sector]
                benchmark_return = peer["return_pct"]
                benchmark_components = [{"ticker": sector, "return_pct": peer["return_pct"]}]
        elif row.get("peers"):
            benchmark_symbols = row["peers"]
            benchmark_components = [{"ticker": name, "return_pct": summaries[name]["return_pct"]}
                                    for name in benchmark_symbols if comparable(summaries.get(name))]
            if len(benchmark_components) == len(benchmark_symbols):
                benchmark_type = "peer_equal_weight"
                benchmark_return = rounded(sum(item["return_pct"] for item in benchmark_components) /
                                           len(benchmark_components))
        benchmark_gap = rounded(s["return_pct"] - benchmark_return) if benchmark_return is not None else None
        threshold = 3 if group == "holding" else 5
        reasons = []
        if abs(s["return_pct"]) >= threshold:
            reasons.append("absolute_move")
        if benchmark_gap is not None and abs(benchmark_gap) >= 2:
            reasons.append("sector_relative_move" if benchmark_type == "sector_etf" else "peer_relative_move")
        elif benchmark_type is None and market_gap is not None and abs(market_gap) >= 2:
            reasons.append("market_relative_move_no_comparable_benchmark")
        if s["return_pct"] <= -5:
            reasons.append("large_decline")
        if status in ("touched", "intraday_below_recovered", "close_below"):
            reasons.append("support_event")
        if recalc:
            reasons.append("support_review")
        if stage_low and stage_low.get("notify"):
            reasons.append(f"new_{stage_low['horizon']}d_closing_low")
        record = {**row, "status": "ok", "daily": s, "market_ticker": "SPY",
                  "market_return_pct": market["return_pct"] if comparable(market) else None,
                  "market_gap_pp": market_gap, "benchmark_type": benchmark_type,
                  "benchmark_symbols": benchmark_symbols, "benchmark_components": benchmark_components,
                  "benchmark_return_pct": benchmark_return, "benchmark_gap_pp": benchmark_gap,
                  "benchmark_complete": benchmark_type is not None,
                  "support": support, "support_status": status,
                  "support_recalculation_reason": "provider_changed" if provider_changed else
                  "two_close_break" if broken_support else None,
                  "stage_low": stage_low,
                  "below_dates": below_dates, "support_candidates": candidates,
                  "research": reasons}
        result["stocks"].append(record)
        if not args.dry_run:
            state["stocks"][ticker] = {"support": support, "below_dates": below_dates,
                                        "low_alert": low_alert, "last_scan_date": s["date"],
                                        "provider": args.provider}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not args.dry_run:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "stocks": len(rows),
                      "data_errors": errors, "research": {x["ticker"]: x.get("research", []) for x in result["stocks"]}},
                     ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
