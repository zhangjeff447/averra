PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('DEPOSIT','BUY','SELL','ADJUSTMENT')),
  ticker TEXT,
  amount_cents INTEGER NOT NULL CHECK(amount_cents != 0),
  shares TEXT,
  fill_price TEXT,
  price_source TEXT,
  note TEXT,
  source TEXT,
  resulting_cash_cents INTEGER,
  realized_pl_cents INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
  ticker TEXT PRIMARY KEY,
  shares TEXT NOT NULL,
  cost_cents INTEGER NOT NULL CHECK(cost_cents >= 0),
  sector TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked_holdings (
  ticker TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('HELD','CLOSED')),
  confirmed_at TEXT NOT NULL,
  closed_at TEXT,
  entry_date TEXT,
  entry_price REAL,
  entry_price_source TEXT,
  entry_thesis_json TEXT,
  exit_price REAL,
  exit_price_source TEXT
);
CREATE TABLE IF NOT EXISTS holding_events (
  id INTEGER PRIMARY KEY,
  ticker TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('BOUGHT','SOLD')),
  confirmed_at TEXT NOT NULL,
  source TEXT NOT NULL,
  price REAL,
  price_source TEXT,
  metrics_json TEXT
);
CREATE TABLE IF NOT EXISTS holding_decisions (
  trading_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  close_price REAL,
  ema21 REAL,
  sma50 REAL,
  data_source TEXT,
  evaluated_at TEXT NOT NULL,
  PRIMARY KEY(trading_date,ticker)
);
CREATE TABLE IF NOT EXISTS exit_decisions (
  trading_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  exit_state TEXT NOT NULL,
  exit_score REAL,
  exit_confidence TEXT NOT NULL,
  details_json TEXT NOT NULL,
  evaluated_at TEXT NOT NULL,
  lifecycle_id TEXT,
  PRIMARY KEY(trading_date,ticker)
);
CREATE TABLE IF NOT EXISTS position_reference_zones (
  lifecycle_id TEXT NOT NULL, trading_date TEXT NOT NULL, ticker TEXT NOT NULL,
  profit_protection_low REAL, profit_protection_high REAL,
  trend_failure_low REAL, trend_failure_high REAL,
  exit_state TEXT NOT NULL, exit_score REAL, current_price REAL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(lifecycle_id,trading_date,ticker)
);
CREATE TABLE IF NOT EXISTS exit_state_transitions (
  lifecycle_id TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  previous_state TEXT,
  exit_state TEXT NOT NULL,
  exit_score REAL,
  reasons_json TEXT NOT NULL,
  close_price REAL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(lifecycle_id,trading_date)
);
CREATE TABLE IF NOT EXISTS exit_highlight_alerts (
  alert_key TEXT PRIMARY KEY,
  lifecycle_id TEXT NOT NULL,
  ticker TEXT NOT NULL,
  trading_date TEXT NOT NULL,
  exit_state TEXT NOT NULL,
  reason_hash TEXT NOT NULL,
  alert_kind TEXT NOT NULL,
  priority TEXT NOT NULL,
  message_text TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','IN_FLIGHT','SENT','UNKNOWN','CANCELLED')),
  message_id INTEGER,
  attempted_at TEXT,
  completed_at TEXT,
  UNIQUE(lifecycle_id,trading_date)
);
CREATE TABLE IF NOT EXISTS quotes (
  ticker TEXT PRIMARY KEY,
  price TEXT NOT NULL,
  observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  ticker TEXT NOT NULL,
  action TEXT NOT NULL,
  suggested_cents INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN','SKIPPED')),
  reasons TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_at TEXT NOT NULL,
  mode TEXT NOT NULL,
  candidates INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS event_triggers (
  fingerprint TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  ticker TEXT NOT NULL,
  note TEXT NOT NULL,
  ai_called INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runtime_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_updates (
  update_id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  reply_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS telegram_command_receipts (
  update_id INTEGER PRIMARY KEY REFERENCES processed_updates(update_id),
  state TEXT NOT NULL CHECK(state IN ('PENDING','IN_FLIGHT','SENT','UNKNOWN')),
  attempted_at TEXT,
  completed_at TEXT,
  message_id INTEGER,
  error_type TEXT
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
  id INTEGER PRIMARY KEY,
  asof TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  cash_cents INTEGER NOT NULL,
  market_value_cents INTEGER,
  portfolio_value_cents INTEGER,
  unrealized_pl_cents INTEGER,
  realized_pl_cents INTEGER NOT NULL,
  positions_json TEXT NOT NULL,
  spy_close REAL,
  qqq_close REAL,
  status TEXT NOT NULL,
  ledger_max_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS portfolio_snapshots_asof ON portfolio_snapshots(asof,id);
CREATE TABLE IF NOT EXISTS portfolio_performance (
  snapshot_id INTEGER NOT NULL,
  horizon TEXT NOT NULL CHECK(horizon IN ('1M','3M','6M','1Y')),
  start_snapshot_id INTEGER NOT NULL,
  flow_adjusted_return REAL,
  spy_return REAL,
  qqq_return REAL,
  spy_alpha REAL,
  qqq_alpha REAL,
  max_drawdown REAL,
  method TEXT NOT NULL,
  PRIMARY KEY(snapshot_id,horizon)
);
CREATE TABLE IF NOT EXISTS allocation_decisions (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  asof TEXT NOT NULL,
  ticker TEXT NOT NULL,
  state TEXT NOT NULL,
  suggested_cents INTEGER NOT NULL,
  quality_score REAL,
  reasons_json TEXT NOT NULL,
  inputs_json TEXT NOT NULL,
  simulation INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS suggested_portfolio_snapshots (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  asof TEXT NOT NULL,
  allocations_json TEXT NOT NULL,
  spy_close REAL,
  qqq_close REAL
);
