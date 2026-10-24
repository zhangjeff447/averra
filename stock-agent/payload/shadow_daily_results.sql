CREATE TABLE IF NOT EXISTS snapshots (
  trading_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  candidate_rank INTEGER NOT NULL,
  quant_score REAL NOT NULL,
  fundamental_score REAL,
  candidate_score REAL NOT NULL,
  research_eligibility TEXT NOT NULL,
  research_quality_tier TEXT,
  research_score REAL,
  effective_conviction REAL,
  entry_score REAL,
  entry_state TEXT,
  close_price REAL NOT NULL,
  market_data_source TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  PRIMARY KEY(trading_date,ticker)
);
CREATE TABLE IF NOT EXISTS forward (
  trading_date TEXT NOT NULL,
  ticker TEXT NOT NULL,
  horizon INTEGER NOT NULL,
  research_quality_tier TEXT,
  status TEXT NOT NULL CHECK(status IN ('PENDING','COMPLETE')),
  end_date TEXT,
  return_pct REAL,
  excess_spy_pct REAL,
  excess_qqq_pct REAL,
  mfe_pct REAL,
  mae_pct REAL,
  PRIMARY KEY(trading_date,ticker,horizon)
);
