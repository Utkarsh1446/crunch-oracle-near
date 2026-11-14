#!/usr/bin/env python3
"""Print the open Poisson forecasts from the OpenLigaDB cache or a live refresh."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oracle.model import forecast_match
from oracle.openliga import CACHE, load_cache, refresh_cache
from oracle.select import select_demo_matches


def main() -> None:
    parser = argparse.ArgumentParser(description="Forecast the demo matches from public results")
    parser.add_argument("--refresh", action="store_true", help="Fetch OpenLigaDB before forecasting")
    args = parser.parse_args()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.refresh or not CACHE.exists():
        matches = refresh_cache(now_iso)
    else:
        matches = load_cache()
    replays, live = select_demo_matches(matches, now_iso)
    rows = []
    for match in replays + ([live] if live else []):
        forecast = forecast_match(matches, match)
        forecast["label"] = "historical replay" if match in replays else "before published kickoff"
        forecast["finished"] = match["finished"]
        forecast["home_goals"] = match["home_goals"]
        forecast["away_goals"] = match["away_goals"]
        forecast["source_url"] = match["source_url"]
        rows.append(forecast)
    print(json.dumps({"as_of": now_iso, "forecasts": rows}, indent=2))


if __name__ == "__main__":
    main()
