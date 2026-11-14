import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import engine_server as engine
import superpumped_crunch.market_backtest as market_testing
import superpumped_crunch.ranking_backtest as ranking


class EngineServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "engine_state.json"
        self.previous_state_file = os.environ.get("ENGINE_STATE_FILE")
        os.environ["ENGINE_STATE_FILE"] = str(self.state_path)

    def tearDown(self):
        if self.previous_state_file is None:
            os.environ.pop("ENGINE_STATE_FILE", None)
        else:
            os.environ["ENGINE_STATE_FILE"] = self.previous_state_file
        self.temp_dir.cleanup()

    def sportsdb_event(self, league_id="epl", event_id="9001", home="Arsenal", away="Chelsea"):
        league = engine.LEAGUES[league_id]
        return {
            "idEvent": event_id,
            "idLeague": league["provider_league_id"],
            "strLeague": league["provider_league_name"],
            "strSport": league["provider_sport"],
            "strHomeTeam": home,
            "strAwayTeam": away,
            "idHomeTeam": "133604",
            "idAwayTeam": "133610",
            "strTimestamp": "2099-08-21T19:00:00",
            "strStatus": "NS",
            "strPostponed": "no",
            "strSeason": "2099-2100",
            "intRound": "1",
            "strVenue": "Verified Stadium",
        }

    def seed_verified_feed(self):
        fixture_id = "epl_thesportsdb_9001"
        feed = {
            "fixtures": [{
                "fixture_id": fixture_id,
                "competition": "epl",
                "home": "Arsenal",
                "away": "Chelsea",
                "starts_at": "2099-08-21T19:00:00Z",
                "provider": "thesportsdb",
                "provider_event_id": "9001",
                "provider_url": "https://www.thesportsdb.com/event/9001",
                "verified": True,
                "verified_at": engine.now_iso(),
                "odds": {
                    "home": 10.0,
                    "draw": 2.0,
                    "away": 1.5,
                    "totals": {"line": 2.5, "over": 1.91, "under": 1.95},
                    "btts": {"yes": 1.82, "no": 2.02},
                },
                "availability": {
                    "home_starters_missing": 0,
                    "away_starters_missing": 1,
                    "goalkeeper_missing": 0,
                    "confidence": 0.95,
                    "lineup_type": "official",
                },
            }]
        }
        path = Path(self.temp_dir.name) / "verified_feed.json"
        path.write_text(json.dumps(feed), encoding="utf-8")
        result = engine.ingest_feed_file(str(path))
        return fixture_id, result

    def write_verified_result(self, fixture_id):
        payload = {
            "results": [{
                "fixture_id": fixture_id,
                "competition": "epl",
                "home": "Arsenal",
                "away": "Chelsea",
                "home_goals": 2,
                "away_goals": 1,
                "provider": "thesportsdb",
                "provider_event_id": "9001",
                "verified": True,
                "observed_at": engine.now_iso(),
            }]
        }
        path = Path(self.temp_dir.name) / "verified_results.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_prediction_probabilities_are_normalized_and_sports_only(self):
        prediction = engine.build_prediction("epl", "arsenal", "chelsea", "T-60m")

        self.assertAlmostEqual(sum(prediction["probabilities"].values()), 1.0, places=5)
        self.assertEqual(prediction["recommendation"]["action"], "analysis_only")
        self.assertFalse(prediction["analysis_policy"]["moneyline_used"])
        self.assertFalse(prediction["analysis_policy"]["odds_used"])
        self.assertFalse(prediction["analysis_policy"]["trading_factors_used"])
        self.assertNotIn("stake_units", prediction["recommendation"])
        self.assertNotIn("market_odds", prediction["recommendation"])

    def test_sport_specific_market_horizons_are_normalized(self):
        cases = {
            "epl": ("arsenal", "chelsea", {"winner", "total_safe", "total_low", "total", "total_high", "spread", "spread_alt", "spread_safe", "btts", "home_team_total", "away_team_total"}, "goals"),
            "nba": ("bos_celtics", "lal_lakers", {"winner", "total_ultra_safe", "total_safe", "total_low", "total", "total_high", "spread", "spread_alt", "spread_safe", "both_teams_threshold"}, "points"),
            "nfl": ("kc_chiefs", "sf_49ers", {"winner", "total_ultra_safe", "total_safe", "total_low", "total", "total_high", "spread", "spread_alt", "spread_safe", "both_teams_threshold"}, "points"),
            "mlb": ("nyy_yankees", "lad_dodgers", {"winner", "total_ultra_safe", "total_safe", "total_low", "total", "total_high", "spread", "spread_alt", "spread_safe", "both_teams_threshold"}, "runs"),
            "nhl": ("edm_oilers", "fla_panthers", {"winner", "total_ultra_safe", "total_safe", "total_low", "total", "total_high", "spread", "spread_alt", "spread_safe", "both_teams_threshold"}, "goals"),
        }

        for competition, (home, away, expected_markets, unit) in cases.items():
            with self.subTest(competition=competition):
                prediction = engine.build_prediction(competition, home, away, "T-60m")
                horizons = prediction["market_horizons"]
                self.assertEqual({market["key"] for market in horizons["markets"]}, expected_markets)
                self.assertEqual(horizons["projection"]["unit"], unit)
                self.assertTrue(horizons["no_lookahead"])
                for market in horizons["markets"]:
                    self.assertAlmostEqual(sum(row["probability"] for row in market["selections"]), 1.0, places=5)
                    self.assertTrue(all("fair_odds" not in row and "market_odds" not in row and "edge_pp" not in row for row in market["selections"]))

    def test_market_horizons_exclude_results_at_or_after_cutoff(self):
        games = [
            {"competition": "epl", "game_id": "before", "start_time": "2026-01-01T12:00:00Z", "verified": True, "home_team": "Arsenal", "away_team": "Chelsea", "home_score": 2, "away_score": 1},
            {"competition": "epl", "game_id": "cutoff", "start_time": "2026-02-01T12:00:00Z", "verified": True, "home_team": "Arsenal", "away_team": "Chelsea", "home_score": 9, "away_score": 9},
            {"competition": "epl", "game_id": "after", "start_time": "2026-03-01T12:00:00Z", "verified": True, "home_team": "Arsenal", "away_team": "Chelsea", "home_score": 8, "away_score": 8},
        ]

        horizons = engine.build_market_horizons(
            "epl",
            "Arsenal",
            "Chelsea",
            {"home": 0.5, "draw": 0.25, "away": 0.25},
            games,
            "2026-02-01T12:00:00Z",
        )

        self.assertEqual(horizons["sample"]["league_games"], 1)
        self.assertEqual(horizons["latest_feature_game_utc"], "2026-01-01T12:00:00Z")
        self.assertTrue(horizons["no_lookahead"])

    def test_secondary_market_simultaneous_games_share_frozen_features(self):
        games = [
            {"game_id": "g1", "competition": "nfl", "start_time": "2025-09-01T20:00:00Z", "verified": True, "home_team_id": "a", "away_team_id": "b", "home_team": "A", "away_team": "B", "home_score": 21, "away_score": 14},
            {"game_id": "g2", "competition": "nfl", "start_time": "2025-09-01T20:00:00Z", "verified": True, "home_team_id": "c", "away_team_id": "d", "home_team": "C", "away_team": "D", "home_score": 7, "away_score": 17},
        ]

        rows = market_testing.chronological_features(games)["nfl"]

        self.assertEqual([row["prior_games"] for row in rows], [0, 0])
        self.assertEqual([row["latest_feature_game_utc"] for row in rows], [None, None])

    def test_expanded_backtest_scores_every_supported_market_family(self):
        games = []
        kickoff = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
        for competition, score_base in (("epl", 1), ("nba", 100)):
            for index in range(40):
                games.append({
                    "game_id": f"{competition}-{index}",
                    "competition": competition,
                    "start_time": (kickoff + timedelta(days=index)).isoformat().replace("+00:00", "Z"),
                    "verified": True,
                    "home_team_id": f"{competition}-home-{index % 4}",
                    "away_team_id": f"{competition}-away-{index % 4}",
                    "home_team": f"Home {index % 4}",
                    "away_team": f"Away {index % 4}",
                    "home_score": score_base + index % 5,
                    "away_score": score_base + (index * 2) % 5,
                })

        report = market_testing.run_expanded_market_backtest(games)
        market_keys = {row["market"] for row in report["per_market"]}

        self.assertEqual(
            market_keys,
            {"winner", "total_ultra_safe", "total_safe", "total_low", "total", "total_high", "spread", "spread_alt", "spread_safe", "btts", "home_team_total", "away_team_total", "both_teams_threshold"},
        )
        self.assertEqual(report["summary"]["verified_games"], 80)
        self.assertEqual(report["summary"]["evaluated_holdout_predictions"], 210)
        self.assertTrue(report["leakage_audit"]["passed"])
        self.assertEqual(report["target"]["minimum_coverage"], 20.0)
        self.assertTrue(all("validation_predictions" in row for row in report["per_market"]))
        self.assertTrue(all(row["holdout_untouched_during_tuning_and_gate_selection"] for row in report["per_market"]))
        self.assertEqual(report["winner_target"]["minimum_coverage"], 20.0)
        self.assertEqual(report["winner_target"]["scope"], "straight match-winner predictions only")
        self.assertFalse(report["winner_target"]["reached"])

    def test_realtime_fixture_prediction_derives_analysis_window(self):
        fixture_id, _ = self.seed_verified_feed()

        prediction = engine.build_realtime_fixture_prediction(fixture_id)

        self.assertEqual(prediction["fixture_id"], fixture_id)
        self.assertEqual(prediction["analysis_mode"], "realtime")
        self.assertEqual(prediction["realtime"]["automatic_window"], prediction["snapshot"])
        self.assertEqual(prediction["realtime"]["provider_event_id"], "9001")

    def test_live_market_requires_frozen_holdout_validation(self):
        baseline = engine.build_market_horizons(
            "nba", "Boston Celtics", "Los Angeles Lakers",
            {"home": 0.5, "draw": 0.0, "away": 0.5}, [], "2026-01-01T00:00:00Z",
        )
        tuned = engine.build_market_horizons(
            "nba", "Boston Celtics", "Los Angeles Lakers",
            {"home": 0.5, "draw": 0.0, "away": 0.5}, [], "2026-01-01T00:00:00Z",
            tuned_parameters={
                "both_teams_threshold": {
                    "score_adjustment": 8.0,
                    "confidence_threshold": 0.5,
                    "training_target_reached": True,
                    "training_accuracy": 0.87,
                    "training_predictions": 300,
                    "holdout_accuracy": 0.84,
                    "holdout_predictions": 100,
                    "validated": True,
                },
                "total": {
                    "score_adjustment": 8.0,
                    "confidence_threshold": 0.5,
                    "training_target_reached": True,
                    "holdout_accuracy": 0.79,
                    "holdout_predictions": 100,
                    "validated": False,
                },
            },
        )
        baseline_markets = {row["key"]: row for row in baseline["markets"]}
        tuned_markets = {row["key"]: row for row in tuned["markets"]}
        baseline_yes = next(row["probability"] for row in baseline_markets["both_teams_threshold"]["selections"] if row["key"] == "yes")
        tuned_yes = next(row["probability"] for row in tuned_markets["both_teams_threshold"]["selections"] if row["key"] == "yes")

        self.assertGreater(tuned_yes, baseline_yes)
        self.assertEqual(tuned_markets["both_teams_threshold"]["lean"]["action"], "qualified_prediction")
        self.assertTrue(tuned_markets["both_teams_threshold"]["validation"]["validated"])
        self.assertEqual(tuned_markets["total"]["lean"]["action"], "withheld")

    def test_prediction_accepts_source_aliases(self):
        prediction = engine.build_prediction("epl", "Arsenal FC", "Man City", "T-60m")

        self.assertEqual(prediction["identity"]["home"]["reason"], "alias")
        self.assertEqual(prediction["identity"]["away"]["team_id"], "man_city")

    def test_point_in_time_rejects_unknown_window(self):
        with self.assertRaises(ValueError):
            engine.validate_point_in_time("T+5m")

    def test_sports_analysis_decision_has_no_trading_fields(self):
        decision = engine.sports_analysis_decision({"home": 0.60, "draw": 0.22, "away": 0.18}, 1.0)
        self.assertEqual(decision["action"], "analysis_only")
        self.assertNotIn("stake_units", decision)
        self.assertNotIn("market_odds", decision)
        self.assertNotIn("kelly_fraction", decision)

    def test_prediction_rejects_same_team(self):
        with self.assertRaises(ValueError):
            engine.build_prediction("epl", "arsenal", "arsenal", "T-60m")

    def test_thesportsdb_fixture_requires_exact_league_identity(self):
        event = self.sportsdb_event()
        fixture = engine.normalize_thesportsdb_fixture("epl", event)

        self.assertEqual(fixture["fixture_id"], "epl_thesportsdb_9001")
        self.assertEqual(fixture["provider_event_id"], "9001")
        self.assertTrue(fixture["verified"])
        self.assertEqual(fixture["starts_at"], "2099-08-21T19:00:00Z")

        event["idLeague"] = "wrong"
        with self.assertRaises(ValueError):
            engine.normalize_thesportsdb_fixture("epl", event)

    def test_openligadb_fixture_normalization(self):
        match = {
            "matchID": 83156,
            "matchDateTimeUTC": "2099-08-28T18:30:00Z",
            "leagueId": 4937,
            "leagueShortcut": "bl1",
            "leagueSeason": 2099,
            "matchIsFinished": False,
            "team1": {"teamId": 40, "teamName": "FC Bayern München"},
            "team2": {"teamId": 16, "teamName": "VfB Stuttgart"},
            "group": {"groupOrderID": 1, "groupName": "1. Spieltag"},
        }
        fixture = engine.normalize_openligadb_fixture("bundesliga", match)

        self.assertEqual(fixture["fixture_id"], "bundesliga_openligadb_83156")
        self.assertEqual(fixture["provider"], "openligadb")

    @mock.patch.object(engine, "openligadb_get")
    def test_fifa_world_cup_refresh_uses_quality_100_feed(self, provider_get):
        provider_get.return_value = [{
            "matchID": 83462,
            "matchDateTimeUTC": "2099-07-10T19:00:00Z",
            "leagueId": 4897,
            "leagueShortcut": "wm26",
            "leagueSeason": 2026,
            "matchIsFinished": False,
            "team1": {"teamId": 122, "teamName": "Spanien"},
            "team2": {"teamId": 123, "teamName": "Belgien"},
            "group": {"groupOrderID": 6, "groupName": "Viertelfinale"},
        }]

        result = engine.refresh_provider_fixtures(["fifa_wc"])
        fixture = result["fixtures"][0]

        self.assertEqual(engine.LEAGUES["fifa_wc"]["provider_league_id"], "4897")
        self.assertEqual(fixture["fixture_id"], "fifa_wc_openligadb_83462")
        self.assertEqual(fixture["home_name"], "Spain")
        self.assertEqual(fixture["away_name"], "Belgium")
        self.assertEqual(fixture["round_name"], "Quarterfinal")

    @mock.patch.object(engine, "fetch_espn_schedule_fixtures")
    @mock.patch.object(engine, "thesportsdb_get")
    def test_provider_refresh_persists_only_verified_fixtures(self, provider_get, espn_schedule):
        provider_get.return_value = {"events": [self.sportsdb_event()]}
        espn_schedule.return_value = {"fixtures": [], "rejected": [], "provider_urls": [], "errors": []}

        result = engine.refresh_provider_fixtures(["epl"])
        state = engine.load_state()

        self.assertEqual(result["run"]["status"], "success")
        self.assertEqual(result["run"]["fixtures"], 1)
        self.assertEqual(state["fixtures"][0]["fixture_id"], "epl_thesportsdb_9001")
        self.assertTrue(engine.fixture_is_provider_verified(state["fixtures"][0]))

    def test_espn_fixture_normalization_preserves_provider_identity(self):
        event = {
            "id": "401999001",
            "date": "2099-08-21T19:00:00Z",
            "status": {"type": {"state": "pre", "completed": False, "description": "Scheduled"}},
            "season": {"year": 2099},
            "week": {"number": 1, "text": "Matchweek 1"},
            "competitions": [{
                "venue": {"fullName": "Verified Stadium"},
                "competitors": [
                    {"homeAway": "home", "team": {"id": "359", "displayName": "Arsenal", "logo": "https://example.test/a.png"}},
                    {"homeAway": "away", "team": {"id": "363", "displayName": "Chelsea", "logo": "https://example.test/c.png"}},
                ],
            }],
        }

        fixture = engine.normalize_espn_fixture("epl", event, "https://example.test/scoreboard")

        self.assertEqual(fixture["fixture_id"], "epl_espn_401999001")
        self.assertEqual(fixture["provider"], "espn")
        self.assertEqual(fixture["home_provider_id"], "359")
        self.assertEqual(fixture["round_name"], "Matchweek 1")
        self.assertTrue(engine.fixture_is_provider_verified(fixture))

    @mock.patch.object(engine, "openligadb_get")
    def test_openligadb_schedule_is_not_truncated_to_ten_games(self, provider_get):
        provider_get.return_value = [{
            "matchID": 90000 + index,
            "matchDateTimeUTC": f"2099-08-{index + 1:02d}T19:00:00Z",
            "leagueShortcut": "bl1",
            "leagueSeason": 2099,
            "matchIsFinished": False,
            "team1": {"teamId": 100 + index, "teamName": f"Home {index}"},
            "team2": {"teamId": 200 + index, "teamName": f"Away {index}"},
            "group": {"groupOrderID": 1, "groupName": "Matchday 1"},
        } for index in range(12)]

        result = engine.fetch_provider_fixtures("bundesliga")

        self.assertEqual(len(result["fixtures"]), 12)

    def test_sample_feed_is_quarantined_from_live_fixtures(self):
        result = engine.ingest_feed_file(str(engine.DATA_DIR / "sample_live_feed.json"))
        state = engine.load_state()

        self.assertEqual(result["run"]["fixtures"], 0)
        self.assertEqual(len(result["quarantined"]), 3)
        self.assertEqual(state["fixtures"], [])
        self.assertEqual(state["odds_ticks"], [])

    def test_load_state_prunes_obsolete_historical_windows(self):
        state = engine.default_state()
        start, end = engine.historical_date_windows("epl")[0]
        expected_key = f"epl:{start}-{end}"
        stale_key = "fifa_wc:20260701-20260710"
        state["historical_window_status"] = {
            expected_key: {"status": "success"},
            stale_key: {"status": "failed"},
        }
        engine.save_state(state)

        loaded = engine.load_state()

        self.assertIn(expected_key, loaded["historical_window_status"])
        self.assertNotIn(stale_key, loaded["historical_window_status"])

    def test_state_read_cache_returns_isolated_copies(self):
        engine.save_state(engine.default_state())
        first = engine.load_state()
        first["historical_games"].append({"game_id": "local_mutation"})

        second = engine.load_state()

        self.assertEqual(second["historical_games"], [])

    def test_verified_file_feed_supplies_availability_but_odds_are_excluded(self):
        fixture_id, result = self.seed_verified_feed()
        prediction = engine.build_prediction("epl", "arsenal", "chelsea", "T-60m", fixture_id)

        self.assertEqual(result["run"]["fixtures"], 1)
        self.assertEqual(prediction["raw_odds"]["source"], "excluded_by_sports_only_policy")
        self.assertEqual(prediction["availability"]["source"], "verified_feed")
        self.assertEqual(prediction["recommendation"]["action"], "analysis_only")

    def test_moneyline_and_secondary_odds_cannot_change_predictions(self):
        fixture_id, _ = self.seed_verified_feed()
        state_with_odds = engine.load_state()
        state_without_odds = json.loads(json.dumps(state_with_odds))
        state_without_odds["odds_ticks"] = []

        with_odds = engine.build_prediction("epl", "arsenal", "chelsea", "T-60m", fixture_id, state_with_odds)
        without_odds = engine.build_prediction("epl", "arsenal", "chelsea", "T-60m", fixture_id, state_without_odds)
        self.assertEqual(with_odds["probabilities"], without_odds["probabilities"])
        self.assertEqual(with_odds["market_horizons"]["markets"], without_odds["market_horizons"]["markets"])
        self.assertEqual(with_odds["quality"]["verified_secondary_markets"], [])
        self.assertTrue(all(market["status"] == "sports_model_line" for market in with_odds["market_horizons"]["markets"]))

    def test_refresh_persists_verified_prediction_ids(self):
        fixture_id, _ = self.seed_verified_feed()
        result = engine.refresh_live_data()
        state = engine.load_state()

        self.assertEqual(result["run"]["status"], "success")
        self.assertEqual(state["recent_predictions"][0]["fixture_id"], fixture_id)
        self.assertEqual(state["snapshots"][0]["fixture_id"], fixture_id)

    def test_verified_results_drive_sports_only_backtest(self):
        fixture_id, _ = self.seed_verified_feed()
        prediction = engine.build_prediction("epl", "arsenal", "chelsea", "T-60m", fixture_id)
        engine.record_prediction(prediction)
        ingestion = engine.ingest_results_file(str(self.write_verified_result(fixture_id)))
        settlement = engine.settle_recommendations()
        with mock.patch.object(engine, "build_prediction", side_effect=AssertionError("backtest must use the frozen prediction")):
            backtest = engine.backtest()

        self.assertEqual(ingestion["run"]["results"], 1)
        self.assertEqual(settlement["run"]["settled"], 0)
        self.assertEqual(engine.load_state()["recommendations"], [])
        self.assertEqual(backtest["mode"], "frozen_prediction_ledger")
        self.assertEqual(len(backtest["games"]), 1)
        self.assertTrue(backtest["leakage_audit"]["passed"])

    def test_historical_scoreboard_parser_uses_completed_provider_result(self):
        payload = {"events": [{
            "id": "401",
            "date": "2025-09-01T20:00:00Z",
            "season": {"year": 2025, "type": 2},
            "status": {"type": {"completed": True, "state": "post"}},
            "competitions": [{"competitors": [
                {"id": "10", "homeAway": "home", "winner": False, "team": {"id": "10", "displayName": "Home Club"}, "score": {"value": 1}},
                {"id": "20", "homeAway": "away", "winner": True, "team": {"id": "20", "displayName": "Away Club"}, "score": {"value": 2}},
            ]}],
        }]}

        games = engine.parse_espn_historical_scoreboard(payload, "epl", "https://example.test/history")

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["outcome"], "away")
        self.assertTrue(games[0]["verified"])
        self.assertEqual(games[0]["source_name"], "espn")

    def test_ranking_prediction_cannot_see_result_fields(self):
        parameters = {
            "home_advantage": 45, "k_factor": 28, "elo_scale": 450,
            "form_window": 5, "draw_base": 0.28,
            "weights": {"elo": 0.65, "form": 0.15, "h2h": 0.08, "long_term": 0.12},
        }
        state = ranking.new_ranking_state(5)
        before = ranking.ranking_probabilities(state, "epl", "home", "away", parameters)
        result_mutated = ranking.ranking_probabilities(state, "epl", "home", "away", parameters)

        self.assertEqual(before, result_mutated)

    def test_simultaneous_games_are_predicted_before_any_result_update(self):
        parameters = {
            "home_advantage": 45, "k_factor": 28, "elo_scale": 450,
            "form_window": 5, "draw_base": 0.0,
            "weights": {"elo": 0.65, "form": 0.15, "h2h": 0.08, "long_term": 0.12},
        }
        games = [
            {"game_id": "g1", "competition": "nfl", "start_time": "2025-09-01T20:00:00Z", "home_team_id": "a", "away_team_id": "b", "home_team": "A", "away_team": "B", "home_score": 21, "away_score": 14, "outcome": "home"},
            {"game_id": "g2", "competition": "nfl", "start_time": "2025-09-01T20:00:00Z", "home_team_id": "c", "away_team_id": "d", "home_team": "C", "away_team": "D", "home_score": 7, "away_score": 17, "outcome": "away"},
        ]

        rows, violations, _ = ranking.simulate_walk_forward(games, parameters)

        self.assertEqual([row["prior_games"] for row in rows], [0, 0])
        self.assertEqual([row["latest_feature_game_utc"] for row in rows], [None, None])
        self.assertEqual(violations, [])

    def test_league_summary_and_fixture_research_use_provider_event(self):
        fixture_id, _ = self.seed_verified_feed()
        fixtures = engine.fixtures_for_league("epl")
        research = engine.fixture_research(fixture_id)
        with mock.patch.object(engine, "fixtures_for_league", side_effect=AssertionError("summary must not rebuild fixture cards")):
            summary = engine.league_summary()
        epl_summary = next(row for row in summary if row["id"] == "epl")

        self.assertEqual(len(fixtures), 1)
        self.assertEqual(fixtures[0]["provider_event_id"], "9001")
        self.assertEqual(epl_summary["fixtures"], 1)
        self.assertEqual(research["fixture"]["fixture_id"], fixture_id)
        self.assertIn("adjusted_final_score", research)

    def test_external_provider_config_and_url_builders(self):
        connectors = engine.connector_config()
        url = engine.thesportsdb_url("eventsnextleague.php", id="4328")

        self.assertEqual(connectors["thesportsdb"]["mode"], "free_key")
        self.assertEqual(connectors["openligadb"]["mode"], "free_api")
        self.assertEqual(connectors["espn"]["mode"], "public_api")
        self.assertIn("eventsnextleague.php?id=4328", url)

    def test_espn_standings_parser_supports_conference_entries(self):
        payload = {"children": [{
            "name": "Western Conference",
            "standings": {"entries": [
                {"team": {"id": "6", "displayName": "Edmonton Oilers"}, "stats": [
                    {"name": "playoffSeed", "value": 5},
                    {"name": "gamesPlayed", "value": 82},
                    {"name": "wins", "value": 41},
                    {"name": "losses", "value": 30},
                    {"name": "points", "value": 93},
                    {"name": "overall", "displayValue": "41-30-11, 93 PTS"},
                ]},
                {"team": {"id": "28", "displayName": "Winnipeg Jets"}, "stats": [
                    {"name": "playoffSeed", "value": 12},
                    {"name": "gamesPlayed", "value": 82},
                    {"name": "wins", "value": 35},
                    {"name": "losses", "value": 35},
                    {"name": "points", "value": 82},
                    {"name": "overall", "displayValue": "35-35-12, 82 PTS"},
                ]},
            ]},
        }]}

        rows = engine.parse_espn_standings_payload(payload, "nhl")
        standings = engine.build_verified_standings(rows, "6", "28", "espn_standings", "https://example.test")

        self.assertTrue(standings["verified"])
        self.assertTrue(standings["model_eligible"])
        self.assertEqual(standings["home"]["points"], 93)
        self.assertEqual(standings["away"]["record"], "35-35-12, 82 PTS")

    def test_zero_game_standings_are_visible_but_not_model_eligible(self):
        rows = [
            {"team_id": "home", "team": "Home", "rank": 1, "played": 0, "points": 0, "win_pct": None},
            {"team_id": "away", "team": "Away", "rank": 8, "played": 0, "points": 0, "win_pct": None},
        ]

        standings = engine.build_verified_standings(rows, "home", "away", "espn_standings", "https://example.test")

        self.assertTrue(standings["verified"])
        self.assertFalse(standings["model_eligible"])
        self.assertEqual(standings["signal"]["home_advantage_score"], 0.0)

    def test_espn_h2h_parser_reads_competition_level_status(self):
        payload = {"events": [{
            "id": "game-1",
            "date": "2098-04-24T23:40Z",
            "competitions": [{
                "status": {"type": {"state": "post", "completed": True}},
                "competitors": [
                    {"id": "8", "homeAway": "home", "team": {"id": "8", "displayName": "Milwaukee Brewers"}, "score": {"value": 0}},
                    {"id": "23", "homeAway": "away", "team": {"id": "23", "displayName": "Pittsburgh Pirates"}, "score": {"value": 6}},
                ],
            }],
        }]}

        matches = engine.parse_espn_h2h_schedule(payload, "23", "8")

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["winner_team_id"], "23")
        self.assertEqual(matches[0]["score"], "0-6")

    def test_thesportsdb_lineup_parser_separates_starters_and_substitutes(self):
        fixture = {
            "home_name": "Arsenal",
            "away_name": "Chelsea",
            "home_provider_id": "133604",
            "away_provider_id": "133610",
            "provider_event_id": "9001",
        }
        payload = {"lineup": [
            {"idPlayer": "1", "idTeam": "133604", "strPlayer": "Home Starter", "strHome": "Yes", "strSubstitute": "No", "strPosition": "Forward"},
            {"idPlayer": "2", "idTeam": "133604", "strPlayer": "Home Substitute", "strHome": "Yes", "strSubstitute": "Yes", "strPosition": "Midfielder"},
            {"idPlayer": "3", "idTeam": "133610", "strPlayer": "Away Starter", "strHome": "No", "strSubstitute": "No", "strPosition": "Defender"},
        ]}

        lineups = engine.parse_thesportsdb_lineup_payload(payload, fixture)

        self.assertTrue(lineups["verified"])
        self.assertEqual(lineups["home"]["lineup_type"], "confirmed_lineup")
        self.assertEqual(len(lineups["home"]["starters"]), 1)
        self.assertEqual(len(lineups["home"]["substitutes"]), 1)

    def test_partial_roster_does_not_claim_complete_injury_coverage(self):
        partial = {
            "verified": True,
            "injury_verified": False,
            "injuries": [],
            "source": "thesportsdb_active_roster",
            "source_url": "https://example.test/partial",
            "coverage": {"players": 10, "expected_minimum": 15, "status": "partial"},
        }
        complete = {
            "verified": True,
            "injury_verified": True,
            "injuries": [],
            "source": "espn_active_roster",
            "source_url": "https://example.test/complete",
            "coverage": {"players": 25, "expected_minimum": 15, "status": "complete"},
        }

        injuries = engine.injuries_from_rosters(complete, partial)

        self.assertFalse(injuries["verified"])
        self.assertFalse(injuries["coverage"]["complete"])

    def test_cached_research_sources_feed_adjusted_score(self):
        fixture_id, _ = self.seed_verified_feed()
        state = engine.load_state()
        state["research_cache"][fixture_id] = {
            "schema_version": engine.RESEARCH_SCHEMA_VERSION,
            "status": "success",
            "observed_at": engine.now_iso(),
            "expires_at": "2099-12-31T23:59:59Z",
            "sources": ["espn_standings", "espn_team_schedules", "espn_active_roster"],
            "h2h": {
                "source": "espn_team_schedules", "verified": True,
                "summary": {"home_wins": 3, "draws": 1, "away_wins": 1, "matches": 5},
                "signal": {"home": 0.55, "draw": 0.2, "away": 0.25}, "matches": [],
            },
            "standings": {
                "source": "espn_standings", "verified": True,
                "home": {"team": "Arsenal", "rank": 1, "points": 70},
                "away": {"team": "Chelsea", "rank": 4, "points": 60},
                "table": [], "signal": {"rank_gap": 3, "points_gap": 10, "home_advantage_score": 0.04},
            },
            "lineups": {"source": "espn_active_roster", "verified": True, "is_confirmed": False, "home": {}, "away": {}},
            "injuries": {
                "source": "espn_active_roster", "verified": True, "home": [], "away": [],
                "signal": {"home_impact": 0.0, "away_impact": 0.0, "confidence": 0.85},
            },
            "failures": [],
        }
        engine.save_state(state)

        research = engine.fixture_research(fixture_id)
        engine_names = {row["name"] for row in research["adjusted_final_score"]["engines"]}

        self.assertTrue({"Availability", "Past H2H", "League Standings"}.issubset(engine_names))
        self.assertTrue(research["research_meta"]["fresh"])

    def test_multi_sport_predictions_are_sports_only(self):
        for competition, home, away in [
            ("nba", "bos_celtics", "lal_lakers"),
            ("nfl", "kc_chiefs", "sf_49ers"),
            ("mlb", "nyy_yankees", "lad_dodgers"),
            ("nhl", "tor_maple_leafs", "nyr_rangers"),
        ]:
            prediction = engine.build_prediction(competition, home, away, "T-60m")
            self.assertAlmostEqual(sum(prediction["probabilities"].values()), 1.0, places=5)
            self.assertEqual(prediction["recommendation"]["action"], "analysis_only")
            self.assertFalse(prediction["analysis_policy"]["odds_used"])

    def test_non_football_adjusted_scorecard_stays_two_way(self):
        prediction = engine.build_prediction("nhl", "tor_maple_leafs", "nyr_rangers", "T-60m")
        unavailable_h2h = {"verified": False, "signal": {"home": 0.0, "draw": 0.0, "away": 0.0}}
        unavailable_standings = {"verified": False, "model_eligible": False, "signal": {"home_advantage_score": 0.0}}
        unavailable_injuries = {"verified": False, "signal": {"home_impact": 0.0, "away_impact": 0.0}}

        scorecard = engine.adjusted_engine_scorecard(
            prediction,
            unavailable_h2h,
            unavailable_standings,
            unavailable_injuries,
        )

        self.assertEqual(scorecard["scores"]["draw"], 0.0)


if __name__ == "__main__":
    unittest.main()
