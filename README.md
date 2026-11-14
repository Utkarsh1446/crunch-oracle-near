# Crunch Oracle

Live demo: https://crunch-oracle-near.pages.dev
Repository: https://github.com/ts200190/crunch-oracle-near

Crunch Oracle is a football forecasting agent with a public track record on NEAR testnet. It commits a home/draw/away probability before a deadline, reveals that probability, and stores a multiclass Brier score after the result is posted. A per-forecast price is quoted from NEAR Intents 1Click with `dry: true` and pinned on the contract as an access receipt.

The problem: a forecast that can be edited after kickoff is not a track record. The chain keeps the commitment, the revealed probabilities, the result, and the score in one account anyone can read back.

This repository also contains the pre-hackathon SuperPumped Crunch research engine (Python HTTP service, local state, and an IEEE-style manuscript). That engine is the baseline. The oracle does not import it and does not read `data/engine_state.json`.

**Not included:** the engine's local state file `data/engine_state.json` (fitted score-fusion coefficients the manuscript calls proprietary, plus a cached provider extract) is not in this repository or its history. Nothing in the oracle, the contract, the dashboard, CI, or the public verify script needs it. The baseline engine starts with an empty state when the file is missing; see "Verify without data/engine_state.json" below.

Tracks: `agents`, `open`.

## What NEAR does here

| Step | Where it happens |
| --- | --- |
| Register a match with its OpenLigaDB URL, source kickoff, and deadline | `register_match` on the testnet contract |
| Commit `sha256(match_id\|home_bps\|draw_bps\|away_bps\|salt)` | `commit_forecast`, signer is the agent |
| Reveal the probabilities and salt before the deadline | `reveal_forecast`. The contract recomputes the hash |
| Post the final score after the deadline | `post_result` |
| Store the multiclass Brier score and the agent's mean | `score_forecast`, callable by anyone |
| Record a 1Click price for one committed forecast | off-chain `POST https://1click.chaindefuser.com/v0/quote` with `dry: true`, then `record_access` |

Probabilities are integer basis points and must sum to 10000. Lower is better.

## Brier score

The contract stores the standard multiclass Brier score: the sum of squared errors over the three outcomes, not the per-class mean.

```
standard sum = (p_home - o_home)^2 + (p_draw - o_draw)^2 + (p_away - o_away)^2
brier_micro  = sum_i (p_bps - o_bps)^2 // 100
```

`p` is the probability in `[0, 1]`. `o` is 1 for the result and 0 for the other two. `p_bps` is that probability in basis points. Dividing `brier_micro` by 1,000,000 returns the standard sum. The contract does not divide by 3.

| Definition | What it is | Uniform 1/3 baseline | This record |
| --- | --- | --- | --- |
| Standard sum (stored) | sum of the three squared errors | 2/3 = 0.666667 | contract mean 0.322223 |
| Per-class mean (not stored) | that sum divided by 3 | 2/9 = 0.222222 | 0.107408 |

Recomputed from `export_public` on `crunch-oracle-8fa9a2b8.testnet` (finality `final`, 2026-11-14). The same integers are in `dashboard/snapshot.json`. `scripts/verify_onchain.py` checks them again.

| Match | Basis points H/D/A | Result | Stored millionths | Standard sum | Per-class mean |
| --- | --- | --- | --- | --- | --- |
| SC Freiburg 2–0 Union Berlin | 5883 / 2118 / 1999 | home | 254316 | 0.254316 | 0.084772 |
| TSG Hoffenheim 2–1 Schalke 04 | 5673 / 2349 / 1978 | home | 281532 | 0.281532 | 0.093844 |
| 1. FC Köln 1–2 Bayer 04 Leverkusen | 3091 / 2246 / 4663 | away | 430823 | 0.430823 | 0.143608 |
| Three scored rows | | | sum 966671 | exact mean 0.322223666… | 0.107408 |

