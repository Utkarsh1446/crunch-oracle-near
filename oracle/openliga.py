"""Trimmed OpenLigaDB match facts.

The public API needs no key. This module keeps id, teams, kickoff, and the
final score. It does not store odds, lineups, or viewer counts.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "oracle" / "cache" / "bundesliga_matches.json"
API = "https://api.openligadb.de/getmatchdata/bl1/{season}"
SEASONS = ("2025", "2026")


def final_score(payload: dict) -> tuple[int, int] | None:
    results = payload.get("matchResults") or []
    chosen = [
        result
        for result in results
        if result.get("resultTypeID") == 2 or result.get("resultName") == "Endergebnis"
    ]
    result = chosen[-1] if chosen else (results[-1] if results else None)
    if not result:
        return None
    if result.get("pointsTeam1") is None or result.get("pointsTeam2") is None:
        return None
    return int(result["pointsTeam1"]), int(result["pointsTeam2"])


def trim_match(payload: dict, season: str) -> dict | None:
    match_id = payload.get("matchID")
    home = (payload.get("team1") or {}).get("teamName")
    away = (payload.get("team2") or {}).get("teamName")
    kickoff = payload.get("matchDateTimeUTC") or ""
    if not match_id or not home or not away or not kickoff:
        return None
    if not kickoff.endswith("Z"):
        kickoff = kickoff + "Z"
    finished = bool(payload.get("matchIsFinished"))
    score = final_score(payload) if finished else None
    if finished and score is None:
        return None
    numeric_id = int(match_id)
    return {
        "id": f"bl1-{season}-{numeric_id}",
        "provider_match_id": numeric_id,
        "season": season,
        "competition": "bundesliga",
        "home": home,
        "away": away,
        "kickoff": kickoff,
        "finished": finished,
        "home_goals": score[0] if score else None,
        "away_goals": score[1] if score else None,
        "source_url": f"https://api.openligadb.de/getmatchdata/{numeric_id}",
    }


def fetch_season(season: str, timeout: int = 60) -> list[dict]:
    url = API.format(season=season)
    request = urllib.request.Request(url, headers={"User-Agent": "crunch-oracle/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    matches = []
    for row in payload:
        trimmed = trim_match(row, season)
        if trimmed:
            matches.append(trimmed)
    return matches


def load_cache(path: Path = CACHE) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["matches"]


def write_cache(matches: list[dict], path: Path = CACHE, fetched_at: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "source": "OpenLigaDB",
        "source_pages": [API.format(season=season) for season in SEASONS],
        "note": "Factual Bundesliga match ids, clubs, kickoffs, and final scores from the public OpenLigaDB API. No odds and no account data.",
        "fetched_at": fetched_at,
        "matches": matches,
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def refresh_cache(fetched_at: str) -> list[dict]:
    matches: list[dict] = []
    for season in SEASONS:
        matches.extend(fetch_season(season))
    matches.sort(key=lambda match: (match["kickoff"], match["id"]))
    write_cache(matches, fetched_at=fetched_at)
    return matches
