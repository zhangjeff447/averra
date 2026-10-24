CREATE TABLE IF NOT EXISTS shadow_daily_runs (
  run_type TEXT NOT NULL CHECK(run_type='SHADOW_DAILY'),
  trading_date TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN (
    'WAITING_FOR_DATA','READY','RUNNING','PIPELINE_COMPLETE_TELEGRAM_PENDING',
    'COMPLETE','FAILED','MISSED_DATA_WINDOW')),
  run_id TEXT,
  checked_at TEXT,
  started_at TEXT,
  pipeline_completed_at TEXT,
  completed_at TEXT,
  failed_at TEXT,
  missed_at TEXT,
  failed_phase TEXT,
  error_type TEXT,
  error_message TEXT,
  market_data_as_of TEXT,
  result_path TEXT,
  result_sha256 TEXT,
  telegram_key TEXT NOT NULL,
  telegram_status TEXT NOT NULL DEFAULT 'NOT_READY',
  telegram_attempted_at TEXT,
  telegram_message_id INTEGER,
  token_cost_json TEXT,
  PRIMARY KEY(run_type,trading_date)
);
CREATE TABLE IF NOT EXISTS shadow_daily_recovery_audit (
  trading_date TEXT NOT NULL,
  recovered_at TEXT NOT NULL,
  prior_phase TEXT NOT NULL,
  prior_error_type TEXT NOT NULL,
  prior_error_message TEXT,
  authorization TEXT NOT NULL,
  PRIMARY KEY(trading_date,recovered_at)
);
CREATE TABLE IF NOT EXISTS shadow_daily_checks (
  trading_date TEXT NOT NULL,
  slot_et TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  PRIMARY KEY(trading_date,slot_et)
);
CREATE TABLE IF NOT EXISTS shadow_daily_progress (
  trading_date TEXT PRIMARY KEY,
  phase TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shadow_monitor_alerts (
  alert_key TEXT PRIMARY KEY,
  trading_date TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  message_id INTEGER,
  error_type TEXT
);