Per-class figures are the standard sum divided by 3, rounded to 6 decimals. The contract mean is the integer division `966671 // 3 = 322223` millionths, which is 0.322223. That is the number `mean_brier_micro` returns. The exact average of the three sums is `966671 / 3000000` = 0.322223666…

Worked row, Freiburg, home win. Basis-point errors are `-4117`, `2118`, `1999`.

```
4117^2 + 2118^2 + 1999^2 = 16949689 + 4485924 + 3996001 = 25431614
25431614 // 100 = 254316 millionths = 0.254316
```

On a full season this same standard sum is often near 0.58–0.62, because many calls miss. These three replays are lower because the highest probability matched the result in each one. That is a sample of three, not a season. A per-class mean would be smaller still (uniform 0.222222). That is not the stored score.

`historical_replay` is set on chain when the declared source kickoff is already in the past at registration. Those rows are replays. They prove the forecast was fixed before a short replay deadline and before the result transaction, not before the original kickoff.

## Do this, then see that

1. Open https://crunch-oracle-near.pages.dev after this build is published (or serve `dashboard/dist` locally). You see four Bundesliga matches. The mean is **0.3222**, labelled as the standard sum. Under it: stored contract mean **0.322223** (322223 millionths), uniform baseline **0.666667**, and the per-class mean **0.107408** with uniform baseline **0.222222**, marked not stored. Two dry access receipts. The page then rereads `export_public` from `https://rpc.testnet.near.org`. The figures stay the same. The `?` control on that card states the formula.
2. Open the scored row **SC Freiburg – Union Berlin**, or `#fixture/bl1-2026-83232`. You see probabilities 58.83 / 21.18 / 19.99, result 2–0, Brier 0.2543, the commitment hash, the canonical reveal string, and the testnet transaction links.
3. The pitch card is **FC Bayern München vs 1. FC Köln**. You see a commit and reveal timestamp before the published kickoff `2026-11-20T19:30:00Z`, `historical_replay: false`, and score locked until that kickoff. The kickoff clock on the card is that instant in IST.
4. Open **Access quotes**. You see two receipts, each 2.00 USDC quoted to about 0.59 wNEAR, both marked dry. No deposit address was issued.
5. Run `python3 scripts/forecast_local.py`. It reprints the same probabilities from the OpenLigaDB cache, using only matches that kicked off earlier.
6. Open **Method**. The page recomputes each stored millionths value from the revealed basis points and shows `matches chain` when they agree. The shared test vector is match `bl1-2026-1`, probabilities 5000/3000/2000, salt `abababababababababababababababab`, sha256 `731ce143d28fe73400d26797feefe89b6c572d2e4e7fc0e879cb7b16a944444d`, home-outcome standard sum `380000` millionths (0.38). The per-class mean of that vector is 0.126666..., which the contract does not store.

## Verify without data/engine_state.json

A judge does not need the research cache. The open Poisson model reads `oracle/cache/bundesliga_matches.json` (match id, clubs, kickoff, final score). The scores and hashes below come from the contract.

1. `python3 scripts/verify_onchain.py` calls `export_public` on `crunch-oracle-8fa9a2b8.testnet`. For every reveal it recomputes `sha256(match_id|home_bps|draw_bps|away_bps|salt)` and compares it to the stored commitment. For every score it recomputes `brier_micro` and compares it to `score.brier_micro`, then checks that the agent sum is 966671 and the integer mean is 322223. Exit code is 1 on a mismatch. The script does not open `data/engine_state.json`.
2. Offline, the same check against the committed export: `python3 scripts/verify_onchain.py --snapshot dashboard/snapshot.json`.
3. One hand check is the Freiburg row in the Brier section. The commitment string and transaction links for that match are on `#fixture/bl1-2026-83232`.

## Weekend forecasts (not sent)

No new forecast was committed for this change. The calendar weekend of 2026-11-14 through 2026-11-16 has no Bundesliga fixture in the OpenLigaDB cache (`fetched_at` 2026-11-14T03:31:25Z). The next matchday is 2026-11-20 through 2026-11-22.

Print the plan. This sends nothing:

```sh
python3 scripts/commit_weekend.py
```

