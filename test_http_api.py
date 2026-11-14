import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import engine_server as engine


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.previous_state_file = os.environ.get("ENGINE_STATE_FILE")
        cls.state_path = Path(cls.temp_dir.name) / "engine_state.json"
        os.environ["ENGINE_STATE_FILE"] = str(cls.state_path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), engine.EngineHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        if cls.previous_state_file is None:
            os.environ.pop("ENGINE_STATE_FILE", None)
        else:
            os.environ["ENGINE_STATE_FILE"] = cls.previous_state_file
        cls.temp_dir.cleanup()

    def setUp(self):
        engine.save_state(engine.default_state())

    def request_json(self, method, path, payload=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = None if payload is None else json.dumps(payload)
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8"))
        conn.close()
        return res.status, data

    def sportsdb_event(self):
        return {
            "idEvent": "9001",
            "idLeague": "4328",
            "strLeague": "English Premier League",
            "strSport": "Soccer",
            "strHomeTeam": "Arsenal",
            "strAwayTeam": "Chelsea",
            "idHomeTeam": "133604",
            "idAwayTeam": "133610",
            "strTimestamp": "2099-08-21T19:00:00",
            "strStatus": "NS",
            "strPostponed": "no",
            "strSeason": "2099-2100",
        }

    def seed_fixture(self):
        fixture = engine.normalize_thesportsdb_fixture("epl", self.sportsdb_event())
        state = engine.default_state()
        state["fixtures"] = [fixture]
        state["source_counts"]["fixtures"] = 1
        engine.save_state(state)
        return fixture

    def test_predict_endpoint_returns_sports_only_analysis(self):
        status, data = self.request_json("POST", "/api/predict", {
            "competition": "epl",
            "home_id": "Arsenal FC",
            "away_id": "Man City",
            "snapshot": "T-60m",
        })

        self.assertEqual(status, 200)
        self.assertTrue(data["point_in_time"]["valid"])
        self.assertEqual(data["recommendation"]["action"], "analysis_only")
        self.assertFalse(data["analysis_policy"]["moneyline_used"])
        self.assertFalse(data["analysis_policy"]["odds_used"])
        self.assertFalse(data["analysis_policy"]["trading_factors_used"])

    def test_health_metrics_use_persisted_live_counts(self):
        state = engine.default_state()
        first_event = self.sportsdb_event()
        second_event = {
            **self.sportsdb_event(),
            "idEvent": "9002",
            "strHomeTeam": "Liverpool",
            "strAwayTeam": "Manchester City",
            "idHomeTeam": "133602",
            "idAwayTeam": "133613",
        }
        state["fixtures"] = [
            engine.normalize_thesportsdb_fixture("epl", first_event),
            engine.normalize_thesportsdb_fixture("epl", second_event),
        ]
        state["prediction_history"] = [{"quality": {"latency_ms": 31}}]
        engine.save_state(state)

        status, data = self.request_json("GET", "/api/health")

        self.assertEqual(status, 200)
        self.assertEqual(data["metrics"]["fixtures_tracked"], 2)
        self.assertEqual(data["metrics"]["predictions_generated"], 1)
        self.assertEqual(data["metrics"]["p95_latency_ms"], 31.0)

    @mock.patch.object(engine, "fetch_espn_schedule_fixtures")
    @mock.patch.object(engine, "thesportsdb_get")
    def test_provider_refresh_endpoint_populates_verified_fixture(self, provider_get, espn_schedule):
        provider_get.return_value = {"events": [self.sportsdb_event()]}
        espn_schedule.return_value = {"fixtures": [], "rejected": [], "provider_urls": [], "errors": []}

        status, data = self.request_json("POST", "/api/leagues/epl/refresh", {})
        fixtures_status, fixtures = self.request_json("GET", "/api/leagues/epl/fixtures")

        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["fixtures"], 1)
        self.assertEqual(fixtures_status, 200)
        self.assertEqual(fixtures[0]["provider_event_id"], "9001")
        self.assertTrue(fixtures[0]["verified"])
        self.assertFalse(fixtures[0]["prediction_available"])
        self.assertIsNone(fixtures[0]["recommendation"])

    def test_unverified_sample_feed_is_quarantined(self):
        status, data = self.request_json("POST", "/api/ingest/file", {"path": "data/sample_live_feed.json"})
        fixtures_status, fixtures = self.request_json("GET", "/api/fixtures")
        odds_status, _ = self.request_json("GET", "/api/raw/odds")

        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["fixtures"], 0)
        self.assertEqual(len(data["quarantined"]), 3)
        self.assertEqual(fixtures_status, 200)
        self.assertEqual(fixtures, [])
        self.assertEqual(odds_status, 404)

    def test_fixture_research_uses_stored_provider_event_id(self):
        fixture = self.seed_fixture()

        fixtures_status, fixtures = self.request_json("GET", "/api/leagues/epl/fixtures")
        research_status, research = self.request_json("GET", f"/api/fixtures/{fixture['fixture_id']}/research")

        self.assertEqual(fixtures_status, 200)
        self.assertEqual(len(fixtures), 1)
        self.assertEqual(research_status, 200)
        self.assertEqual(research["fixture"]["provider_event_id"], "9001")
        self.assertEqual(research["prediction"]["recommendation"]["action"], "analysis_only")
        for key in ["past_h2h", "lineups", "standings", "injuries", "adjusted_final_score", "market_horizons"]:
            self.assertIn(key, research)

    def test_refresh_with_no_verified_fixtures_stays_empty(self):
        status, data = self.request_json("POST", "/api/refresh", {})
        state_status, state = self.request_json("GET", "/api/state")

        self.assertEqual(status, 200)
        self.assertEqual(data["run"]["predictions"], 0)
        self.assertEqual(state_status, 200)
        self.assertEqual(state["snapshots"], [])

    def test_external_provider_status_endpoints(self):
        sportsdb_status, sportsdb = self.request_json("GET", "/api/providers/thesportsdb/status")
        sportsipy_status, sportsipy = self.request_json("GET", "/api/providers/sportsipy/status")
        openliga_status, openliga = self.request_json("GET", "/api/providers/openligadb/status")
        espn_status, espn = self.request_json("GET", "/api/providers/espn/status")

        self.assertEqual(sportsdb_status, 200)
        self.assertEqual(sportsdb["mode"], "free_key")
        self.assertEqual(sportsipy_status, 200)
        self.assertIn("installed", sportsipy)
        self.assertEqual(openliga_status, 200)
        self.assertEqual(openliga["mode"], "free_api")
        self.assertEqual(espn_status, 200)
        self.assertEqual(espn["mode"], "public_api")

    def test_fixture_research_refresh_endpoint(self):
        fixture = self.seed_fixture()
        expected = {
            "fixture_id": fixture["fixture_id"],
            "cached": False,
            "research": {"status": "success", "standings": {"verified": True}},
        }

        with mock.patch.object(engine, "refresh_fixture_research", return_value=expected) as refresh:
            status, data = self.request_json(
                "POST",
                f"/api/fixtures/{fixture['fixture_id']}/research/refresh",
                {"force": True},
            )

        self.assertEqual(status, 200)
        self.assertEqual(data, expected)
        refresh.assert_called_once_with(fixture["fixture_id"], True)

    def test_walk_forward_backtest_refresh_endpoint(self):
        expected = {
            "run": {"games": 120, "status": "success"},
            "backtest": {"mode": "walk_forward_frozen_predictions", "leakage_audit": {"passed": True}},
        }

        with mock.patch.object(engine, "refresh_historical_games", return_value=expected) as refresh:
            status, data = self.request_json("POST", "/api/backtest/refresh", {"league_ids": ["epl"]})

        self.assertEqual(status, 200)
        self.assertEqual(data, expected)
        refresh.assert_called_once_with(["epl"])

    def test_backtest_endpoint_includes_expanded_market_report(self):
        state = engine.default_state()
        state["ranking_backtest"] = {
            "mode": "walk_forward_frozen_predictions",
            "metrics": [],
            "leakage_audit": {"passed": True},
        }
        state["expanded_market_backtest"] = {
            "mode": "expanded_markets_walk_forward_v1",
            "summary": {"qualified_accuracy": 82.31},
            "leakage_audit": {"passed": True},
        }
        engine.save_state(state)

        status, data = self.request_json("GET", "/api/backtest")

        self.assertEqual(status, 200)
        self.assertEqual(data["expanded_markets"]["summary"]["qualified_accuracy"], 82.31)
        self.assertTrue(data["expanded_markets"]["leakage_audit"]["passed"])

    def test_sports_and_leagues_have_real_fixture_provider_mappings(self):
        sports_status, sports = self.request_json("GET", "/api/sports")
        leagues_status, leagues = self.request_json("GET", "/api/leagues")
        by_id = {league["id"]: league for league in leagues}

        self.assertEqual(sports_status, 200)
        self.assertEqual(leagues_status, 200)
        self.assertEqual(sports["nba"]["fixture_provider"], "espn/thesportsdb")
        self.assertEqual(by_id["fifa_wc"]["name"], "FIFA World Cup 2026")
        self.assertEqual(by_id["fifa_wc"]["openligadb_shortcut"], "wm26")
        self.assertEqual(by_id["bundesliga"]["provider"], "openligadb")
        self.assertEqual(by_id["mlb"]["provider_league_id"], "4424")

    def test_predict_endpoint_accepts_multi_sport_display_names(self):
        examples = [
            ("nba", "Boston Celtics", "Los Angeles Lakers"),
            ("nfl", "Kansas City Chiefs", "San Francisco 49ers"),
            ("mlb", "New York Yankees", "Los Angeles Dodgers"),
            ("nhl", "Toronto Maple Leafs", "New York Rangers"),
        ]
        for competition, home_team, away_team in examples:
            status, prediction = self.request_json("POST", "/api/predict", {
                "competition": competition,
                "homeTeam": home_team,
                "awayTeam": away_team,
                "snapshot": "T-60m",
            })
            self.assertEqual(status, 200)
            self.assertEqual(prediction["recommendation"]["action"], "analysis_only")

    def test_pit_audit_endpoint_rejects_bad_window(self):
        status, data = self.request_json("POST", "/api/pit/audit", {"snapshot": "T+5m"})

        self.assertEqual(status, 400)
        self.assertIn("Unsupported snapshot", data["error"])


if __name__ == "__main__":
    unittest.main()
