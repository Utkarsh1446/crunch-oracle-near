const UNIFORM_SUM = 2 / 3;
const UNIFORM_CLASS = 2 / 9;

const app = document.querySelector("#app");
const accountPill = document.querySelector("#account-pill");
const accountLink = document.querySelector("#account-id");
const seasonPill = document.querySelector("#season-pill");
const footState = document.querySelector("#foot-state");

let snapshot = null;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function chain() {
  return (snapshot && snapshot.chain) || { matches: [], receipts: [], agents: [] };
}

function agent() {
  return (chain().agents && chain().agents[0]) || {};
}

function meanBrier() {
  const micro = agent().mean_brier_micro;
  if (micro === null || micro === undefined) return null;
  return Number(micro) / 1e6;
}

function fmt6(value) {
  return Number(value).toFixed(6);
}

function recomputeMicro(reveal, outcome) {
  const observed = { home: [10000, 0, 0], draw: [0, 10000, 0], away: [0, 0, 10000] }[String(outcome || "").toLowerCase()];
  if (!observed || !reveal) return null;
  const probs = [Number(reveal.home_bps), Number(reveal.draw_bps), Number(reveal.away_bps)];
  let total = 0;
  for (let index = 0; index < 3; index += 1) {
    const delta = probs[index] - observed[index];
    total += delta * delta;
  }
  return Math.floor(total / 100);
}

function scoreRollup() {
  const matches = scoredMatches();
  if (!matches.length) return null;
  let sum = 0;
  for (const match of matches) sum += Number(match.score.brier_micro);
  const contract = agent().mean_brier_micro;
  return {
    count: matches.length,
    sum,
    contractMicro: contract === null || contract === undefined ? null : Number(contract),
    exactMean: sum / matches.length / 1e6,
    perClass: sum / matches.length / 3 / 1e6,
  };
}

function brierText(micro, digits) {
  if (micro === null || micro === undefined) return "—";
  return (Number(micro) / 1e6).toFixed(digits);
}

function txUrl(hash) {
  return hash ? `https://testnet.nearblocks.io/txns/${hash}` : "";
}

function accountUrl(id) {
  return id ? `https://testnet.nearblocks.io/address/${id}` : "#";
}

function shortHash(hash) {
  if (!hash) return "—";
  if (hash.length < 14) return hash;
  return `${hash.slice(0, 8)}…${hash.slice(-4)}`;
}

function txAnchor(hash) {
  if (!hash) return "—";
  return `<a href="${txUrl(hash)}">${esc(shortHash(hash))}</a>`;
}

function matchTx(id) {
  return (snapshot.transactions && snapshot.transactions.matches && snapshot.transactions.matches[id]) || {};
}

function pad(value) {
  return String(value).padStart(2, "0");
}

