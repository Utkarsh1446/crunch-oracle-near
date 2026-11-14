# Data audit (before the public demo)

> Public snapshot note: this repository is a single-commit snapshot made after this audit. `data/engine_state.json` was removed from the tree before that commit, so it is not in this repository or its history. The findings below describe the private development repository.

Checked on 2026-11-14, before the NEAR work was added. Nothing here was posted anywhere. Repository visibility was not changed.

## Safe to keep

| Path | Finding |
| --- | --- |
| `data/sample_live_feed.json` | Five synthetic Premier League rows. Invented odds and availability. No people, no keys. |
| `data/sample_results.json` | Five synthetic final scores. No provider payload. |
| `.env.example` | `THESPORTSDB_API_KEY=123` is TheSportsDB's published free demo key, also hardcoded in `engine_server.py` as `SPORTSDB_FREE_KEY`. It is not a private credential. |
| `superpumped_crunch/sports/*.json` | Sport configuration: lines, labels, distribution family. No fitted fusion weights. |
| `oracle/testdata/sample_match.json` | One trimmed OpenLigaDB-shaped example used by unit tests. |
| `oracle/cache/bundesliga_matches.json` | Created by the hackathon path. Match id, club names, kickoff, final score only, with the OpenLigaDB URL. No odds, lineups, or viewer counts. |

No email addresses, private keys, mnemonics, or bearer tokens were found in `data/` or the manuscript metadata.

## Flag before making the repository public

`data/engine_state.json` (about 42 MB, already in the pre-hackathon history) is a local research cache, not a secret dump, and it is the wrong file to treat as cleared for publication:

- `ranking_parameters` and `market_parameters` hold the fitted weights the manuscript describes as proprietary score-fusion coefficients (`weights` families include elo, form, h2h, and long_term, plus per-market training stats). The manuscript says those coefficients are withheld on purpose.
- `historical_games` has 15,333 rows derived from ESPN site feeds and other providers (World Cup, MLB, NFL, La Liga, Ligue 1, Premier League, Serie A, Bundesliga, NHL, NBA), including provider event ids and source URLs. Republishing that extract is a licensing question even though the file contains no account data.
- `fixtures`, `recent_predictions`, and `research_cache` are the same kind of cached provider research.

The hackathon path does not read this file. `oracle/` forecasts from the OpenLigaDB cache only.

`SuperPumped_Crunch_IEEE_Research_Paper.docx` is an IEEE-style manuscript dated July 2026. Core properties have an empty creator and no email. The text is written as a disclosure boundary: it describes the evaluation protocol and states that proprietary fusion coefficients are omitted. It does not contain customer data. It should not be read as permission to publish `data/engine_state.json`.

## Rescan on the Brier clarification commit

Tracked files were searched for copies of the `ranking_parameters` and `market_parameters` sections of `data/engine_state.json`. No other file contained those values. Path mentions remain in `engine_server.py`, `superpumped_crunch/__main__.py`, `test_engine_server.py`, `test_http_api.py`, `.env.example`, this note, and the README. `dashboard/`, `oracle/`, `contract/`, and `scripts/` do not contain the file or those sections. Git history was not rewritten, so the file remains in earlier commits as well as at HEAD.

## What the new path publishes

On-chain records and `dashboard/snapshot.json` contain club names, kickoffs, final scores, probabilities, salts (after reveal), Brier scores, the testnet account id, transaction hashes, and the public fields of a dry 1Click quote (assets, integer amounts, quote hash). Quote responses are checked for a deposit address and rejected if one is present. Refund and recipient addresses used only as dry-quote parameters are not written into the snapshot or the contract.
