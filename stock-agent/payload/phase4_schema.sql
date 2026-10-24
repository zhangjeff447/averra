CREATE TABLE IF NOT EXISTS research_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fundamental_run_id INTEGER NOT NULL,
  quant_run_id INTEGER NOT NULL,
  candidate_scan_date TEXT NOT NULL,
  research_cutoff TEXT NOT NULL,
  created_at TEXT NOT NULL,
  config_json TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_priority (
  run_id INTEGER NOT NULL REFERENCES research_runs(id),
  ticker TEXT NOT NULL,
  priority_rank INTEGER NOT NULL,
  priority_score REAL NOT NULL,
  reasons_json TEXT NOT NULL,
  packet_json TEXT NOT NULL,
  packet_bytes INTEGER NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE TABLE IF NOT EXISTS research_results (
  run_id INTEGER NOT NULL REFERENCES research_runs(id),
  ticker TEXT NOT NULL,
  status TEXT NOT NULL,
  refresh_mode TEXT NOT NULL,
  candidate_score REAL NOT NULL,
  research_score REAL,
  catalyst_score REAL,
  conviction_score REAL,
  confidence TEXT,
  label TEXT NOT NULL,
  thesis_json TEXT,
  events_json TEXT,
  sources_json TEXT,
  flags_json TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  elapsed_seconds REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE INDEX IF NOT EXISTS research_results_history ON research_results(ticker,run_id);
CREATE TABLE IF NOT EXISTS research_token_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  usage_day TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  reserved_input INTEGER NOT NULL,
  reserved_output INTEGER NOT NULL,
  actual_input INTEGER,
  actual_output INTEGER,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_token_day ON research_token_ledger(usage_day);
CREATE TABLE IF NOT EXISTS research_usage_details (
  ledger_id INTEGER PRIMARY KEY REFERENCES research_token_ledger(id),
  estimated_input INTEGER NOT NULL,
  estimated_output INTEGER NOT NULL,
  estimated_model_cost_usd REAL NOT NULL,
  estimated_search_cost_usd REAL NOT NULL,
  estimated_total_cost_usd REAL NOT NULL,
  actual_model_cost_usd REAL,
  actual_search_cost_usd REAL,
  actual_total_cost_usd REAL,
  web_search_calls INTEGER,
  token_breakdown_json TEXT NOT NULL,
  flags_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_response_archive (
  run_id INTEGER NOT NULL,
  ticker TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE TABLE IF NOT EXISTS research_request_state (
  ledger_id INTEGER PRIMARY KEY REFERENCES research_token_ledger(id),
  state TEXT NOT NULL,
  error_category TEXT,
  request_id TEXT,
  request_started_at TEXT,
  connection_established INTEGER,
  response_started INTEGER,
  usage_received INTEGER NOT NULL DEFAULT 0,
  raw_response_saved INTEGER NOT NULL DEFAULT 0,
  reconciled_cost_usd REAL,
  reconciliation_json TEXT
);
CREATE TABLE IF NOT EXISTS research_evidence_cache (
  ticker TEXT NOT NULL,
  cutoff TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  packet_json TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  PRIMARY KEY(ticker,cutoff,input_hash)
);
CREATE TABLE IF NOT EXISTS evidence_source_documents (
  url TEXT PRIMARY KEY,
  ticker TEXT NOT NULL,
  source_type TEXT NOT NULL,
  form TEXT,
  accession TEXT,
  published_at TEXT,
  retrieved_at TEXT NOT NULL,
  content_type TEXT,
  body_zlib BLOB NOT NULL,
  sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_source_errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker TEXT NOT NULL,
  source TEXT NOT NULL,
  request_url TEXT NOT NULL,
  error_class TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  last_success_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence_packets_v2 (
  ticker TEXT NOT NULL,
  evidence_asof TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  packet_json TEXT NOT NULL,
  coverage_pct REAL NOT NULL,
  confidence TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(ticker,evidence_asof,source_hash)
);
CREATE TABLE IF NOT EXISTS evidence_events_v2 (
  ticker TEXT NOT NULL,
  evidence_asof TEXT NOT NULL,
  duplicate_group TEXT NOT NULL,
  event_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(ticker,evidence_asof,duplicate_group)
);
CREATE TABLE IF NOT EXISTS evidence_reasoner_results (
  run_id INTEGER NOT NULL REFERENCES research_runs(id),
  ticker TEXT NOT NULL,
  evidence_asof TEXT NOT NULL,
  research_status TEXT NOT NULL,
  candidate_score REAL NOT NULL,
  research_score REAL,
  catalyst_score REAL,
  conviction_score REAL,
  thesis_confidence TEXT,
  result_json TEXT NOT NULL,
  input_tokens INTEGER,
  output_tokens INTEGER,
  estimated_cost_usd REAL,
  local_cost_usd REAL,
  runtime_seconds REAL NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE TABLE IF NOT EXISTS research_overrides (
  ticker TEXT NOT NULL,
  event_type TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_url TEXT NOT NULL,
  verified_at TEXT NOT NULL,
  verification_note TEXT NOT NULL,
  PRIMARY KEY(ticker,event_type,event_date,source_url)
);
