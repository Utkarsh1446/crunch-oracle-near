//! Crunch Oracle — commit, reveal, and score football forecasts on NEAR.
//!
//! A forecast is three probabilities in basis points (home, draw, away) that
//! sum to 10_000. The agent commits `sha256(match_id|home|draw|away|salt)`
//! before the match deadline, reveals the same payload before that deadline,
//! and anyone can score it after the oracle posts the final score.
//!
//! The score is the multiclass Brier score, stored in millionths:
//! `sum_i (p_i - o_i)^2` with probabilities in [0, 1]. Lower is better.
//! A perfect call is 0. A uniform 1/3 forecast scores about 0.666667.
//!
//! Historical matches are labelled on chain. `historical_replay` is true when
//! the declared source kickoff is already in the past at registration, so a
//! replay cannot be mistaken for a forecast that was fixed before the real
//! kickoff. Upcoming matches use the published kickoff as the deadline.

use std::collections::BTreeMap;

use near_sdk::{env, near, require, AccountId, PanicOnDefault};

const BPS: u64 = 10_000;
const MAX_MATCHES: u32 = 32;
const MAX_RECEIPTS: u32 = 32;
const MAX_DEADLINE_AHEAD_MS: u64 = 21 * 24 * 60 * 60 * 1000;

#[near(serializers = [borsh, json])]
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Outcome {
    Home,
    Draw,
    Away,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reveal {
    pub home_bps: u16,
    pub draw_bps: u16,
    pub away_bps: u16,
    pub salt: String,
    pub revealed_at_ms: u64,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct FinalResult {
    pub home_goals: u8,
    pub away_goals: u8,
    pub outcome: Outcome,
    pub posted_at_ms: u64,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScoreRecord {
    pub brier_micro: u64,
    pub scored_at_ms: u64,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct MatchRecord {
    pub match_id: String,
    pub competition: String,
    pub season: String,
    pub home: String,
    pub away: String,
    pub source_kickoff_ms: u64,
    pub deadline_ms: u64,
    pub source_url: String,
    pub historical_replay: bool,
    pub agent: Option<AccountId>,
    pub commitment: Option<String>,
    pub committed_at_ms: Option<u64>,
    pub reveal: Option<Reveal>,
    pub result: Option<FinalResult>,
    pub score: Option<ScoreRecord>,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct AccessReceipt {
    pub id: u64,
    pub buyer_label: String,
    pub match_id: String,
    pub origin_asset: String,
    pub destination_asset: String,
    pub amount_in: String,
    pub amount_out: String,
    pub amount_in_formatted: String,
    pub amount_out_formatted: String,
    pub quote_hash: String,
    pub dry: bool,
    pub recorded_at_ms: u64,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct AgentStats {
    pub agent: AccountId,
    pub committed: u32,
    pub revealed: u32,
    pub scored: u32,
    pub brier_micro_sum: u64,
    pub mean_brier_micro: Option<u64>,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct PublicState {
    pub owner: AccountId,
    pub oracle: AccountId,
    pub match_count: u32,
    pub receipt_count: u32,
    pub matches: Vec<MatchRecord>,
    pub receipts: Vec<AccessReceipt>,
    pub agents: Vec<AgentStats>,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Contract {
    owner: AccountId,
    oracle: AccountId,
    matches: BTreeMap<String, MatchRecord>,
    receipts: BTreeMap<u64, AccessReceipt>,
    next_receipt_id: u64,
    agents: BTreeMap<AccountId, AgentStats>,
}

#[near]
impl Contract {
    #[init]
    pub fn new(owner: AccountId, oracle: AccountId) -> Self {
        Self {
            owner,
            oracle,
            matches: BTreeMap::new(),
            receipts: BTreeMap::new(),
            next_receipt_id: 1,
            agents: BTreeMap::new(),
        }
    }

    pub fn register_match(
        &mut self,
        match_id: String,
        competition: String,
        season: String,
        home: String,
        away: String,
        source_kickoff_ms: u64,
        deadline_ms: u64,
        source_url: String,
    ) {
        self.assert_owner();
        require!(self.matches.len() < MAX_MATCHES as usize, "match cap");
        require!(self.matches.get(&match_id).is_none(), "match exists");
        assert_id(&match_id);
        assert_label(&competition, 2, 32);
        assert_label(&season, 4, 8);
        assert_name(&home);
        assert_name(&away);
        require!(home != away, "teams must differ");
        require!(
            source_url.starts_with("https://api.openligadb.de/"),
            "source must be OpenLigaDB"
        );
        require!(source_url.len() <= 180, "source url too long");
        let now = now_ms();
        require!(deadline_ms > now, "deadline must be in the future");
        require!(
            deadline_ms - now <= MAX_DEADLINE_AHEAD_MS,
            "deadline too far ahead"
        );
        let historical_replay = source_kickoff_ms < now;
        let record = MatchRecord {
            match_id: match_id.clone(),
            competition,
            season,
            home,
            away,
            source_kickoff_ms,
            deadline_ms,
            source_url,
            historical_replay,
            agent: None,
            commitment: None,
            committed_at_ms: None,
            reveal: None,
            result: None,
            score: None,
        };
        env::log_str(&format!(
            "register match_id={match_id} historical_replay={historical_replay} deadline_ms={deadline_ms}"
        ));
        self.matches.insert(match_id, record);
    }

    pub fn commit_forecast(&mut self, match_id: String, commitment: String) {
        assert_id(&match_id);
        assert_hex64(&commitment);
        let now = now_ms();
        let agent = env::predecessor_account_id();
        let record = self.matches.get(&match_id).expect("unknown match").clone();
        require!(now < record.deadline_ms, "commit window closed");
        require!(record.commitment.is_none(), "already committed");
        let mut record = record;
        record.agent = Some(agent.clone());
        record.commitment = Some(commitment.clone());
        record.committed_at_ms = Some(now);
        self.matches.insert(match_id.clone(), record);
        self.bump_agent(&agent, |stats| stats.committed += 1);
        env::log_str(&format!(
            "commit match_id={match_id} agent={agent} commitment={commitment}"
        ));
    }

    pub fn reveal_forecast(
        &mut self,
        match_id: String,
        home_bps: u16,
        draw_bps: u16,
        away_bps: u16,
        salt: String,
    ) {
        assert_id(&match_id);
        assert_probabilities(home_bps, draw_bps, away_bps);
        assert_salt(&salt);
        let now = now_ms();
        let agent = env::predecessor_account_id();
        let record = self.matches.get(&match_id).expect("unknown match").clone();
        require!(now < record.deadline_ms, "reveal window closed");
        require!(record.agent.as_ref() == Some(&agent), "only the committing agent");
        let expected = record.commitment.clone().expect("commit first");
        let payload = canonical_payload(&match_id, home_bps, draw_bps, away_bps, &salt);
        let got = sha256_hex(payload.as_bytes());
        require!(got == expected, "commitment mismatch");
        let mut record = record;
        record.reveal = Some(Reveal {
            home_bps,
            draw_bps,
            away_bps,
            salt,
            revealed_at_ms: now,
        });
        self.matches.insert(match_id.clone(), record);
        self.bump_agent(&agent, |stats| stats.revealed += 1);
        env::log_str(&format!(
            "reveal match_id={match_id} home_bps={home_bps} draw_bps={draw_bps} away_bps={away_bps}"
        ));
    }

    pub fn post_result(&mut self, match_id: String, home_goals: u8, away_goals: u8) {
        self.assert_oracle();
        assert_id(&match_id);
        require!(home_goals <= 20 && away_goals <= 20, "unlikely score");
        let now = now_ms();
        let record = self.matches.get(&match_id).expect("unknown match").clone();
        require!(record.reveal.is_some(), "reveal before the result");
        require!(now >= record.deadline_ms, "deadline has not passed");
        require!(record.result.is_none(), "result already posted");
        let outcome = outcome_of(home_goals, away_goals);
        let mut record = record;
        record.result = Some(FinalResult {
            home_goals,
            away_goals,
            outcome,
            posted_at_ms: now,
        });
        self.matches.insert(match_id.clone(), record);
        env::log_str(&format!(
            "result match_id={match_id} home_goals={home_goals} away_goals={away_goals}"
        ));
    }

    pub fn score_forecast(&mut self, match_id: String) -> u64 {
        assert_id(&match_id);
        let record = self.matches.get(&match_id).expect("unknown match").clone();
        require!(record.score.is_none(), "already scored");
        let reveal = record.reveal.clone().expect("reveal first");
        let result = record.result.clone().expect("result first");
        let brier = multiclass_brier_micro(
            reveal.home_bps,
            reveal.draw_bps,
            reveal.away_bps,
            result.outcome,
        );
        let agent = record.agent.clone().expect("agent");
        let mut record = record;
        record.score = Some(ScoreRecord {
            brier_micro: brier,
            scored_at_ms: now_ms(),
        });
        self.matches.insert(match_id.clone(), record);
        self.bump_agent(&agent, |stats| {
            stats.scored += 1;
            stats.brier_micro_sum += brier;
            stats.mean_brier_micro = Some(stats.brier_micro_sum / u64::from(stats.scored));
        });
        env::log_str(&format!("score match_id={match_id} brier_micro={brier}"));
        brier
    }

    /// Record a NEAR Intents 1Click quote for access to one committed forecast.
    /// `dry` must be true: this deployment never records a settled payment.
    pub fn record_access(
        &mut self,
        buyer_label: String,
        match_id: String,
        origin_asset: String,
        destination_asset: String,
        amount_in: String,
        amount_out: String,
        amount_in_formatted: String,
        amount_out_formatted: String,
        quote_hash: String,
        dry: bool,
    ) -> u64 {
        self.assert_owner();
        require!(dry, "only dry quotes can be recorded");
        require!(self.receipts.len() < MAX_RECEIPTS as usize, "receipt cap");
        assert_id(&match_id);
        assert_buyer(&buyer_label);
        assert_asset(&origin_asset);
        assert_asset(&destination_asset);
        assert_digits(&amount_in, 40);
        assert_digits(&amount_out, 40);
        assert_decimal(&amount_in_formatted, 32);
        assert_decimal(&amount_out_formatted, 32);
        assert_hex64(&quote_hash);
        let record = self.matches.get(&match_id).expect("unknown match");
        require!(record.commitment.is_some(), "forecast is not committed");
        let id = self.next_receipt_id;
        self.next_receipt_id += 1;
        let receipt = AccessReceipt {
            id,
            buyer_label,
            match_id: match_id.clone(),
            origin_asset,
            destination_asset,
            amount_in,
            amount_out,
            amount_in_formatted,
            amount_out_formatted,
            quote_hash,
            dry,
            recorded_at_ms: now_ms(),
        };
        env::log_str(&format!("access id={id} match_id={match_id} dry=true"));
        self.receipts.insert(id, receipt);
        id
    }

    pub fn get_match(&self, match_id: String) -> Option<MatchRecord> {
        self.matches.get(&match_id).cloned()
    }

    pub fn get_receipt(&self, id: u64) -> Option<AccessReceipt> {
        self.receipts.get(&id).cloned()
    }

    pub fn get_agent(&self, agent: AccountId) -> Option<AgentStats> {
        self.agents.get(&agent).cloned()
    }

    pub fn export_public(&self) -> PublicState {
        PublicState {
            owner: self.owner.clone(),
            oracle: self.oracle.clone(),
            match_count: self.matches.len() as u32,
            receipt_count: self.receipts.len() as u32,
            matches: self.matches.values().cloned().collect(),
            receipts: self.receipts.values().cloned().collect(),
            agents: self.agents.values().cloned().collect(),
        }
    }
}

impl Contract {
    fn assert_owner(&self) {
        require!(env::predecessor_account_id() == self.owner, "owner only");
    }

    fn assert_oracle(&self) {
        let caller = env::predecessor_account_id();
        require!(caller == self.oracle || caller == self.owner, "oracle only");
    }

    fn bump_agent(&mut self, agent: &AccountId, update: impl FnOnce(&mut AgentStats)) {
        let mut stats = self.agents.get(agent).cloned().unwrap_or(AgentStats {
            agent: agent.clone(),
            committed: 0,
            revealed: 0,
            scored: 0,
            brier_micro_sum: 0,
            mean_brier_micro: None,
        });
        update(&mut stats);
        self.agents.insert(agent.clone(), stats);
    }
}

pub fn multiclass_brier_micro(
    home_bps: u16,
    draw_bps: u16,
    away_bps: u16,
    outcome: Outcome,
) -> u64 {
    let probs = [i64::from(home_bps), i64::from(draw_bps), i64::from(away_bps)];
    let idx = match outcome {
        Outcome::Home => 0,
        Outcome::Draw => 1,
        Outcome::Away => 2,
    };
    let mut sum: i64 = 0;
    for (i, probability) in probs.iter().enumerate() {
        let observed = if i == idx { BPS as i64 } else { 0 };
        let delta = probability - observed;
        sum += delta * delta;
    }
    sum as u64 / 100
}

fn outcome_of(home_goals: u8, away_goals: u8) -> Outcome {
    match home_goals.cmp(&away_goals) {
        std::cmp::Ordering::Greater => Outcome::Home,
        std::cmp::Ordering::Equal => Outcome::Draw,
        std::cmp::Ordering::Less => Outcome::Away,
    }
}

fn canonical_payload(
    match_id: &str,
    home_bps: u16,
    draw_bps: u16,
    away_bps: u16,
    salt: &str,
) -> String {
    format!("{match_id}|{home_bps}|{draw_bps}|{away_bps}|{salt}")
}

fn sha256_hex(bytes: &[u8]) -> String {
    let hash = env::sha256(bytes);
    let mut out = String::with_capacity(64);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for byte in hash {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 0x0f) as usize] as char);
    }
    out
}

fn now_ms() -> u64 {
    env::block_timestamp() / 1_000_000
}

fn assert_id(value: &str) {
    require!(
        (8..=48).contains(&value.len()),
        "match id length"
    );
    require!(
        value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'),
        "match id charset"
    );
}

fn assert_label(value: &str, min: usize, max: usize) {
    require!(value.len() >= min && value.len() <= max, "bad label");
    require!(
        value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit()),
        "bad label charset"
    );
}

fn assert_name(value: &str) {
    require!((2..=48).contains(&value.len()), "bad team name");
    require!(
        value
            .chars()
            .all(|ch| ch.is_alphanumeric() || matches!(ch, ' ' | '.' | '-' | '\'')),
        "bad team name charset"
    );
}

fn assert_probabilities(home_bps: u16, draw_bps: u16, away_bps: u16) {
    let sum = u32::from(home_bps) + u32::from(draw_bps) + u32::from(away_bps);
    require!(sum == 10_000, "probabilities must sum to 10000 bps");
}

fn assert_salt(salt: &str) {
    require!((16..=64).contains(&salt.len()), "salt length");
    require!(
        salt.bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()),
        "salt must be lowercase hex"
    );
}

fn assert_hex64(value: &str) {
    require!(value.len() == 64, "expected 64 hex chars");
    require!(
        value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase()),
        "lowercase hex"
    );
}

fn assert_buyer(value: &str) {
    require!((3..=40).contains(&value.len()), "buyer label length");
    require!(
        value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-'),
        "buyer label charset"
    );
}

fn assert_asset(value: &str) {
    require!(
        value.starts_with("nep141:") && (12..=120).contains(&value.len()),
        "asset id"
    );
    require!(
        value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b':' | b'.' | b'_' | b'-')),
        "asset charset"
    );
}

