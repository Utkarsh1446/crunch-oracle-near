import json
import unittest
from pathlib import Path

from oracle.commit import UNIFORM_PER_CLASS, UNIFORM_SUM, audit_public_state, brier_micro, brier_sum, canonical_payload, commitment_hex, per_class_mean
from oracle.model import forecast_match, prior_matches, to_bps
from oracle.openliga import load_cache, trim_match
from oracle.select import plan_forecasts, select_kickoff_window

ROOT = Path(__file__).resolve().parent
SALT = "abababababababababababababababab"
VECTOR_HASH = "731ce143d28fe73400d26797feefe89b6c572d2e4e7fc0e879cb7b16a944444d"


def played(match_id: str, kickoff: str, home: str, away: str, home_goals: int, away_goals: int) -> dict:
    return {
        "id": match_id,
        "kickoff": kickoff,
        "home": home,
        "away": away,
        "finished": True,
        "home_goals": home_goals,
        "away_goals": away_goals,
    }


class CommitTests(unittest.TestCase):
    def test_payload_and_hash_match_the_contract_vector(self):
        payload = canonical_payload("bl1-2026-1", 5000, 3000, 2000, SALT)
        self.assertEqual(payload, "bl1-2026-1|5000|3000|2000|" + SALT)
        self.assertEqual(commitment_hex("bl1-2026-1", 5000, 3000, 2000, SALT), VECTOR_HASH)

    def test_brier_vectors(self):
        self.assertEqual(brier_micro(5000, 3000, 2000, "home"), 380_000)
        self.assertEqual(brier_micro(5000, 3000, 2000, "draw"), 780_000)
        self.assertEqual(brier_micro(5000, 3000, 2000, "away"), 980_000)
        self.assertEqual(brier_micro(5000, 3000, 2000, "Home"), 380_000)

    def test_standard_sum_is_not_divided_by_three(self):
        self.assertAlmostEqual(UNIFORM_SUM, 2 / 3)
        self.assertAlmostEqual(UNIFORM_PER_CLASS, 2 / 9)
        self.assertAlmostEqual(brier_sum(380_000), 0.38)
        self.assertAlmostEqual(per_class_mean(380_000), 0.38 / 3)
        # Freiburg, home win. Squares: 4117^2 + 2118^2 + 1999^2 = 25431614; // 100 = 254316.
        self.assertEqual(brier_micro(5883, 2118, 1999, "home"), 254_316)
        self.assertEqual(brier_micro(5673, 2349, 1978, "home"), 281_532)
        self.assertEqual(brier_micro(3091, 2246, 4663, "away"), 430_823)
        self.assertEqual(254_316 + 281_532 + 430_823, 966_671)
        self.assertEqual(966_671 // 3, 322_223)

    def test_snapshot_chain_recomputes(self):
        snapshot = json.loads((ROOT.parent / "dashboard" / "snapshot.json").read_text(encoding="utf-8"))
        report = audit_public_state(snapshot["chain"])
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["scored"], 3)
        self.assertEqual(report["brier_micro_sum"], 966_671)
        self.assertEqual(report["contract_mean_micro"], 322_223)
        self.assertAlmostEqual(report["standard_mean"], 966_671 / 3 / 1_000_000)
        self.assertAlmostEqual(report["per_class_mean"], 966_671 / 9 / 1_000_000)
        self.assertAlmostEqual(report["uniform_sum"], 2 / 3)
        self.assertAlmostEqual(report["uniform_per_class"], 2 / 9)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.history = []
        clubs = ["Alpha FC", "Beta FC", "Gamma FC", "Delta FC"]
        for index in range(24):
            home = clubs[index % 4]
            away = clubs[(index + 1) % 4]
            self.history.append(
                played(
                    f"bl1-2025-{index}",
                    f"2025-08-{(index % 27) + 1:02d}T15:30:00Z",
                    home,
                    away,
                    1 + (index % 3),
                    index % 2,
                )
            )
        self.target = {
            "id": "bl1-2026-900",
            "kickoff": "2026-11-08T18:30:00Z",
            "home": "Alpha FC",
            "away": "Beta FC",
            "finished": True,
            "home_goals": 4,
            "away_goals": 0,
        }

    def test_basis_points_sum_to_10000(self):
        self.assertEqual(sum(to_bps(0.5, 0.3, 0.2)), 10000)
        self.assertEqual(sum(to_bps(1 / 3, 1 / 3, 1 / 3)), 10000)

    def test_forecast_uses_only_earlier_matches(self):
        later = played("bl1-2026-901", "2026-11-09T15:30:00Z", "Alpha FC", "Gamma FC", 0, 5)
        earlier_extra = played("bl1-2025-99", "2025-09-01T15:30:00Z", "Alpha FC", "Delta FC", 3, 0)
        base = forecast_match(self.history + [self.target, later], self.target)
        with_later = forecast_match(self.history + [self.target, later], self.target)
        with_earlier = forecast_match(self.history + [self.target, later, earlier_extra], self.target)
        self.assertEqual(base["home_bps"], with_later["home_bps"])
        self.assertEqual(base["prior_matches"], len(self.history))
        self.assertNotEqual(base["home_bps"], with_earlier["home_bps"])
        self.assertEqual(sum((base["home_bps"], base["draw_bps"], base["away_bps"])), 10000)
        self.assertNotIn(self.target["id"], [match["id"] for match in prior_matches(self.history + [self.target], self.target["kickoff"], self.target["id"])])


class WeekendPlanTests(unittest.TestCase):
    def test_calendar_weekend_is_empty_and_next_matchday_skips_bayern(self):
        matches = load_cache()
        empty = select_kickoff_window(matches, "2026-11-14T00:00:00Z", "2026-11-17T00:00:00Z")
        self.assertEqual(empty, [])
        rows = plan_forecasts(matches, "2026-11-20T00:00:00Z", "2026-11-23T00:00:00Z", ["bl1-2026-83237"])
        self.assertEqual(
            [row["match_id"] for row in rows],
            [
                "bl1-2026-83238",
                "bl1-2026-83239",
                "bl1-2026-83241",
                "bl1-2026-83242",
                "bl1-2026-83245",
                "bl1-2026-83244",
                "bl1-2026-83240",
                "bl1-2026-83243",
            ],
        )
        self.assertNotIn("bl1-2026-83237", [row["match_id"] for row in rows])
        for row in rows:
            self.assertEqual(row["home_bps"] + row["draw_bps"] + row["away_bps"], 10000)
            self.assertTrue(row["home_name_ok"])
            self.assertTrue(row["away_name_ok"])
            self.assertGreaterEqual(row["kickoff"], "2026-11-20")
            self.assertLess(row["kickoff"], "2026-11-23")


class OpenLigaTests(unittest.TestCase):
    def test_trim_keeps_final_score_and_utc(self):
        payload = json.loads((ROOT / "testdata" / "sample_match.json").read_text(encoding="utf-8"))
        trimmed = trim_match(payload, "2026")
        self.assertEqual(trimmed["id"], "bl1-2026-77500")
        self.assertEqual(trimmed["home_goals"], 2)
        self.assertEqual(trimmed["away_goals"], 1)
        self.assertTrue(trimmed["kickoff"].endswith("Z"))
        self.assertEqual(trimmed["source_url"], "https://api.openligadb.de/getmatchdata/77500")
        self.assertNotIn("numberOfViewers", trimmed)


if __name__ == "__main__":
    unittest.main()
