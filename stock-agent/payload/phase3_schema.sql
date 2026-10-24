CREATE TABLE IF NOT EXISTS sec_cache (
    cik TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    payload BLOB NOT NULL,
    fetched_at TEXT NOT NULL,
    last_filing TEXT,
    source TEXT NOT NULL,
    cache_status TEXT NOT NULL,
    PRIMARY KEY(cik,endpoint)
);

CREATE TABLE IF NOT EXISTS fundamental_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    quant_run_id INTEGER NOT NULL,
    scan_date TEXT NOT NULL,
    scanned_at TEXT NOT NULL,
    config_json TEXT NOT NULL,
    source TEXT NOT NULL,
    api_requests INTEGER NOT NULL,
    cache_hits INTEGER NOT NULL,
    processed_count INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS fundamental_scores (
    run_id INTEGER NOT NULL REFERENCES fundamental_runs(id),
    ticker TEXT NOT NULL,
    quant_rank INTEGER NOT NULL,
    candidate_rank INTEGER NOT NULL,
    quant_score REAL NOT NULL,
    fundamental_score REAL,
    candidate_score REAL NOT NULL,
    confidence TEXT NOT NULL,
    coverage_pct REAL NOT NULL,
    growth_score REAL,
    profitability_score REAL,
    financial_health_score REAL,
    cash_flow_quality_score REAL,
    valuation_score REAL,
    trend_score REAL,
    trend_label TEXT NOT NULL,
    effective_quant_weight REAL NOT NULL,
    effective_fundamental_weight REAL NOT NULL,
    flags_json TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    features_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    data_quality_status TEXT NOT NULL,
    PRIMARY KEY(run_id,ticker)
);

CREATE INDEX IF NOT EXISTS fundamental_scores_ticker_run ON fundamental_scores(ticker,run_id);