fn assert_digits(value: &str, max: usize) {
    require!(!value.is_empty() && value.len() <= max, "bad amount");
    require!(value.bytes().all(|byte| byte.is_ascii_digit()), "digits");
}

fn assert_decimal(value: &str, max: usize) {
    require!(!value.is_empty() && value.len() <= max, "bad formatted amount");
    require!(
        value.bytes().all(|byte| byte.is_ascii_digit() || byte == b'.'),
        "formatted amount"
    );
    require!(value.bytes().filter(|byte| *byte == b'.').count() <= 1, "one dot");
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::VMContextBuilder;
    use near_sdk::testing_env;

    const NOW_MS: u64 = 1_763_100_000_000;
    const SALT: &str = "abababababababababababababababab";
    const HASH: &str = "731ce143d28fe73400d26797feefe89b6c572d2e4e7fc0e879cb7b16a944444d";

    fn use_context(predecessor: &str, now: u64) {
        let mut builder = VMContextBuilder::new();
        builder
            .current_account_id("crunch.testnet".parse().unwrap())
            .predecessor_account_id(predecessor.parse().unwrap())
            .signer_account_id(predecessor.parse().unwrap())
            .block_timestamp(now * 1_000_000);
        testing_env!(builder.build());
    }

    fn contract() -> Contract {
        use_context("owner.testnet", NOW_MS);
        Contract::new("owner.testnet".parse().unwrap(), "oracle.testnet".parse().unwrap())
    }

    fn register_replay(contract: &mut Contract, match_id: &str) {
        use_context("owner.testnet", NOW_MS);
        contract.register_match(
            match_id.to_string(),
            "bundesliga".to_string(),
            "2026".to_string(),
            "TSG Hoffenheim".to_string(),
            "FC Schalke 04".to_string(),
            NOW_MS - 86_400_000,
            NOW_MS + 120_000,
            format!("https://api.openligadb.de/getmatchdata/{match_id}"),
        );
    }

    #[test]
    fn brier_vector_matches_python() {
        assert_eq!(
            multiclass_brier_micro(5000, 3000, 2000, Outcome::Home),
            380_000
        );
        // (0.5-0)^2 + (0.3-1)^2 + (0.2-0)^2 = 0.25 + 0.49 + 0.04 = 0.78
        assert_eq!(
            multiclass_brier_micro(5000, 3000, 2000, Outcome::Draw),
            780_000
        );
        // (0.5-0)^2 + (0.3-0)^2 + (0.2-1)^2 = 0.25 + 0.09 + 0.64 = 0.98
        assert_eq!(
            multiclass_brier_micro(5000, 3000, 2000, Outcome::Away),
            980_000
        );
    }

    #[test]
    fn commitment_hash_matches_python() {
        use_context("owner.testnet", NOW_MS);
        let payload = canonical_payload("bl1-2026-1", 5000, 3000, 2000, SALT);
        assert_eq!(sha256_hex(payload.as_bytes()), HASH);
    }

    #[test]
    fn replay_is_labelled_and_scored() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        let stored = oracle.get_match("bl1-2026-1".to_string()).unwrap();
        assert!(stored.historical_replay);

        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        oracle.reveal_forecast("bl1-2026-1".to_string(), 5000, 3000, 2000, SALT.to_string());

        use_context("oracle.testnet", NOW_MS + 120_000);
        oracle.post_result("bl1-2026-1".to_string(), 2, 1);
        let brier = oracle.score_forecast("bl1-2026-1".to_string());
        assert_eq!(brier, 380_000);
        let stats = oracle
            .get_agent("agent.testnet".parse().unwrap())
            .unwrap();
        assert_eq!(stats.committed, 1);
        assert_eq!(stats.revealed, 1);
        assert_eq!(stats.scored, 1);
        assert_eq!(stats.mean_brier_micro, Some(380_000));
        let exported = oracle.export_public();
        assert_eq!(exported.match_count, 1);
        assert_eq!(exported.matches[0].score.as_ref().unwrap().brier_micro, 380_000);
    }

    #[test]
    fn upcoming_match_is_not_a_replay() {
        let mut oracle = contract();
        use_context("owner.testnet", NOW_MS);
        oracle.register_match(
            "bl1-2026-83237".to_string(),
            "bundesliga".to_string(),
            "2026".to_string(),
            "FC Bayern Munchen".to_string(),
            "1. FC Koln".to_string(),
            NOW_MS + 5 * 86_400_000,
            NOW_MS + 5 * 86_400_000,
            "https://api.openligadb.de/getmatchdata/83237".to_string(),
        );
        let stored = oracle.get_match("bl1-2026-83237".to_string()).unwrap();
        assert!(!stored.historical_replay);
    }

    #[test]
    #[should_panic(expected = "commitment mismatch")]
    fn wrong_salt_is_rejected() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        oracle.reveal_forecast(
            "bl1-2026-1".to_string(),
            5000,
            3000,
            2000,
            "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb".to_string(),
        );
    }

    #[test]
    #[should_panic(expected = "reveal window closed")]
    fn reveal_after_deadline_is_rejected() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        use_context("agent.testnet", NOW_MS + 120_000);
        oracle.reveal_forecast("bl1-2026-1".to_string(), 5000, 3000, 2000, SALT.to_string());
    }

    #[test]
    #[should_panic(expected = "deadline has not passed")]
    fn result_before_deadline_is_rejected() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        oracle.reveal_forecast("bl1-2026-1".to_string(), 5000, 3000, 2000, SALT.to_string());
        use_context("oracle.testnet", NOW_MS + 2_000);
        oracle.post_result("bl1-2026-1".to_string(), 1, 1);
    }

    #[test]
    #[should_panic(expected = "probabilities must sum to 10000 bps")]
    fn probabilities_must_be_normalized() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        oracle.reveal_forecast("bl1-2026-1".to_string(), 5000, 3000, 1000, SALT.to_string());
    }

    #[test]
    #[should_panic(expected = "only dry quotes can be recorded")]
    fn settled_quote_is_rejected() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        use_context("owner.testnet", NOW_MS + 1_500);
        oracle.record_access(
            "demo-reader".to_string(),
            "bl1-2026-1".to_string(),
            "nep141:wrap.near".to_string(),
            "nep141:wrap.near".to_string(),
            "1000000".to_string(),
            "1000000".to_string(),
            "1.0".to_string(),
            "1.0".to_string(),
            HASH.to_string(),
            false,
        );
    }

    #[test]
    fn dry_access_receipt_is_public() {
        let mut oracle = contract();
        register_replay(&mut oracle, "bl1-2026-1");
        use_context("agent.testnet", NOW_MS + 1_000);
        oracle.commit_forecast("bl1-2026-1".to_string(), HASH.to_string());
        use_context("owner.testnet", NOW_MS + 1_500);
        let id = oracle.record_access(
            "demo-reader".to_string(),
            "bl1-2026-1".to_string(),
            "nep141:eth-0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48.omft.near".to_string(),
            "nep141:wrap.near".to_string(),
            "1000000".to_string(),
            "250000000000000000000000".to_string(),
            "1.0".to_string(),
            "0.25".to_string(),
            HASH.to_string(),
            true,
        );
        let receipt = oracle.get_receipt(id).unwrap();
        assert!(receipt.dry);
        assert_eq!(receipt.match_id, "bl1-2026-1");
        assert_eq!(oracle.export_public().receipt_count, 1);
    }

    #[test]
    #[should_panic(expected = "owner only")]
    fn stranger_cannot_register() {
        let mut oracle = contract();
        use_context("stranger.testnet", NOW_MS);
        oracle.register_match(
            "bl1-2026-9".to_string(),
            "bundesliga".to_string(),
            "2026".to_string(),
            "A Team".to_string(),
            "B Team".to_string(),
            NOW_MS - 1,
            NOW_MS + 1_000,
            "https://api.openligadb.de/getmatchdata/9".to_string(),
        );
    }
}
