CREATE TABLE IF NOT EXISTS securities (
    ticker TEXT PRIMARY KEY,
    asset_type TEXT NOT NULL,
    country TEXT NOT NULL,
    exchange TEXT NOT NULL,
    is_etf INTEGER NOT NULL,
    is_otc INTEGER NOT NULL,
    is_preferred INTEGER NOT NULL,
    is_warrant INTEGER NOT NULL,
    sector_etf TEXT
);

CREATE TABLE IF NOT EXISTS daily_bars (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (ticker, date)
);

CREATE INDEX IF NOT EXISTS daily_bars_ticker_date ON daily_bars(ticker, date DESC);

CREATE TABLE IF NOT EXISTS quant_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at TEXT NOT NULL,
    asof TEXT NOT NULL,
    config_json TEXT NOT NULL,
    eligible_count INTEGER NOT NULL,
    scored_count INTEGER NOT NULL,
    failed_count INTEGER NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    data_mode TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quant_scores (
    run_id INTEGER NOT NULL REFERENCES quant_runs(id),
    ticker TEXT NOT NULL,
    rank INTEGER NOT NULL,
    quant_score REAL NOT NULL,
    momentum_score REAL NOT NULL,
    relative_strength_score REAL NOT NULL,
    technical_score REAL NOT NULL,
    volume_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    features_json TEXT NOT NULL,
    memory_json TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);

CREATE INDEX IF NOT EXISTS quant_scores_ticker_run ON quant_scores(ticker, run_id);

CREATE TABLE IF NOT EXISTS quant_failures (
    run_id INTEGER NOT NULL REFERENCES quant_runs(id),
    ticker TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (run_id, ticker)
);

CREATE TABLE IF NOT EXISTS market_sync_days (
    date TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    downloaded_rows INTEGER NOT NULL,
    accepted_rows INTEGER NOT NULL,
    rejected_rows INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS market_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    asof TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    universe_count INTEGER NOT NULL DEFAULT 0,
    phase1_passed INTEGER NOT NULL DEFAULT 0,
    phase2_success INTEGER NOT NULL DEFAULT 0,
    phase2_failed INTEGER NOT NULL DEFAULT 0,
    api_requests INTEGER NOT NULL DEFAULT 0,
    downloaded_rows INTEGER NOT NULL DEFAULT 0,
    updated_symbols INTEGER NOT NULL DEFAULT 0,
    skipped_symbols INTEGER NOT NULL DEFAULT 0,
    missing_symbols INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS volatility_daily (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(symbol,date)
);

CREATE TABLE IF NOT EXISTS market_regime_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    regime TEXT NOT NULL,
    regime_source TEXT NOT NULL,
    confidence TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    action_date TEXT NOT NULL,
    action_type TEXT NOT NULL,
    source TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_validations (
    ticker TEXT NOT NULL,
    jump_date TEXT NOT NULL,
    corporate_action_type TEXT NOT NULL,
    corporate_action_date TEXT,
    split_ratio REAL,
    source TEXT,
    validation_status TEXT NOT NULL,
    details_json TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(ticker,jump_date)
);

CREATE TABLE IF NOT EXISTS forward_snapshots (
    run_id INTEGER NOT NULL REFERENCES quant_runs(id),
    ticker TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    quant_score REAL NOT NULL,
    momentum_score REAL NOT NULL,
    relative_strength_score REAL NOT NULL,
    technical_score REAL NOT NULL,
    volume_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    close_price REAL NOT NULL,
    sector TEXT,
    market_regime TEXT NOT NULL,
    PRIMARY KEY(run_id,ticker)
);

CREATE TABLE IF NOT EXISTS forward_observations (
    run_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    completion_date TEXT NOT NULL,
    stock_return REAL NOT NULL,
    spy_return REAL NOT NULL,
    qqq_return REAL NOT NULL,
    excess_spy REAL NOT NULL,
    excess_qqq REAL NOT NULL,
    mfe REAL,
    mae REAL,
    PRIMARY KEY(run_id,ticker,horizon),
    FOREIGN KEY(run_id,ticker) REFERENCES forward_snapshots(run_id,ticker)
);
CREATE INDEX IF NOT EXISTS forward_snapshots_date ON forward_snapshots(scan_date,run_id);
