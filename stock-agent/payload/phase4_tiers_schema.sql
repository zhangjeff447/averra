CREATE TABLE IF NOT EXISTS tier_research_results (
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  tier TEXT NOT NULL CHECK(tier IN ('INITIAL','DEEP')),
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  research_score REAL,
  catalyst_score REAL,
  thesis TEXT,
  confidence TEXT,
  evidence_hash TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL,
  completed_at TEXT NOT NULL,
  response_json TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker,tier)
);
CREATE TRIGGER IF NOT EXISTS tier_result_immutable_update BEFORE UPDATE ON tier_research_results
BEGIN SELECT RAISE(ABORT,'tier result is immutable'); END;
CREATE TRIGGER IF NOT EXISTS tier_result_immutable_delete BEFORE DELETE ON tier_research_results
BEGIN SELECT RAISE(ABORT,'tier result is immutable'); END;

CREATE TABLE IF NOT EXISTS tier_escalations (
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  score REAL NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN ('NO_ESCALATION','ESCALATE_SOL')),
  reasons_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE TRIGGER IF NOT EXISTS tier_escalation_immutable_update BEFORE UPDATE ON tier_escalations
BEGIN SELECT RAISE(ABORT,'escalation is immutable'); END;

CREATE TABLE IF NOT EXISTS tier_final_results (
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  final_research_tier TEXT NOT NULL CHECK(final_research_tier IN ('INITIAL','DEEP','INITIAL_FALLBACK','UNAVAILABLE')),
  status TEXT NOT NULL,
  source_tier TEXT CHECK(source_tier IN ('INITIAL','DEEP')),
  final_research_score REAL,
  final_catalyst_score REAL,
  final_thesis TEXT,
  final_confidence TEXT,
  decided_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE TRIGGER IF NOT EXISTS tier_final_immutable_update BEFORE UPDATE ON tier_final_results
BEGIN SELECT RAISE(ABORT,'final result is immutable'); END;

CREATE TABLE IF NOT EXISTS tier_deep_limitations (
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  limitation_code TEXT NOT NULL CHECK(limitation_code='DEEP_RESEARCH_LIMITATION'),
  reasons_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);

CREATE TABLE IF NOT EXISTS tier_budget_ledger (
  id INTEGER PRIMARY KEY,
  usage_day TEXT NOT NULL,
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  tier TEXT NOT NULL CHECK(tier IN ('INITIAL','DEEP')),
  state TEXT NOT NULL CHECK(state IN ('RESERVED','IN_FLIGHT','COMPLETED','FAILED_NO_REQUEST','FAILED_UNKNOWN','BUDGET_BLOCKED','CONFIRMED_BILLED','CONFIRMED_NO_CHARGE')),
  estimated_input_tokens INTEGER NOT NULL,
  estimated_output_tokens INTEGER NOT NULL,
  estimated_cost_usd REAL NOT NULL,
  actual_input_tokens INTEGER,
  actual_output_tokens INTEGER,
  confirmed_cost_usd REAL,
  request_id TEXT,
  reconciliation_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(run_id,ticker,tier)
);
CREATE INDEX IF NOT EXISTS tier_budget_day ON tier_budget_ledger(usage_day,tier);

CREATE TABLE IF NOT EXISTS tier_forward_snapshots (
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  tier TEXT NOT NULL CHECK(tier IN ('INITIAL','DEEP','FINAL')),
  research_completed_at TEXT NOT NULL,
  anchor_date TEXT NOT NULL,
  research_score REAL NOT NULL,
  stock_close REAL NOT NULL,
  spy_close REAL NOT NULL,
  qqq_close REAL NOT NULL,
  PRIMARY KEY(run_id,ticker,tier)
);
CREATE TABLE IF NOT EXISTS tier_forward_observations (
  run_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  tier TEXT NOT NULL,
  horizon INTEGER NOT NULL,
  end_date TEXT NOT NULL,
  stock_return REAL NOT NULL,
  excess_spy REAL NOT NULL,
  excess_qqq REAL NOT NULL,
  PRIMARY KEY(run_id,ticker,tier,horizon),
  FOREIGN KEY(run_id,ticker,tier) REFERENCES tier_forward_snapshots(run_id,ticker,tier)
);

CREATE TABLE IF NOT EXISTS tier_ab_cases (
  case_id TEXT PRIMARY KEY,
  ticker TEXT NOT NULL,
  baseline_model TEXT NOT NULL,
  trial_model TEXT NOT NULL,
  evidence_hash TEXT NOT NULL,
  quant_summary_hash TEXT NOT NULL,
  fundamental_summary_hash TEXT NOT NULL,
  task_hash TEXT NOT NULL,
  output_schema_hash TEXT NOT NULL,
  web_search_enabled INTEGER NOT NULL CHECK(web_search_enabled=0),
  expected_catalyst_ids_json TEXT NOT NULL,
  expected_risk_ids_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
