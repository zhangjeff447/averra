CREATE TABLE IF NOT EXISTS intraday_checks (
  trading_date TEXT NOT NULL, check_window TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETE','FAILED')),
  started_at TEXT NOT NULL, completed_at TEXT, error_type TEXT, error_message TEXT,
  tickers_checked INTEGER NOT NULL DEFAULT 0, market_state TEXT,
  event_state TEXT, alerts_generated INTEGER NOT NULL DEFAULT 0,
  iex_requests INTEGER NOT NULL DEFAULT 0, evidence_requests INTEGER NOT NULL DEFAULT 0,
  luna_calls INTEGER NOT NULL DEFAULT 0, luna_input_tokens INTEGER NOT NULL DEFAULT 0,
  luna_output_tokens INTEGER NOT NULL DEFAULT 0, luna_cost_usd REAL NOT NULL DEFAULT 0,
  luna_unknown_cost_usd REAL NOT NULL DEFAULT 0,
  sol_calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(trading_date,check_window)
);
CREATE TABLE IF NOT EXISTS intraday_observations (
  trading_date TEXT NOT NULL, check_window TEXT NOT NULL, ticker TEXT NOT NULL,
  role TEXT NOT NULL, status TEXT NOT NULL, state TEXT,
  price_asof TEXT, data_source TEXT NOT NULL, details_json TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(trading_date,check_window,ticker)
);
CREATE TABLE IF NOT EXISTS intraday_alerts (
  id INTEGER PRIMARY KEY, trading_date TEXT NOT NULL, check_window TEXT NOT NULL,
  ticker TEXT NOT NULL, event_key TEXT NOT NULL, state TEXT NOT NULL,
  message TEXT NOT NULL, send_status TEXT NOT NULL
    CHECK(send_status IN ('RESERVED','SENDING','SENT','UNKNOWN','FAILED_NO_REQUEST')),
  telegram_message_id INTEGER, created_at TEXT NOT NULL, sent_at TEXT,
  UNIQUE(trading_date,check_window,ticker,event_key,state)
);
CREATE INDEX IF NOT EXISTS intraday_observation_ticker ON intraday_observations(ticker,recorded_at);
CREATE TABLE IF NOT EXISTS intraday_research_queue (
  ticker TEXT NOT NULL, evidence_key TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('LUNA_REFRESH_REQUIRED','COMPLETE','FAILED_UNKNOWN',
    'FAILED_VALIDATION','INPUTS_UNAVAILABLE','BUDGET_BLOCKED','DEEP_RESEARCH_PENDING')),
  input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
  PRIMARY KEY(ticker,evidence_key)
);