Default window: `2026-11-20T00:00:00Z` inclusive, `2026-11-23T00:00:00Z` exclusive. It skips `bl1-2026-83237` (FC Bayern München vs 1. FC Köln), which is already committed and revealed. The other eight fixtures are the plan. Probabilities are computed at run time from the OpenLigaDB cache. Salts are not created in this mode.

After a separate yes from the owner, the same command with `--execute` registers, commits, and reveals those eight on `crunch-oracle-8fa9a2b8.testnet`. It does not post results, does not call 1Click, and does not create an account. That flag was not used.

## Run locally

Python 3.12 and Rust stable with the `wasm32-unknown-unknown` target.

```sh
python3 -m unittest oracle.test_oracle
cargo test --manifest-path contract/Cargo.toml
python3 scripts/forecast_local.py
python3 scripts/build_dashboard.py
python3 -m http.server 8765 --directory dashboard/dist
```

`forecast_local.py --refresh` fetches OpenLigaDB again. Without `--refresh` it uses `oracle/cache/bundesliga_matches.json`.

Repeating the chain demo on a new throwaway account (testnet faucet, no mainnet, no spend):

```sh
python3 scripts/testnet_demo.py --new-account
```

Running it without `--new-account` resumes `docs/testnet.json` and does not create a second account. The account key stays in `~/.near-credentials/testnet/` and is not part of this repository.

## Testnet contract

