CREATE TABLE IF NOT EXISTS shadow_day1_snapshots (
    run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    market_data_source TEXT NOT NULL,
    candidate_rank INTEGER NOT NULL,
    candidate_score REAL NOT NULL,
    quant_score REAL NOT NULL,
    fundamental_score REAL,
    research_status TEXT NOT NULL,
    research_score REAL,
    conviction_score REAL,
    entry_state TEXT,
    entry_score REAL,
    position_sizing_json TEXT,
    close_price REAL NOT NULL,
    spy_close REAL NOT NULL,
    qqq_close REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(run_id,ticker)
);
CREATE TABLE IF NOT EXISTS shadow_day1_forward (
    run_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','COMPLETE')),
    end_date TEXT,
    return_pct REAL,
    excess_spy_pct REAL,
    excess_qqq_pct REAL,
    mfe_pct REAL,
    mae_pct REAL,
    PRIMARY KEY(run_id,ticker,horizon)
);