function utcClock(ms) {
  if (!ms) return "—";
  const date = new Date(Number(ms));
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}Z`;
}

function kickoffIst(ms) {
  const date = new Date(Number(ms));
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Kolkata",
    weekday: "short",
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const get = (type) => parts.find((part) => part.type === type).value;
  return `${get("weekday").toUpperCase()} ${get("day")} ${get("month").toUpperCase()}<br>${get("hour")}:${get("minute")} IST`;
}

function awayShort(name) {
  return name.replace(/^(1\.\s+FC|Bayer\s+\d+|FC|TSG|SC)\s+/i, "");
}

function clubNick(name) {
  const known = [
    [/Bayern/, "Bayern"],
    [/Leverkusen/, "Leverkusen"],
    [/Köln|Koln/, "Köln"],
    [/Freiburg/, "Freiburg"],
    [/Union/, "Union Berlin"],
    [/Hoffenheim/, "Hoffenheim"],
    [/Schalke/, "Schalke 04"],
  ];
  for (const [pattern, label] of known) {
    if (pattern.test(name)) return label;
  }
  return awayShort(name);
}

function tableName(match) {
  return `${match.home} – ${awayShort(match.away)}`;
}

function explain(id, text, extra) {
  return `<span class="tip ${extra || ""}" data-tip="${id}"><button type="button" class="q" aria-expanded="false" aria-label="Explain">?</button><span class="pop" role="dialog">${text}</span></span>`;
}

function stackName(name) {
  const split = name.lastIndexOf(" ");
  if (split < 1) return esc(name);
  return `${esc(name.slice(0, split))}<br>${esc(name.slice(split + 1))}`;
}

function scoredMatches() {
  return chain().matches
    .filter((match) => match.score)
    .slice()
    .sort((a, b) => a.score.brier_micro - b.score.brier_micro);
}

function liveMatch() {
  return chain().matches
    .filter((match) => !match.historical_replay)
    .sort((a, b) => a.source_kickoff_ms - b.source_kickoff_ms)[0];
}

function pct(bps) {
  return (bps / 100).toFixed(2);
}

function barWidths(reveal) {
  const weights = [reveal.home_bps, Math.max(reveal.draw_bps, 1500), Math.max(reveal.away_bps, 1500)];
  const total = weights.reduce((sum, value) => sum + value, 0);
  return weights.map((value) => (value / total) * 100);
}

function setRoute(name) {
  document.querySelectorAll("header nav a").forEach((link) => {
    link.classList.toggle("on", link.dataset.route === name);
  });
}

function seasonLabel() {
  const match = liveMatch() || chain().matches[0];
  if (!match) return "Bundesliga";
  const year = Number(match.season);
  if (!year) return `Bundesliga ${match.season}`;
  return `Bundesliga ${year}/${String(year + 1).slice(2)}`;
}

function pitchCard(match) {
  const reveal = match.reveal;
  const txs = matchTx(match.match_id);
  const widths = barWidths(reveal);
  const flag = match.historical_replay ? "historical_replay: true" : "historical_replay: false";
  const scoreLine = match.score
    ? `score <b>${brierText(match.score.brier_micro, 4)}</b>`
    : "score <b>locked until kickoff</b>";
  return `
    <section class="fx">
      <div class="ring"></div>
      <div class="lbl">${match.historical_replay ? "Historical replay" : "Committed before kickoff"} <span class="live">${esc(flag)}</span> ${explain(match.historical_replay ? "replay" : "commit", match.historical_replay
        ? "Replays use a short on-chain deadline because the real kickoff has already passed. The commitment is before that deadline and before the result transaction. It does not prove the forecast existed before the original kickoff."
        : "A forecast you can edit after kickoff isn't a track record. The hash is fixed first. The contract recomputes it on reveal and scores it after the result. This deadline is the published kickoff, so the score stays locked until then.")}</div>
      <div class="teams">
        <div class="team">${stackName(match.home)}</div>
        <div class="vs">${kickoffIst(match.source_kickoff_ms)}</div>
        <div class="team r">${stackName(match.away)}</div>
      </div>
      <div class="prob">
        <div class="ph" style="width:${widths[0]}%">${pct(reveal.home_bps)}%</div>
        <div class="pd" style="width:${widths[1]}%">${pct(reveal.draw_bps)}</div>
        <div class="pa" style="width:${widths[2]}%">${pct(reveal.away_bps)}</div>
      </div>
      <div class="plab"><span>HOME</span><span>DRAW · AWAY</span></div>
      <div class="fxrows">
        <div>commit ${txAnchor(txs.commit)} · ${utcClock(match.committed_at_ms)}</div>
        <div>reveal ${txAnchor(txs.reveal)} · ${utcClock(reveal.revealed_at_ms)}</div>
        <div>${scoreLine}</div>
        <div>model <b>open Poisson 1X2</b></div>
      </div>
    </section>`;
}

function forecastCells(reveal) {
  return `
    <div class="sb">
      <i style="width:${reveal.home_bps / 100}%;background:var(--home)"></i>
      <i style="width:${reveal.draw_bps / 100}%;background:var(--draw)"></i>
      <i style="width:${reveal.away_bps / 100}%;background:var(--away)"></i>
    </div>
    <div class="sbl">${pct(reveal.home_bps)} / ${pct(reveal.draw_bps)} / ${pct(reveal.away_bps)}</div>`;
}

function scoredTable() {
  const rows = scoredMatches().map((match) => {
    const txs = matchTx(match.match_id);
    return `
      <tr>
        <td><div class="m"><a href="#fixture/${esc(match.match_id)}">${esc(tableName(match))}</a></div><div class="sbl">${esc(match.match_id)}</div></td>
        <td>${forecastCells(match.reveal)}</td>
        <td class="res">${match.result.home_goals}–${match.result.away_goals}</td>
        <td class="bs">${brierText(match.score.brier_micro, 4)}</td>
        <td><span class="tag y">replay</span></td>
        <td class="h">${txAnchor(txs.score)}</td>
      </tr>`;
  }).join("");
  return `
    <h2 id="scored">Scored forecasts <small>commit → reveal → result → score</small> ${explain("replay", "Each yellow replay tag means the real kickoff was already past when the match was registered. The chain proves the probabilities were fixed before the result transaction, not before that original kickoff.")}</h2>
    <table>
      <thead><tr><th>Match</th><th>Forecast H / D / A</th><th>Result</th><th>Brier</th><th>Label</th><th>Score tx</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function receiptCards() {
  const cards = (chain().receipts || []).map((receipt) => {
    const quote = (snapshot.quotes || []).find((item) => item.quote_hash === receipt.quote_hash) || {};
    const hash = quote.transaction || (snapshot.transactions.receipts || {})[receipt.buyer_label];
    const match = chain().matches.find((item) => item.match_id === receipt.match_id);
    const pair = match ? `${clubNick(match.home)} vs ${clubNick(match.away)}` : receipt.match_id;
    const amountIn = Number(receipt.amount_in_formatted).toFixed(2);
    const amountOut = Number(receipt.amount_out_formatted).toFixed(4);
    return `
      <div class="card rc">
        <div class="lbl">${esc(receipt.buyer_label)}</div>
        <div class="big">${amountIn} USDC → ${amountOut} wNEAR</div>
        <div class="row"><span>for</span><b>${esc(pair)}</b></div>
        <div class="row"><span>tx</span>${txAnchor(hash)}</div>
      </div>`;
  }).join("");
  return `
    <h2 id="receipts">Access receipts ${explain("dry", "Each receipt is a NEAR Intents 1Click quote with dry set to true. No deposit address was created and no tokens moved. The contract rejects a receipt that is not marked dry.", "up")}</h2>
    <div class="rec">
      ${cards}
    </div>`;
}

