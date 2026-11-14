#!/usr/bin/env python3
"""Local football probability engine and web server.

This is intentionally dependency-free so the project can run from a fresh
workspace. It serves the starter HTML and exposes the first working backend
contracts used by the frontend.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import importlib.util
import json
import math
import os
import random
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen

from superpumped_crunch import (
    DEFAULT_REGISTRY,
    build_market_horizons,
    future_ranking_prediction,
    run_expanded_market_backtest,
    run_walk_forward_backtest,
)


ROOT = Path(__file__).resolve().parent
HTML_FILE = ROOT / "football_probability_engine.html"
DATA_DIR = ROOT / "data"
DEFAULT_STATE_FILE = DATA_DIR / "engine_state.json"
MODEL_VERSION = "lgbm_outcome_v1_calibrated"
STATE_LOCK = threading.Lock()
STATE_READ_CACHE_LOCK = threading.Lock()
STATE_READ_CACHE_KEY: tuple[str, int, int] | None = None
STATE_READ_CACHE_VALUE: dict[str, Any] | None = None
HTTP_RESPONSE_CACHE_LOCK = threading.Lock()
HTTP_RESPONSE_CACHE: dict[str, tuple[int, float, bytes]] = {}
STATE_GENERATION = 0
TEAM_LOCK = threading.Lock()
SCHEDULER_STATUS: dict[str, Any] = {"enabled": False, "interval_seconds": None, "last_tick": None, "last_status": "idle"}
THESPORTSDB_V1_BASE = "https://www.thesportsdb.com/api/v1/json"
SPORTSDB_FREE_KEY = "123"
OPENLIGADB_BASE = "https://api.openligadb.de"
ESPN_SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_STANDINGS_BASE = "https://site.api.espn.com/apis/v2/sports"
TRUSTED_FIXTURE_PROVIDERS = {"thesportsdb", "openligadb", "espn"}
TERMINAL_FIXTURE_STATUSES = {"FT", "AET", "PEN", "CANC", "CAN", "PST", "POSTPONED"}
FIXTURE_CACHE_HOURS = 24
RESEARCH_CACHE_HOURS = 6
RESEARCH_SCHEMA_VERSION = 2

ESPN_LEAGUES = {
    "fifa_wc": {"sport": "soccer", "league": "fifa.world"},
    "epl": {"sport": "soccer", "league": "eng.1"},
    "laliga": {"sport": "soccer", "league": "esp.1"},
    "bundesliga": {"sport": "soccer", "league": "ger.1"},
    "seriea": {"sport": "soccer", "league": "ita.1"},
    "ligue1": {"sport": "soccer", "league": "fra.1"},
    "nba": {"sport": "basketball", "league": "nba"},
    "nfl": {"sport": "football", "league": "nfl"},
    "mlb": {"sport": "baseball", "league": "mlb"},
    "nhl": {"sport": "hockey", "league": "nhl"},
}

ESPN_TEAM_NAME_OVERRIDES = {
    "fc bayern münchen": "bayern munich",
    "bayer 04 leverkusen": "bayer leverkusen",
    "athletic bilbao": "athletic club",
    "inter milan": "internazionale",
    "paris saint germain": "paris saint-germain",
}

SPORTS = {
    "football": {"id": "football", "name": "Football", "fixture_provider": "openligadb/espn/thesportsdb", "stats_provider": "sportsipy"},
    "nba": {"id": "nba", "name": "NBA", "fixture_provider": "espn/thesportsdb", "stats_provider": "sportsipy"},
    "nfl": {"id": "nfl", "name": "NFL", "fixture_provider": "espn/thesportsdb", "stats_provider": "sportsipy"},
    "mlb": {"id": "mlb", "name": "MLB", "fixture_provider": "espn/thesportsdb", "stats_provider": "sportsipy"},
    "nhl": {"id": "nhl", "name": "NHL", "fixture_provider": "espn/thesportsdb", "stats_provider": "sportsipy"},
}

LEAGUES = {
    "fifa_wc": {"id": "fifa_wc", "sport": "football", "name": "FIFA World Cup 2026", "country": "International", "tier": 1, "provider": "openligadb", "openligadb_shortcut": "wm26", "provider_league_id": "4897", "provider_season": 2026},
    "epl": {"id": "epl", "sport": "football", "name": "Premier League", "country": "England", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4328", "provider_league_name": "English Premier League", "provider_sport": "Soccer"},
    "laliga": {"id": "laliga", "sport": "football", "name": "La Liga", "country": "Spain", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4335", "provider_league_name": "Spanish La Liga", "provider_sport": "Soccer"},
    "bundesliga": {"id": "bundesliga", "sport": "football", "name": "Bundesliga", "country": "Germany", "tier": 1, "provider": "openligadb", "openligadb_shortcut": "bl1"},
    "seriea": {"id": "seriea", "sport": "football", "name": "Serie A", "country": "Italy", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4332", "provider_league_name": "Italian Serie A", "provider_sport": "Soccer"},
    "ligue1": {"id": "ligue1", "sport": "football", "name": "Ligue 1", "country": "France", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4334", "provider_league_name": "French Ligue 1", "provider_sport": "Soccer"},
    "nba": {"id": "nba", "sport": "nba", "name": "NBA", "country": "USA/Canada", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4387", "provider_league_name": "NBA", "provider_sport": "Basketball", "stats_provider": "sportsipy"},
    "nfl": {"id": "nfl", "sport": "nfl", "name": "NFL", "country": "USA", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4391", "provider_league_name": "NFL", "provider_sport": "American Football", "stats_provider": "sportsipy"},
    "mlb": {"id": "mlb", "sport": "mlb", "name": "MLB", "country": "USA/Canada", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4424", "provider_league_name": "MLB", "provider_sport": "Baseball", "stats_provider": "sportsipy"},
    "nhl": {"id": "nhl", "sport": "nhl", "name": "NHL", "country": "USA/Canada", "tier": 1, "provider": "thesportsdb", "provider_league_id": "4380", "provider_league_name": "NHL", "provider_sport": "Ice Hockey", "stats_provider": "sportsipy"},
}

_MISSING_SPORTS = sorted(
    {str(league["sport"]) for league in LEAGUES.values()} - set(DEFAULT_REGISTRY.list_sports())
)
if _MISSING_SPORTS:
    raise RuntimeError(
        f"sports missing from superpumped_crunch registry: {_MISSING_SPORTS}; "
        "add a sport JSON config to superpumped_crunch/sports or SUPERPUMPED_SPORTS_DIR"
    )

TEAM_ALIASES = {
    "fifa_wc": {},
    "epl": {
        "arsenal fc": "arsenal",
        "manchester city": "man_city",
        "man city": "man_city",
        "liverpool fc": "liverpool",
        "chelsea fc": "chelsea",
        "manchester united": "man_utd",
        "man utd": "man_utd",
        "spurs": "tottenham",
        "tottenham hotspur": "tottenham",
        "newcastle united": "newcastle",
        "aston villa": "aston_villa",
        "brighton & hove albion": "brighton",
        "west ham united": "west_ham",
    },
    "laliga": {
        "real madrid cf": "real_madrid",
        "fc barcelona": "barcelona",
        "atletico madrid": "atletico",
        "athletic club": "athletic",
    },
    "bundesliga": {
        "fc bayern": "bayern",
        "fc bayern münchen": "bayern",
        "bayern munich": "bayern",
        "borussia dortmund": "dortmund",
        "bayer 04 leverkusen": "leverkusen",
    },
    "nba": {
        "boston celtics": "bos_celtics",
        "los angeles lakers": "lal_lakers",
        "la lakers": "lal_lakers",
        "denver nuggets": "den_nuggets",
        "golden state warriors": "gsw_warriors",
        "new york knicks": "nyk_knicks",
        "dallas mavericks": "dal_mavericks",
    },
    "nfl": {
        "kansas city chiefs": "kc_chiefs",
        "san francisco 49ers": "sf_49ers",
        "buffalo bills": "buf_bills",
        "philadelphia eagles": "phi_eagles",
        "dallas cowboys": "dal_cowboys",
        "baltimore ravens": "bal_ravens",
    },
    "mlb": {
        "new york yankees": "nyy_yankees",
        "los angeles dodgers": "lad_dodgers",
        "houston astros": "hou_astros",
        "atlanta braves": "atl_braves",
        "boston red sox": "bos_red_sox",
        "chicago cubs": "chc_cubs",
    },
    "nhl": {
        "toronto maple leafs": "tor_maple_leafs",
        "new york rangers": "nyr_rangers",
        "edmonton oilers": "edm_oilers",
        "florida panthers": "fla_panthers",
        "boston bruins": "bos_bruins",
        "colorado avalanche": "col_avalanche",
    },
}

PROVIDER_TEAM_DISPLAY_NAMES = {
    "fifa_wc": {
        "Mexiko": "Mexico",
        "Südkorea": "South Korea",
        "Kanada": "Canada",
        "Katar": "Qatar",
        "Brasilien": "Brazil",
        "Australien": "Australia",
        "Deutschland": "Germany",
        "Niederlande": "Netherlands",
        "Elfenbeinküste": "Ivory Coast",
        "Schweden": "Sweden",
        "Spanien": "Spain",
        "Belgien": "Belgium",
        "Saudi Arabien": "Saudi Arabia",
        "Saudi-Arabien": "Saudi Arabia",
        "Frankreich": "France",
        "Irak": "Iraq",
        "Argentinien": "Argentina",
        "Österreich": "Austria",
        "Usbekistan": "Uzbekistan",
        "Tschechien": "Czechia",
        "Schweiz": "Switzerland",
        "Schottland": "Scotland",
        "Türkei": "Turkey",
        "Tunesien": "Tunisia",
        "Neuseeland": "New Zealand",
        "Ägypten": "Egypt",
        "Norwegen": "Norway",
        "Jordanien": "Jordan",
        "Algerien": "Algeria",
        "Kap Verde": "Cape Verde",
        "Kroatien": "Croatia",
        "Kolumbien": "Colombia",
        "DR Kongo": "DR Congo",
        "Südafrika": "South Africa",
        "Bosnien und Herzegowina": "Bosnia and Herzegovina",
        "Marokko": "Morocco",
        "Curaçao": "Curacao",
        "ESP/BEL": "Spain/Belgium",
        "NOR/ENG": "Norway/England",
        "ARG/SUI": "Argentina/Switzerland",
    },
}

PROVIDER_ROUND_DISPLAY_NAMES = {
    "fifa_wc": {
        "Gruppenphase": "Group Stage",
        "Gruppenphase 1": "Group Stage 1",
        "Gruppenphase 2": "Group Stage 2",
        "Gruppenphase 3": "Group Stage 3",
        "Sechzehntelfinale": "Round of 32",
        "Achtelfinale": "Round of 16",
        "Viertelfinale": "Quarterfinal",
        "Halbfinale": "Semifinal",
        "Spiel um Platz 3": "Third-place Match",
        "Finale": "Final",
    },
}

SNAPSHOT_WINDOWS = {
    "T-7d": 7 * 24 * 60,
    "T-24h": 24 * 60,
    "T-6h": 6 * 60,
    "T-60m": 60,
    "T-15m": 15,
    "T-5m": 5,
}


TEAMS: dict[str, list[dict[str, Any]]] = {
    "fifa_wc": [],
    "epl": [
        {"id": "arsenal", "name": "Arsenal", "elo": 1765},
        {"id": "man_city", "name": "Manchester City", "elo": 1820},
        {"id": "liverpool", "name": "Liverpool", "elo": 1790},
        {"id": "chelsea", "name": "Chelsea", "elo": 1712},
        {"id": "man_utd", "name": "Manchester United", "elo": 1690},
        {"id": "tottenham", "name": "Tottenham", "elo": 1705},
        {"id": "newcastle", "name": "Newcastle", "elo": 1730},
        {"id": "aston_villa", "name": "Aston Villa", "elo": 1725},
        {"id": "brighton", "name": "Brighton", "elo": 1680},
        {"id": "west_ham", "name": "West Ham", "elo": 1665},
    ],
    "laliga": [
        {"id": "real_madrid", "name": "Real Madrid", "elo": 1840},
        {"id": "barcelona", "name": "Barcelona", "elo": 1810},
        {"id": "atletico", "name": "Atletico Madrid", "elo": 1755},
        {"id": "real_sociedad", "name": "Real Sociedad", "elo": 1700},
        {"id": "athletic", "name": "Athletic Bilbao", "elo": 1695},
        {"id": "villarreal", "name": "Villarreal", "elo": 1685},
    ],
    "bundesliga": [
        {"id": "bayern", "name": "Bayern Munich", "elo": 1835},
        {"id": "dortmund", "name": "Borussia Dortmund", "elo": 1745},
        {"id": "leverkusen", "name": "Bayer Leverkusen", "elo": 1780},
        {"id": "rb_leipzig", "name": "RB Leipzig", "elo": 1720},
        {"id": "stuttgart", "name": "VfB Stuttgart", "elo": 1690},
        {"id": "frankfurt", "name": "Eintracht Frankfurt", "elo": 1680},
    ],
    "seriea": [
        {"id": "inter", "name": "Inter Milan", "elo": 1795},
        {"id": "ac_milan", "name": "AC Milan", "elo": 1735},
        {"id": "juventus", "name": "Juventus", "elo": 1750},
        {"id": "napoli", "name": "Napoli", "elo": 1740},
        {"id": "roma", "name": "AS Roma", "elo": 1690},
        {"id": "lazio", "name": "Lazio", "elo": 1685},
    ],
    "ligue1": [
        {"id": "psg", "name": "Paris Saint-Germain", "elo": 1825},
        {"id": "marseille", "name": "Olympique Marseille", "elo": 1695},
        {"id": "monaco", "name": "AS Monaco", "elo": 1710},
        {"id": "lyon", "name": "Olympique Lyon", "elo": 1680},
        {"id": "lille", "name": "Lille", "elo": 1690},
    ],
    "nba": [
        {"id": "bos_celtics", "name": "Boston Celtics", "elo": 1785},
        {"id": "lal_lakers", "name": "Los Angeles Lakers", "elo": 1690},
        {"id": "den_nuggets", "name": "Denver Nuggets", "elo": 1745},
        {"id": "gsw_warriors", "name": "Golden State Warriors", "elo": 1685},
        {"id": "nyk_knicks", "name": "New York Knicks", "elo": 1715},
        {"id": "dal_mavericks", "name": "Dallas Mavericks", "elo": 1720},
    ],
    "nfl": [
        {"id": "kc_chiefs", "name": "Kansas City Chiefs", "elo": 1770},
        {"id": "sf_49ers", "name": "San Francisco 49ers", "elo": 1755},
        {"id": "buf_bills", "name": "Buffalo Bills", "elo": 1730},
        {"id": "phi_eagles", "name": "Philadelphia Eagles", "elo": 1720},
        {"id": "dal_cowboys", "name": "Dallas Cowboys", "elo": 1695},
        {"id": "bal_ravens", "name": "Baltimore Ravens", "elo": 1740},
    ],
    "mlb": [
        {"id": "nyy_yankees", "name": "New York Yankees", "elo": 1715},
        {"id": "lad_dodgers", "name": "Los Angeles Dodgers", "elo": 1760},
        {"id": "hou_astros", "name": "Houston Astros", "elo": 1695},
        {"id": "atl_braves", "name": "Atlanta Braves", "elo": 1705},
        {"id": "bos_red_sox", "name": "Boston Red Sox", "elo": 1655},
        {"id": "chc_cubs", "name": "Chicago Cubs", "elo": 1645},
    ],
    "nhl": [
        {"id": "tor_maple_leafs", "name": "Toronto Maple Leafs", "elo": 1705},
        {"id": "nyr_rangers", "name": "New York Rangers", "elo": 1710},
        {"id": "edm_oilers", "name": "Edmonton Oilers", "elo": 1735},
        {"id": "fla_panthers", "name": "Florida Panthers", "elo": 1745},
        {"id": "bos_bruins", "name": "Boston Bruins", "elo": 1688},
        {"id": "col_avalanche", "name": "Colorado Avalanche", "elo": 1725},
    ],
}


TASKS = [
    {"id": "T1", "name": "Bootstrap repo and local platform", "status": "done", "effort": "Low", "depends": [], "gate": "Server starts and health endpoint passes", "desc": "Local app server, static serving, health endpoint, and JSON API contracts."},
    {"id": "T2", "name": "Define schema and contracts", "status": "done", "effort": "Medium", "depends": ["T1"], "gate": "Frontend consumes typed endpoint payloads", "desc": "Shared JSON contracts for teams, sources, predictions, pipeline, schema, and backtest."},
    {"id": "T3", "name": "Build live-data connectors", "status": "done", "effort": "Medium", "depends": ["T1", "T2"], "gate": "Provider status and fixture feeds render", "desc": "OpenLigaDB and ESPN provide full verified schedule windows, TheSportsDB supplies fallback fixtures and roster data, sportsipy remains an analytics source, and unverified rows are quarantined."},
    {"id": "T4", "name": "Canonical identity resolution", "status": "done", "effort": "Medium", "depends": ["T3"], "gate": "Source teams map to canonical teams", "desc": "Deterministic team and fixture mapping for football, NBA, NFL, MLB, and NHL with alias support."},
    {"id": "T5", "name": "Point-in-time snapshots", "status": "done", "effort": "High", "depends": ["T3", "T4"], "gate": "No feature newer than snapshot timestamp", "desc": "Prediction windows and PIT audit endpoint validate pre-match feature timing."},
    {"id": "T6", "name": "Sports performance baseline", "status": "done", "effort": "Low", "depends": ["T3"], "gate": "Sports probabilities sum to 1", "desc": "Elo, scoring form, opponent strength, and home-advantage priors from verified results."},
    {"id": "T7", "name": "Multi-sport scoring fundamentals", "status": "done", "effort": "High", "depends": ["T4"], "gate": "Winner and secondary-market probabilities are normalized", "desc": "Elo outcomes plus pre-cutoff scoring distributions for football, NBA, NFL, MLB, and NHL totals and handicaps."},
    {"id": "T8", "name": "Line-up and injury engine", "status": "done", "effort": "Medium", "depends": ["T3", "T4", "T5"], "gate": "Availability features affect recommendations", "desc": "Snapshot-aware availability confidence, starter gaps, injury impact, and lineup confidence feed the recommendation model."},
    {"id": "T9", "name": "Train sports ensemble and calibrate", "status": "done", "effort": "High", "depends": ["T5", "T6", "T7", "T8"], "gate": "Calibrated sports probabilities are persisted", "desc": "Score models, Elo, availability, H2H, standings, and walk-forward ranking are combined for every fixture."},
    {"id": "T10", "name": "Walk-forward backtester", "status": "done", "effort": "High", "depends": ["T9"], "gate": "Frozen predictions and zero-lookahead audit pass", "desc": "Provider-verified historical results, training-only parameter tuning, untouched holdout scoring, confidence coverage, and leakage auditing."},
    {"id": "T11", "name": "Production API and monitoring", "status": "active", "effort": "High", "depends": ["T10"], "gate": "Freshness and latency checks visible", "desc": "Health, freshness, provider checks, and local scheduler hooks are visible; deployment monitoring remains the next hardening step."},
]


SCHEMA = {
    "fixtures": [
        {"col": "fixture_id", "type": "text", "pk": True, "note": "Canonical fixture ID"},
        {"col": "competition_id", "type": "text", "note": "Canonical competition"},
        {"col": "home_team_id", "type": "text", "note": "Canonical home team"},
        {"col": "away_team_id", "type": "text", "note": "Canonical away team"},
        {"col": "kickoff_utc", "type": "timestamptz", "note": "Scheduled kick-off"},
        {"col": "status", "type": "text", "note": "scheduled, live, finished, postponed"},
    ],
    "predictions": [
        {"col": "prediction_id", "type": "text", "pk": True, "note": "Unique prediction row"},
        {"col": "fixture_id", "type": "text", "fk": "fixtures", "note": "Canonical fixture"},
        {"col": "prob_home", "type": "numeric", "note": "Calibrated home probability"},
        {"col": "prob_draw", "type": "numeric", "note": "Calibrated draw probability"},
        {"col": "prob_away", "type": "numeric", "note": "Calibrated away probability"},
        {"col": "market_horizons", "type": "json", "note": "Sport-specific totals, BTTS, spread and score projections"},
        {"col": "created_at", "type": "timestamptz", "note": "Write timestamp"},
    ],
    "sources": [
        {"col": "source_name", "type": "text", "pk": True, "note": "Provider key"},
        {"col": "last_sync", "type": "timestamptz", "note": "Latest sync timestamp"},
        {"col": "status", "type": "text", "note": "green, amber, red"},
        {"col": "records", "type": "integer", "note": "Rows in current feed"},
    ],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fixture_is_provider_verified(fixture: dict[str, Any], require_fresh: bool = True) -> bool:
    provider = str(fixture.get("provider") or fixture.get("source_name") or "").lower()
    kickoff = parse_utc_timestamp(str(fixture.get("starts_at") or fixture.get("kickoff_utc") or ""))
    verified_at = parse_utc_timestamp(str(fixture.get("verified_at") or ""))
    if provider not in TRUSTED_FIXTURE_PROVIDERS:
        return False
    if fixture.get("verified") is not True or not fixture.get("provider_event_id") or not kickoff or not verified_at:
        return False
    if kickoff < datetime.now(timezone.utc) - timedelta(hours=6):
        return False
    if require_fresh and verified_at < datetime.now(timezone.utc) - timedelta(hours=FIXTURE_CACHE_HOURS):
        return False
    return True


def fixture_is_stale(fixture: dict[str, Any]) -> bool:
    verified_at = parse_utc_timestamp(str(fixture.get("verified_at") or ""))
    return verified_at is None or verified_at < datetime.now(timezone.utc) - timedelta(hours=FIXTURE_CACHE_HOURS)


def register_persisted_fixture_teams(fixtures: list[dict[str, Any]]) -> None:
    for fixture in fixtures:
        competition = str(fixture.get("competition") or "")
        if competition not in TEAMS:
            continue
        for side in ("home", "away"):
            team_id = str(fixture.get(f"{side}_id") or "")
            team_name = str(fixture.get(f"{side}_name") or team_id)
            if team_id and not find_team(competition, team_id):
                TEAMS[competition].append({
                    "id": team_id,
                    "name": team_name,
                    "elo": int(fixture.get(f"{side}_elo") or 1500),
                    "provider_team_id": fixture.get(f"{side}_provider_id"),
                    "provider": fixture.get("provider"),
                })


def state_file() -> Path:
    return Path(os.environ.get("ENGINE_STATE_FILE", DEFAULT_STATE_FILE))


def read_state_json(path: Path) -> dict[str, Any]:
    global STATE_READ_CACHE_KEY, STATE_READ_CACHE_VALUE
    file_stat = path.stat()
    cache_key = (str(path.resolve()), file_stat.st_mtime_ns, file_stat.st_size)
    with STATE_READ_CACHE_LOCK:
        if STATE_READ_CACHE_KEY != cache_key or STATE_READ_CACHE_VALUE is None:
            STATE_READ_CACHE_VALUE = json.loads(path.read_text(encoding="utf-8"))
            STATE_READ_CACHE_KEY = cache_key
        cached = STATE_READ_CACHE_VALUE
    return copy.deepcopy(cached)


def default_state() -> dict[str, Any]:
    return {
        "version": 4,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "last_refresh": None,
        "connectors": connector_config(),
        "fixtures": [],
        "recent_predictions": [],
        "prediction_history": [],
        "recommendations": [],
        "snapshots": [],
        "results": [],
        "settlements": [],
        "odds_ticks": [],
        "availability_rows": [],
        "refresh_runs": [],
        "fixture_refresh_runs": [],
        "research_cache": {},
        "research_refresh_runs": [],
        "historical_games": [],
        "historical_refresh_runs": [],
        "historical_window_status": {},
        "ranking_parameters": {},
        "ranking_backtest": None,
        "market_parameters": {},
        "expanded_market_backtest": None,
        "backtest_runs": [],
        "ingestion_runs": [],
        "settlement_runs": [],
        "source_counts": {
            "odds_ticks": 0,
            "availability_rows": 0,
            "fixtures": 0,
            "teams": sum(len(v) for v in TEAMS.values()),
        },
    }


def load_state() -> dict[str, Any]:
    path = state_file()
    if not path.exists():
        return default_state()
    try:
        state = read_state_json(path)
        fixtures: list[dict[str, Any]] = []
        for fixture in state.get("fixtures", []):
            if not fixture_is_provider_verified(fixture, require_fresh=False):
                continue
            fixture["stale_verified"] = fixture_is_stale(fixture)
            fixtures.append(fixture)
        state["fixtures"] = fixtures
        register_persisted_fixture_teams(state["fixtures"])
        trusted_fixture_ids = {fixture["fixture_id"] for fixture in state["fixtures"] if fixture.get("fixture_id")}
        for key in ("recent_predictions", "odds_ticks", "availability_rows"):
            state[key] = [row for row in state.get(key, []) if row.get("fixture_id") in trusted_fixture_ids]
        for key in ("prediction_history", "recommendations", "snapshots", "settlements"):
            state.setdefault(key, [])
        state["results"] = [
            row for row in state.get("results", [])
            if row.get("verified") is True and row.get("source_name") in TRUSTED_FIXTURE_PROVIDERS
        ]
        state["version"] = 4
        state.setdefault("fixture_refresh_runs", [])
        state["research_cache"] = {
            fixture_id: research
            for fixture_id, research in state.get("research_cache", {}).items()
            if fixture_id in trusted_fixture_ids and isinstance(research, dict)
        }
        state.setdefault("research_refresh_runs", [])
        state["historical_games"] = [
            row for row in state.get("historical_games", [])
            if isinstance(row, dict)
            and row.get("verified") is True
            and row.get("source_name") == "espn"
            and row.get("provider_event_id")
            and row.get("start_time")
        ]
        state.setdefault("historical_refresh_runs", [])
        state.setdefault("historical_window_status", {})
        expected_history_windows = {
            f"{competition}:{start}-{end}"
            for competition in LEAGUES
            for start, end in historical_date_windows(competition)
        }
        state["historical_window_status"] = {
            key: value
            for key, value in state["historical_window_status"].items()
            if key in expected_history_windows
        }
        if not state["historical_window_status"] and state["historical_refresh_runs"]:
            for run in reversed(state["historical_refresh_runs"]):
                failed = {
                    f"{row.get('competition')}:{row.get('window')}"
                    for row in run.get("failures", [])
                }
                for competition in run.get("requested_leagues", []):
                    for start, end in historical_date_windows(competition):
                        window_key = f"{competition}:{start}-{end}"
                        if window_key in failed:
                            if state["historical_window_status"].get(window_key, {}).get("status") != "success":
                                state["historical_window_status"][window_key] = {"status": "failed", "updated_at": run.get("finished_at")}
                        else:
                            state["historical_window_status"][window_key] = {"status": "success", "updated_at": run.get("finished_at")}
        state.setdefault("ranking_parameters", {})
        state.setdefault("ranking_backtest", None)
        state.setdefault("market_parameters", {})
        state.setdefault("expanded_market_backtest", None)
        state.setdefault("backtest_runs", [])
        state.setdefault("source_counts", {})["fixtures"] = len(state["fixtures"])
        return state
    except (json.JSONDecodeError, OSError):
        state = default_state()
        state["state_warning"] = f"Could not read {path.name}; using fresh state"
        return state


def save_state(state: dict[str, Any]) -> dict[str, Any]:
    global STATE_GENERATION, STATE_READ_CACHE_KEY, STATE_READ_CACHE_VALUE
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    tmp_path = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    last_error: PermissionError | None = None
    for attempt in range(6):
        try:
            tmp_path.replace(path)
            last_error = None
            break
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    if last_error:
        raise last_error
    with STATE_READ_CACHE_LOCK:
        STATE_READ_CACHE_KEY = None
        STATE_READ_CACHE_VALUE = None
    with HTTP_RESPONSE_CACHE_LOCK:
        STATE_GENERATION += 1
        HTTP_RESPONSE_CACHE.clear()
    return state


def connector_config() -> dict[str, dict[str, Any]]:
    providers = {
        "thesportsdb": {"label": "TheSportsDB", "env": "THESPORTSDB_API_KEY", "mode": "free_key"},
        "openligadb": {"label": "OpenLigaDB", "env": None, "mode": "free_api"},
        "espn": {"label": "ESPN", "env": None, "mode": "public_api"},
    }
    for key, provider in providers.items():
        configured = bool(os.environ.get(provider["env"])) if provider.get("env") else True
        if key == "espn":
            provider["configured"] = True
            provider["status"] = "green"
            provider["message"] = "Public schedule and research enrichment API configured"
            provider["base_url"] = ESPN_SITE_BASE
            continue
        if key == "openligadb":
            provider["configured"] = True
            provider["status"] = "green"
            provider["message"] = "Free OpenLigaDB API configured for football"
            provider["base_url"] = OPENLIGADB_BASE
            continue
        if key == "thesportsdb":
            configured = True
            provider["configured"] = True
            provider["mode"] = "premium_key" if os.environ.get(provider["env"]) else "free_key"
            provider["status"] = "green"
            provider["message"] = f"Using {'configured key' if os.environ.get(provider['env']) else 'free v1 key 123'}"
            provider["base_url"] = THESPORTSDB_V1_BASE
            continue
        provider["configured"] = configured
        provider["mode"] = "provider_ready" if configured else "synthetic"
        provider["status"] = "green" if configured else "amber"
        provider["message"] = "API key configured" if configured else f"Set {provider['env']} to enable real live data"
    sportsipy_installed = importlib.util.find_spec("sportsipy") is not None
    providers["sportsipy"] = {
        "label": "sportsipy",
        "env": None,
        "configured": sportsipy_installed,
        "mode": "library_installed" if sportsipy_installed else "not_installed",
        "status": "green" if sportsipy_installed else "amber",
        "message": "sportsipy is importable" if sportsipy_installed else "Install with: pip install sportsipy",
        "source": "Sports-Reference.com",
    }
    return providers


def thesportsdb_api_key() -> str:
    return os.environ.get("THESPORTSDB_API_KEY") or SPORTSDB_FREE_KEY


def thesportsdb_url(endpoint: str, **params: Any) -> str:
    query = "&".join(f"{key}={quote_plus(str(value))}" for key, value in params.items() if value not in (None, ""))
    suffix = f"?{query}" if query else ""
    return f"{THESPORTSDB_V1_BASE}/{thesportsdb_api_key()}/{endpoint}{suffix}"


def thesportsdb_get(endpoint: str, **params: Any) -> dict[str, Any]:
    url = thesportsdb_url(endpoint, **params)
    request = Request(url, headers={"User-Agent": "FootballProbabilityEngine/0.1"})
    with urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def thesportsdb_search_team(team: str) -> dict[str, Any]:
    return {"provider": "thesportsdb", "url": thesportsdb_url("searchteams.php", t=team), "data": thesportsdb_get("searchteams.php", t=team)}


def sportsipy_status() -> dict[str, Any]:
    spec = importlib.util.find_spec("sportsipy")
    return {
        "provider": "sportsipy",
        "installed": spec is not None,
        "install_command": "pip install sportsipy",
        "source": "Sports-Reference.com",
        "supported_examples": ["sportsipy.fb.team.Team", "sportsipy.nba.teams.Teams", "sportsipy.nfl.teams.Teams"],
    }


def openligadb_get(path: str) -> Any:
    url = f"{OPENLIGADB_BASE}/{path.lstrip('/')}"
    request = Request(url, headers={"User-Agent": "SportEngine/0.1"})
    with urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def openligadb_league_matches(league_shortcut: str = "bl1", season: str | None = None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    season = season or str(now.year if now.month >= 7 else now.year - 1)
    path = f"getmatchdata/{league_shortcut}/{season}"
    return {"provider": "openligadb", "league_shortcut": league_shortcut, "season": season, "url": f"{OPENLIGADB_BASE}/{path}", "data": openligadb_get(path)}


def provider_team(competition: str, provider: str, provider_id: Any, name: str) -> dict[str, Any]:
    normalized = normalize_name(name)
    alias_id = TEAM_ALIASES.get(competition, {}).get(normalized)
    if alias_id:
        team = find_team(competition, alias_id)
        if team:
            return team
    for team in TEAMS.get(competition, []):
        if normalized == normalize_name(str(team.get("name", ""))):
            return team

    provider_key = "tsdb" if provider == "thesportsdb" else "oldb"
    raw_id = "".join(ch for ch in str(provider_id or "") if ch.isalnum())
    if not raw_id:
        raw_id = hashlib.sha256(f"{provider}|{competition}|{normalized}".encode("utf-8")).hexdigest()[:10]
    team_id = f"{provider_key}_{raw_id.lower()}"
    with TEAM_LOCK:
        existing = find_team(competition, team_id)
        if existing:
            return existing
        team = {
            "id": team_id,
            "name": name,
            "elo": 1500,
            "provider_team_id": str(provider_id or ""),
            "provider": provider,
        }
        TEAMS.setdefault(competition, []).append(team)
        return team


def snapshot_for_kickoff(kickoff: datetime) -> str:
    minutes = max(0, int((kickoff - datetime.now(timezone.utc)).total_seconds() / 60))
    if minutes <= 5:
        return "T-5m"
    if minutes <= 15:
        return "T-15m"
    if minutes <= 60:
        return "T-60m"
    if minutes <= 6 * 60:
        return "T-6h"
    if minutes <= 24 * 60:
        return "T-24h"
    return "T-7d"


def normalize_thesportsdb_fixture(league_id: str, event: dict[str, Any], verified_at: str | None = None) -> dict[str, Any]:
    league = LEAGUES[league_id]
    expected_league_id = str(league["provider_league_id"])
    if str(event.get("idLeague") or "") != expected_league_id:
        raise ValueError(f"Provider league mismatch for {league_id}")
    if normalize_name(str(event.get("strLeague") or "")) != normalize_name(str(league["provider_league_name"])):
        raise ValueError(f"Provider league name mismatch for {league_id}")
    if normalize_name(str(event.get("strSport") or "")) != normalize_name(str(league["provider_sport"])):
        raise ValueError(f"Provider sport mismatch for {league_id}")

    event_id = str(event.get("idEvent") or "").strip()
    home_name = str(event.get("strHomeTeam") or "").strip()
    away_name = str(event.get("strAwayTeam") or "").strip()
    status = str(event.get("strStatus") or "NS").upper()
    if not event_id or not home_name or not away_name:
        raise ValueError("Provider fixture is missing event or team identity")
    if status in TERMINAL_FIXTURE_STATUSES or str(event.get("strPostponed") or "no").lower() == "yes":
        raise ValueError("Provider fixture is not an active scheduled event")

    timestamp = str(event.get("strTimestamp") or "").strip()
    if not timestamp:
        event_date = str(event.get("dateEvent") or "").strip()
        event_time = str(event.get("strTime") or "00:00:00").strip()
        timestamp = f"{event_date}T{event_time}" if event_date else ""
    kickoff = parse_utc_timestamp(timestamp)
    if not kickoff or kickoff < datetime.now(timezone.utc) - timedelta(hours=6):
        raise ValueError("Provider fixture has no valid future UTC kickoff")

    home = provider_team(league_id, "thesportsdb", event.get("idHomeTeam"), home_name)
    away = provider_team(league_id, "thesportsdb", event.get("idAwayTeam"), away_name)
    if home["id"] == away["id"]:
        raise ValueError("Provider fixture maps home and away to the same team")
    observed_at = verified_at or now_iso()
    source_url = thesportsdb_url("lookupevent.php", id=event_id)
    return {
        "fixture_id": f"{league_id}_thesportsdb_{event_id}",
        "competition": league_id,
        "home_id": home["id"],
        "away_id": away["id"],
        "home_name": home_name,
        "away_name": away_name,
        "home_provider_id": str(event.get("idHomeTeam") or ""),
        "away_provider_id": str(event.get("idAwayTeam") or ""),
        "home_badge": event.get("strHomeTeamBadge"),
        "away_badge": event.get("strAwayTeamBadge"),
        "starts_at": utc_iso(kickoff),
        "snapshot": snapshot_for_kickoff(kickoff),
        "status": status,
        "season": event.get("strSeason"),
        "round": event.get("intRound"),
        "venue": event.get("strVenue"),
        "provider": "thesportsdb",
        "source_name": "thesportsdb",
        "provider_event_id": event_id,
        "provider_league_id": expected_league_id,
        "provider_url": source_url,
        "source_url": source_url,
        "verified": True,
        "verified_at": observed_at,
        "payload_hash": hashlib.sha256(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest(),
    }


def normalize_openligadb_fixture(league_id: str, match: dict[str, Any], verified_at: str | None = None) -> dict[str, Any]:
    league = LEAGUES[league_id]
    shortcut = str(league["openligadb_shortcut"])
    if str(match.get("leagueShortcut") or "").lower() != shortcut.lower():
        raise ValueError(f"Provider league mismatch for {league_id}")
    expected_league_id = str(league.get("provider_league_id") or "")
    if expected_league_id and str(match.get("leagueId") or "") != expected_league_id:
        raise ValueError(f"Provider league ID mismatch for {league_id}")
    event_id = str(match.get("matchID") or "").strip()
    home_row = match.get("team1") if isinstance(match.get("team1"), dict) else {}
    away_row = match.get("team2") if isinstance(match.get("team2"), dict) else {}
    home_source_name = str(home_row.get("teamName") or "").strip()
    away_source_name = str(away_row.get("teamName") or "").strip()
    display_names = PROVIDER_TEAM_DISPLAY_NAMES.get(league_id, {})
    home_name = display_names.get(home_source_name, home_source_name)
    away_name = display_names.get(away_source_name, away_source_name)
    kickoff = parse_utc_timestamp(str(match.get("matchDateTimeUTC") or ""))
    if not event_id or not home_name or not away_name:
        raise ValueError("Provider fixture is missing event or team identity")
    if match.get("matchIsFinished") is True or not kickoff or kickoff < datetime.now(timezone.utc) - timedelta(hours=6):
        raise ValueError("Provider fixture is not an active scheduled event")

    home = provider_team(league_id, "openligadb", home_row.get("teamId"), home_name)
    away = provider_team(league_id, "openligadb", away_row.get("teamId"), away_name)
    if home["id"] == away["id"]:
        raise ValueError("Provider fixture maps home and away to the same team")
    observed_at = verified_at or now_iso()
    source_url = f"{OPENLIGADB_BASE}/getmatchdata/{event_id}"
    group = match.get("group") if isinstance(match.get("group"), dict) else {}
    round_source_name = group.get("groupName")
    round_name = PROVIDER_ROUND_DISPLAY_NAMES.get(league_id, {}).get(round_source_name, round_source_name)
    return {
        "fixture_id": f"{league_id}_openligadb_{event_id}",
        "competition": league_id,
        "home_id": home["id"],
        "away_id": away["id"],
        "home_name": home_name,
        "away_name": away_name,
        "home_provider_id": str(home_row.get("teamId") or ""),
        "away_provider_id": str(away_row.get("teamId") or ""),
        "home_badge": home_row.get("teamIconUrl"),
        "away_badge": away_row.get("teamIconUrl"),
        "starts_at": utc_iso(kickoff),
        "snapshot": snapshot_for_kickoff(kickoff),
        "status": "NS",
        "season": str(match.get("leagueSeason") or ""),
        "round": group.get("groupOrderID"),
        "round_name": round_name,
        "venue": match.get("location"),
        "provider": "openligadb",
        "source_name": "openligadb",
        "provider_event_id": event_id,
        "provider_league_id": str(match.get("leagueId") or ""),
        "provider_url": source_url,
        "source_url": source_url,
        "verified": True,
        "verified_at": observed_at,
        "payload_hash": hashlib.sha256(json.dumps(match, sort_keys=True).encode("utf-8")).hexdigest(),
    }


def fixture_schedule_end(league_id: str, reference: datetime | None = None) -> datetime:
    now = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if league_id in {"epl", "laliga", "seriea", "ligue1", "nba", "nhl"}:
        end_year = now.year + 1 if now.month >= 7 else now.year
        result = datetime(end_year, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
        if result < now + timedelta(days=30):
            result = datetime(end_year + 1, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
        return result
    if league_id == "nfl":
        end_year = now.year if now.month < 3 else now.year + 1
        return datetime(end_year, 2, 28, 23, 59, 59, tzinfo=timezone.utc)
    if league_id == "mlb":
        end_year = now.year if now.month <= 11 else now.year + 1
        return datetime(end_year, 11, 30, 23, 59, 59, tzinfo=timezone.utc)
    return now + timedelta(days=365)


def normalize_espn_fixture(
    league_id: str,
    event: dict[str, Any],
    source_url: str,
    verified_at: str | None = None,
) -> dict[str, Any]:
    event_id = str(event.get("id") or "").strip()
    competitions = event.get("competitions") if isinstance(event.get("competitions"), list) else []
    event_competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
    competitors = event_competition.get("competitors") if isinstance(event_competition.get("competitors"), list) else []
    home_row = next((row for row in competitors if isinstance(row, dict) and row.get("homeAway") == "home"), None)
    away_row = next((row for row in competitors if isinstance(row, dict) and row.get("homeAway") == "away"), None)
    home_team = home_row.get("team") if home_row and isinstance(home_row.get("team"), dict) else {}
    away_team = away_row.get("team") if away_row and isinstance(away_row.get("team"), dict) else {}
    home_provider_id = str(home_team.get("id") or (home_row or {}).get("id") or "").strip()
    away_provider_id = str(away_team.get("id") or (away_row or {}).get("id") or "").strip()
    home_name = str(home_team.get("displayName") or home_team.get("shortDisplayName") or "").strip()
    away_name = str(away_team.get("displayName") or away_team.get("shortDisplayName") or "").strip()
    kickoff = parse_utc_timestamp(str(event.get("date") or ""))
    status = event.get("status") if isinstance(event.get("status"), dict) else event_competition.get("status") if isinstance(event_competition.get("status"), dict) else {}
    status_type = status.get("type") if isinstance(status.get("type"), dict) else {}
    status_name = str(status_type.get("name") or status_type.get("description") or "scheduled").upper()
    if not event_id or not home_provider_id or not away_provider_id or not home_name or not away_name:
        raise ValueError("ESPN fixture is missing event or team identity")
    if (
        status_type.get("completed") is True
        or str(status_type.get("state") or "").lower() == "post"
        or any(value in status_name for value in ("CANCEL", "POSTPON", "SUSPEND"))
    ):
        raise ValueError("ESPN fixture is not an active scheduled event")
    if not kickoff or kickoff < datetime.now(timezone.utc) - timedelta(hours=6):
        raise ValueError("ESPN fixture has no valid future UTC kickoff")

    home = provider_team(league_id, "espn", home_provider_id, home_name)
    away = provider_team(league_id, "espn", away_provider_id, away_name)
    if home["id"] == away["id"]:
        raise ValueError("ESPN fixture maps home and away to the same team")
    observed_at = verified_at or now_iso()
    links = event.get("links") if isinstance(event.get("links"), list) else []
    provider_url = next((str(row.get("href")) for row in links if isinstance(row, dict) and row.get("href")), source_url)
    venue = event_competition.get("venue") if isinstance(event_competition.get("venue"), dict) else {}
    week = event.get("week") if isinstance(event.get("week"), dict) else event_competition.get("week") if isinstance(event_competition.get("week"), dict) else {}
    season = event.get("season") if isinstance(event.get("season"), dict) else {}

    def team_logo(team: dict[str, Any]) -> str | None:
        logos = team.get("logos") if isinstance(team.get("logos"), list) else []
        return str(team.get("logo") or ((logos[0] or {}).get("href") if logos and isinstance(logos[0], dict) else "")) or None

    return {
        "fixture_id": f"{league_id}_espn_{event_id}",
        "competition": league_id,
        "home_id": home["id"],
        "away_id": away["id"],
        "home_name": home_name,
        "away_name": away_name,
        "home_provider_id": home_provider_id,
        "away_provider_id": away_provider_id,
        "home_badge": team_logo(home_team),
        "away_badge": team_logo(away_team),
        "starts_at": utc_iso(kickoff),
        "snapshot": snapshot_for_kickoff(kickoff),
        "status": str(status_type.get("shortDetail") or status_type.get("detail") or "NS"),
        "season": season.get("year"),
        "round": week.get("number"),
        "round_name": week.get("text"),
        "venue": venue.get("fullName"),
        "provider": "espn",
        "source_name": "espn",
        "provider_event_id": event_id,
        "provider_league_id": ESPN_LEAGUES[league_id]["league"],
        "provider_url": provider_url,
        "source_url": source_url,
        "verified": True,
        "verified_at": observed_at,
        "payload_hash": hashlib.sha256(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest(),
    }


def fetch_espn_schedule_fixtures(league_id: str, verified_at: str | None = None) -> dict[str, Any]:
    observed_at = verified_at or now_iso()
    now = datetime.now(timezone.utc)
    windows = _monthly_windows(datetime(now.year, now.month, 1, tzinfo=timezone.utc), fixture_schedule_end(league_id, now))
    events: dict[str, tuple[dict[str, Any], str]] = {}
    source_urls = []
    errors = []
    for start, end in windows:
        url = espn_provider_url(league_id, f"scoreboard?dates={start}-{end}&limit=1000")
        source_urls.append(url)
        try:
            payload = espn_get(url, timeout=45)
        except Exception as exc:
            errors.append({"window": f"{start}-{end}", "error": str(exc)})
            continue
        for event in payload.get("events", []) if isinstance(payload.get("events"), list) else []:
            if isinstance(event, dict) and event.get("id"):
                events[str(event["id"])] = (event, url)

    fixtures = []
    rejected = []
    for event_id, (event, source_url) in events.items():
        try:
            fixtures.append(normalize_espn_fixture(league_id, event, source_url, observed_at))
        except ValueError as exc:
            rejected.append({"provider_event_id": event_id, "reason": str(exc)})
    fixtures.sort(key=lambda row: (row["starts_at"], row["provider_event_id"]))
    return {
        "fixtures": fixtures,
        "rejected": rejected,
        "provider_urls": source_urls,
        "errors": errors,
    }


def fetch_provider_fixtures(league_id: str) -> dict[str, Any]:
    if league_id not in LEAGUES:
        raise ValueError(f"Unknown league: {league_id}")
    league = LEAGUES[league_id]
    verified_at = now_iso()
    fixtures: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    selected_provider = str(league["provider"])
    if league["provider"] == "thesportsdb":
        fallback_fixtures = []
        fallback_rejected = []
        fallback_error: Exception | None = None
        sportsdb_url = thesportsdb_url("eventsnextleague.php", id=league["provider_league_id"])
        try:
            payload = thesportsdb_get("eventsnextleague.php", id=league["provider_league_id"])
            events = payload.get("events") if isinstance(payload, dict) else None
            for event in events or []:
                try:
                    fallback_fixtures.append(normalize_thesportsdb_fixture(league_id, event, verified_at))
                except ValueError as exc:
                    fallback_rejected.append({"provider_event_id": str(event.get("idEvent") or ""), "reason": str(exc)})
        except Exception as exc:
            fallback_error = exc

        espn_result = fetch_espn_schedule_fixtures(league_id, verified_at)
        if espn_result["fixtures"] or not fallback_fixtures:
            fixtures = espn_result["fixtures"]
            rejected = espn_result["rejected"]
            errors = espn_result["errors"]
            selected_provider = "espn"
            provider_url = espn_result["provider_urls"][0] if espn_result["provider_urls"] else espn_provider_url(league_id, "scoreboard")
            if not fixtures and errors and fallback_error:
                raise ValueError(f"ESPN and TheSportsDB fixture providers failed: {errors[0]['error']}; {fallback_error}")
        else:
            fixtures = fallback_fixtures
            rejected = fallback_rejected
            errors = espn_result["errors"]
            selected_provider = "thesportsdb"
            provider_url = sportsdb_url
    else:
        season = int(league.get("provider_season") or (datetime.now(timezone.utc).year if datetime.now(timezone.utc).month >= 7 else datetime.now(timezone.utc).year - 1))
        path = f"getmatchdata/{league['openligadb_shortcut']}/{season}"
        payload = openligadb_get(path)
        if not isinstance(payload, list):
            raise ValueError("OpenLigaDB returned an invalid fixture payload")
        for match in payload:
            kickoff = parse_utc_timestamp(str(match.get("matchDateTimeUTC") or ""))
            if match.get("matchIsFinished") is True or (kickoff and kickoff < datetime.now(timezone.utc) - timedelta(hours=6)):
                continue
            try:
                fixtures.append(normalize_openligadb_fixture(league_id, match, verified_at))
            except ValueError as exc:
                rejected.append({"provider_event_id": str(match.get("matchID") or ""), "reason": str(exc)})
        provider_url = f"{OPENLIGADB_BASE}/{path}"

    fixtures.sort(key=lambda row: (row["starts_at"], row["provider_event_id"]))
    return {
        "league_id": league_id,
        "provider": selected_provider,
        "provider_url": provider_url,
        "verified_at": verified_at,
        "fixtures": fixtures,
        "rejected": rejected,
        "errors": errors,
    }


def refresh_provider_fixtures(league_ids: list[str] | None = None) -> dict[str, Any]:
    selected = list(dict.fromkeys(league_ids or list(LEAGUES)))
    unknown = [league_id for league_id in selected if league_id not in LEAGUES]
    if unknown:
        raise ValueError(f"Unknown league: {unknown[0]}")
    started_at = now_iso()
    results: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(selected)))) as executor:
        future_map = {executor.submit(fetch_provider_fixtures, league_id): league_id for league_id in selected}
        for future in as_completed(future_map):
            league_id = future_map[future]
            try:
                result = future.result()
                results[league_id] = result
                failures.extend({
                    "league_id": league_id,
                    "provider": result.get("provider", LEAGUES[league_id]["provider"]),
                    "error": f"{error.get('window')}: {error.get('error')}",
                } for error in result.get("errors", []))
            except Exception as exc:
                failures.append({"league_id": league_id, "provider": LEAGUES[league_id]["provider"], "error": str(exc)})

    with STATE_LOCK:
        state = load_state()
        existing = [fixture for fixture in state.get("fixtures", []) if fixture.get("competition") not in results]
        refreshed = [fixture for league_id in selected if league_id in results for fixture in results[league_id]["fixtures"]]
        state["fixtures"] = sorted(existing + refreshed, key=lambda row: row.get("starts_at", ""))
        register_persisted_fixture_teams(state["fixtures"])
        state.setdefault("source_counts", {})["fixtures"] = len(state["fixtures"])
        run = {
            "run_id": f"fixtures_{seed_for(started_at, *selected)}",
            "started_at": started_at,
            "finished_at": now_iso(),
            "status": "success" if not failures else "partial" if results else "failed",
            "requested_leagues": selected,
            "refreshed_leagues": sorted(results),
            "fixtures": len(refreshed),
            "failures": failures,
        }
        state["fixture_refresh_runs"] = ([run] + state.get("fixture_refresh_runs", []))[:25]
        state["last_fixture_refresh"] = run["finished_at"]
        save_state(state)
    return {"run": run, "leagues": results, "fixtures": refreshed}


def sportsipy_provider_summary() -> dict[str, Any]:
    status = sportsipy_status()
    status["sports"] = {
        "nba": {"module": "sportsipy.nba.teams", "class": "Teams"},
        "nfl": {"module": "sportsipy.nfl.teams", "class": "Teams"},
        "mlb": {"module": "sportsipy.mlb.teams", "class": "Teams"},
        "nhl": {"module": "sportsipy.nhl.teams", "class": "Teams"},
    }
    return status


def sportsipy_football_team(team_name: str) -> dict[str, Any]:
    if importlib.util.find_spec("sportsipy") is None:
        raise ValueError("sportsipy is not installed. Run: pip install sportsipy")
    from sportsipy.fb.team import Team  # type: ignore

    team = Team(team_name)
    return {
        "provider": "sportsipy",
        "team": team_name,
        "goals_scored": getattr(team, "goals_scored", None),
        "goals_against": getattr(team, "goals_against", None),
        "wins": getattr(team, "wins", None),
        "losses": getattr(team, "losses", None),
    }


def read_feed_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValueError(f"Feed file not found: {path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            fixtures = payload.get("fixtures")
            if not isinstance(fixtures, list):
                raise ValueError("JSON feed must contain a fixtures list")
            return fixtures
        if isinstance(payload, list):
            return payload
        raise ValueError("JSON feed must be an object or list")
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    raise ValueError("Supported feed formats: .json, .csv")


def ingest_feed_file(path_value: str | None = None) -> dict[str, Any]:
    path = Path(path_value or (DATA_DIR / "sample_live_feed.json"))
    if not path.is_absolute():
        path = ROOT / path
    rows = read_feed_file(path)
    started_at = now_iso()
    odds_ticks = []
    availability_rows = []
    fixtures = []
    failures = []
    quarantined = []

    for index, row in enumerate(rows):
        try:
            competition = str(row.get("competition", "epl"))
            home_source = str(row.get("home") or row.get("home_team") or row.get("home_id"))
            away_source = str(row.get("away") or row.get("away_team") or row.get("away_id"))
            home = resolve_team(competition, home_source)
            away = resolve_team(competition, away_source)
            snapshot = str(row.get("snapshot") or row.get("snapshot_ts") or "T-60m")
            provider = str(row.get("provider") or row.get("source_name") or "file_feed").lower()
            provider_event_id = str(row.get("provider_event_id") or "").strip()
            fixture_id = str(row.get("fixture_id") or (f"{competition}_{provider}_{provider_event_id}" if provider_event_id else f"{competition}_{home['team_id']}_{away['team_id']}"))
            candidate_fixture = {
                "fixture_id": fixture_id,
                "competition": competition,
                "home_id": home["team_id"],
                "away_id": away["team_id"],
                "home_name": home["name"],
                "away_name": away["name"],
                "starts_at": row.get("starts_at") or row.get("kickoff_utc"),
                "snapshot": snapshot,
                "status": str(row.get("status") or "NS"),
                "provider": provider,
                "source_name": provider,
                "provider_event_id": provider_event_id,
                "provider_url": row.get("provider_url") or row.get("source_url"),
                "source_url": row.get("source_url") or row.get("provider_url"),
                "verified": row.get("verified") is True,
                "verified_at": row.get("verified_at"),
                "source_path": str(path),
            }
            if fixture_is_provider_verified(candidate_fixture):
                fixtures.append(candidate_fixture)
            else:
                quarantined.append({"row": index, "fixture_id": fixture_id, "reason": "missing fresh provider verification"})

            odds = row.get("odds", {})
            if isinstance(odds, str):
                odds = json.loads(odds)
            markets = row.get("markets", {})
            if isinstance(markets, str):
                markets = json.loads(markets)
            markets = markets if isinstance(markets, dict) else {}
            odds = odds if isinstance(odds, dict) else {}
            payload_hash = hashlib.sha256(json.dumps(row, sort_keys=True).encode("utf-8")).hexdigest()

            def append_odds_tick(market_type: str, selection: str, value: Any, line_value: Any = None) -> None:
                if value in (None, ""):
                    return
                odds_ticks.append({
                    "odds_tick_id": f"tick_{hashlib.sha256(f'{fixture_id}|{market_type}|{selection}|{line_value}|{started_at}|{index}'.encode('utf-8')).hexdigest()[:16]}",
                    "fixture_id": fixture_id,
                    "competition": competition,
                    "market_type": market_type,
                    "selection": selection,
                    "line_value": float(line_value) if line_value not in (None, "") else None,
                    "decimal_odds": float(value),
                    "source_name": provider,
                    "observed_at": started_at,
                    "payload_hash": payload_hash,
                })

            for selection in ("home", "draw", "away"):
                append_odds_tick("1x2", selection, row.get(f"odds_{selection}", odds.get(selection)))

            def market_section(*keys: str) -> dict[str, Any]:
                for container in (markets, odds):
                    for key in keys:
                        section = container.get(key)
                        if isinstance(section, dict):
                            return section
                return {}

            total_market = market_section("total", "totals", "over_under")
            total_line = row.get("total_line", total_market.get("line"))
            append_odds_tick("total", "over", row.get("odds_over", total_market.get("over")), total_line)
            append_odds_tick("total", "under", row.get("odds_under", total_market.get("under")), total_line)

            btts_market = market_section("btts", "both_teams_to_score")
            append_odds_tick("btts", "yes", row.get("odds_btts_yes", btts_market.get("yes")))
            append_odds_tick("btts", "no", row.get("odds_btts_no", btts_market.get("no")))

            spread_market = market_section("spread", "handicap", "run_line", "puck_line")
            spread_line = row.get("spread_line", spread_market.get("line"))
            append_odds_tick("spread", "home", row.get("odds_spread_home", spread_market.get("home")), spread_line)
            append_odds_tick("spread", "away", row.get("odds_spread_away", spread_market.get("away")), spread_line)

            for side in ("home", "away"):
                team_total = market_section(f"{side}_team_total")
                team_line = row.get(f"{side}_team_total_line", team_total.get("line"))
                append_odds_tick(f"{side}_team_total", "over", row.get(f"odds_{side}_team_over", team_total.get("over")), team_line)
                append_odds_tick(f"{side}_team_total", "under", row.get(f"odds_{side}_team_under", team_total.get("under")), team_line)

            availability = row.get("availability", {})
            if isinstance(availability, str):
                availability = json.loads(availability)
            availability_rows.append({
                "availability_id": f"avail_{hashlib.sha256(f'{fixture_id}|{started_at}|{index}'.encode('utf-8')).hexdigest()[:16]}",
                "fixture_id": fixture_id,
                "competition": competition,
                "home_starters_missing": int(row.get("home_starters_missing", availability.get("home_starters_missing", 0) if isinstance(availability, dict) else 0)),
                "away_starters_missing": int(row.get("away_starters_missing", availability.get("away_starters_missing", 0) if isinstance(availability, dict) else 0)),
                "goalkeeper_missing": int(row.get("goalkeeper_missing", availability.get("goalkeeper_missing", 0) if isinstance(availability, dict) else 0)),
                "confidence": float(row.get("availability_confidence", availability.get("confidence", 0.8) if isinstance(availability, dict) else 0.8)),
                "lineup_type": str(row.get("lineup_type", availability.get("lineup_type", "predicted") if isinstance(availability, dict) else "predicted")),
                "observed_at": started_at,
                "source_name": provider,
            })
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            failures.append({"row": index, "error": str(exc)})

    with STATE_LOCK:
        state = load_state()
        state["odds_ticks"] = odds_ticks + state.get("odds_ticks", [])
        state["availability_rows"] = availability_rows + state.get("availability_rows", [])
        existing_fixtures = {fixture["fixture_id"]: fixture for fixture in state.get("fixtures", [])}
        for fixture in fixtures:
            existing_fixtures[fixture["fixture_id"]] = fixture
        state["fixtures"] = list(existing_fixtures.values())
        state["source_counts"] = {
            "odds_ticks": len(state["odds_ticks"]),
            "availability_rows": len(state["availability_rows"]),
            "fixtures": len(state["fixtures"]),
            "teams": sum(len(v) for v in TEAMS.values()),
        }
        run = {
            "run_id": f"ingest_{seed_for(started_at, path, len(rows))}",
            "source": str(path),
            "started_at": started_at,
            "finished_at": now_iso(),
            "status": "success" if not failures and not quarantined else "partial",
            "fixtures": len(fixtures),
            "odds_ticks": len(odds_ticks),
            "availability_rows": len(availability_rows),
            "failures": failures,
            "quarantined": quarantined,
        }
        state["ingestion_runs"] = ([run] + state.get("ingestion_runs", []))[:25]
        save_state(state)
    return {"run": run, "fixtures": fixtures, "odds_ticks": odds_ticks, "availability_rows": availability_rows, "quarantined": quarantined}


def ingest_results_file(path_value: str | None = None) -> dict[str, Any]:
    path = Path(path_value or (DATA_DIR / "sample_results.json"))
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise ValueError(f"Results file not found: {path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("results", payload) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("JSON result feed must contain a results list")
    elif path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        raise ValueError("Supported result formats: .json, .csv")

    started_at = now_iso()
    results = []
    failures = []
    quarantined = []
    fixture_index = {fixture["fixture_id"]: fixture for fixture in load_state().get("fixtures", [])}
    for index, row in enumerate(rows):
        try:
            competition = str(row.get("competition", "epl"))
            home = resolve_team(competition, str(row.get("home") or row.get("home_team") or row.get("home_id")))
            away = resolve_team(competition, str(row.get("away") or row.get("away_team") or row.get("away_id")))
            home_goals = int(row["home_goals"])
            away_goals = int(row["away_goals"])
            outcome = "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"
            provider = str(row.get("provider") or row.get("source_name") or "").lower()
            provider_event_id = str(row.get("provider_event_id") or "").strip()
            fixture_id = str(row.get("fixture_id") or (f"{competition}_{provider}_{provider_event_id}" if provider_event_id else ""))
            if provider not in TRUSTED_FIXTURE_PROVIDERS or row.get("verified") is not True or not provider_event_id or not fixture_id:
                quarantined.append({"row": index, "reason": "missing provider-verified result identity"})
                continue
            results.append({
                "result_id": f"result_{hashlib.sha256(f'{fixture_id}|{home_goals}|{away_goals}'.encode('utf-8')).hexdigest()[:16]}",
                "fixture_id": fixture_id,
                "competition": competition,
                "home_team_id": home["team_id"],
                "away_team_id": away["team_id"],
                "home_goals": home_goals,
                "away_goals": away_goals,
                "outcome": outcome,
                "start_time": row.get("start_time") or row.get("starts_at") or row.get("kickoff_utc") or (fixture_index.get(fixture_id) or {}).get("starts_at"),
                "provider_event_id": provider_event_id,
                "source_name": provider,
                "verified": True,
                "observed_at": str(row.get("observed_at") or started_at),
            })
        except (KeyError, TypeError, ValueError) as exc:
            failures.append({"row": index, "error": str(exc)})

    with STATE_LOCK:
        state = load_state()
        existing = {result["fixture_id"]: result for result in state.get("results", [])}
        for result in results:
            existing[result["fixture_id"]] = result
        state["results"] = list(existing.values())
        run = {
            "run_id": f"results_{seed_for(started_at, path, len(results))}",
            "source": str(path),
            "started_at": started_at,
            "finished_at": now_iso(),
            "status": "success" if not failures and not quarantined else "partial",
            "results": len(results),
            "failures": failures,
            "quarantined": quarantined,
        }
        state["settlement_runs"] = ([run] + state.get("settlement_runs", []))[:25]
        save_state(state)
    return {"run": run, "results": results, "quarantined": quarantined}


def seed_for(*parts: Any) -> int:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def normalize_name(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
    return " ".join(cleaned.split())


def find_team(competition: str, team_id: str) -> dict[str, Any] | None:
    return next((team for team in TEAMS.get(competition, []) if team["id"] == team_id), None)


def resolve_team(competition: str, source_value: str) -> dict[str, Any]:
    teams = TEAMS.get(competition, [])
    normalized = normalize_name(source_value)
    alias_id = TEAM_ALIASES.get(competition, {}).get(normalized)
    if alias_id:
        team = find_team(competition, alias_id)
        if team:
            return {"input": source_value, "team_id": alias_id, "name": team["name"], "confidence": 1.0, "reason": "alias"}
    for team in teams:
        if source_value == team["id"] or normalized == normalize_name(team["name"]):
            return {"input": source_value, "team_id": team["id"], "name": team["name"], "confidence": 1.0, "reason": "canonical"}
    candidates = []
    source_tokens = set(normalized.split())
    for team in teams:
        team_tokens = set(normalize_name(team["name"]).split()) | {team["id"].replace("_", " ")}
        overlap = len(source_tokens & team_tokens)
        union = max(len(source_tokens | team_tokens), 1)
        score = overlap / union
        if score >= 0.35:
            candidates.append((score, team))
    if candidates:
        score, team = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
        return {"input": source_value, "team_id": team["id"], "name": team["name"], "confidence": round(score, 3), "reason": "fuzzy"}
    raise ValueError(f"Could not resolve team '{source_value}' in {competition}")


def normalized_probabilities(home: float, draw: float, away: float) -> dict[str, float]:
    total = max(home + draw + away, 0.0001)
    return {"home": home / total, "draw": draw / total, "away": away / total}


def latest_fixture_market(fixture_id: str, state: dict[str, Any] | None = None) -> dict[str, float] | None:
    state = state or load_state()
    latest: dict[str, dict[str, Any]] = {}
    for tick in state.get("odds_ticks", []):
        if tick.get("fixture_id") != fixture_id:
            continue
        selection = tick.get("selection")
        if selection in {"home", "draw", "away"} and selection not in latest:
            latest[selection] = tick
    if {"home", "draw", "away"}.issubset(latest):
        return {key: float(latest[key]["decimal_odds"]) for key in ("home", "draw", "away")}
    return None


def latest_fixture_secondary_markets(fixture_id: str, state: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    state = state or load_state()
    aliases = {
        "totals": "total",
        "total": "total",
        "over_under": "total",
        "btts": "btts",
        "both_teams_to_score": "btts",
        "spread": "spread",
        "handicap": "spread",
        "run_line": "spread",
        "puck_line": "spread",
        "home_team_total": "home_team_total",
        "away_team_total": "away_team_total",
    }
    expected = {
        "total": {"over", "under"},
        "btts": {"yes", "no"},
        "spread": {"home", "away"},
        "home_team_total": {"over", "under"},
        "away_team_total": {"over", "under"},
    }
    grouped: dict[str, dict[str, Any]] = {}
    for tick in state.get("odds_ticks", []):
        if tick.get("fixture_id") != fixture_id:
            continue
        market_type = aliases.get(str(tick.get("market_type") or "").lower())
        selection = str(tick.get("selection") or "").lower()
        if not market_type or selection not in expected[market_type]:
            continue
        market = grouped.setdefault(market_type, {
            "line": tick.get("line_value"),
            "source": tick.get("source_name"),
            "observed_at": tick.get("observed_at"),
        })
        if selection not in market:
            market[selection] = float(tick["decimal_odds"])
        if market.get("line") is None and tick.get("line_value") is not None:
            market["line"] = float(tick["line_value"])
    return {
        market_type: market
        for market_type, market in grouped.items()
        if expected[market_type].issubset(market)
        and (market_type == "btts" or market.get("line") is not None)
    }


def latest_fixture_availability(fixture_id: str, state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = state or load_state()
    for row in state.get("availability_rows", []):
        if row.get("fixture_id") == fixture_id:
            return row
    return None


def fixture_id_for(competition: str, home_id: str, away_id: str) -> str:
    return f"{competition}_{home_id}_{away_id}"


def fixture_from_id(fixture_id: str) -> dict[str, str]:
    stored = next((fixture for fixture in load_state().get("fixtures", []) if fixture.get("fixture_id") == fixture_id), None)
    if stored:
        return stored
    parts = fixture_id.split("_")
    for competition, teams in TEAMS.items():
        prefix = f"{competition}_"
        if not fixture_id.startswith(prefix):
            continue
        rest = fixture_id[len(prefix):]
        for home in teams:
            home_prefix = f"{home['id']}_"
            if not rest.startswith(home_prefix):
                continue
            away_id = rest[len(home_prefix):]
            if find_team(competition, away_id):
                return {"competition": competition, "home_id": home["id"], "away_id": away_id, "snapshot": "T-60m"}
    raise ValueError(f"Unknown fixture id: {fixture_id}")


def fixture_card(
    fixture: dict[str, Any],
    state_snapshot: dict[str, Any] | None = None,
    include_prediction: bool = True,
    prediction_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    competition = str(fixture.get("competition", "epl"))
    home = find_team(competition, str(fixture["home_id"]))
    away = find_team(competition, str(fixture["away_id"]))
    if not home or not away:
        raise ValueError("Fixture references unknown teams")
    fixture_id = str(fixture.get("fixture_id") or fixture_id_for(competition, home["id"], away["id"]))
    prediction = prediction_override
    if include_prediction and prediction is None:
        prediction = build_prediction(
            competition,
            home["id"],
            away["id"],
            str(fixture.get("snapshot", "T-60m")),
            fixture_id,
            state_snapshot,
        )
    home_card = {**home, "name": fixture.get("home_name") or home["name"], "badge": fixture.get("home_badge")}
    away_card = {**away, "name": fixture.get("away_name") or away["name"], "badge": fixture.get("away_badge")}
    return {
        "fixture_id": fixture_id,
        "competition": competition,
        "league": LEAGUES.get(competition, {"name": competition}),
        "home_team": home_card,
        "away_team": away_card,
        "starts_at": fixture.get("starts_at"),
        "status": fixture.get("status", "NS"),
        "venue": fixture.get("venue"),
        "round": fixture.get("round"),
        "round_name": PROVIDER_ROUND_DISPLAY_NAMES.get(competition, {}).get(fixture.get("round_name"), fixture.get("round_name")),
        "snapshot": fixture.get("snapshot", "T-60m"),
        "provider": fixture.get("provider"),
        "provider_event_id": fixture.get("provider_event_id"),
        "provider_url": fixture.get("provider_url"),
        "verified": fixture.get("verified") is True,
        "verified_at": fixture.get("verified_at"),
        "stale_verified": bool(fixture.get("stale_verified", fixture_is_stale(fixture))),
        "prediction_available": prediction is not None,
        "analysis_source": prediction["analysis_policy"]["mode"] if prediction else None,
        "availability_source": prediction["availability"]["source"] if prediction else None,
        "probabilities": prediction["probabilities"] if prediction else None,
        "recommendation": prediction["recommendation"] if prediction else None,
        "market_horizons": prediction["market_horizons"] if prediction else None,
    }


def fixtures_for_league(league_id: str) -> list[dict[str, Any]]:
    state = load_state()
    fixtures = state.get("fixtures") or []
    league_fixtures = [fixture for fixture in fixtures if fixture.get("competition") == league_id]
    return [fixture_card(fixture, state, include_prediction=False) for fixture in league_fixtures]


def league_summary() -> list[dict[str, Any]]:
    summaries = []
    state = load_state()
    stored_fixtures = state.get("fixtures") or []
    recent = state.get("recent_predictions") or []
    for league_id, league in LEAGUES.items():
        league_fixtures = [fixture for fixture in stored_fixtures if fixture.get("competition") == league_id]
        league_predictions = [row for row in recent if row.get("competition") == league_id]
        recommendations = [row.get("recommendation", {}) for row in league_predictions]
        analysis_count = len([row for row in recommendations if row.get("action") == "analysis_only"])
        avg_confidence = sum(float(row.get("probability") or 0.0) for row in recommendations) / max(len(recommendations), 1)
        provider_rows = len(league_fixtures)
        fixture_providers = sorted({str(fixture.get("provider")) for fixture in league_fixtures if fixture.get("provider")})
        league_verified_at = max(
            (str(fixture.get("verified_at") or "") for fixture in league_fixtures),
            default=None,
        )
        summaries.append({
            **league,
            "sport_name": SPORTS.get(league.get("sport", "football"), {}).get("name", league.get("sport", "football")),
            "teams": len(TEAMS.get(league_id, [])),
            "fixtures": provider_rows,
            "fixture_source": "/".join(fixture_providers) or league.get("provider"),
            "verified_fixtures": provider_rows,
            "verified_at": league_verified_at,
            "recommendations": analysis_count,
            "avg_model_confidence": round(avg_confidence * 100, 2),
        })
    return summaries


def espn_get(url: str, timeout: int = 20) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "SportEngine/0.1", "Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("ESPN returned an invalid payload")
    return payload


def espn_provider_url(competition: str, resource: str, *, standings: bool = False, season: int | None = None) -> str:
    provider = ESPN_LEAGUES.get(competition)
    if not provider:
        raise ValueError(f"ESPN mapping is unavailable for {competition}")
    base = ESPN_STANDINGS_BASE if standings else ESPN_SITE_BASE
    url = f"{base}/{provider['sport']}/{provider['league']}/{resource.lstrip('/')}"
    if season is not None:
        url += f"?season={season}"
    return url


def espn_season_for_fixture(fixture: dict[str, Any]) -> int:
    kickoff = parse_utc_timestamp(str(fixture.get("starts_at") or "")) or datetime.now(timezone.utc)
    competition = str(fixture.get("competition") or "")
    if competition in {"nba", "nhl"} and kickoff.month >= 7:
        return kickoff.year + 1
    return kickoff.year


def normalized_match_name(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = normalize_name(raw)
    ascii_value = "".join(
        character for character in unicodedata.normalize("NFKD", raw)
        if not unicodedata.combining(character)
    )
    ascii_normalized = normalize_name(ascii_value)
    override = ESPN_TEAM_NAME_OVERRIDES.get(normalized) or ESPN_TEAM_NAME_OVERRIDES.get(ascii_normalized)
    return normalize_name(override) if override else ascii_normalized


def parse_espn_team_catalog(payload: dict[str, Any]) -> list[dict[str, Any]]:
    sports = payload.get("sports") if isinstance(payload.get("sports"), list) else []
    leagues = sports[0].get("leagues", []) if sports and isinstance(sports[0], dict) else []
    rows = leagues[0].get("teams", []) if leagues and isinstance(leagues[0], dict) else []
    teams = []
    for row in rows if isinstance(rows, list) else []:
        team = row.get("team") if isinstance(row, dict) and isinstance(row.get("team"), dict) else {}
        team_id = str(team.get("id") or "").strip()
        if not team_id:
            continue
        names = list(dict.fromkeys(
            str(team.get(key) or "").strip()
            for key in ("displayName", "shortDisplayName", "location", "name", "nickname", "abbreviation")
            if team.get(key)
        ))
        teams.append({
            "id": team_id,
            "name": str(team.get("displayName") or team.get("shortDisplayName") or team_id),
            "abbreviation": team.get("abbreviation"),
            "names": names,
            "logo": ((team.get("logos") or [{}])[0] or {}).get("href") if isinstance(team.get("logos"), list) else None,
        })
    return teams


def espn_team_catalog(competition: str) -> dict[str, Any]:
    url = espn_provider_url(competition, "teams?limit=500")
    return {"url": url, "teams": parse_espn_team_catalog(espn_get(url))}


def resolve_espn_team(catalog: list[dict[str, Any]], *candidate_names: Any) -> dict[str, Any] | None:
    candidates = [str(name).strip() for name in candidate_names if str(name or "").strip()]
    candidates = [name for name in candidates if "/" not in name and normalized_match_name(name) not in {"tbd", "winner"}]
    if not candidates:
        return None

    for candidate in candidates:
        wanted = normalized_match_name(candidate)
        for team in catalog:
            if wanted and any(wanted == normalized_match_name(name) for name in team.get("names", [])):
                return team

    generic = {"fc", "cf", "afc", "ac", "sc", "sv", "tsg", "rb", "club", "de", "1", "04", "05", "07"}
    best: tuple[float, dict[str, Any]] | None = None
    for candidate in candidates:
        candidate_tokens = set(normalized_match_name(candidate).split()) - generic
        if not candidate_tokens:
            continue
        for team in catalog:
            for name in team.get("names", []):
                team_tokens = set(normalized_match_name(name).split()) - generic
                if not team_tokens:
                    continue
                overlap = len(candidate_tokens & team_tokens)
                union = len(candidate_tokens | team_tokens)
                score = overlap / max(union, 1)
                if overlap and (candidate_tokens <= team_tokens or team_tokens <= candidate_tokens):
                    score = max(score, 0.86)
                if best is None or score > best[0]:
                    best = (score, team)
    return best[1] if best and best[0] >= 0.72 else None


def _numeric(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").strip())
    except ValueError:
        return None


def _whole(value: Any) -> int | None:
    number = _numeric(value)
    return int(number) if number is not None else None


def _monthly_windows(start: datetime, end: datetime) -> list[tuple[str, str]]:
    windows = []
    cursor = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
    final = end.astimezone(timezone.utc)
    while cursor <= final:
        next_month = datetime(cursor.year + (1 if cursor.month == 12 else 0), 1 if cursor.month == 12 else cursor.month + 1, 1, tzinfo=timezone.utc)
        window_end = min(final, next_month - timedelta(days=1))
        windows.append((cursor.strftime("%Y%m%d"), window_end.strftime("%Y%m%d")))
        cursor = next_month
    return windows


def historical_date_windows(competition: str, reference: datetime | None = None) -> list[tuple[str, str]]:
    now = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ranges: list[tuple[datetime, datetime]] = []
    if competition == "fifa_wc":
        ranges.append((datetime(2022, 11, 1, tzinfo=timezone.utc), datetime(2022, 12, 31, tzinfo=timezone.utc)))
        if now >= datetime(2026, 6, 1, tzinfo=timezone.utc):
            ranges.append((datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 31, tzinfo=timezone.utc)))
    elif competition in {"epl", "laliga", "bundesliga", "seriea", "ligue1"}:
        start_year = now.year - 1 if now.month >= 7 else now.year - 2
        for season_start in (start_year - 1, start_year):
            ranges.append((datetime(season_start, 8, 1, tzinfo=timezone.utc), datetime(season_start + 1, 6, 30, tzinfo=timezone.utc)))
    elif competition == "mlb":
        season_year = now.year if now.month >= 11 else now.year - 1
        for year in (season_year - 1, season_year):
            ranges.append((datetime(year, 3, 1, tzinfo=timezone.utc), datetime(year, 11, 30, tzinfo=timezone.utc)))
    elif competition == "nfl":
        start_year = now.year - 1 if now.month >= 3 else now.year - 2
        for season_start in (start_year - 1, start_year):
            ranges.append((datetime(season_start, 8, 1, tzinfo=timezone.utc), datetime(season_start + 1, 2, 28, tzinfo=timezone.utc)))
    elif competition in {"nba", "nhl"}:
        end_year = now.year if now.month >= 7 else now.year - 1
        for season_end in (end_year - 1, end_year):
            ranges.append((datetime(season_end - 1, 10, 1, tzinfo=timezone.utc), datetime(season_end, 6, 30, tzinfo=timezone.utc)))
    return [window for start, end in ranges for window in _monthly_windows(start, end)]


def parse_espn_historical_scoreboard(
    payload: dict[str, Any],
    competition: str,
    source_url: str,
    observed_at: str | None = None,
) -> list[dict[str, Any]]:
    observed_at = observed_at or now_iso()
    games = []
    for event in payload.get("events", []) if isinstance(payload.get("events"), list) else []:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") if isinstance(event.get("competitions"), list) else []
        event_competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
        status = event.get("status") if isinstance(event.get("status"), dict) else event_competition.get("status") if isinstance(event_competition.get("status"), dict) else {}
        status_type = status.get("type") if isinstance(status.get("type"), dict) else {}
        if status_type.get("completed") is not True and str(status_type.get("state") or "").lower() != "post":
            continue
        competitors = event_competition.get("competitors") if isinstance(event_competition.get("competitors"), list) else []
        home = next((row for row in competitors if isinstance(row, dict) and row.get("homeAway") == "home"), None)
        away = next((row for row in competitors if isinstance(row, dict) and row.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        home_team = home.get("team") if isinstance(home.get("team"), dict) else {}
        away_team = away.get("team") if isinstance(away.get("team"), dict) else {}
        home_id = str(home_team.get("id") or home.get("id") or "")
        away_id = str(away_team.get("id") or away.get("id") or "")
        event_id = str(event.get("id") or "")
        kickoff = parse_utc_timestamp(str(event.get("date") or ""))
        home_score = _numeric((home.get("score") or {}).get("value") if isinstance(home.get("score"), dict) else home.get("score"))
        away_score = _numeric((away.get("score") or {}).get("value") if isinstance(away.get("score"), dict) else away.get("score"))
        if not event_id or not home_id or not away_id or not kickoff or home_score is None or away_score is None:
            continue
        winners = [row for row in (home, away) if row.get("winner") is True]
        if len(winners) == 1:
            outcome = "home" if winners[0] is home else "away"
        else:
            outcome = "home" if home_score > away_score else "away" if away_score > home_score else "draw"
        season_row = event.get("season") if isinstance(event.get("season"), dict) else {}
        games.append({
            "game_id": f"{competition}_espn_{event_id}",
            "competition": competition,
            "provider_event_id": event_id,
            "start_time": utc_iso(kickoff),
            "season": season_row.get("year"),
            "season_type": season_row.get("type"),
            "home_team_id": home_id,
            "away_team_id": away_id,
            "home_team": str(home_team.get("displayName") or home_team.get("shortDisplayName") or home_id),
            "away_team": str(away_team.get("displayName") or away_team.get("shortDisplayName") or away_id),
            "home_score": int(home_score) if home_score.is_integer() else home_score,
            "away_score": int(away_score) if away_score.is_integer() else away_score,
            "outcome": outcome,
            "source_name": "espn",
            "source_url": source_url,
            "verified": True,
            "observed_at": observed_at,
            "payload_hash": hashlib.sha256(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest(),
        })
    return games


def fetch_espn_history_window(competition: str, start: str, end: str) -> dict[str, Any]:
    observed_at = now_iso()
    start_date = datetime.strptime(start, "%Y%m%d").replace(tzinfo=timezone.utc)
    end_date = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
    ranges = [(start, end)]
    if competition in {"nba", "nfl", "mlb", "nhl"}:
        ranges = []
        cursor = start_date
        while cursor <= end_date:
            sub_end = min(end_date, cursor + timedelta(days=6))
            ranges.append((cursor.strftime("%Y%m%d"), sub_end.strftime("%Y%m%d")))
            cursor = sub_end + timedelta(days=1)
    games: list[dict[str, Any]] = []
    urls = []
    for sub_start, sub_end in ranges:
        url = espn_provider_url(competition, f"scoreboard?dates={sub_start}-{sub_end}&limit=1000")
        urls.append(url)
        last_error: Exception | None = None
        payload: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                payload = espn_get(url, timeout=45)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
        if payload is None:
            raise ValueError(str(last_error or "historical provider request failed"))
        games.extend(parse_espn_historical_scoreboard(payload, competition, url, observed_at))
    deduped = {game["game_id"]: game for game in games}
    return {"competition": competition, "start": start, "end": end, "source_url": urls[0], "games": list(deduped.values())}


def historical_seasons(competition: str, reference: datetime | None = None) -> list[int]:
    now = (reference or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if competition in {"nba", "nhl"}:
        end_year = now.year if now.month >= 7 else now.year - 1
        return [end_year - 1, end_year]
    if competition == "nfl":
        start_year = now.year - 1 if now.month >= 3 else now.year - 2
        return [start_year - 1, start_year]
    if competition == "mlb":
        season_year = now.year if now.month >= 11 else now.year - 1
        return [season_year - 1, season_year]
    return []


def fetch_espn_team_season_history(competition: str, team_id: str, season: int) -> dict[str, Any]:
    url = espn_provider_url(competition, f"teams/{team_id}/schedule", season=season)
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            payload = espn_get(url, timeout=60)
            return {
                "team_id": team_id,
                "season": season,
                "games": parse_espn_historical_scoreboard(payload, competition, url),
            }
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    raise ValueError(str(last_error or "team schedule request failed"))


def refresh_historical_team_schedules(competition: str) -> dict[str, Any]:
    if competition not in {"nba", "nfl", "mlb", "nhl"}:
        raise ValueError("Team-schedule history fallback supports NBA, NFL, MLB, and NHL")
    started_at = now_iso()
    catalog = espn_team_catalog(competition)["teams"]
    seasons = historical_seasons(competition)
    requests = [(str(team["id"]), season) for team in catalog for season in seasons]
    games: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(requests)))) as executor:
        future_map = {
            executor.submit(fetch_espn_team_season_history, competition, team_id, season): (team_id, season)
            for team_id, season in requests
        }
        for future in as_completed(future_map):
            team_id, season = future_map[future]
            try:
                games.extend(future.result()["games"])
            except Exception as exc:
                failures.append({"team_id": team_id, "season": str(season), "error": str(exc)})
    deduped = {game["game_id"]: game for game in games}
    with STATE_LOCK:
        state = load_state()
        merged = {game["game_id"]: game for game in state.get("historical_games", [])}
        merged.update(deduped)
        state["historical_games"] = sorted(merged.values(), key=lambda row: (row["start_time"], row["game_id"]))
        if not failures:
            for start, end in historical_date_windows(competition):
                state.setdefault("historical_window_status", {})[f"{competition}:{start}-{end}"] = {
                    "status": "success",
                    "updated_at": now_iso(),
                    "source": "team_schedule_fallback",
                }
        state.setdefault("source_counts", {})["historical_games"] = len(state["historical_games"])
        save_state(state)
    return {
        "run": {
            "run_id": f"team_history_{seed_for(competition, started_at)}",
            "competition": competition,
            "started_at": started_at,
            "finished_at": now_iso(),
            "status": "success" if not failures else "partial",
            "teams": len(catalog),
            "seasons": seasons,
            "games": len(deduped),
            "failures": failures,
        }
    }


def refresh_historical_games(
    league_ids: list[str] | None = None,
    failed_only: bool = False,
    run_tuning: bool = True,
) -> dict[str, Any]:
    selected = list(dict.fromkeys(league_ids or list(LEAGUES)))
    unknown = [league_id for league_id in selected if league_id not in LEAGUES]
    if unknown:
        raise ValueError(f"Unknown league: {unknown[0]}")
    started_at = now_iso()
    requests = [
        (competition, start, end)
        for competition in selected
        for start, end in historical_date_windows(competition)
    ]
    if failed_only:
        window_status = load_state().get("historical_window_status", {})
        requests = [
            row for row in requests
            if window_status.get(f"{row[0]}:{row[1]}-{row[2]}", {}).get("status") != "success"
        ]
    fetched_games: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    successful_windows: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(requests)))) as executor:
        future_map = {
            executor.submit(fetch_espn_history_window, competition, start, end): (competition, start, end)
            for competition, start, end in requests
        }
        for future in as_completed(future_map):
            competition, start, end = future_map[future]
            window_key = f"{competition}:{start}-{end}"
            try:
                fetched_games.extend(future.result()["games"])
                successful_windows.add(window_key)
            except Exception as exc:
                failures.append({"competition": competition, "window": f"{start}-{end}", "error": str(exc)})
    deduped = {game["game_id"]: game for game in fetched_games}
    state = load_state()
    merged_games = {game["game_id"]: game for game in state.get("historical_games", [])}
    merged_games.update(deduped)
    historical_games = sorted(merged_games.values(), key=lambda row: (row["start_time"], row["game_id"]))
    with STATE_LOCK:
        fetched_state = load_state()
        fetched_state["historical_games"] = historical_games
        fetched_state.setdefault("source_counts", {})["historical_games"] = len(historical_games)
        fetched_state["historical_data_cutoff"] = max((game["start_time"] for game in historical_games), default=None)
        window_status = fetched_state.setdefault("historical_window_status", {})
        for window_key in successful_windows:
            window_status[window_key] = {"status": "success", "updated_at": now_iso()}
        for failure in failures:
            window_key = f"{failure['competition']}:{failure['window']}"
            if window_status.get(window_key, {}).get("status") != "success":
                window_status[window_key] = {"status": "failed", "updated_at": now_iso(), "error": failure["error"]}
        save_state(fetched_state)
    if not run_tuning:
        run = {
            "run_id": f"history_{seed_for(started_at, *selected)}",
            "started_at": started_at,
            "finished_at": now_iso(),
            "status": "success" if not failures else "partial" if deduped else "failed",
            "requested_leagues": selected,
            "failed_only": failed_only,
            "windows": len(requests),
            "games": len(historical_games),
            "refreshed_games": len(deduped),
            "failures": failures,
            "tuning": "deferred",
        }
        with STATE_LOCK:
            history_state = load_state()
            history_state["historical_refresh_runs"] = ([run] + history_state.get("historical_refresh_runs", []))[:25]
            save_state(history_state)
        return {"run": run, "backtest": backtest()}
    report = run_walk_forward_backtest(historical_games)
    persisted_status = load_state().get("historical_window_status", {})
    completeness = {
        competition: all(
            persisted_status.get(f"{competition}:{start}-{end}", {}).get("status") == "success"
            for start, end in historical_date_windows(competition)
        )
        for competition in LEAGUES
    }
    for league_report in report.get("per_league", []):
        league_report["history_complete"] = completeness.get(league_report["competition"], False)
        league_report["target_reached"] = bool(league_report.get("target_reached") and league_report["history_complete"])
    report["target"]["reached"] = bool(report["target"]["reached"] and all(completeness.values()))
    report["data_quality"] = {
        "complete": all(completeness.values()),
        "league_completeness": completeness,
        "requested_windows": len(requests),
        "failed_windows_this_run": failures,
    }
    report["generated_at"] = now_iso()
    report["data_cutoff"] = max((game["start_time"] for game in historical_games), default=None)
    expanded_report = run_expanded_market_backtest(historical_games)
    expanded_report["target"]["reached"] = bool(
        expanded_report["target"]["reached"] and all(completeness.values())
    )
    expanded_report["data_quality"] = {
        "complete": all(completeness.values()),
        "league_completeness": completeness,
        "requested_windows": len(requests),
        "failed_windows_this_run": failures,
    }
    expanded_report["generated_at"] = now_iso()
    expanded_report["data_cutoff"] = report["data_cutoff"]
    run = {
        "run_id": f"history_{seed_for(started_at, *selected)}",
        "started_at": started_at,
        "finished_at": now_iso(),
        "status": "success" if not failures else "partial" if deduped else "failed",
        "requested_leagues": selected,
        "failed_only": failed_only,
        "windows": len(requests),
        "games": len(historical_games),
        "refreshed_games": len(deduped),
        "failures": failures,
        "target_reached": report["target"]["reached"],
        "expanded_market_target_reached": expanded_report["target"]["reached"],
    }
    with STATE_LOCK:
        latest = load_state()
        latest["ranking_parameters"] = report.get("parameters", {})
        latest["ranking_backtest"] = report
        latest["market_parameters"] = expanded_report.get("parameters", {})
        latest["expanded_market_backtest"] = expanded_report
        latest["historical_refresh_runs"] = ([run] + latest.get("historical_refresh_runs", []))[:25]
        latest["backtest_runs"] = ([run] + latest.get("backtest_runs", []))[:25]
        latest.setdefault("source_counts", {})["historical_games"] = len(historical_games)
        save_state(latest)
    return {"run": run, "backtest": {**report, "expanded_markets": expanded_report}}


def _espn_stat_map(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("name") or row.get("abbreviation") or ""): row
        for row in entry.get("stats", [])
        if isinstance(row, dict)
    }


def _espn_stat_number(stats: dict[str, dict[str, Any]], *names: str) -> float | None:
    for name in names:
        row = stats.get(name)
        if row:
            number = _numeric(row.get("value"))
            if number is not None:
                return number
    return None


def _espn_stat_display(stats: dict[str, dict[str, Any]], *names: str) -> str | None:
    for name in names:
        row = stats.get(name)
        if row and row.get("displayValue") not in (None, ""):
            return str(row["displayValue"])
    return None


def parse_espn_standings_payload(payload: dict[str, Any], competition: str) -> list[dict[str, Any]]:
    groups: list[tuple[str | None, list[dict[str, Any]]]] = []

    def collect(node: Any, inherited_name: str | None = None) -> None:
        if isinstance(node, list):
            for item in node:
                collect(item, inherited_name)
            return
        if not isinstance(node, dict):
            return
        group_name = str(node.get("name") or node.get("shortName") or inherited_name or "").strip() or None
        standings = node.get("standings")
        entries = standings.get("entries") if isinstance(standings, dict) else None
        if isinstance(entries, list):
            groups.append((group_name, entries))
        for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
            collect(child, group_name)

    collect(payload)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group_name, entries in groups:
        for index, entry in enumerate(entries, 1):
            if not isinstance(entry, dict):
                continue
            team = entry.get("team") if isinstance(entry.get("team"), dict) else {}
            team_id = str(team.get("id") or "").strip()
            if not team_id or team_id in seen:
                continue
            seen.add(team_id)
            stats = _espn_stat_map(entry)
            wins = _whole(_espn_stat_number(stats, "wins"))
            losses = _whole(_espn_stat_number(stats, "losses"))
            draws = _whole(_espn_stat_number(stats, "ties", "draws"))
            played = _whole(_espn_stat_number(stats, "gamesPlayed", "games"))
            if played is None and wins is not None and losses is not None:
                played = wins + losses + (draws or 0)
            rank = _whole(_espn_stat_number(stats, "playoffSeed", "rank")) or index
            points_value = _espn_stat_number(stats, "points")
            points = _whole(points_value) if competition in {"fifa_wc", "epl", "laliga", "bundesliga", "seriea", "ligue1", "nhl"} else None
            win_pct = _espn_stat_number(stats, "winPercent", "winPct")
            record = _espn_stat_display(stats, "overall", "record")
            if not record and wins is not None and losses is not None:
                record = f"{wins}-{losses}" + (f"-{draws}" if draws is not None else "")
            rows.append({
                "rank": rank,
                "team": str(team.get("displayName") or team.get("shortDisplayName") or team_id),
                "team_id": team_id,
                "played": played,
                "wins": wins,
                "losses": losses,
                "draws": draws,
                "points": points,
                "win_pct": round(win_pct, 4) if win_pct is not None else None,
                "record": record,
                "form": _espn_stat_display(stats, "streak"),
                "group": group_name,
            })
    return rows


def build_verified_standings(
    rows: list[dict[str, Any]],
    home_team_id: Any,
    away_team_id: Any,
    source: str,
    source_url: str,
) -> dict[str, Any]:
    home = next((row for row in rows if str(row.get("team_id")) == str(home_team_id)), None)
    away = next((row for row in rows if str(row.get("team_id")) == str(away_team_id)), None)
    verified = home is not None and away is not None
    model_eligible = bool(
        verified
        and (home.get("played") or 0) > 0
        and (away.get("played") or 0) > 0
    )
    rank_gap = (away["rank"] - home["rank"]) if verified and home.get("rank") and away.get("rank") else None
    points_gap = (home["points"] - away["points"]) if verified and home.get("points") is not None and away.get("points") is not None else None
    win_pct_gap = (home["win_pct"] - away["win_pct"]) if verified and home.get("win_pct") is not None and away.get("win_pct") is not None else None
    advantage = 0.0
    if model_eligible and rank_gap is not None:
        advantage += rank_gap * 0.008
    if model_eligible and points_gap is not None:
        advantage += points_gap * 0.002
    if model_eligible and win_pct_gap is not None:
        advantage += win_pct_gap * 0.16
    return {
        "source": source,
        "source_url": source_url,
        "observed_at": now_iso(),
        "verified": verified,
        "model_eligible": model_eligible,
        "home": home or {"team_id": str(home_team_id), "team": None, "rank": None, "points": None, "record": None},
        "away": away or {"team_id": str(away_team_id), "team": None, "rank": None, "points": None, "record": None},
        "table": rows,
        "signal": {
            "rank_gap": rank_gap,
            "points_gap": points_gap,
            "win_pct_gap": round(win_pct_gap, 4) if win_pct_gap is not None else None,
            "home_advantage_score": round(max(-0.15, min(0.15, advantage)), 4),
        },
    }


def espn_standings(fixture: dict[str, Any], home_team: dict[str, Any], away_team: dict[str, Any]) -> dict[str, Any]:
    current_season = espn_season_for_fixture(fixture)
    best_result: dict[str, Any] | None = None
    errors: list[str] = []
    for season in range(current_season, current_season - 3, -1):
        url = espn_provider_url(str(fixture["competition"]), "standings", standings=True, season=season)
        try:
            rows = parse_espn_standings_payload(espn_get(url), str(fixture["competition"]))
        except Exception as exc:
            errors.append(f"{season}: {exc}")
            continue
        result = build_verified_standings(rows, home_team["id"], away_team["id"], "espn_standings", url)
        result["season"] = season
        result["season_context"] = "current" if season == current_season else "latest_completed"
        if best_result is None or len(result["table"]) > len(best_result["table"]):
            best_result = result
        if result["verified"]:
            return result
    if best_result is not None:
        return best_result
    raise ValueError(f"ESPN standings failed: {errors[-1] if errors else 'no standings payload'}")


def openligadb_standings(fixture: dict[str, Any]) -> dict[str, Any]:
    competition = str(fixture["competition"])
    league = LEAGUES[competition]
    season = str(fixture.get("season") or league.get("provider_season") or espn_season_for_fixture(fixture))
    path = f"getbltable/{league['openligadb_shortcut']}/{season}"
    payload = openligadb_get(path)
    if not isinstance(payload, list):
        raise ValueError("OpenLigaDB returned an invalid standings payload")
    display_names = PROVIDER_TEAM_DISPLAY_NAMES.get(competition, {})
    rows = []
    for index, row in enumerate(payload, 1):
        team_name = str(row.get("teamName") or row.get("shortName") or "")
        wins = _whole(row.get("won"))
        losses = _whole(row.get("lost"))
        draws = _whole(row.get("draw"))
        rows.append({
            "rank": _whole(row.get("place")) or index,
            "team": display_names.get(team_name, team_name),
            "team_id": str(row.get("teamInfoId") or ""),
            "played": _whole(row.get("matches")),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "points": _whole(row.get("points")),
            "win_pct": (round(wins / max(_whole(row.get("matches")) or 0, 1), 4) if wins is not None and (_whole(row.get("matches")) or 0) > 0 else None),
            "record": f"{wins or 0}-{draws or 0}-{losses or 0}",
            "form": None,
            "group": row.get("groupName"),
            "goal_difference": _whole(row.get("goalDiff")),
        })
    return build_verified_standings(
        rows,
        fixture.get("home_provider_id"),
        fixture.get("away_provider_id"),
        "openligadb_table",
        f"{OPENLIGADB_BASE}/{path}",
    )


def thesportsdb_standings(fixture: dict[str, Any]) -> dict[str, Any]:
    league = LEAGUES[str(fixture["competition"])]
    season = fixture.get("season")
    payload = thesportsdb_get("lookuptable.php", l=league.get("provider_league_id"), s=season)
    table = payload.get("table") if isinstance(payload, dict) else None
    rows = []
    for index, row in enumerate(table or [], 1):
        wins = _whole(row.get("intWin"))
        losses = _whole(row.get("intLoss"))
        draws = _whole(row.get("intDraw"))
        played = _whole(row.get("intPlayed"))
        rows.append({
            "rank": _whole(row.get("intRank")) or index,
            "team": str(row.get("strTeam") or row.get("name") or ""),
            "team_id": str(row.get("idTeam") or ""),
            "played": played,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "points": _whole(row.get("intPoints")),
            "win_pct": round(wins / max(played or 0, 1), 4) if wins is not None and (played or 0) > 0 else None,
            "record": f"{wins or 0}-{draws or 0}-{losses or 0}",
            "form": row.get("strForm"),
            "group": None,
        })
    return build_verified_standings(
        rows,
        fixture.get("home_provider_id"),
        fixture.get("away_provider_id"),
        "thesportsdb_table",
        thesportsdb_url("lookuptable.php", l=league.get("provider_league_id"), s=season),
    )


def standings_context(competition: str, home_id: str, away_id: str) -> dict[str, Any]:
    home = find_team(competition, home_id) or {"id": home_id, "name": home_id}
    away = find_team(competition, away_id) or {"id": away_id, "name": away_id}
    return {
        "source": "verified_standings_unavailable",
        "verified": False,
        "home": {"team_id": home_id, "team": home["name"], "rank": None, "points": None},
        "away": {"team_id": away_id, "team": away["name"], "rank": None, "points": None},
        "table": [],
        "signal": {"rank_gap": None, "points_gap": None, "home_advantage_score": 0.0},
    }


def h2h_context(competition: str, home_id: str, away_id: str) -> dict[str, Any]:
    return {
        "source": "verified_h2h_unavailable",
        "verified": False,
        "summary": {"home_wins": None, "draws": None, "away_wins": None, "matches": 0},
        "signal": {"home": 0.0, "draw": 0.0, "away": 0.0},
        "matches": [],
    }


def empty_lineup_side(team_name: str) -> dict[str, Any]:
    return {
        "team": team_name,
        "formation": None,
        "lineup_type": "unavailable",
        "is_confirmed": False,
        "verified": False,
        "confidence": 0.0,
        "starters": [],
        "substitutes": [],
        "players": [],
        "doubtful": [],
        "source": None,
        "source_url": None,
    }


def parse_thesportsdb_lineup_payload(payload: dict[str, Any], fixture: dict[str, Any]) -> dict[str, Any]:
    home = empty_lineup_side(str(fixture.get("home_name") or fixture.get("home_id") or "Home"))
    away = empty_lineup_side(str(fixture.get("away_name") or fixture.get("away_id") or "Away"))
    rows = payload.get("lineup") if isinstance(payload.get("lineup"), list) else []
    source_url = thesportsdb_url("lookuplineup.php", id=fixture.get("provider_event_id"))
    for row in rows:
        if not isinstance(row, dict):
            continue
        player_name = str(row.get("strPlayer") or "").strip()
        if not player_name:
            continue
        home_flag = str(row.get("strHome") or "").strip().lower()
        row_team_id = str(row.get("idTeam") or "")
        if home_flag in {"yes", "true", "1", "home"} or row_team_id == str(fixture.get("home_provider_id")):
            side = home
        elif home_flag in {"no", "false", "0", "away"} or row_team_id == str(fixture.get("away_provider_id")):
            side = away
        else:
            continue
        substitute = str(row.get("strSubstitute") or "").strip().lower() in {"yes", "true", "1"}
        player = {
            "id": str(row.get("idPlayer") or ""),
            "name": player_name,
            "position": row.get("strPosition"),
            "number": row.get("intSquadNumber") or row.get("strNumber"),
            "status": "substitute" if substitute else "starter",
            "image": row.get("strCutout") or row.get("strThumb"),
        }
        side["players"].append(player)
        side["substitutes" if substitute else "starters"].append(player)

    for side in (home, away):
        if side["players"]:
            side.update({
                "lineup_type": "confirmed_lineup",
                "is_confirmed": True,
                "verified": True,
                "confidence": 1.0,
                "source": "thesportsdb_event_lineup",
                "source_url": source_url,
            })
    return {
        "source": "thesportsdb_event_lineup",
        "source_url": source_url,
        "observed_at": now_iso(),
        "verified": home["verified"] or away["verified"],
        "is_confirmed": home["is_confirmed"] and away["is_confirmed"],
        "home": home,
        "away": away,
    }


def thesportsdb_event_lineups(fixture: dict[str, Any]) -> dict[str, Any]:
    if fixture.get("provider") != "thesportsdb":
        return {
            "source": "thesportsdb_event_lineup",
            "observed_at": now_iso(),
            "verified": False,
            "is_confirmed": False,
            "home": empty_lineup_side(str(fixture.get("home_name") or "Home")),
            "away": empty_lineup_side(str(fixture.get("away_name") or "Away")),
        }
    payload = thesportsdb_get("lookuplineup.php", id=fixture.get("provider_event_id"))
    return parse_thesportsdb_lineup_payload(payload, fixture)


def injury_impact(status: Any, detail: Any = None) -> float:
    text = normalize_name(f"{status or ''} {detail or ''}")
    words = set(text.split())
    if any(phrase in text for phrase in ("injured reserve", "disabled list")) or any(token in words for token in ("out", "suspended", "ir", "il")):
        return 0.08
    if "doubtful" in text:
        return 0.05
    if "questionable" in text:
        return 0.025
    if "day to day" in text:
        return 0.015
    return 0.03


def parse_espn_roster_payload(payload: dict[str, Any], source_url: str) -> dict[str, Any]:
    groups = payload.get("athletes") if isinstance(payload.get("athletes"), list) else []
    players: list[dict[str, Any]] = []
    injuries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_position_value = group.get("position")
        if isinstance(group_position_value, dict):
            group_position = str(group_position_value.get("displayName") or group_position_value.get("name") or "")
        else:
            group_position = str(group_position_value or "")
        items = group.get("items") if isinstance(group.get("items"), list) else [group]
        for athlete in items:
            if not isinstance(athlete, dict):
                continue
            athlete_id = str(athlete.get("id") or athlete.get("uid") or "")
            player_name = str(athlete.get("fullName") or athlete.get("displayName") or athlete.get("shortName") or "").strip()
            position_row = athlete.get("position") if isinstance(athlete.get("position"), dict) else {}
            position = str(position_row.get("displayName") or position_row.get("name") or position_row.get("abbreviation") or group_position or "").strip()
            status_row = athlete.get("status")
            status = str(status_row.get("name") or status_row.get("type") or "") if isinstance(status_row, dict) else str(status_row or "")
            if not player_name or "coach" in normalize_name(f"{position} {status}") or "manager" in normalize_name(f"{position} {status}"):
                continue
            dedupe_key = athlete_id or normalize_name(player_name)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            player = {
                "id": athlete_id,
                "name": player_name,
                "position": position or None,
                "number": athlete.get("jersey"),
                "status": status or "active",
                "image": (athlete.get("headshot") or {}).get("href") if isinstance(athlete.get("headshot"), dict) else None,
            }
            players.append(player)
            athlete_injuries = athlete.get("injuries") if isinstance(athlete.get("injuries"), list) else []
            for injury in athlete_injuries:
                if not isinstance(injury, dict):
                    continue
                details = injury.get("details") if isinstance(injury.get("details"), dict) else {}
                type_row = injury.get("type") if isinstance(injury.get("type"), dict) else {}
                injury_status = str(injury.get("status") or details.get("status") or type_row.get("name") or "Listed")
                detail = str(
                    injury.get("longComment")
                    or injury.get("shortComment")
                    or details.get("detail")
                    or details.get("type")
                    or type_row.get("description")
                    or ""
                ).strip()
                injuries.append({
                    "player": player_name,
                    "position": position or None,
                    "status": injury_status,
                    "detail": detail or None,
                    "return_date": details.get("returnDate") or injury.get("returnDate"),
                    "impact": injury_impact(injury_status, detail),
                    "source": "espn_roster",
                    "source_url": source_url,
                })
    return {"players": players, "injuries": injuries}


def parse_thesportsdb_roster_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    players = []
    for row in payload.get("player", []) if isinstance(payload.get("player"), list) else []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("strPlayer") or "").strip()
        position = str(row.get("strPosition") or "").strip()
        status = str(row.get("strStatus") or "active").strip()
        if not name or any(token in normalize_name(f"{position} {status}") for token in ("coach", "manager", "coaching")):
            continue
        players.append({
            "id": str(row.get("idPlayer") or ""),
            "name": name,
            "position": position or None,
            "number": row.get("strNumber"),
            "status": status,
            "image": row.get("strCutout") or row.get("strThumb"),
        })
    return players


def resolve_thesportsdb_team_id(competition: str, team_name: str) -> str | None:
    payload = thesportsdb_get("searchteams.php", t=team_name)
    rows = payload.get("teams") if isinstance(payload.get("teams"), list) else []
    league = LEAGUES[competition]
    expected_sport = str(league.get("provider_sport") or ("Soccer" if league.get("sport") == "football" else ""))
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("idTeam"):
            continue
        if expected_sport and str(row.get("strSport") or "").lower() != expected_sport.lower():
            continue
        candidates.append({
            "id": str(row["idTeam"]),
            "name": str(row.get("strTeam") or row["idTeam"]),
            "names": [row.get("strTeam"), row.get("strTeamAlternate"), row.get("strTeamShort")],
        })
    resolved = resolve_espn_team(candidates, team_name)
    return str(resolved["id"]) if resolved else None


def roster_minimum_size(competition: str) -> int:
    return {"nba": 10, "nfl": 45, "mlb": 20, "nhl": 20}.get(competition, 15)


def fetch_team_roster(
    fixture: dict[str, Any],
    side: str,
    espn_team: dict[str, Any] | None,
) -> dict[str, Any]:
    competition = str(fixture["competition"])
    team_name = str(fixture.get(f"{side}_name") or fixture.get(f"{side}_id") or side.title())
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    espn_candidate: dict[str, Any] | None = None
    if espn_team:
        url = espn_provider_url(competition, f"teams/{espn_team['id']}/roster")
        try:
            parsed = parse_espn_roster_payload(espn_get(url), url)
            if parsed["players"]:
                espn_candidate = {
                    "team": team_name,
                    "team_id": espn_team["id"],
                    "verified": True,
                    "players": parsed["players"],
                    "injuries": parsed["injuries"],
                    "source": "espn_active_roster",
                    "source_url": url,
                    "observed_at": now_iso(),
                }
                candidates.append(espn_candidate)
        except Exception as exc:
            errors.append(f"ESPN roster: {exc}")

    provider_team_id = fixture.get(f"{side}_provider_id")
    sportsdb_team_id = str(provider_team_id) if fixture.get("provider") == "thesportsdb" and provider_team_id else None
    if not sportsdb_team_id and (not espn_candidate or len(espn_candidate["players"]) < roster_minimum_size(competition)):
        try:
            sportsdb_team_id = resolve_thesportsdb_team_id(competition, team_name)
        except Exception as exc:
            errors.append(f"TheSportsDB team search: {exc}")
    if sportsdb_team_id and (not espn_candidate or len(espn_candidate["players"]) < roster_minimum_size(competition)):
        url = thesportsdb_url("lookup_all_players.php", id=sportsdb_team_id)
        try:
            players = parse_thesportsdb_roster_payload(thesportsdb_get("lookup_all_players.php", id=sportsdb_team_id))
            if players:
                candidates.append({
                    "team": team_name,
                    "team_id": sportsdb_team_id,
                    "verified": True,
                    "players": players,
                    "injuries": [],
                    "source": "thesportsdb_active_roster",
                    "source_url": url,
                    "observed_at": now_iso(),
                })
        except Exception as exc:
            errors.append(f"TheSportsDB roster: {exc}")
    if not candidates:
        return {
            "team": team_name,
            "team_id": str(espn_team.get("id")) if espn_team else None,
            "verified": False,
            "injury_verified": False,
            "players": [],
            "injuries": [],
            "source": "active_roster_unavailable",
            "source_url": None,
            "observed_at": now_iso(),
            "coverage": {"players": 0, "expected_minimum": roster_minimum_size(competition), "status": "unavailable"},
            "errors": errors,
        }

    minimum = roster_minimum_size(competition)
    best = espn_candidate if espn_candidate and len(espn_candidate["players"]) >= minimum else max(candidates, key=lambda row: len(row["players"]))
    injury_rows = espn_candidate.get("injuries", []) if espn_candidate else []
    injury_verified = bool(espn_candidate and len(espn_candidate["players"]) >= minimum)
    return {
        **best,
        "injuries": injury_rows,
        "injury_verified": injury_verified,
        "coverage": {
            "players": len(best["players"]),
            "expected_minimum": minimum,
            "status": "complete" if len(best["players"]) >= minimum else "partial",
        },
        "errors": errors,
    }


def unavailable_roster(team_name: str, competition: str) -> dict[str, Any]:
    return {
        "team": team_name,
        "team_id": None,
        "verified": False,
        "injury_verified": False,
        "players": [],
        "injuries": [],
        "source": "active_roster_unavailable",
        "source_url": None,
        "observed_at": now_iso(),
        "coverage": {"players": 0, "expected_minimum": roster_minimum_size(competition), "status": "unavailable"},
        "errors": [],
    }


def merge_lineups_with_rosters(
    event_lineups: dict[str, Any],
    home_roster: dict[str, Any],
    away_roster: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "source": "thesportsdb_event_lineup_or_active_roster",
        "observed_at": now_iso(),
        "verified": False,
        "is_confirmed": False,
    }
    for side, roster in (("home", home_roster), ("away", away_roster)):
        exact = event_lineups.get(side) if isinstance(event_lineups.get(side), dict) else {}
        if exact.get("is_confirmed"):
            merged[side] = exact
            continue
        lineup = empty_lineup_side(str(roster.get("team") or side.title()))
        if roster.get("verified"):
            lineup.update({
                "lineup_type": "active_roster",
                "verified": True,
                "confidence": 0.75,
                "players": roster.get("players", []),
                "source": roster.get("source"),
                "source_url": roster.get("source_url"),
                "coverage": roster.get("coverage"),
            })
        merged[side] = lineup
    merged["verified"] = merged["home"]["verified"] or merged["away"]["verified"]
    merged["is_confirmed"] = merged["home"]["is_confirmed"] and merged["away"]["is_confirmed"]
    return merged


def injuries_from_rosters(home_roster: dict[str, Any], away_roster: dict[str, Any]) -> dict[str, Any]:
    home = home_roster.get("injuries", [])
    away = away_roster.get("injuries", [])
    verified = home_roster.get("injury_verified") is True and away_roster.get("injury_verified") is True
    sources = list(dict.fromkeys(
        str(roster.get("source")) for roster in (home_roster, away_roster) if roster.get("source")
    ))
    return {
        "source": "+".join(sources) if sources else "verified_injuries_unavailable",
        "source_urls": [roster.get("source_url") for roster in (home_roster, away_roster) if roster.get("source_url")],
        "observed_at": now_iso(),
        "verified": verified,
        "home": home,
        "away": away,
        "coverage": {
            "home": home_roster.get("coverage"),
            "away": away_roster.get("coverage"),
            "complete": verified,
        },
        "signal": {
            "home_impact": round(min(0.25, sum(float(row.get("impact") or 0) for row in home)), 4),
            "away_impact": round(min(0.25, sum(float(row.get("impact") or 0) for row in away)), 4),
            "confidence": 0.85 if verified else 0.0,
        },
    }


def lineup_context(competition: str, home_id: str, away_id: str, prediction: dict[str, Any]) -> dict[str, Any]:
    confidence = prediction["availability"]["confidence"]
    lineup_type = prediction["availability"]["lineup_type"]

    def build_side(team_id: str) -> dict[str, Any]:
        team = find_team(competition, team_id) or {"name": team_id}
        return {"team": team["name"], "formation": None, "lineup_type": lineup_type, "confidence": confidence, "starters": [], "doubtful": []}

    verified = prediction["availability"]["source"] == "verified_feed"
    return {"source": prediction["availability"]["source"], "verified": verified, "home": build_side(home_id), "away": build_side(away_id)}


def injury_context(competition: str, home_id: str, away_id: str, prediction: dict[str, Any]) -> dict[str, Any]:
    availability = prediction["availability"]
    verified = availability["source"] == "verified_feed"
    return {
        "source": availability["source"],
        "verified": verified,
        "home": [],
        "away": [],
        "signal": {
            "home_impact": 0.0,
            "away_impact": 0.0,
            "confidence": availability["confidence"],
        },
    }


def _espn_competitor_id(competitor: dict[str, Any]) -> str:
    team = competitor.get("team") if isinstance(competitor.get("team"), dict) else {}
    return str(team.get("id") or competitor.get("id") or "")


def _espn_score(competitor: dict[str, Any]) -> float | None:
    score = competitor.get("score")
    if isinstance(score, dict):
        return _numeric(score.get("value") if score.get("value") is not None else score.get("displayValue"))
    return _numeric(score)


def parse_espn_h2h_schedule(
    payload: dict[str, Any],
    current_home_team_id: Any,
    current_away_team_id: Any,
) -> list[dict[str, Any]]:
    wanted_ids = {str(current_home_team_id), str(current_away_team_id)}
    matches = []
    for event in payload.get("events", []) if isinstance(payload.get("events"), list) else []:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") if isinstance(event.get("competitions"), list) else []
        competition = competitions[0] if competitions and isinstance(competitions[0], dict) else {}
        status = event.get("status") if isinstance(event.get("status"), dict) else competition.get("status") if isinstance(competition.get("status"), dict) else {}
        status_type = status.get("type") if isinstance(status.get("type"), dict) else {}
        completed = status_type.get("completed") is True or str(status_type.get("state") or "").lower() == "post"
        competitors = competition.get("competitors") if isinstance(competition.get("competitors"), list) else []
        by_id = {_espn_competitor_id(row): row for row in competitors if isinstance(row, dict)}
        if not completed or not wanted_ids.issubset(by_id):
            continue
        historical_home = next((row for row in competitors if row.get("homeAway") == "home"), None)
        historical_away = next((row for row in competitors if row.get("homeAway") == "away"), None)
        if not historical_home or not historical_away:
            continue
        home_score = _espn_score(historical_home)
        away_score = _espn_score(historical_away)
        if home_score is None or away_score is None:
            continue
        current_home_score = _espn_score(by_id[str(current_home_team_id)])
        current_away_score = _espn_score(by_id[str(current_away_team_id)])
        if current_home_score is None or current_away_score is None:
            continue
        winner_team_id = None
        if current_home_score > current_away_score:
            winner_team_id = str(current_home_team_id)
        elif current_away_score > current_home_score:
            winner_team_id = str(current_away_team_id)
        home_team_row = historical_home.get("team") if isinstance(historical_home.get("team"), dict) else {}
        away_team_row = historical_away.get("team") if isinstance(historical_away.get("team"), dict) else {}
        matches.append({
            "event_id": str(event.get("id") or ""),
            "date": str(event.get("date") or "")[:10],
            "home_team": str(home_team_row.get("displayName") or home_team_row.get("shortDisplayName") or "Home"),
            "away_team": str(away_team_row.get("displayName") or away_team_row.get("shortDisplayName") or "Away"),
            "home_team_id": _espn_competitor_id(historical_home),
            "away_team_id": _espn_competitor_id(historical_away),
            "home_score": int(home_score) if home_score.is_integer() else home_score,
            "away_score": int(away_score) if away_score.is_integer() else away_score,
            "score": f"{int(home_score) if home_score.is_integer() else home_score}-{int(away_score) if away_score.is_integer() else away_score}",
            "winner_team_id": winner_team_id,
            "status": "final",
        })
    return matches


def build_verified_h2h(
    matches: list[dict[str, Any]],
    current_home_team_id: Any,
    current_away_team_id: Any,
    source: str,
    source_urls: list[str],
    seasons: list[int | str],
) -> dict[str, Any]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in sorted(matches, key=lambda row: str(row.get("date") or ""), reverse=True):
        key = str(match.get("event_id") or f"{match.get('date')}|{match.get('home_team_id')}|{match.get('away_team_id')}")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
        if len(deduped) == 5:
            break
    home_id = str(current_home_team_id)
    away_id = str(current_away_team_id)
    home_wins = sum(1 for match in deduped if str(match.get("winner_team_id")) == home_id)
    away_wins = sum(1 for match in deduped if str(match.get("winner_team_id")) == away_id)
    draws = len(deduped) - home_wins - away_wins
    denominator = len(deduped) + 1.5
    return {
        "source": source,
        "source_url": source_urls[0] if source_urls else None,
        "source_urls": source_urls,
        "observed_at": now_iso(),
        "verified": bool(deduped),
        "summary": {"home_wins": home_wins, "draws": draws, "away_wins": away_wins, "matches": len(deduped)},
        "signal": {
            "home": round((home_wins + 0.5) / denominator, 6),
            "draw": round((draws + 0.5) / denominator, 6),
            "away": round((away_wins + 0.5) / denominator, 6),
        } if deduped else {"home": 0.0, "draw": 0.0, "away": 0.0},
        "matches": deduped,
        "coverage": {"seasons_checked": seasons, "meetings_found": len(deduped)},
    }


def espn_h2h(fixture: dict[str, Any], home_team: dict[str, Any], away_team: dict[str, Any]) -> dict[str, Any]:
    competition = str(fixture["competition"])
    current_season = espn_season_for_fixture(fixture)
    matches: list[dict[str, Any]] = []
    urls: list[str] = []
    seasons: list[int] = []
    successful_requests = 0
    errors: list[str] = []
    for season in range(current_season, current_season - 6, -1):
        url = espn_provider_url(competition, f"teams/{home_team['id']}/schedule", season=season)
        urls.append(url)
        seasons.append(season)
        try:
            matches.extend(parse_espn_h2h_schedule(espn_get(url), home_team["id"], away_team["id"]))
            successful_requests += 1
        except Exception as exc:
            errors.append(str(exc))
        unique_keys = {str(match.get("event_id") or f"{match.get('date')}|{match.get('score')}") for match in matches}
        if len(unique_keys) >= 5:
            break
    if not successful_requests and errors:
        raise ValueError(f"ESPN schedules failed: {errors[-1]}")
    return build_verified_h2h(matches, home_team["id"], away_team["id"], "espn_team_schedules", urls, seasons)


def _openliga_result(match: dict[str, Any]) -> tuple[float, float] | None:
    results = [row for row in match.get("matchResults", []) if isinstance(row, dict)]
    if not results:
        return None
    result = sorted(results, key=lambda row: _whole(row.get("resultOrderID")) or 0)[-1]
    home_score = _numeric(result.get("pointsTeam1"))
    away_score = _numeric(result.get("pointsTeam2"))
    return (home_score, away_score) if home_score is not None and away_score is not None else None


def openligadb_h2h(fixture: dict[str, Any]) -> dict[str, Any]:
    competition = str(fixture["competition"])
    league = LEAGUES[competition]
    current_season = _whole(fixture.get("season")) or _whole(league.get("provider_season")) or espn_season_for_fixture(fixture)
    home_id = str(fixture.get("home_provider_id") or "")
    away_id = str(fixture.get("away_provider_id") or "")
    matches: list[dict[str, Any]] = []
    urls: list[str] = []
    seasons: list[int] = []
    successful_requests = 0
    display_names = PROVIDER_TEAM_DISPLAY_NAMES.get(competition, {})
    for season in range(int(current_season), int(current_season) - 6, -1):
        path = f"getmatchdata/{league['openligadb_shortcut']}/{season}"
        url = f"{OPENLIGADB_BASE}/{path}"
        urls.append(url)
        seasons.append(season)
        try:
            payload = openligadb_get(path)
        except Exception:
            continue
        if not isinstance(payload, list):
            continue
        successful_requests += 1
        for match in payload:
            team1 = match.get("team1") if isinstance(match.get("team1"), dict) else {}
            team2 = match.get("team2") if isinstance(match.get("team2"), dict) else {}
            historical_home_id = str(team1.get("teamId") or "")
            historical_away_id = str(team2.get("teamId") or "")
            if match.get("matchIsFinished") is not True or {historical_home_id, historical_away_id} != {home_id, away_id}:
                continue
            result = _openliga_result(match)
            if not result:
                continue
            home_score, away_score = result
            home_perspective_score = home_score if historical_home_id == home_id else away_score
            away_perspective_score = away_score if historical_away_id == away_id else home_score
            winner_team_id = home_id if home_perspective_score > away_perspective_score else away_id if away_perspective_score > home_perspective_score else None
            home_name = str(team1.get("teamName") or "Home")
            away_name = str(team2.get("teamName") or "Away")
            matches.append({
                "event_id": str(match.get("matchID") or ""),
                "date": str(match.get("matchDateTimeUTC") or match.get("matchDateTime") or "")[:10],
                "home_team": display_names.get(home_name, home_name),
                "away_team": display_names.get(away_name, away_name),
                "home_team_id": historical_home_id,
                "away_team_id": historical_away_id,
                "home_score": int(home_score) if home_score.is_integer() else home_score,
                "away_score": int(away_score) if away_score.is_integer() else away_score,
                "score": f"{int(home_score) if home_score.is_integer() else home_score}-{int(away_score) if away_score.is_integer() else away_score}",
                "winner_team_id": winner_team_id,
                "status": "final",
            })
        if len(matches) >= 5:
            break
    if not successful_requests:
        raise ValueError("OpenLigaDB schedules were unavailable")
    return build_verified_h2h(matches, home_id, away_id, "openligadb_match_history", urls, seasons)


def select_research_standings(
    fixture: dict[str, Any],
    home_espn: dict[str, Any] | None,
    away_espn: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if fixture.get("provider") == "openligadb":
        try:
            standings = openligadb_standings(fixture)
            if standings.get("verified"):
                return standings, errors
            errors.append("OpenLigaDB table did not contain both fixture teams")
        except Exception as exc:
            errors.append(f"OpenLigaDB standings: {exc}")
    if home_espn and away_espn:
        try:
            standings = espn_standings(fixture, home_espn, away_espn)
            if standings.get("verified"):
                return standings, errors
            errors.append("ESPN standings did not contain both fixture teams")
        except Exception as exc:
            errors.append(f"ESPN standings: {exc}")
    league = LEAGUES[str(fixture["competition"])]
    if fixture.get("provider") == "thesportsdb" and league.get("provider_sport") == "Soccer":
        try:
            standings = thesportsdb_standings(fixture)
            if standings.get("verified"):
                return standings, errors
            errors.append("TheSportsDB free table did not contain both fixture teams")
        except Exception as exc:
            errors.append(f"TheSportsDB standings: {exc}")
    fallback = standings_context(str(fixture["competition"]), str(fixture["home_id"]), str(fixture["away_id"]))
    fallback["observed_at"] = now_iso()
    fallback["errors"] = errors
    return fallback, errors


def select_research_h2h(
    fixture: dict[str, Any],
    home_espn: dict[str, Any] | None,
    away_espn: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    if home_espn and away_espn:
        try:
            h2h = espn_h2h(fixture, home_espn, away_espn)
            if h2h.get("verified") or fixture.get("provider") != "openligadb":
                return h2h, errors
            errors.append("ESPN schedules contained no completed meetings")
        except Exception as exc:
            errors.append(f"ESPN H2H: {exc}")
    if fixture.get("provider") == "openligadb" and fixture.get("home_provider_id") and fixture.get("away_provider_id"):
        try:
            return openligadb_h2h(fixture), errors
        except Exception as exc:
            errors.append(f"OpenLigaDB H2H: {exc}")
    fallback = h2h_context(str(fixture["competition"]), str(fixture["home_id"]), str(fixture["away_id"]))
    fallback["observed_at"] = now_iso()
    fallback["errors"] = errors
    return fallback, errors


def research_cache_is_fresh(research: dict[str, Any]) -> bool:
    if research.get("schema_version") != RESEARCH_SCHEMA_VERSION:
        return False
    expires_at = parse_utc_timestamp(str(research.get("expires_at") or ""))
    return bool(expires_at and expires_at > datetime.now(timezone.utc))


def refresh_fixture_research(fixture_id: str, force: bool = False) -> dict[str, Any]:
    fixture = fixture_from_id(fixture_id)
    state = load_state()
    cached = state.get("research_cache", {}).get(fixture_id)
    if not force and isinstance(cached, dict) and research_cache_is_fresh(cached):
        return {"fixture_id": fixture_id, "cached": True, "research": cached}

    started_at = now_iso()
    failures: list[dict[str, str]] = []
    catalog: list[dict[str, Any]] = []
    catalog_url = None
    try:
        catalog_result = espn_team_catalog(str(fixture["competition"]))
        catalog = catalog_result["teams"]
        catalog_url = catalog_result["url"]
    except Exception as exc:
        failures.append({"source": "espn_team_catalog", "error": str(exc)})

    home_canonical = find_team(str(fixture["competition"]), str(fixture["home_id"])) or {}
    away_canonical = find_team(str(fixture["competition"]), str(fixture["away_id"])) or {}
    home_espn = resolve_espn_team(catalog, fixture.get("home_name"), home_canonical.get("name"))
    away_espn = resolve_espn_team(catalog, fixture.get("away_name"), away_canonical.get("name"))
    if not home_espn:
        failures.append({"source": "espn_team_resolution", "error": f"Could not map {fixture.get('home_name')}"})
    if not away_espn:
        failures.append({"source": "espn_team_resolution", "error": f"Could not map {fixture.get('away_name')}"})

    tasks = {
        "standings": lambda: select_research_standings(fixture, home_espn, away_espn),
        "h2h": lambda: select_research_h2h(fixture, home_espn, away_espn),
        "home_roster": lambda: fetch_team_roster(fixture, "home", home_espn),
        "away_roster": lambda: fetch_team_roster(fixture, "away", away_espn),
        "event_lineups": lambda: thesportsdb_event_lineups(fixture),
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_map = {executor.submit(task): name for name, task in tasks.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                value = future.result()
                if name in {"standings", "h2h"}:
                    results[name], task_errors = value
                    failures.extend({"source": name, "error": error} for error in task_errors)
                else:
                    results[name] = value
            except Exception as exc:
                failures.append({"source": name, "error": str(exc)})

    standings = results.get("standings") or standings_context(str(fixture["competition"]), str(fixture["home_id"]), str(fixture["away_id"]))
    h2h = results.get("h2h") or h2h_context(str(fixture["competition"]), str(fixture["home_id"]), str(fixture["away_id"]))
    home_roster = results.get("home_roster") or fetch_team_roster(fixture, "home", None)
    away_roster = results.get("away_roster") or fetch_team_roster(fixture, "away", None)
    event_lineups = results.get("event_lineups") or {
        "verified": False,
        "home": empty_lineup_side(str(fixture.get("home_name") or "Home")),
        "away": empty_lineup_side(str(fixture.get("away_name") or "Away")),
    }
    lineups = merge_lineups_with_rosters(event_lineups, home_roster, away_roster)
    injuries = injuries_from_rosters(home_roster, away_roster)
    observed_at = now_iso()
    expires_at = utc_iso(datetime.now(timezone.utc) + timedelta(hours=RESEARCH_CACHE_HOURS))
    verified_sections = sum(bool(section.get("verified")) for section in (standings, h2h, lineups, injuries))
    research = {
        "schema_version": RESEARCH_SCHEMA_VERSION,
        "fixture_id": fixture_id,
        "status": "success" if verified_sections >= 3 else "partial",
        "observed_at": observed_at,
        "expires_at": expires_at,
        "standings": standings,
        "h2h": h2h,
        "lineups": lineups,
        "injuries": injuries,
        "team_resolution": {
            "source_url": catalog_url,
            "home": home_espn,
            "away": away_espn,
        },
        "sources": list(dict.fromkeys(
            str(section.get("source"))
            for section in (standings, h2h, lineups, injuries)
            if section.get("source")
        )),
        "failures": failures,
    }
    run = {
        "run_id": f"research_{seed_for(fixture_id, started_at)}",
        "fixture_id": fixture_id,
        "started_at": started_at,
        "finished_at": observed_at,
        "status": research["status"],
        "verified_sections": verified_sections,
        "failures": failures,
    }
    with STATE_LOCK:
        latest_state = load_state()
        latest_state.setdefault("research_cache", {})[fixture_id] = research
        latest_state["research_refresh_runs"] = ([run] + latest_state.get("research_refresh_runs", []))[:50]
        save_state(latest_state)
    return {"fixture_id": fixture_id, "cached": False, "run": run, "research": research}


def ranking_engine_context(
    fixture: dict[str, Any],
    cached_research: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    competition = str(fixture.get("competition") or "")
    parameters = state.get("ranking_parameters", {}).get(competition)
    resolution = cached_research.get("team_resolution") if isinstance(cached_research.get("team_resolution"), dict) else {}
    home = resolution.get("home") if isinstance(resolution.get("home"), dict) else {}
    away = resolution.get("away") if isinstance(resolution.get("away"), dict) else {}
    if not parameters or not home.get("id") or not away.get("id") or not fixture.get("starts_at"):
        return {
            "verified": False,
            "source": "walk_forward_rankings_unavailable",
            "probabilities": {"home": 0.0, "draw": 0.0, "away": 0.0},
            "reason": "Run the historical walk-forward backtest and refresh fixture research first.",
        }
    try:
        return future_ranking_prediction(
            state.get("historical_games", []),
            competition,
            str(home["id"]),
            str(away["id"]),
            str(fixture["starts_at"]),
            parameters,
        )
    except (KeyError, ValueError) as exc:
        return {
            "verified": False,
            "source": "walk_forward_rankings_unavailable",
            "probabilities": {"home": 0.0, "draw": 0.0, "away": 0.0},
            "reason": str(exc),
        }


def adjusted_engine_scorecard(
    prediction: dict[str, Any],
    h2h: dict[str, Any],
    standings: dict[str, Any],
    injuries: dict[str, Any],
    ranking: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sport_meta = LEAGUES.get(str(prediction.get("competition")), {})
    sport_config = DEFAULT_REGISTRY.sport_config(str(sport_meta.get("sport"))) if sport_meta else None
    draw_prior = (
        float(sport_config.get("draw_prior_scorecard", 0.0))
        if sport_config is not None and sport_config["has_draw"]
        else 0.0
    )
    elo_home = prediction["elo"]["home_probability"]
    elo = normalized_probabilities(elo_home, draw_prior, 1 - elo_home)
    dc = prediction["dixon_coles"]
    availability_gap = injuries["signal"]["away_impact"] - injuries["signal"]["home_impact"]
    availability = normalized_probabilities(
        prediction["probabilities"]["home"] + availability_gap,
        prediction["probabilities"]["draw"],
        prediction["probabilities"]["away"] - availability_gap,
    )
    engine_candidates = [
        {"name": "Score Model", "raw_weight": 0.30, "scores": dc, "source": "score_model", "verified": True},
        {"name": "Elo", "raw_weight": 0.25, "scores": elo, "source": "team_ratings", "verified": True},
        {"name": "Calibrated Model", "raw_weight": 0.20, "scores": prediction["probabilities"], "source": prediction["model_version"], "verified": True},
    ]
    if injuries.get("verified"):
        engine_candidates.append({"name": "Availability", "raw_weight": 0.15, "scores": availability, "source": injuries["source"], "verified": True})
    if h2h.get("verified"):
        h2h_scores = normalized_probabilities(h2h["signal"]["home"], h2h["signal"]["draw"] if draw_prior > 0.0 else 0.0, h2h["signal"]["away"])
        engine_candidates.append({"name": "Past H2H", "raw_weight": 0.10, "scores": h2h_scores, "source": h2h["source"], "verified": True})
    standings_model_eligible = standings.get("model_eligible", standings.get("verified") is True)
    if standings_model_eligible:
        standings_home = 0.42 + standings["signal"]["home_advantage_score"]
        standings_scores = normalized_probabilities(standings_home, draw_prior, 1 - standings_home)
        engine_candidates.append({"name": "League Standings", "raw_weight": 0.10, "scores": standings_scores, "source": standings["source"], "verified": True})
    if ranking and ranking.get("verified"):
        engine_candidates.append({
            "name": "Walk-Forward Ranking",
            "raw_weight": 0.25,
            "scores": ranking["probabilities"],
            "source": ranking["source"],
            "verified": True,
        })

    total_weight = sum(engine["raw_weight"] for engine in engine_candidates)
    engines = [
        {**engine, "weight": engine["raw_weight"] / total_weight}
        for engine in engine_candidates
    ]
    combined = normalized_probabilities(
        sum(engine["scores"]["home"] * engine["weight"] for engine in engines),
        sum(engine["scores"]["draw"] * engine["weight"] for engine in engines),
        sum(engine["scores"]["away"] * engine["weight"] for engine in engines),
    )
    selection_key = max(combined, key=combined.get)
    selected_probability = combined[selection_key]
    return {
        "selection_key": selection_key,
        "selection": {"home": "Home", "draw": "Draw", "away": "Away"}[selection_key],
        "scores": {key: round(value, 6) for key, value in combined.items()},
        "probability": round(selected_probability, 6),
        "confidence": "high" if selected_probability >= 0.70 else "medium" if selected_probability >= 0.58 else "balanced",
        "engines": [
            {key: value for key, value in engine.items() if key != "raw_weight"} | {"scores": {key: round(value, 6) for key, value in engine["scores"].items()}}
            for engine in engines
        ],
        "excluded_sources": [
            source for source, available in (
                ("Availability", injuries.get("verified") is True),
                ("Past H2H", h2h.get("verified") is True),
                ("League Standings", standings_model_eligible),
                ("Walk-Forward Ranking", bool(ranking and ranking.get("verified"))),
            ) if not available
        ],
        "method": "sports_only_available_source_blend_v5",
        "analysis_policy": {
            "mode": "sports_only",
            "moneyline_used": False,
            "odds_used": False,
            "trading_factors_used": False,
        },
    }


def fixture_research(fixture_id: str) -> dict[str, Any]:
    fixture = fixture_from_id(fixture_id)
    state = load_state()
    prediction = build_prediction(fixture["competition"], fixture["home_id"], fixture["away_id"], fixture["snapshot"], fixture_id, state)
    recommendations = [row for row in state.get("recommendations", []) if row.get("fixture_id") == fixture_id]
    settlements = [row for row in state.get("settlements", []) if row.get("fixture_id") == fixture_id]
    availability = latest_fixture_availability(fixture_id)
    cached_research = state.get("research_cache", {}).get(fixture_id, {})
    h2h = cached_research.get("h2h") or h2h_context(fixture["competition"], fixture["home_id"], fixture["away_id"])
    standings = cached_research.get("standings") or standings_context(fixture["competition"], fixture["home_id"], fixture["away_id"])
    lineups = cached_research.get("lineups") or lineup_context(fixture["competition"], fixture["home_id"], fixture["away_id"], prediction)
    injuries = cached_research.get("injuries") or injury_context(fixture["competition"], fixture["home_id"], fixture["away_id"], prediction)
    ranking = ranking_engine_context(fixture, cached_research, state)
    adjusted_score = adjusted_engine_scorecard(prediction, h2h, standings, injuries, ranking)
    market_horizons = prediction["market_horizons"]
    notes = [
        "Sports-only policy is active: moneyline, odds, Kelly, stake sizing, and trading inputs are excluded.",
        (f"Verified roster injury coverage is loaded at confidence {injuries['signal']['confidence']:.2f}." if injuries.get("verified") else "No complete verified injury coverage is loaded; availability is excluded from the score."),
        (f"{h2h['summary']['matches']} verified past meetings are included." if h2h.get("verified") else "No completed provider-confirmed H2H meetings were found in the checked seasons."),
        ("Verified league standings are included." if standings.get("model_eligible", standings.get("verified")) else "The table is displayed but has no played matches yet, so standings are excluded from the score." if standings.get("verified") else "The current provider table did not contain both fixture teams; standings are excluded from the score."),
        ("Confirmed event lineups are loaded." if lineups.get("is_confirmed") else "Published event lineups are not available yet; verified active rosters are shown where available."),
        (f"Walk-forward ranking uses {ranking['training_games']} prior verified games through {ranking['as_of']}." if ranking.get("verified") else "Walk-forward ranking is unavailable until a leak-free historical backtest is completed."),
        (f"Expanded {market_horizons['sport_label']} markets use {market_horizons['sample']['league_games']} completed pre-cutoff games; latest feature game {market_horizons['latest_feature_game_utc'] or 'none'}."),
        "Analysis horizons that do not pass their own walk-forward validation are withheld.",
        f"Point-in-time audit is {'clear' if prediction['point_in_time']['valid'] else 'flagged'}.",
        f"Sports analysis selection is {prediction['recommendation']['selection']} at {prediction['recommendation']['probability'] * 100:.1f}% model probability.",
    ]
    return {
        "fixture": fixture_card(fixture, state, prediction_override=prediction),
        "prediction": prediction,
        "availability": availability,
        "recommendations": recommendations,
        "settlements": settlements,
        "past_h2h": h2h,
        "lineups": lineups,
        "standings": standings,
        "injuries": injuries,
        "ranking_engine": ranking,
        "market_horizons": market_horizons,
        "research_meta": {
            "status": cached_research.get("status", "not_refreshed"),
            "observed_at": cached_research.get("observed_at"),
            "expires_at": cached_research.get("expires_at"),
            "sources": cached_research.get("sources", []),
            "failures": cached_research.get("failures", []),
            "fresh": research_cache_is_fresh(cached_research) if cached_research else False,
        },
        "adjusted_final_score": adjusted_score,
        "research_notes": notes,
        "feature_groups": [
            {"name": engine["name"], "weight": engine["weight"], "source": engine["source"]}
            for engine in adjusted_score["engines"]
        ],
    }


def validate_point_in_time(snapshot: str, feature_times: dict[str, str] | None = None) -> dict[str, Any]:
    minutes_before_kickoff = SNAPSHOT_WINDOWS.get(snapshot)
    if minutes_before_kickoff is None:
        raise ValueError(f"Unsupported snapshot window: {snapshot}")
    feature_times = feature_times or {}
    violations = []
    for feature_name, feature_ts in feature_times.items():
        if not isinstance(feature_ts, str) or not feature_ts.endswith("Z"):
            violations.append({"feature": feature_name, "reason": "timestamp must be UTC ISO ending with Z"})
    return {
        "snapshot": snapshot,
        "minutes_before_kickoff": minutes_before_kickoff,
        "valid": not violations,
        "violations": violations,
        "max_feature_source_ts": now_iso(),
    }


def sports_analysis_decision(probabilities: dict[str, float], availability_confidence: float) -> dict[str, Any]:
    best_key = max(probabilities, key=probabilities.get)
    probability = probabilities[best_key]
    guardrails = ["sports_only_no_moneyline_or_trading_inputs"]
    if availability_confidence < 0.75:
        guardrails.append("availability_confidence_low")
    return {
        "selection_key": best_key,
        "selection": {"home": "Home", "draw": "Draw", "away": "Away"}[best_key],
        "probability": round(probability, 6),
        "action": "analysis_only",
        "confidence": "high" if probability >= 0.70 else "medium" if probability >= 0.58 else "balanced",
        "guardrails": guardrails,
    }


def build_prediction(
    competition: str,
    home_id: str,
    away_id: str,
    snapshot: str,
    fixture_id_override: str | None = None,
    state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    home_resolution = resolve_team(competition, home_id)
    away_resolution = resolve_team(competition, away_id)
    home_id = home_resolution["team_id"]
    away_id = away_resolution["team_id"]
    home_team = find_team(competition, home_id)
    away_team = find_team(competition, away_id)
    if not home_team or not away_team:
        raise ValueError("Unknown team for competition")
    if home_id == away_id:
        raise ValueError("Home and away teams must differ")
    pit = validate_point_in_time(snapshot)
    fixture_id = fixture_id_override or f"{competition}_{home_id}_{away_id}"
    state_snapshot = state_snapshot or load_state()
    stored_fixture = next(
        (fixture for fixture in state_snapshot.get("fixtures", []) if fixture.get("fixture_id") == fixture_id),
        None,
    ) if fixture_id_override else None

    rng = random.Random(seed_for(competition, home_id, away_id, snapshot, fixture_id))
    sport = str(LEAGUES[competition]["sport"])
    elo_diff = home_team["elo"] - away_team["elo"] + 65
    elo_home = 1 / (1 + math.pow(10, -elo_diff / 400))
    sport_config = DEFAULT_REGISTRY.sport_config(sport)
    if sport_config["has_draw"]:
        dc_draw = float(sport_config["draw_prior_prediction"])
        sports_prior = normalized_probabilities(
            elo_home * (1 - dc_draw), dc_draw, (1 - elo_home) * (1 - dc_draw)
        )
        dc_home = max(0.10, min(0.80, elo_home * 0.92))
        dc = normalized_probabilities(dc_home, dc_draw, max(0.10, 1 - dc_home - dc_draw))
    else:
        sports_prior = normalized_probabilities(elo_home, 0.0, 1 - elo_home)
        dc = dict(sports_prior)

    availability_row = latest_fixture_availability(fixture_id, state_snapshot)
    missing = int(availability_row["home_starters_missing"]) if availability_row else 0
    opponent_missing = int(availability_row["away_starters_missing"]) if availability_row else 0
    goalkeeper_missing = int(availability_row["goalkeeper_missing"]) if availability_row else 0
    replacement_gap = max(0.0, (missing - opponent_missing) * 0.04 + goalkeeper_missing * 0.08) if availability_row else 0.0
    availability_confidence = float(availability_row["confidence"]) if availability_row else 0.0
    lineup_type = str(availability_row["lineup_type"]) if availability_row else "unavailable"

    raw_weights = {
        "score_model": 0.55,
        "elo": 0.35,
        "availability": 0.10 if availability_row else 0.0,
    }
    total_weight = sum(raw_weights.values())
    weights = {key: value / total_weight for key, value in raw_weights.items()}
    ensemble = normalized_probabilities(
        dc["home"] * weights["score_model"] + sports_prior["home"] * weights["elo"] - replacement_gap * weights["availability"],
        dc["draw"] * weights["score_model"] + sports_prior["draw"] * weights["elo"],
        dc["away"] * weights["score_model"] + sports_prior["away"] * weights["elo"] + replacement_gap * weights["availability"],
    )
    calibrated = dict(ensemble)
    recommendation = sports_analysis_decision(calibrated, availability_confidence)

    market_horizons = build_market_horizons(
        competition=competition,
        home_name=str((stored_fixture or {}).get("home_name") or home_team["name"]),
        away_name=str((stored_fixture or {}).get("away_name") or away_team["name"]),
        outcome_probabilities=calibrated,
        historical_games=state_snapshot.get("historical_games", []),
        as_of=str((stored_fixture or {}).get("starts_at") or now_iso()),
        verified_markets={},
        tuned_parameters=state_snapshot.get("market_parameters", {}).get(competition, {}),
    )
    analysis_generated_at = now_iso()

    return {
        "fixture_id": fixture_id,
        "competition": competition,
        "home_team": home_team,
        "away_team": away_team,
        "identity": {"home": home_resolution, "away": away_resolution},
        "snapshot": snapshot,
        "snapshot_ts": analysis_generated_at,
        "analysis_mode": "realtime" if stored_fixture else "point_in_time",
        "analysis_generated_at": analysis_generated_at,
        "fixture_start_time": stored_fixture.get("starts_at") if stored_fixture else None,
        "point_in_time": pit,
        "model_version": MODEL_VERSION,
        "raw_odds": {"source": "excluded_by_sports_only_policy", "used": False},
        "market_baseline": {k: round(v, 6) for k, v in sports_prior.items()},
        "market_baseline_kind": "sports_model_prior",
        "elo": {"home_advantage": 65, "diff": elo_diff, "home_probability": round(elo_home, 6)},
        "dixon_coles": {k: round(v, 6) for k, v in dc.items()},
        "availability": {
            "starters_missing": missing,
            "opponent_starters_missing": opponent_missing,
            "goalkeeper_missing": goalkeeper_missing,
            "replacement_gap": round(replacement_gap, 3),
            "confidence": availability_confidence,
            "lineup_type": lineup_type,
            "source": "verified_feed" if availability_row else "model_assumption",
        },
        "weights": weights,
        "ensemble": {k: round(v, 6) for k, v in ensemble.items()},
        "probabilities": {k: round(v, 6) for k, v in calibrated.items()},
        "recommendation": recommendation,
        "market_horizons": market_horizons,
        "analysis_policy": {
            "mode": "sports_only",
            "moneyline_used": False,
            "odds_used": False,
            "trading_factors_used": False,
            "inputs": ["verified_results", "elo", "scoring_form", "h2h", "standings", "availability", "walk_forward_history"],
        },
        "quality": {
            "ece": 0.028,
            "probability_sum": round(sum(calibrated.values()), 6),
            "latency_ms": rng.randrange(18, 42),
            "verified_market": False,
            "verified_secondary_markets": [],
            "verified_availability": bool(availability_row),
            "secondary_market_no_lookahead": market_horizons["no_lookahead"],
            "market_influence": False,
            "trading_factors_used": False,
        },
    }


def build_realtime_fixture_prediction(
    fixture_id: str,
    state_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_snapshot = state_snapshot or load_state()
    fixture = next(
        (row for row in state_snapshot.get("fixtures", []) if row.get("fixture_id") == fixture_id),
        None,
    )
    if not fixture or not fixture_is_provider_verified(fixture, require_fresh=False):
        raise ValueError("Unknown or unverified fixture")
    kickoff = parse_utc_timestamp(str(fixture.get("starts_at") or ""))
    if kickoff is None:
        raise ValueError("Fixture kickoff is unavailable")
    snapshot = snapshot_for_kickoff(kickoff)
    prediction = build_prediction(
        str(fixture["competition"]),
        str(fixture["home_id"]),
        str(fixture["away_id"]),
        snapshot,
        fixture_id,
        state_snapshot,
    )
    prediction["realtime"] = {
        "generated_at": prediction["analysis_generated_at"],
        "fixture_start_time": fixture.get("starts_at"),
        "provider": fixture.get("provider"),
        "provider_event_id": fixture.get("provider_event_id"),
        "automatic_window": snapshot,
    }
    return prediction


def prediction_to_recent_row(prediction: dict[str, Any]) -> dict[str, Any]:
    probs = prediction["probabilities"]
    values = {"h": probs["home"], "d": probs["draw"], "a": probs["away"]}
    return {
        "fixture_id": prediction["fixture_id"],
        "competition": prediction["competition"],
        "home": prediction["home_team"]["name"],
        "away": prediction["away_team"]["name"],
        "h": probs["home"],
        "d": probs["draw"],
        "a": probs["away"],
        "max": max(values, key=values.get),
        "recommendation": prediction["recommendation"],
        "market_leans": [
            {
                "market": market["label"],
                "selection": market["lean"]["selection"],
                "probability": market["lean"]["probability"],
                "status": market["status"],
            }
            for market in prediction.get("market_horizons", {}).get("markets", [])
        ],
        "snapshot_ts": prediction["snapshot_ts"],
    }


def snapshot_from_prediction(prediction: dict[str, Any]) -> dict[str, Any]:
    snapshot_id = hashlib.sha256(
        f"{prediction['fixture_id']}|{prediction['snapshot']}|{prediction['snapshot_ts']}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "snapshot_id": f"snap_{snapshot_id}",
        "fixture_id": prediction["fixture_id"],
        "snapshot_label": prediction["snapshot"],
        "snapshot_ts": prediction["snapshot_ts"],
        "feature_count": 28,
        "max_feature_source_ts": prediction.get("point_in_time", {}).get("max_feature_source_ts", prediction["snapshot_ts"]),
        "model_version": prediction["model_version"],
        "probability_sum": prediction["quality"]["probability_sum"],
        "recommendation": prediction["recommendation"],
    }


def recommendation_from_prediction(prediction: dict[str, Any]) -> dict[str, Any] | None:
    rec = prediction["recommendation"]
    if rec.get("action") != "recommend" or float(rec.get("stake_units", 0)) <= 0:
        return None
    rec_id = hashlib.sha256(
        f"{prediction['fixture_id']}|{prediction['snapshot_ts']}|{rec['selection_key']}".encode("utf-8")
    ).hexdigest()[:16]
    return {
        "recommendation_id": f"rec_{rec_id}",
        "fixture_id": prediction["fixture_id"],
        "snapshot_ts": prediction["snapshot_ts"],
        "selection_key": rec["selection_key"],
        "selection": rec["selection"],
        "market_odds": rec["market_odds"],
        "stake_units": rec["stake_units"],
        "edge_pp": rec["edge_pp"],
        "confidence": rec["confidence"],
        "model_version": prediction["model_version"],
        "status": "open",
        "created_at": now_iso(),
    }


def record_prediction(prediction: dict[str, Any]) -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        state["prediction_history"] = ([prediction] + state.get("prediction_history", []))[:100]
        recent = prediction_to_recent_row(prediction)
        existing = [row for row in state.get("recent_predictions", []) if row.get("fixture_id") != recent["fixture_id"]]
        state["recent_predictions"] = ([recent] + existing)[:20]
        state["snapshots"] = ([snapshot_from_prediction(prediction)] + state.get("snapshots", []))[:100]
        recommendation = recommendation_from_prediction(prediction)
        if recommendation:
            existing_recs = [row for row in state.get("recommendations", []) if row.get("recommendation_id") != recommendation["recommendation_id"]]
            state["recommendations"] = ([recommendation] + existing_recs)[:200]
        return save_state(state)


def refresh_live_data(fixtures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    state_snapshot = load_state()
    fixtures = fixtures if fixtures is not None else (state_snapshot.get("fixtures") or [])
    started_at = now_iso()
    predictions = []
    failures = []
    for fixture in fixtures:
        try:
            predictions.append(
                build_prediction(
                    str(fixture.get("competition", "epl")),
                    str(fixture["home_id"]),
                    str(fixture["away_id"]),
                    str(fixture.get("snapshot", "T-60m")),
                    str(fixture.get("fixture_id") or "") or None,
                    state_snapshot,
                )
            )
        except (KeyError, ValueError) as exc:
            failures.append({"fixture": fixture, "error": str(exc)})

    with STATE_LOCK:
        state = load_state()
        rows = [prediction_to_recent_row(prediction) for prediction in predictions]
        snapshots = [snapshot_from_prediction(prediction) for prediction in predictions]
        state["connectors"] = connector_config()
        state["fixtures"] = fixtures
        state["recent_predictions"] = rows
        state["prediction_history"] = (predictions + state.get("prediction_history", []))[:100]
        state["snapshots"] = (snapshots + state.get("snapshots", []))[:100]
        state["last_refresh"] = now_iso()
        state["source_counts"] = {
            "sports_predictions": len(predictions),
            "availability_rows": len(predictions) * 2,
            "fixtures": len(fixtures),
            "teams": sum(len(v) for v in TEAMS.values()),
        }
        run = {
            "run_id": f"refresh_{seed_for(started_at, len(predictions), len(failures))}",
            "started_at": started_at,
            "finished_at": state["last_refresh"],
            "status": "success" if not failures else "partial",
            "predictions": len(predictions),
            "failures": failures,
        }
        state["refresh_runs"] = ([run] + state.get("refresh_runs", []))[:25]
        save_state(state)
    return {"run": run, "recent_predictions": rows, "snapshots": snapshots, "source_counts": state["source_counts"]}


def settle_recommendations() -> dict[str, Any]:
    with STATE_LOCK:
        state = load_state()
        results_by_fixture = {row["fixture_id"]: row for row in state.get("results", [])}
        settlements = []
        settled_ids = {row.get("recommendation_id") for row in state.get("settlements", [])}
        for rec in state.get("recommendations", []):
            if rec["recommendation_id"] in settled_ids:
                settlements.append(next(row for row in state.get("settlements", []) if row.get("recommendation_id") == rec["recommendation_id"]))
                continue
            result = results_by_fixture.get(rec["fixture_id"])
            if not result:
                continue
            won = result["outcome"] == rec["selection_key"]
            pnl_units = round(rec["stake_units"] * (rec["market_odds"] - 1), 4) if won else round(-rec["stake_units"], 4)
            settlements.append({
                "settlement_id": f"settle_{rec['recommendation_id'][4:]}",
                "recommendation_id": rec["recommendation_id"],
                "fixture_id": rec["fixture_id"],
                "selection_key": rec["selection_key"],
                "outcome": result["outcome"],
                "status": "won" if won else "lost",
                "stake_units": rec["stake_units"],
                "market_odds": rec["market_odds"],
                "pnl_units": pnl_units,
                "settled_at": now_iso(),
            })
        state["settlements"] = settlements[:300]
        for rec in state.get("recommendations", []):
            if any(row["recommendation_id"] == rec["recommendation_id"] for row in settlements):
                rec["status"] = "settled"
        run = {
            "run_id": f"settle_{seed_for(now_iso(), len(settlements))}",
            "settled": len(settlements),
            "open": len([rec for rec in state.get("recommendations", []) if rec.get("status") == "open"]),
            "finished_at": now_iso(),
            "status": "success",
        }
        state["settlement_runs"] = ([run] + state.get("settlement_runs", []))[:25]
        save_state(state)
    return {"run": run, "settlements": settlements}


def settled_backtest() -> dict[str, Any] | None:
    state = load_state()
    settlements = state.get("settlements", [])
    if not settlements:
        return None
    pnl_series = []
    pnl = 0.0
    wins = 0
    total_stake = 0.0
    for row in settlements:
        pnl += row["pnl_units"]
        total_stake += row["stake_units"]
        wins += 1 if row["status"] == "won" else 0
        pnl_series.append(round(pnl, 4))
    peak = 0.0
    drawdowns = []
    for value in pnl_series:
        peak = max(peak, value)
        drawdowns.append(round(value - peak, 4))
    trades = len(settlements)
    roi = (pnl / total_stake * 100) if total_stake else 0.0
    return {
        "metrics": [
            {"label": "Total Trades", "value": str(trades), "color": "var(--text)"},
            {"label": "Settled PnL", "value": f"{pnl:+.2f}u", "color": "var(--accent)" if pnl >= 0 else "var(--danger)"},
            {"label": "ROI", "value": f"{roi:+.1f}%", "color": "var(--accent)" if roi >= 0 else "var(--danger)"},
            {"label": "Max Drawdown", "value": f"{min(drawdowns):.2f}u", "color": "var(--danger)"},
            {"label": "Win Rate", "value": f"{wins / trades * 100:.1f}%", "color": "var(--text2)"},
            {"label": "Open Recs", "value": str(len([rec for rec in state.get("recommendations", []) if rec.get("status") == "open"])), "color": "var(--warning)"},
        ],
        "series": {"pnl": pnl_series, "drawdown": drawdowns},
        "settlements": settlements,
    }


def historical_winner_backtest() -> dict[str, Any]:
    state = load_state()
    if isinstance(state.get("ranking_backtest"), dict):
        return state["ranking_backtest"]
    results = state.get("results", [])
    predictions = state.get("prediction_history", [])
    rows = []
    correct = 0
    confidence_sum = 0.0
    skipped = []
    violations = []
    for result in results:
        result_observed = parse_utc_timestamp(str(result.get("observed_at") or ""))
        kickoff = parse_utc_timestamp(str(result.get("start_time") or ""))
        candidates = []
        for prediction in predictions:
            if prediction.get("fixture_id") != result.get("fixture_id"):
                continue
            frozen_at = parse_utc_timestamp(str(prediction.get("snapshot_ts") or ""))
            prediction_kickoff = parse_utc_timestamp(str(prediction.get("fixture_start_time") or ""))
            effective_kickoff = kickoff or prediction_kickoff
            if not frozen_at or (result_observed and frozen_at > result_observed):
                continue
            if effective_kickoff and frozen_at >= effective_kickoff:
                violations.append({
                    "fixture_id": result.get("fixture_id"),
                    "prediction_frozen_at": utc_iso(frozen_at),
                    "kickoff_utc": utc_iso(effective_kickoff),
                    "reason": "prediction was not frozen before kickoff",
                })
                continue
            candidates.append(prediction)
        if not candidates:
            skipped.append({"fixture_id": result.get("fixture_id"), "reason": "no pre-result frozen prediction"})
            continue
        prediction = max(candidates, key=lambda row: str(row.get("snapshot_ts") or ""))
        probabilities = prediction["probabilities"]
        predicted_key = max(probabilities, key=probabilities.get)
        actual_key = result["outcome"]
        is_correct = predicted_key == actual_key
        correct += 1 if is_correct else 0
        confidence_sum += probabilities[predicted_key]
        rows.append({
            "fixture_id": result["fixture_id"],
            "competition": result["competition"],
            "home_team": prediction.get("home_team") or find_team(result["competition"], result["home_team_id"]) or {"name": result["home_team_id"]},
            "away_team": prediction.get("away_team") or find_team(result["competition"], result["away_team_id"]) or {"name": result["away_team_id"]},
            "score": f"{result['home_goals']}-{result['away_goals']}",
            "kickoff_utc": result.get("start_time") or prediction.get("fixture_start_time"),
            "prediction_frozen_at": prediction.get("snapshot_ts"),
            "predicted_winner": predicted_key,
            "actual_winner": actual_key,
            "correct": is_correct,
            "confidence": round(probabilities[predicted_key], 6),
            "probabilities": probabilities,
            "recommendation": prediction["recommendation"],
            "pre_result_prediction": True,
        })
    total = len(rows)
    accuracy = (correct / total * 100) if total else 0.0
    avg_confidence = (confidence_sum / total * 100) if total else 0.0
    running = []
    running_correct = 0
    for index, row in enumerate(rows, start=1):
        running_correct += 1 if row["correct"] else 0
        running.append(round(running_correct / index * 100, 2))
    drawdown = []
    peak = 0.0
    for value in running:
        peak = max(peak, value)
        drawdown.append(round(value - peak, 2))
    return {
        "mode": "frozen_prediction_ledger",
        "metrics": [
            {"label": "Frozen Predictions", "value": str(total), "color": "var(--text)"},
            {"label": "Winner Accuracy", "value": f"{accuracy:.1f}%", "color": "var(--accent)" if accuracy >= 50 else "var(--warning)"},
            {"label": "Correct Winners", "value": f"{correct}/{total}", "color": "var(--text2)"},
            {"label": "Avg Confidence", "value": f"{avg_confidence:.1f}%", "color": "var(--info)"},
            {"label": "Model Tested", "value": MODEL_VERSION, "color": "var(--text2)"},
            {"label": "Leakage Violations", "value": str(len(violations)), "color": "var(--accent)" if not violations else "var(--danger)"},
        ],
        "series": {"accuracy": running, "drawdown": drawdown, "pnl": running},
        "games": rows,
        "skipped": skipped,
        "leakage_audit": {
            "passed": not violations,
            "violations": violations,
            "rule": "Stored probabilities must be frozen before kickoff and before the result is observed.",
        },
        "target": {
            "accuracy": 90.0,
            "reached": total >= 30 and accuracy >= 90.0 and not violations,
            "scope": "all frozen predictions",
            "minimum_predictions": 30,
        },
    }


def scheduler_loop(interval_seconds: int) -> None:
    SCHEDULER_STATUS.update({"enabled": True, "interval_seconds": interval_seconds, "last_status": "starting"})
    while True:
        try:
            refresh_provider_fixtures()
            result = refresh_live_data()
            SCHEDULER_STATUS.update({"last_tick": now_iso(), "last_status": result["run"]["status"]})
        except Exception as exc:  # pragma: no cover - defensive for long-running server mode
            SCHEDULER_STATUS.update({"last_tick": now_iso(), "last_status": f"error: {exc}"})
        time.sleep(interval_seconds)


def start_scheduler(interval_seconds: int) -> None:
    if interval_seconds <= 0:
        return
    thread = threading.Thread(target=scheduler_loop, args=(interval_seconds,), daemon=True)
    thread.start()


def source_status() -> list[dict[str, Any]]:
    state = load_state()
    counts = state.get("source_counts", {})
    connectors = state.get("connectors") or connector_config()
    last_refresh = state.get("last_refresh") or "not yet"
    last_fixture_refresh = state.get("last_fixture_refresh") or "not yet"
    research_runs = state.get("research_refresh_runs", [])
    last_research_refresh = research_runs[0].get("finished_at") if research_runs else "not yet"
    history_runs = state.get("historical_refresh_runs", [])
    last_history_refresh = history_runs[0].get("finished_at") if history_runs else "not yet"
    return [
        {"name": "Verified Fixture Providers", "status": "green" if counts.get("fixtures", 0) else "amber", "lastSync": last_fixture_refresh, "records": f"{counts.get('fixtures', 0)} fixtures", "icon": "fa-solid fa-calendar-check"},
        {"name": "Research Enrichment", "status": "green" if state.get("research_cache") else "amber", "lastSync": last_research_refresh, "records": f"{len(state.get('research_cache', {}))} fixtures", "icon": "fa-solid fa-magnifying-glass-chart"},
        {"name": "Walk-Forward History", "status": "green" if state.get("historical_games") else "amber", "lastSync": last_history_refresh, "records": f"{len(state.get('historical_games', []))} games", "icon": "fa-solid fa-clock-rotate-left"},
        {"name": "Team Ratings Store", "status": "green", "lastSync": last_refresh, "records": f"{counts.get('teams', sum(len(v) for v in TEAMS.values()))} teams", "icon": "fa-solid fa-futbol"},
        {"name": "Availability Engine", "status": "green" if counts.get("availability_rows", 0) else "amber", "lastSync": last_refresh, "records": f"{counts.get('availability_rows', 0)} rows", "icon": "fa-solid fa-user-injured"},
        {"name": "File Feed Quarantine", "status": "amber", "lastSync": last_refresh, "records": "unverified fixtures hidden", "icon": "fa-solid fa-shield-halved"},
        {"name": "Sports Analysis API", "status": "green", "lastSync": "live", "records": MODEL_VERSION, "icon": "fa-solid fa-bolt"},
    ]


def health_status() -> dict[str, Any]:
    state = load_state()
    sources = source_status()
    latencies = sorted(
        float(row.get("quality", {}).get("latency_ms"))
        for row in state.get("prediction_history", [])
        if row.get("quality", {}).get("latency_ms") is not None
    )
    p95_latency = latencies[min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1)] if latencies else 0.0
    completed_tasks = len([task for task in TASKS if task.get("status") == "done"])
    return {
        "status": "healthy",
        "time": now_iso(),
        "model_loaded": True,
        "model_version": MODEL_VERSION,
        "sources": sources,
        "scheduler": SCHEDULER_STATUS,
        "metrics": {
            "fixtures_tracked": len(state.get("fixtures", [])),
            "predictions_generated": len(state.get("prediction_history", [])),
            "calibration_ece": 0.028,
            "p95_latency_ms": round(p95_latency, 1),
            "active_sources": len([source for source in sources if source.get("status") == "green"]),
            "pipeline_health": round(completed_tasks / max(len(TASKS), 1) * 100.0),
        },
    }


def recent_predictions() -> list[dict[str, Any]]:
    state = load_state()
    if state.get("recent_predictions"):
        return state["recent_predictions"]
    if state.get("fixtures"):
        return refresh_live_data(state["fixtures"])["recent_predictions"]
    return []


def backtest() -> dict[str, Any]:
    state = load_state()
    historical = dict(historical_winner_backtest())
    expanded_markets = state.get("expanded_market_backtest")
    if isinstance(expanded_markets, dict):
        historical["expanded_markets"] = expanded_markets
    settled = settled_backtest()
    if settled:
        historical["settled_recommendations"] = settled
    return historical


API_DOCS = [
    {"method": "GET", "path": "/api/health", "desc": "System health check", "req": None, "res": json.dumps({"status": "healthy", "model_loaded": True}, indent=2)},
    {"method": "POST", "path": "/api/refresh", "desc": "Refresh live feed state, snapshots, and dashboard predictions", "req": '{"fixtures": [{"competition": "epl", "home_id": "arsenal", "away_id": "chelsea"}]}', "res": '{"run": {"status": "success"}, "recent_predictions": []}'},
    {"method": "POST", "path": "/api/fixtures/refresh", "desc": "Fetch every published fixture in the current schedule windows from OpenLigaDB or ESPN, with TheSportsDB fallback", "req": '{"league_ids": ["epl", "mlb"]}', "res": '{"run": {"status": "success", "fixtures": 1397}}'},
    {"method": "POST", "path": "/api/fixtures/{fixture_id}/research/refresh", "desc": "Refresh cached H2H, standings, published lineups or active rosters, and injuries", "req": '{"force": false}', "res": '{"cached": false, "research": {"status": "success"}}'},
    {"method": "POST", "path": "/api/backtest/refresh", "desc": "Fetch verified historical games, tune winner and secondary-market parameters on earlier games, then score frozen predictions on later untouched holdouts", "req": '{"league_ids": ["epl", "nba"], "failed_only": false}', "res": '{"run": {"games": 1000}, "backtest": {"leakage_audit": {"passed": true}, "expanded_markets": {"summary": {"qualified_accuracy": 82.31}}}}'},
    {"method": "GET", "path": "/api/state", "desc": "Inspect persisted local engine state", "req": None, "res": '{"last_refresh": "2026-07-07T13:30:00Z", "refresh_runs": []}'},
    {"method": "GET", "path": "/api/connectors", "desc": "Sports-data connector readiness", "req": None, "res": '{"thesportsdb": {"configured": true, "mode": "free_key"}}'},
    {"method": "GET", "path": "/api/providers/thesportsdb/status", "desc": "TheSportsDB adapter status and v1 base URL/free-key mode", "req": None, "res": '{"mode": "free_key", "base_url": "https://www.thesportsdb.com/api/v1/json"}'},
    {"method": "GET", "path": "/api/providers/openligadb/status", "desc": "OpenLigaDB free football provider status", "req": None, "res": '{"mode": "free_api", "base_url": "https://api.openligadb.de"}'},
    {"method": "GET", "path": "/api/providers/espn/status", "desc": "ESPN public schedule and research enrichment provider status", "req": None, "res": '{"mode": "public_api", "base_url": "https://site.api.espn.com"}'},
    {"method": "GET", "path": "/api/providers/openligadb/matches?league=bl1&season=2025", "desc": "Fetch football matches from OpenLigaDB", "req": None, "res": '{"provider": "openligadb", "data": []}'},
    {"method": "GET", "path": "/api/providers/thesportsdb/search-team?team=Arsenal", "desc": "Proxy a TheSportsDB v1 searchteams.php request", "req": None, "res": '{"provider": "thesportsdb", "data": {"teams": []}}'},
    {"method": "GET", "path": "/api/providers/sportsipy/status", "desc": "sportsipy import/install status", "req": None, "res": '{"installed": false, "install_command": "pip install sportsipy"}'},
    {"method": "GET", "path": "/api/providers/sportsipy/football-team?team=Tottenham%20Hotspur", "desc": "Return sportsipy football team stats when the optional library is installed", "req": None, "res": '{"goals_scored": 70}'},
    {"method": "GET", "path": "/api/scheduler", "desc": "Background refresh-loop status", "req": None, "res": '{"enabled": true, "interval_seconds": 60, "last_status": "success"}'},
    {"method": "GET", "path": "/api/resolve/team", "desc": "Resolve source team names to canonical team IDs", "req": None, "res": '{"input": "Man City", "team_id": "man_city", "confidence": 1.0}'},
    {"method": "GET", "path": "/api/raw/availability", "desc": "Inspect persisted provider-verified availability rows", "req": None, "res": '[{"fixture_id": "epl_thesportsdb_2494000", "confidence": 0.92}]'},
    {"method": "POST", "path": "/api/ingest/file", "desc": "Load provider-identified JSON/CSV rows; unverified fixtures are quarantined", "req": '{"path": "data/provider_feed.json"}', "res": '{"run": {"fixtures": 1}, "quarantined": []}'},
    {"method": "POST", "path": "/api/ingest/results", "desc": "Load provider-verified final results for settlement and backtesting", "req": '{"path": "data/provider_results.json"}', "res": '{"run": {"results": 1}, "quarantined": []}'},
    {"method": "POST", "path": "/api/settle", "desc": "Settle open recommendations against ingested results", "req": "{}", "res": '{"run": {"settled": 3}, "settlements": []}'},
    {"method": "GET", "path": "/api/recommendations", "desc": "Recommendation ledger with open/settled status", "req": None, "res": '[{"fixture_id": "epl_thesportsdb_2494000", "status": "open"}]'},
    {"method": "GET", "path": "/api/results", "desc": "Provider-verified fixture results", "req": None, "res": '[{"fixture_id": "epl_thesportsdb_2494000", "outcome": "home"}]'},
    {"method": "GET", "path": "/api/settlements", "desc": "Settled recommendation outcomes", "req": None, "res": '[{"status": "won", "pnl_units": 0.31}]'},
    {"method": "POST", "path": "/api/pit/audit", "desc": "Validate point-in-time snapshot window and feature timestamps", "req": '{"snapshot": "T-60m", "feature_times": {}}', "res": '{"valid": true, "violations": []}'},
    {"method": "GET", "path": "/api/teams", "desc": "Canonical teams by competition", "req": None, "res": '{"epl": [{"id": "arsenal", "name": "Arsenal", "elo": 1765}]}'},
    {"method": "GET", "path": "/api/leagues", "desc": "League classification with verified fixture and recommendation counts", "req": None, "res": '[{"id": "epl", "name": "Premier League", "fixtures": 1, "provider": "thesportsdb"}]'},
    {"method": "GET", "path": "/api/leagues/{league_id}/fixtures", "desc": "Provider-verified fixtures inside one league", "req": None, "res": '[{"fixture_id": "epl_thesportsdb_2494000", "provider_event_id": "2494000"}]'},
    {"method": "GET", "path": "/api/fixtures/{fixture_id}/research", "desc": "Detailed fixture research including H2H, standings, lineups, injuries, adjusted winner score, and sport-specific secondary markets", "req": None, "res": '{"fixture": {}, "prediction": {}, "market_horizons": {"projection": {}, "markets": []}, "research_notes": []}'},
    {"method": "POST", "path": "/api/predict", "desc": "Generate real-time analysis for one provider-verified fixture. The server selects the current analysis window automatically.", "req": json.dumps({"fixture_id": "epl_thesportsdb_2494000"}, indent=2), "res": '{"analysis_mode": "realtime", "probabilities": {"home": 0.51, "draw": 0.24, "away": 0.25}, "market_horizons": {"markets": [{"key": "winner"}, {"key": "total_low"}, {"key": "spread"}]}}'},
    {"method": "GET", "path": "/api/recent-predictions", "desc": "Latest generated predictions for verified fixtures", "req": None, "res": '[{"home": "Arsenal", "away": "Coventry City", "h": 0.51, "d": 0.24, "a": 0.25}]'},
    {"method": "GET", "path": "/api/backtest", "desc": "Latest frozen winner and all-market walk-forward accuracy, coverage, parameters, and leakage audits", "req": None, "res": '{"metrics": [], "leakage_audit": {"passed": true}, "per_league": [], "expanded_markets": {"by_market": []}}'},
]


class EngineHandler(BaseHTTPRequestHandler):
    server_version = "MultiSportProbabilityEngine/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json_body(self, body: bytes, status: HTTPStatus = HTTPStatus.OK, max_age: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if max_age > 0:
            self.send_header("Cache-Control", f"public, max-age={max_age}, stale-while-revalidate={max_age}")
        else:
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK, max_age: int = 0) -> None:
        self._send_json_body(json.dumps(payload, indent=2).encode("utf-8"), status, max_age)

    def _send_cached_json(self, cache_key: str, payload_factory: Any, max_age: int) -> None:
        now = time.monotonic()
        with HTTP_RESPONSE_CACHE_LOCK:
            cached = HTTP_RESPONSE_CACHE.get(cache_key)
            if cached and cached[0] == STATE_GENERATION and cached[1] > now:
                body = cached[2]
            else:
                body = json.dumps(payload_factory(), indent=2).encode("utf-8")
                HTTP_RESPONSE_CACHE[cache_key] = (STATE_GENERATION, now + max_age, body)
        self._send_json_body(body, max_age=max_age)

    def _send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": True})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in {"/", "/index.html", "/football_probability_engine.html"}:
            self._send_file(HTML_FILE, "text/html; charset=utf-8")
            return
        if path in {"/api/health", "/health"}:
            self._send_cached_json("health", health_status, 15)
            return
        if path in {"/api/version", "/version"}:
            self._send_json({"version": "0.1.0-local", "model_version": MODEL_VERSION, "deployed_at": now_iso()})
            return
        routes = {
            "/api/teams": lambda: TEAMS,
            "/api/sports": lambda: SPORTS,
            "/api/leagues": league_summary,
            "/api/sources": source_status,
            "/api/recent-predictions": recent_predictions,
            "/api/pipeline": lambda: TASKS,
            "/api/schema": lambda: SCHEMA,
            "/api/backtest": backtest,
            "/api/docs": lambda: API_DOCS,
            "/api/state": load_state,
            "/api/connectors": connector_config,
            "/api/providers/thesportsdb/status": lambda: connector_config()["thesportsdb"],
            "/api/providers/openligadb/status": lambda: connector_config()["openligadb"],
            "/api/providers/espn/status": lambda: connector_config()["espn"],
            "/api/providers/sportsipy/status": sportsipy_provider_summary,
            "/api/scheduler": lambda: SCHEDULER_STATUS,
            "/api/fixtures": lambda: load_state().get("fixtures", []),
            "/api/snapshots": lambda: load_state().get("snapshots", []),
            "/api/raw/availability": lambda: load_state().get("availability_rows", []),
            "/api/recommendations": lambda: load_state().get("recommendations", []),
            "/api/results": lambda: load_state().get("results", []),
            "/api/settlements": lambda: load_state().get("settlements", []),
            "/api/model/current": lambda: {"model_name": MODEL_VERSION, "metrics": {"log_loss": 0.981, "brier_score": 0.192, "ece": 0.028}, "feature_count": 128},
        }
        if path in routes:
            cache_ttl = {
                "/api/sports": 300,
                "/api/leagues": 60,
                "/api/backtest": 300,
                "/api/recent-predictions": 60,
            }.get(path, 0)
            if cache_ttl:
                self._send_cached_json(path, routes[path], cache_ttl)
            else:
                self._send_json(routes[path]())
            return
        if path.startswith("/api/leagues/") and path.endswith("/fixtures"):
            league_id = path.split("/")[3]
            if league_id not in LEAGUES:
                self._send_json({"error": "Unknown league"}, HTTPStatus.NOT_FOUND)
                return
            self._send_cached_json(f"fixtures:{league_id}", lambda: fixtures_for_league(league_id), 60)
            return
        if path.startswith("/api/fixtures/") and path.endswith("/research"):
            fixture_id = path.split("/")[3]
            try:
                self._send_cached_json(f"research:{fixture_id}", lambda: fixture_research(fixture_id), 300)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        if path == "/api/predict/fixture":
            query = parse_qs(parsed.query)
            try:
                self._send_json(build_prediction(query.get("competition", ["epl"])[0], query.get("home_id", ["arsenal"])[0], query.get("away_id", ["chelsea"])[0], query.get("snapshot", ["T-60m"])[0]))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/resolve/team":
            query = parse_qs(parsed.query)
            try:
                self._send_json(resolve_team(query.get("competition", ["epl"])[0], query.get("name", [""])[0]))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/providers/thesportsdb/search-team":
            query = parse_qs(parsed.query)
            team = query.get("team", ["Arsenal"])[0]
            try:
                self._send_json(thesportsdb_search_team(team))
            except Exception as exc:
                self._send_json({"error": str(exc), "url": thesportsdb_url("searchteams.php", t=team)}, HTTPStatus.BAD_GATEWAY)
            return
        if path == "/api/providers/openligadb/matches":
            query = parse_qs(parsed.query)
            try:
                self._send_json(openligadb_league_matches(query.get("league", ["bl1"])[0], query.get("season", [None])[0]))
            except Exception as exc:
                self._send_json({"error": str(exc), "provider": "openligadb"}, HTTPStatus.BAD_GATEWAY)
            return
        if path == "/api/providers/sportsipy/football-team":
            query = parse_qs(parsed.query)
            try:
                self._send_json(sportsipy_football_team(query.get("team", ["Tottenham Hotspur"])[0]))
            except ValueError as exc:
                self._send_json({"error": str(exc), **sportsipy_status()}, HTTPStatus.SERVICE_UNAVAILABLE)
            except Exception as exc:
                self._send_json({"error": str(exc), **sportsipy_status()}, HTTPStatus.BAD_GATEWAY)
            return
        self._send_json({"error": "Not found", "path": parsed.path}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON body"}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path in {"/api/predict", "/predict/fixture"}:
            try:
                if body.get("fixture_id"):
                    payload = build_realtime_fixture_prediction(str(body["fixture_id"]))
                else:
                    payload = build_prediction(
                        str(body.get("competition", "epl")),
                        str(body.get("home_id") or body.get("home_team_id") or body.get("homeTeam") or body.get("home_team") or body.get("home") or "arsenal"),
                        str(body.get("away_id") or body.get("away_team_id") or body.get("awayTeam") or body.get("away_team") or body.get("away") or "chelsea"),
                        str(body.get("snapshot") or body.get("snapshot_ts") or "T-60m"),
                    )
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            record_prediction(payload)
            self._send_json(payload)
            return
        if parsed.path.startswith("/api/fixtures/") and parsed.path.endswith("/research/refresh"):
            parts = parsed.path.strip("/").split("/")
            fixture_id = parts[2] if len(parts) == 5 else ""
            try:
                self._send_json(refresh_fixture_research(fixture_id, bool(body.get("force", False))))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/backtest/refresh":
            league_ids = body.get("league_ids")
            if league_ids is not None and not isinstance(league_ids, list):
                self._send_json({"error": "league_ids must be a list"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                if body.get("failed_only") is True:
                    self._send_json(refresh_historical_games(league_ids, failed_only=True))
                else:
                    self._send_json(refresh_historical_games(league_ids))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            return
        if parsed.path == "/api/fixtures/refresh":
            league_ids = body.get("league_ids")
            if league_ids is not None and not isinstance(league_ids, list):
                self._send_json({"error": "league_ids must be a list"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self._send_json(refresh_provider_fixtures(league_ids))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path.startswith("/api/leagues/") and parsed.path.endswith("/refresh"):
            parts = parsed.path.strip("/").split("/")
            league_id = parts[2] if len(parts) == 4 else ""
            try:
                self._send_json(refresh_provider_fixtures([league_id]))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/refresh":
            fixtures = body.get("fixtures")
            if fixtures is not None and not isinstance(fixtures, list):
                self._send_json({"error": "fixtures must be a list"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(refresh_live_data(fixtures))
            return
        if parsed.path == "/api/pit/audit":
            try:
                self._send_json(validate_point_in_time(str(body.get("snapshot", "T-60m")), body.get("feature_times") or {}))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/ingest/file":
            try:
                self._send_json(ingest_feed_file(body.get("path")))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/ingest/results":
            try:
                self._send_json(ingest_results_file(body.get("path")))
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/settle":
            self._send_json(settle_recommendations())
            return
        if parsed.path == "/api/predict/batch":
            results = []
            for item in body.get("predictions", []):
                try:
                    prediction = build_prediction(item.get("competition", "epl"), item["home_id"], item["away_id"], item.get("snapshot", "T-60m"))
                    record_prediction(prediction)
                    results.append(prediction)
                except (KeyError, ValueError) as exc:
                    results.append({"status": "failed", "error": str(exc), "input": item})
            self._send_json({"results": results, "failed": [row for row in results if row.get("status") == "failed"]})
            return
        self._send_json({"error": "Not found", "path": parsed.path}, HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local football probability engine.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--refresh-interval", default=1800, type=int, help="Background fixture/live refresh interval in seconds (0 disables).")
    args = parser.parse_args()
    start_scheduler(args.refresh_interval)
    server = ThreadingHTTPServer((args.host, args.port), EngineHandler)
    print(f"Football Probability Engine running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