| | |
| --- | --- |
| Account | `crunch-oracle-8fa9a2b8.testnet` |
| Network | NEAR testnet |
| NearBlocks | https://testnet.nearblocks.io/address/crunch-oracle-8fa9a2b8.testnet |
| Explorer | https://explorer.testnet.near.org/accounts/crunch-oracle-8fa9a2b8.testnet |
| Deploy | [`7ogjjBeyVBGU6Lwfmo7dgV2o9RyvhMNbWyNqQzvwkgSW`](https://testnet.nearblocks.io/txns/7ogjjBeyVBGU6Lwfmo7dgV2o9RyvhMNbWyNqQzvwkgSW) |

Live view checked 2026-11-14 against `export_public`: 4 matches, 2 receipts, agent `brier_micro_sum` 966671, `mean_brier_micro` 322223 (standard-sum mean 0.322223). Per-class mean of those three scores: 0.107408. Not stored.

The agent, owner, and oracle are this same throwaway account.

### Scored historical replays

Source matches are Bundesliga 2026/27 matchdays already finished on 2026-11-08. The on-chain deadline was `2026-11-14T03:29:24Z`. Commits and reveals are before that deadline. Results and scores are after it.

| Match | Forecast home/draw/away | Result | Brier | Commit | Reveal | Score |
| --- | --- | --- | --- | --- | --- | --- |
| 1. FC Köln 1–2 Bayer 04 Leverkusen (`bl1-2026-83235`) | 30.91 / 22.46 / 46.63 | Away | 0.430823 | [`8S38BoiM7D4NVkePRcYh5ZE5L1g2BeUxLNLEN3VddN45`](https://testnet.nearblocks.io/txns/8S38BoiM7D4NVkePRcYh5ZE5L1g2BeUxLNLEN3VddN45) | [`C3y7dTD39HpowQvZudytrFDLJtU7nP6DDz6dEiKDEJ2r`](https://testnet.nearblocks.io/txns/C3y7dTD39HpowQvZudytrFDLJtU7nP6DDz6dEiKDEJ2r) | [`3UPTr6619Zv9pWJXb2z9FFqC7976oPMzn5yjsqHwKxwE`](https://testnet.nearblocks.io/txns/3UPTr6619Zv9pWJXb2z9FFqC7976oPMzn5yjsqHwKxwE) |
| SC Freiburg 2–0 1. FC Union Berlin (`bl1-2026-83232`) | 58.83 / 21.18 / 19.99 | Home | 0.254316 | [`38v3U7ExRPAMBoQ9icDeL1UXuALPnYofLVxKnqAuTfFt`](https://testnet.nearblocks.io/txns/38v3U7ExRPAMBoQ9icDeL1UXuALPnYofLVxKnqAuTfFt) | [`EtgVNgmMmBKZE1D4f4LrHyeQP1PgG9zyHA4SgrrBhiem`](https://testnet.nearblocks.io/txns/EtgVNgmMmBKZE1D4f4LrHyeQP1PgG9zyHA4SgrrBhiem) | [`E4BEHtCgYZArvsdPdLGksVtwHXCENX7CTFVpm2kVTRoM`](https://testnet.nearblocks.io/txns/E4BEHtCgYZArvsdPdLGksVtwHXCENX7CTFVpm2kVTRoM) |
| TSG Hoffenheim 2–1 FC Schalke 04 (`bl1-2026-83231`) | 56.73 / 23.49 / 19.78 | Home | 0.281532 | [`AsxvQ1HyW3cJfAhyRyRfYkUHi11fhU6BHH2kkS4qjvek`](https://testnet.nearblocks.io/txns/AsxvQ1HyW3cJfAhyRyRfYkUHi11fhU6BHH2kkS4qjvek) | [`5xfSpAAPq6V8yRTy1GwvADd9968kHo6CBkejxtwkLW9i`](https://testnet.nearblocks.io/txns/5xfSpAAPq6V8yRTy1GwvADd9968kHo6CBkejxtwkLW9i) | [`CyUEDrGdm7LLNSEwqYwcKFkMgapHyj8gbATqFULFNLqu`](https://testnet.nearblocks.io/txns/CyUEDrGdm7LLNSEwqYwcKFkMgapHyj8gbATqFULFNLqu) |

Register and result transactions are in `dashboard/snapshot.json` under `transactions.matches`.

### Commit before a real kickoff

| Match | Kickoff | Commit | Reveal |
| --- | --- | --- | --- |
| FC Bayern München vs 1. FC Köln (`bl1-2026-83237`) | 2026-11-20T19:30:00Z | [`F89q4FnHSnzoNybmDTuJSJzA2pLak8UC2tV3hCN7nTEu`](https://testnet.nearblocks.io/txns/F89q4FnHSnzoNybmDTuJSJzA2pLak8UC2tV3hCN7nTEu) at 2026-11-14T03:27:31Z | [`3azoLXndckFP3t7bH2tCUWq5FmUXLuZZGNmc9UkzWTdT`](https://testnet.nearblocks.io/txns/3azoLXndckFP3t7bH2tCUWq5FmUXLuZZGNmc9UkzWTdT) at 2026-11-14T03:27:44Z |

Forecast 88.74 / 7.27 / 3.99. The contract will reject `post_result` until the deadline, which is that kickoff. There is no score for this row yet.

### Dry access receipts

Both quotes are `POST /v0/quote` with `dry: true`, 2 USDC on Ethereum (`nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near`) to wNEAR. The responses had no `depositAddress`. The contract method rejects `dry: false`.

| Buyer label | Quoted out | Transaction |
| --- | --- | --- |
| `replay-reader` (Köln vs Leverkusen) | 0.59104448 wNEAR | [`6Df3sWCLPsR2QnnvibVtKT1bdMiYzRaL39j5KGs5A1pM`](https://testnet.nearblocks.io/txns/6Df3sWCLPsR2QnnvibVtKT1bdMiYzRaL39j5KGs5A1pM) |
| `pre-kickoff-reader` (Bayern vs Köln) | 0.59035381 wNEAR | [`6gGM4Sg79F4zT6ZcxeB96xsk4peWjepdYb2o1to8TCCh`](https://testnet.nearblocks.io/txns/6gGM4Sg79F4zT6ZcxeB96xsk4peWjepdYb2o1to8TCCh) |

No 1Click API key was used. Q-006: an unauthenticated quote carries the higher fee. That fee is visible inside the dry quote; nothing was paid.

## Static dashboard

Cloudflare Pages, no server:

| | |
| --- | --- |
| Build command | `python3 scripts/build_dashboard.py` |
| Output directory | `dashboard/dist` |

The build copies `dashboard/index.html`, `styles.css`, `app.js`, and `snapshot.json`. The browser reads the snapshot, then refreshes from testnet RPC.

## Model

`oracle/model.py` is an independent Poisson 1X2 model.

- History is every cached finished match with an earlier kickoff. The target is excluded.
- Attack and defense are goals scored and conceded per game, shrunk by 8 pseudo-games toward the league average.
- `home_xg = league_home_gpg * home_attack * away_defense`, and the same for away.
- Scorelines 0 through 10 are renormalized. Basis points are the largest-remainder split of 10000.

The cache is factual match id, clubs, kickoff, and final score from `https://api.openligadb.de/getmatchdata/bl1/2025` and `.../2026`. No odds and no viewer counts. See `docs/DATA_AUDIT.md`.

## Hackathon window vs baseline

The original development repository was created in July 2026. Its last baseline commit is `99e988f` (2026-08-26). Hackathon work was done on its `near-hackathon` branch. That repository stays private because its history contains `data/engine_state.json`.

This public repository is a single-commit snapshot of that branch (tip `0ef1663`, including the Brier clarification), with `data/engine_state.json` removed, so the per-commit history is not included here. The tag `hackathon-submission` points at the snapshot commit. The table below separates window work from baseline file by file.

| In the window (this branch) | Baseline, before 2026-10-19 |
| --- | --- |
| `contract/` NEAR contract and tests | `engine_server.py`, `superpumped_crunch/`, `football_probability_engine.html` |
| `oracle/` open Poisson model, OpenLigaDB trimmer, tests | the sample JSON files (`data/engine_state.json` is withheld, see above) |
| `scripts/testnet_demo.py`, `forecast_local.py`, `build_dashboard.py`, `verify_onchain.py`, `commit_weekend.py` | `test_engine_server.py`, `test_http_api.py` |
| `dashboard/` | `SuperPumped_Crunch_IEEE_Research_Paper.docx` |
| `.github/workflows/ci.yml`, `LICENSE`, this README | |

Libraries used for the window code: `near-sdk` 5.11.0, `near-cli-rs` 0.31.1, the `cryptography` package for the throwaway ed25519 key, Python 3.12 standard library, OpenLigaDB's public API, NEAR testnet RPC and faucet (`https://helper.nearprotocol.com/account`), NEAR Intents 1Click. No paid data feed.

The window code was written with an AI coding agent. Q-005 allows that. The baseline engine was already in the repository and is not relicensed as new work; the MIT license added in this branch covers the software in the repository.

## Limitations

- Three of the four on-chain forecasts are labelled historical replays. Their original kickoffs were 2026-11-08. The commitment does not claim those probabilities existed before then. The mean 0.322223 is the standard sum on those three rows, each of which had the modal outcome correct. It is not a per-class mean and not a season-long score.
- Bayern vs Köln is a real pre-kickoff commit. It cannot be scored until after `2026-11-20T19:30:00Z`.
- Dry quotes do not transfer tokens. The receipt is the quoted price, not revenue. `record_access` requires `dry: true`.
- NEAR view calls are public. The receipt does not hide the revealed forecast. Hiding a forecast would need an off-chain API that checks the receipt.
- The open Poisson model is not the proprietary fusion model described in the manuscript. Bayern's 88.7% home probability is what that simple rate model emits. It is not a calibrated claim.
- The scoreline distribution is truncated at 10 goals.
- One account is owner, oracle, and agent. A separated oracle would be a different key.
- Testnet only. No mainnet transaction was signed and no tokens were spent.
- `data/engine_state.json` (the baseline engine's cache with the proprietary fitted coefficients and an ESPN-derived extract) is withheld from this repository. Without it the baseline engine starts from an empty state, so its backtests and rankings are not reproducible from this repository. The NEAR oracle path does not depend on it.

## CI

`.github/workflows/ci.yml` runs the contract tests, the wasm release build, the Python tests, and the dashboard copy.