function renderTrack() {
  setRoute("track");
  const rollup = scoreRollup();
  const mean = rollup && rollup.contractMicro !== null ? rollup.contractMicro / 1e6 : meanBrier();
  const marker = mean === null ? 0 : Math.max(2, Math.min(98, (mean / UNIFORM_SUM) * 100));
  const scored = rollup ? rollup.count : (agent().scored || scoredMatches().length);
  const storedExact = rollup && rollup.contractMicro !== null ? fmt6(rollup.contractMicro / 1e6) : "—";
  const perClass = rollup ? fmt6(rollup.perClass) : "—";
  const live = liveMatch();
  app.innerHTML = `
    <div class="top">
      <section class="card">
        <div class="lbl">Agent mean Brier · standard sum <span class="tag">lower is better</span> ${explain("brier", "Stored score is the standard multiclass Brier: (p_home − o_home)² + (p_draw − o_draw)² + (p_away − o_away)². Each outcome is 1 or 0. The contract keeps that sum in millionths and does not divide by 3. A uniform one-third forecast scores 0.666667. The per-class mean is that sum divided by 3, with uniform baseline 0.222222, and is not stored. The mean uses only the three scored replays. In each of them the highest probability matched the result, so this is not a full-season score.", "wide")}</div>
        <div class="brier"><b>${mean === null ? "—" : mean.toFixed(4)}</b><span>across ${scored} scored forecasts, all labelled historical replays</span></div>
        <div class="scale">
          <div class="mk" style="left:0%" data-l="0 perfect"></div>
          <div class="mk" style="left:${marker}%;background:var(--pitch);width:5px"></div>
          <div class="mk" style="left:100%" data-l="0.667 uniform"></div>
        </div>
        <div class="ticks"><span>0.00</span><span>uniform sum ${fmt6(UNIFORM_SUM)}</span></div>
        <p class="def">Stored contract mean <b>${storedExact}</b>${rollup ? ` (${rollup.contractMicro} millionths, ${rollup.sum} // ${rollup.count})` : ""}. Standard sum Σ(p−o)². Uniform <b>${fmt6(UNIFORM_SUM)}</b>.</p>
        <p class="def">Per-class mean (that sum ÷ 3) <b>${perClass}</b>. Uniform <b>${fmt6(UNIFORM_CLASS)}</b>. Not stored.</p>
        <div class="mini">
          <div><b>${chain().matches.length}</b><span>matches on chain</span></div>
          <div><b>${scored}</b><span>scored</span></div>
          <div><b>${(chain().receipts || []).length}</b><span>access receipts</span></div>
        </div>
      </section>
      ${live && live.reveal ? pitchCard(live) : `<section class="card"><p>No pre-kickoff fixture is on this contract.</p></section>`}
    </div>
    ${scoredTable()}
    ${receiptCards()}`;
}

function renderFixtures() {
  setRoute("fixtures");
  const rows = chain().matches.slice().sort((a, b) => a.source_kickoff_ms - b.source_kickoff_ms).map((match) => {
    const label = match.historical_replay
      ? `<span class="tag y">replay</span>`
      : `<span class="tag">before kickoff</span>`;
    const result = match.result ? `${match.result.home_goals}–${match.result.away_goals}` : "—";
    return `
      <tr>
        <td><div class="m"><a href="#fixture/${esc(match.match_id)}">${esc(match.home)} – ${esc(match.away)}</a></div><div class="sbl">${esc(match.match_id)}</div></td>
        <td>${match.reveal ? forecastCells(match.reveal) : "—"}</td>
        <td class="res">${result}</td>
        <td class="bs">${match.score ? brierText(match.score.brier_micro, 4) : "—"}</td>
        <td>${label}</td>
      </tr>`;
  }).join("");
  app.innerHTML = `
    <h2 style="margin-top:0">Fixtures <small>OpenLigaDB ids · the chain stores the commitment, not the club names alone</small></h2>
    <table>
      <thead><tr><th>Match</th><th>Forecast H / D / A</th><th>Result</th><th>Brier</th><th>Label</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderFixture(id) {
  setRoute("fixtures");
  const match = chain().matches.find((item) => item.match_id === id);
  if (!match) {
    app.innerHTML = `<section class="card"><p>No fixture ${esc(id)}.</p></section>`;
    return;
  }
  const txs = matchTx(id);
  const reveal = match.reveal;
  const payload = reveal ? `${match.match_id}|${reveal.home_bps}|${reveal.draw_bps}|${reveal.away_bps}|${reveal.salt}` : "Hidden until reveal.";
  const links = [
    ["Register", txs.register],
    ["Commit", txs.commit],
    ["Reveal", txs.reveal],
    ["Result", txs.result],
    ["Score", txs.score],
  ];
  app.innerHTML = `
    <p class="sbl" style="margin-bottom:12px"><a href="#fixtures">All fixtures</a></p>
    ${reveal ? pitchCard(match) : ""}
    <div class="top" style="margin-top:16px">
      <section class="card prose">
        <div class="lbl">Canonical reveal string</div>
        <p class="formula">${esc(payload)}</p>
        <p class="sbl" style="margin-top:10px">Commitment ${esc(match.commitment || "—")}</p>
        <p class="sbl"><a href="${esc(match.source_url)}">${esc(match.source_url.replace("https://", ""))}</a></p>
      </section>
      <section class="card">
        <div class="lbl">Transaction trail</div>
        ${links.map(([label, hash]) => `<div class="row rc" style="display:flex;justify-content:space-between;padding:8px 0;border-top:1px dashed var(--line);font:12px var(--mono)"><span>${label}</span><b>${hash ? txAnchor(hash) : "—"}</b></div>`).join("")}
      </section>
    </div>`;
}

function renderAccess() {
  setRoute("access");
  const cards = (chain().receipts || []).map((receipt) => {
    const quote = (snapshot.quotes || []).find((item) => item.quote_hash === receipt.quote_hash) || {};
    const hash = quote.transaction || (snapshot.transactions.receipts || {})[receipt.buyer_label];
    const match = chain().matches.find((item) => item.match_id === receipt.match_id);
    const amountIn = Number(receipt.amount_in_formatted).toFixed(2);
    const amountOut = Number(receipt.amount_out_formatted).toFixed(4);
    return `
      <article class="card rc">
        <div class="lbl">${esc(receipt.buyer_label)} <span class="tag y">dry</span></div>
        <div class="big">${amountIn} USDC → ${amountOut} wNEAR</div>
        <div class="row"><span>match</span><b><a href="#fixture/${esc(receipt.match_id)}">${esc(match ? `${clubNick(match.home)} vs ${clubNick(match.away)}` : receipt.match_id)}</a></b></div>
        <div class="row"><span>origin</span><b>${esc(receipt.origin_asset)}</b></div>
        <div class="row"><span>destination</span><b>${esc(receipt.destination_asset)}</b></div>
        <div class="row"><span>quote hash</span><b>${esc(receipt.quote_hash)}</b></div>
        <div class="row"><span>tx</span>${txAnchor(hash)}</div>
      </article>`;
  }).join("");
  app.innerHTML = `
    <h2 style="margin-top:0">Access quotes <small>NEAR Intents 1Click · dry: true · no deposit address · no tokens moved</small></h2>
    <div class="stack">${cards}</div>`;
}

function renderMethod() {
  setRoute("method");
  const rollup = scoreRollup();
  const rows = scoredMatches().map((match) => {
    const stored = Number(match.score.brier_micro);
    const micro = recomputeMicro(match.reveal, match.result.outcome);
    const same = micro === stored;
    return `
      <tr>
        <td><div class="m">${esc(tableName(match))}</div><div class="sbl">${match.reveal.home_bps}/${match.reveal.draw_bps}/${match.reveal.away_bps} bps · ${esc(String(match.result.outcome).toLowerCase())}</div></td>
        <td class="bs">${stored}</td>
        <td class="bs">${micro === null ? "—" : micro}</td>
        <td>${fmt6(stored / 1e6)}</td>
        <td>${fmt6(stored / 1e6 / 3)}</td>
        <td>${same ? "matches chain" : "mismatch"}</td>
      </tr>`;
  }).join("");
  app.innerHTML = `
    <h2 style="margin-top:0">Method <small>standard sum, recomputed in the browser from the revealed basis points</small></h2>
    <div class="top">
      <section class="card prose">
        <div class="lbl">Commitment</div>
        <p>The agent commits the SHA-256 of this string before the deadline, then reveals the same string:</p>
        <p class="formula">match_id|home_bps|draw_bps|away_bps|salt</p>
        <p>Basis points sum to 10000. The contract recomputes the hash on reveal.</p>
      </section>
      <section class="card prose">
        <div class="lbl">Two Brier definitions</div>
        <p class="formula">standard sum = (p_home − o_home)² + (p_draw − o_draw)² + (p_away − o_away)²
millionths = sum_i (p_bps − o_bps)² // 100
uniform sum = 2/3 = ${fmt6(UNIFORM_SUM)}
per-class mean = standard sum / 3
uniform per-class = 2/9 = ${fmt6(UNIFORM_CLASS)}</p>
        <p>The contract stores the standard sum. It does not store the per-class mean. ${rollup ? `Stored millionths sum to ${rollup.sum}. The contract mean is ${rollup.sum} // ${rollup.count} = ${rollup.contractMicro} (${storedExact(rollup)}). The exact average is ${rollup.sum} / ${rollup.count} / 1000000 = ${(rollup.sum / rollup.count / 1e6).toFixed(9)}.` : ""}</p>
      </section>
    </div>
    <table>
      <thead><tr><th>Match</th><th>Stored</th><th>Recomputed</th><th>Standard sum</th><th>Per-class mean</th><th>Check</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function storedExact(rollup) {
  return fmt6(rollup.contractMicro / 1e6);
}

function render() {
  const hash = location.hash || "#track";
  seasonPill.textContent = seasonLabel();
  if (!snapshot || !snapshot.contract_id || !chain().matches) {
    setRoute("track");
    app.innerHTML = `<section class="card"><p>This build has no recorded testnet state yet.</p></section>`;
    return;
  }
  if (hash.startsWith("#fixture/")) renderFixture(decodeURIComponent(hash.slice("#fixture/".length)));
  else if (hash.startsWith("#fixtures")) renderFixtures();
  else if (hash.startsWith("#access")) renderAccess();
  else if (hash.startsWith("#method")) renderMethod();
  else renderTrack();
  const shot = new URLSearchParams(location.search).get("shot");
  if (shot === "lower") {
    const target = document.getElementById("scored");
    if (target) target.scrollIntoView();
  }
}

async function loadSnapshot() {
  const response = await fetch("snapshot.json", { cache: "no-store" });
  snapshot = await response.json();
  accountLink.textContent = snapshot.contract_id || "testnet";
  accountLink.href = accountUrl(snapshot.contract_id);
  footState.textContent = "snapshot";
  render();
  if (!snapshot.contract_id) return;
  try {
    const body = {
      jsonrpc: "2.0",
      id: "crunch",
      method: "query",
      params: {
        request_type: "call_function",
        finality: "final",
        account_id: snapshot.contract_id,
        method_name: "export_public",
        args_base64: btoa("{}"),
      },
    };
    const rpc = await fetch("https://rpc.testnet.near.org", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const payload = await rpc.json();
    if (!payload.result || !payload.result.result) throw new Error("empty view");
    const text = new TextDecoder().decode(new Uint8Array(payload.result.result));
    snapshot.chain = JSON.parse(text);
    snapshot.chain_source = "rpc.testnet.near.org";
    footState.textContent = "testnet · live";
    render();
  } catch (error) {
    accountPill.classList.add("warn");
    footState.textContent = "snapshot · rpc unreachable";
  }
}

function closeTips() {
  document.querySelectorAll(".tip.open, .tip.show").forEach((node) => {
    node.classList.remove("open", "show");
    const button = node.querySelector(".q");
    if (button) button.setAttribute("aria-expanded", "false");
  });
}

function openTip(tip, pinned) {
  closeTips();
  tip.classList.add(pinned ? "open" : "show");
  const button = tip.querySelector(".q");
  if (button) button.setAttribute("aria-expanded", "true");
}

document.addEventListener("click", (event) => {
  const button = event.target.closest(".q");
  if (button) {
    event.preventDefault();
    event.stopPropagation();
    const tip = button.closest(".tip");
    if (tip.classList.contains("open")) closeTips();
    else openTip(tip, true);
    return;
  }
  if (!event.target.closest(".pop")) closeTips();
});

document.addEventListener("mouseover", (event) => {
  const tip = event.target.closest(".tip");
  if (!tip || tip.classList.contains("open")) return;
  document.querySelectorAll(".tip.show").forEach((node) => node.classList.remove("show"));
  tip.classList.add("show");
});

document.addEventListener("mouseout", (event) => {
  const tip = event.target.closest(".tip");
  if (!tip || tip.contains(event.relatedTarget) || tip.classList.contains("open")) return;
  tip.classList.remove("show");
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeTips();
});

window.addEventListener("hashchange", () => {
  window.scrollTo(0, 0);
  render();
});
loadSnapshot().then(() => {
  const open = new URLSearchParams(location.search).get("open");
  if (!open) return;
  const tip = document.querySelector(`.tip[data-tip="${open}"]`);
  if (tip) openTip(tip, true);
});

if (new URLSearchParams(location.search).get("demo") === "1") {
  const frames = ["#track", "#fixture/bl1-2026-83232", "#access", "#method"];
  let frame = 0;
  setInterval(() => {
    frame = (frame + 1) % frames.length;
    location.hash = frames[frame];
  }, 4000);
}
