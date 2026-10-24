CREATE TABLE IF NOT EXISTS entry_intraday_bars (
  ticker TEXT NOT NULL, ts_ms INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
  low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, bar_vwap REAL,
  source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(ticker,ts_ms)
);
CREATE INDEX IF NOT EXISTS entry_intraday_lookup ON entry_intraday_bars(ticker,ts_ms DESC);
CREATE TABLE IF NOT EXISTS entry_daily_overlay (
  ticker TEXT NOT NULL, date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
  low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
  source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(ticker,date)
);
CREATE TABLE IF NOT EXISTS entry_sync (
  ticker TEXT NOT NULL, interval TEXT NOT NULL, last_asof TEXT, last_fetch_at TEXT,
  status TEXT NOT NULL, rows_downloaded INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(ticker,interval)
);
CREATE TABLE IF NOT EXISTS entry_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, asof TEXT NOT NULL, started_at TEXT NOT NULL,
  ended_at TEXT, status TEXT NOT NULL, source TEXT NOT NULL, api_requests INTEGER NOT NULL DEFAULT 0,
  rows_downloaded INTEGER NOT NULL DEFAULT 0, config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entry_decisions (
  run_id INTEGER NOT NULL, ticker TEXT NOT NULL, decided_at TEXT NOT NULL,
  entry_price REAL, entry_score REAL, entry_state TEXT NOT NULL,
  signal_validity TEXT NOT NULL,
  chase_risk TEXT, regime TEXT, components_json TEXT NOT NULL,
  timeframes_json TEXT NOT NULL, features_json TEXT NOT NULL,
  explanation_json TEXT NOT NULL, research_reference_json TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE TABLE IF NOT EXISTS entry_forward (
  run_id INTEGER NOT NULL, ticker TEXT NOT NULL, horizon INTEGER NOT NULL,
  anchor_date TEXT NOT NULL, end_date TEXT NOT NULL, return_pct REAL NOT NULL,
  mfe_pct REAL NOT NULL, mae_pct REAL NOT NULL,
  PRIMARY KEY(run_id,ticker,horizon)
);
CREATE TABLE IF NOT EXISTS entry_reference_plans (
  run_id INTEGER NOT NULL, ticker TEXT NOT NULL,
  generated_from_trading_date TEXT NOT NULL, target_trading_date TEXT NOT NULL,
  reference_generated_at TEXT NOT NULL,
  preferred_low REAL NOT NULL, preferred_high REAL NOT NULL,
  do_not_chase_above REAL NOT NULL,
  nearest_support REAL, nearest_resistance REAL, chase_risk TEXT,
  plan_json TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
CREATE INDEX IF NOT EXISTS entry_reference_target ON entry_reference_plans(target_trading_date);
CREATE TABLE IF NOT EXISTS entry_reference_observations (
  run_id INTEGER NOT NULL, ticker TEXT NOT NULL, target_trading_date TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
  source TEXT NOT NULL, quality_status TEXT NOT NULL,
  entry_zone_touched INTEGER NOT NULL, do_not_chase_exceeded INTEGER NOT NULL,
  open_reference_status TEXT NOT NULL, observed_at TEXT NOT NULL,
  PRIMARY KEY(run_id,ticker)
);
