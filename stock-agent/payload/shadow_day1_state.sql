CREATE TABLE IF NOT EXISTS shadow_runs (
    shadow_run_type TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'WAITING_FOR_DATA', 'READY', 'RESUME_AUTHORIZED', 'RUNNING',
        'PIPELINE_COMPLETE_TELEGRAM_PENDING', 'COMPLETE', 'FAILED')),
    run_id TEXT,
    market_data_as_of TEXT,
    started_at TEXT,
    completed_at TEXT,
    pipeline_status TEXT NOT NULL DEFAULT 'NOT_STARTED',
    result_path TEXT,
    result_sha256 TEXT,
    token_cost_summary_json TEXT,
    telegram_idempotency_key TEXT NOT NULL UNIQUE,
    telegram_delivery_status TEXT NOT NULL DEFAULT 'NOT_READY' CHECK (
        telegram_delivery_status IN ('NOT_READY', 'PENDING', 'IN_FLIGHT', 'UNKNOWN', 'SENT')),
    telegram_attempted_at TEXT,
    telegram_message_id INTEGER,
    failed_phase TEXT,
    error_type TEXT,
    error_message TEXT,
    failed_at TEXT,
    resume_count INTEGER NOT NULL DEFAULT 0 CHECK (resume_count IN (0,1)),
    resume_authorized_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (shadow_run_type, trading_date),
    CHECK (state <> 'COMPLETE' OR
           (pipeline_status = 'COMPLETE' AND telegram_message_id IS NOT NULL
            AND telegram_delivery_status = 'SENT' AND completed_at IS NOT NULL)),
    CHECK (state <> 'FAILED' OR
           (failed_phase IS NOT NULL AND error_type IS NOT NULL AND failed_at IS NOT NULL))
);
