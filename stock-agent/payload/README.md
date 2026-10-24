PUBLIC SOURCE RELEASE: credentials and runtime state are excluded. The downloadable intraday_config.json is disabled by default; set your own paths, keys, and activation date before opting in. The author's local LaunchAgent status does not transfer with this ZIP.

# Averra Stock Agent V1

An independent, monitor-only US common-stock research agent, named **Averra Stock Agent**. The existing QQQ/SMH system is the **Averra ETF Agent**. This project has no Robinhood dependency, credentials, order API, or account sync. It never places trades. Telegram suggestions and user-confirmed transactions are stored separately in its own SQLite database. The ETF Agent is untouched.

Telegram bot: [@AverraStockAgentBot](https://t.me/AverraStockAgentBot). Its token lives only in the ignored local `.env` file and must never be copied into source, screenshots, or public packages.

## Source reliability upgrade — 2026-10-16

### Shadow evaluation and version evidence

Run `python3 shadow_evaluation.py --asof 2026-10-16` to create local `data/evaluation/shadow-2026-10-16.json` and `.html`. The report reads the existing `effective_forward` correction view through a read-only SQLite connection; it does not scan markets, fetch evidence, call models, send Telegram or modify historical records. Pending horizons, quarantined outcomes, non-finite values and outcomes unavailable by the requested date cannot become returns. It reports date-balanced candidate returns, excess SPY/QQQ, MFE/MAE, Entry/Research cohorts and daily score rank IC. The configured minimum of 20 independent scan dates is a descriptive sample gate, not statistical proof. Group comparisons are not causal evidence of AI contribution.

These observations use the existing reference-close anchors. A post-close recommendation cannot be assumed filled at that earlier close. Consequently this module deliberately does not produce simulated trade profits, a portfolio equity curve, Sharpe or portfolio drawdown. A separate execution-aware test would need subsequent tradable prices, conditional buy zones, gaps, costs and actual confirmations. New daily runs save `run_manifest.json` with code/config fingerprints before processing; old runs without this file are explicitly unversioned, never backfilled with today's hashes. This implements the recording/evaluation loop without changing the frozen strategy or adding an unvalidated ML model.

SEC fundamentals and Phase 4A document downloads now append per-attempt timing to private `data/source_requests.jsonl`. Records contain the source, public host, elapsed time, response size/status, classified error, attempt number and next retry delay; URLs, credentials, request/response bodies and exception text are excluded. Existing retry and cache rules are unchanged. Run `python3 request_diagnostics.py --date 2026-10-16` for a local UTC-day summary of request time, retries and errors. This report never makes network or AI calls. Historical runs cannot be retroactively diagnosed from these new records.

The reviewed IR registry now also includes OKTA, VEEV and DINO, with official RSS feeds, company identity/CIK and verification date. Only registered HTTPS hosts are fetched. A successful RSS fetch is not proof that every announcement was read. Each ticker reads at most six eligible release documents per collection, prioritizing material categories and recent publication dates, and deduplicates URLs before requests. Overflow is explicitly retained as `IR_COLLECTION_LIMIT` metadata, with IR coverage marked PARTIAL; it is not promoted to verified evidence. Missing publication time, future publications, identity mismatch and off-domain articles are rejected. Existing SEC material-event extraction, evidence packet protection, AI budgets and frozen Entry/Exit rules remain unchanged. Blocked official sites are not added as working feeds.

## Intraday Risk & Event Monitor — enabled from 2026-10-02

This is a separate monitor-only layer. `intraday_config.json` is enabled with an activation-date guard for the next regular session, 2026-10-02. `com.averra.stock.intraday-monitor` is an independent user LaunchAgent. The existing close scheduler, frozen Entry/Exit/Research strategies, QQQ/SMH agent, and Telegram command listener are unchanged. Installation does not run a scan, send a test alert, or call AI.

The runner wakes each minute but processes only scheduled ET windows: normal 09:50/12:15/14:45/15:45; a verified 14:00 FOMC decision adds 13:55/14:10/14:35 and merges the 15:45 final-risk check; a verified 08:30 release adds 09:35; a verified 10:00 release reuses 09:50 as PRE_EVENT and adds 10:10, not 09:55. Other whitelisted HIGH/MATERIAL events use configurable offsets and merge with nearby checks. Holidays skip; verified NYSE early closes truncate later windows and add 12:45. It reads only user-confirmed `tracked_holdings`, current-day `entry_reference_plans`, and an optional bounded WATCH list (default zero). It never rescans the full universe. `--plan` is read-only. A failed macro-calendar refresh disables only event-specific checks (`CALENDAR_EVENT_SCHEDULING_DEGRADED`); normal checks continue.

`intraday_calendar_import.py` imports reviewed `events`, `market_sessions`, and `coverage`. The current official 2026 Q4 manifest is `official_calendar_2026_q4.json`. SQLite keeps current facts and append-only revision history. Only explicit official date/time facts are `VERIFIED`; missing times are `UNVERIFIED` and never scheduled. `intraday_calendar_refresh.py` attempts weekly and prior-trading-day source revalidation. If a source is unreachable or its facts no longer match, it never silently renews verification. On this Mac direct BLS requests currently return HTTP 403; manually verified entries remain valid for their initial window, but later BLS event-specific checks fail closed unless successfully revalidated. Official sources: [BLS](https://www.bls.gov/schedule/2026/), [BEA](https://www.bea.gov/news/schedule/full), [Federal Reserve](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm), [NYSE](https://www.nyse.com/trade/hours-calendars).

For a reviewed update: `python3 intraday_calendar_import.py /absolute/path/reviewed-calendar.json`. A source failure never falls back to AI guesses. Daily local counters are written to `data/intraday_reports/YYYY-MM-DD.json` after completed checks.

Price checks use read-only Alpaca Market Data `feed=iex` 1-minute bars. IEX is one venue, not consolidated SIP: its volume is never compared with historical all-market daily volume, so RVOL is explicitly unavailable unless a comparable IEX baseline is added. The first bar is an IEX opening indication, not an official consolidated open. Missing/stale bars or SPY/QQQ benchmarks produce `DATA_UNAVAILABLE`. VIX/VXN are recorded only if an existing Yahoo snapshot has a fresh same-day timestamp; otherwise they are omitted. ATR-normalized gap/move, VWAP when bar values are complete, relative strength, and market volatility are deterministic Python calculations. The close-based `position_reference_zones` and price plans are read-only inputs; the frozen daily Exit/Entry states are never overwritten.

Each scoped holding/plan gets a fresh Phase 4A SEC submissions and verified-IR-feed check. Only new, official HIGH/MATERIAL evidence enters the separate research queue. The bounded Initial refresh uses `gpt-5.6-luna`, existing Phase 4 evidence-only schema and shared daily Initial budget; Web Search and automatic Sol are off. Missing verified inputs or budget leave an explicit pending/blocked state. Any Sol escalation is stored as `DEEP_RESEARCH_PENDING`, never executed here. The paid raw response is saved before usage accounting and parsing, and uncertain requests are never automatically retried. No material event means zero AI calls.

Only meaningful state changes or new material evidence reserve an alert in `data/intraday_monitor.db`. Normal `POSITION_NORMAL` and `ENTRY_PLAN_VALID` are local-only. Telegram includes ET time, `INTRADAY DATA SOURCE: IEX`, evidence IDs where applicable, and `No trade was placed`; no BUY_NOW is produced. A unique ticker/event/state/window key prevents duplicate sends, while an ambiguous send is marked `UNKNOWN` and is not blindly retried. Each check stores tickers, observations, market/event state, alerts/message IDs, request counts and AI token/cost totals. Failed tickers are isolated. The separate launch template uses local one-minute wakeups so ET/DST and dynamic macro events are evaluated in Python rather than assuming the Mac's local timezone is ET.

The independent LaunchAgent has `RunAtLoad=false`; the first eligible window is 2026-10-02 09:35 ET for the verified Employment Situation release, followed by the four normal windows. Nothing runs outside a scheduled window. Keep the Mac awake and logged in for unattended operation; no Terminal window is needed.

## Current operation — ongoing close-only monitor

The September 30 automation repair replaces the four-date-only schedule. The existing `com.averra.stock.shadow-v11` service now wakes locally every 60 seconds and evaluates America/New_York time itself, including DST. It performs at most one market check per trading-day slot: 16:20, 16:35, 16:50, 17:05. Weekends/market holidays and all earlier minutes do no market or AI work. It continues after October 2. A new daily run cannot start at/after 17:10; a run claimed before cutoff can finish afterward. The local exchange-holiday rules live in `market_regime.py`; extraordinary exchange closures require a calendar update. Early-close sessions are processed conservatively at the same 16:20 check, not treated as real-time sessions.

`com.averra.stock.commands` separately runs `serve-commands`, providing Telegram commands continuously without invoking the legacy scanner. `/status` reads the durable daily state and Telegram message ID; `/candidates` reads the hash-verified saved daily report. Command confirmations preserve update-ID accounting and sent message IDs. Unknown sends are not blindly repeated.

The daily flow is Alpaca historical SIP current Daily → bounded missing-session repair → Phase 1/2/3 → cache-aware evidence and Initial research → Deep plan only → close Entry → portfolio/forward observations → one Telegram summary. Massive history remains intact; Yahoo provides VIX/VXN. Recent 20-session history, all 13 benchmarks and ≥95% market coverage must pass. At most three missing sessions are automatically repaired, requesting only missing symbols. Evidence sources are checked after 12 hours with document cache reuse, so unchanged evidence still reuses research. The existing material/grounding and Limited eligibility rules remain enforced. BUY_NOW and automatic Sol execution are disabled. Luna limits remain 10 calls and $0.05 local estimated daily cost, Web Search OFF. Unknown prior research requests block a new request for that ticker across dates.

The OS execution lock covers data update, research and Telegram. SQLite stores each slot attempt, phase progress, complete run, failure and delivery claim. COMPLETE never replays. A lost RUNNING process is marked FAILED, reports its last phase, sends a deduplicated operational alert, and blocks new research pending review. FAILED/uncertain Telegram delivery also block automatic new runs. A missed data window alerts once and never starts that missed run late. Telegram ambiguity requires manual reconciliation because sendMessage has no server-side idempotency key.

Install/update the two Stock services with `python3 install_monitor.py --install`; it backs up the previous Stock plist and does not modify the ETF service. `python3 install_monitor.py` checks configuration presence without printing secrets. `python3 shadow_daily_scheduler.py --status` reports durable states. Logs: `data/shadow_daily_runs.jsonl`, `data/shadow_daily_stdout.log`, `data/shadow_daily_stderr.log`, `data/commands_stderr.log`. Neither Terminal nor Codex needs to stay open. LaunchAgents need the Mac powered on and the user logged in; an already-running pipeline prevents idle sleep, but closed-lid sleep/power-off cannot execute jobs. Waking before cutoff permits the next eligible check; waking after cutoff produces MISSED_DATA_WINDOW.

Forward returns use actual exchange sessions, verified stock/SPY/QQQ endpoints, and reject unresolved corporate-action/mixed-source inconsistencies. Read `effective_forward` for performance: append-only `forward_corrections` preserve original outcomes while correcting the 30 Day 1 one-day observations from an erroneous September 30 endpoint to September 29. Completed Day 3 research, signals, original snapshots and Telegram message 25 remain unchanged. Its original missing-September-29 input caveat remains historical; later backfill does not make that original observation a clean-data validation.

For a complete local health report, run `python3 monitor_status.py` (read-only, no network). The installed services, next ET check, listener heartbeat, data dates and separate confirmed/reserved/unknown costs are included. `--notify-ready` sends the one-time activation confirmation only; its saved message ID prevents replay. The September 30 activation report is `data/monitor_activation_report.json` (Telegram confirmation 26); it is distinct from Day 3 summary 25.

The phase-by-phase sections below document earlier development interfaces; the two services above are the current automatic entrypoints.

## Current operating mode — ticker-only holding and sell monitoring

The Stock Bot no longer asks for a deposit, purchase amount, total capital, or suggested allocation. `/bought TICKER [PER_SHARE_PRICE]` confirms that you hold a validated US common stock; `/sold TICKER [PER_SHARE_PRICE]` confirms that you have fully exited it. A supplied price is your stated per-share fill, not an order or investment amount. Without an entry price, return and profit-protection metrics remain unavailable. `/portfolio` lists tracked tickers and the latest completed-close Exit review; `/history` lists confirmations, and `/status` shows monitor state. Old money-ledger records remain in SQLite for audit but are not used for current decisions. `/deposit` and `/balance` explain that cash tracking is retired. No broker order is ever placed.

A BUY TOMORROW recommendation is not a purchase confirmation. Without `/bought`, that ticker remains an unheld entry plan only for its stated target session; it never enters HOLD/SELL monitoring. `/bought` switches it to confirmed-holding risk and exit monitoring at the next scheduled check. Each eligible close run independently refreshes the market, re-ranks candidates, and generates a new next-session BUY/WATCH/REJECT report even if no stock was bought that day. A prior unfilled recommendation does not automatically carry into the next day's BUY list.

After a confirmation, each completed daily scan checks the stock's own daily bars, even if it is no longer in the day's Top 30. The first eligible scan is the next trading session after confirmation. The independent local `exit_engine.py` combines original Research thesis and verified new material evidence, confirmed EMA21/SMA50/SMA200 trend, ATR-adjusted profit trails and multi-factor overextension, 5/10/20-session relative strength versus SPY/QQQ and a reliable sector ETF, time/opportunity cost, and dated earnings/event risk. Component scores share the direction 0 = lower exit concern, 100 = higher concern; hard evidence/trend gates can override the composite. Actions are `HOLD`, `HOLD_TIGHT`, `TRIM_CANDIDATE`, `REDUCE_CANDIDATE`, `EXIT_CANDIDATE`, or `THESIS_REVIEW`. Invalid or stale data yields `HOLD_DATA_INSUFFICIENT`, never a fabricated sell signal. Thresholds/weights are in `exit_config.json`; legacy `holdings_config.json` remains only for compatibility tests. A material headline alone cannot mark a thesis broken: that requires verified official evidence and an explicit, structured thesis conflict. If the evidence lacks that structure, the engine records uncertainty rather than inventing a conflict. Daily Exit checks use Python only, with no LLM or broker call.

Buy-candidate scoring and Entry Score weights are unchanged. For each `BUY_TOMORROW_CANDIDATE`, `price_plan.py` saves a close/ATR/support/resistance/EMA21/VWAP reference zone and `do_not_chase_above`. The existing +5% wait and +12% cancel gates remain, with additional gap-in-ATR sensitivity. An observed open outside the zone defaults to wait/reassess; an extreme gap cancels chasing. A future 5–15-minute opening check also requires reliable opening range, VWAP, relative strength and volume. It is **logic only** today: historical SIP does not provide this bot with a real-time opening alert, and BUY_NOW remains disabled. The daily Telegram summary names buy candidates and each confirmed holding's action; `/portfolio` shows fuller Exit details. Protection/trend zones are references, not stop orders or guaranteed fills.

### Exit notifications — FROZEN_V1

`exit_config.json` marks the Exit Engine `FROZEN_V1`; its thesis, trend, profit-protection, ATR trailing, relative-strength, time-stop, earnings/event, preferred-entry and gap/ATR thresholds are not automatically optimized during Shadow validation. Each confirmed buy begins a new lifecycle, keyed by its confirmation timestamp. Daily Exit decisions and state transitions retain the date, score, reasons and close price; a confirmed sell closes that lifecycle and cancels unsent highlights. A later buy starts fresh tracking and does not inherit the prior exit state, peak or holding duration. If prices are confirmed, the sell event also records per-share realized return and available maximum gain/drawdown observations.

Every completed daily Shadow message includes all tracked positions in its **Held Stocks** section, with entry/current price, return and trend when available. Repeated `HOLD` does not create an additional message. State changes create a separate highlight: `EXIT_CANDIDATE` is CRITICAL; `THESIS_REVIEW` and `REDUCE_CANDIDATE` are HIGH; `TRIM_CANDIDATE` and `HOLD_TIGHT` are MEDIUM. Improvement alerts are INFO and may be disabled. Same-state `HOLD_TIGHT` highlights only when profit protection tightens, peak drawdown materially worsens or event risk changes. Same-state TRIM/REDUCE highlights only for a material score increase, new reason or worse trend/thesis. EXIT and THESIS reminders have configurable 2- and 3-trading-day cooldowns, respectively.

The unchanged Shadow scheduler still sends exactly one main summary. Separately, the Stock command listener releases persisted position highlights **only after that day's main summary is confirmed sent**. The highlight key includes lifecycle, ticker, trading date, exit state and reason hash. An uncertain Telegram `sendMessage` is marked `UNKNOWN` and is not automatically retried or re-alerted until reconciled; Telegram itself has no server-side idempotency key. The listener never calls an LLM, a broker or a trading API for these notifications. Configuration and logic are in `exit_config.json` and `exit_notifications.py`.

### Shadow validation reporting

The scheduler's timing, data gate, execution lock and delivery state machine are unchanged. Its main Telegram text now includes a bounded, explicit BUY TOMORROW card (research tier, conviction, Entry Score, preferred zone, no-chase ceiling, chase risk, regime, reasons and next session), distinct WATCH/REJECT/DATA INSUFFICIENT sections, short Luna cost totals, and P1 Deep approval details if any. Missing price data is shown as unavailable, never fabricated. P1 remains a plan only: automatic Sol calls are disabled. Full tokens, results and deep estimates remain in the daily local report.

`shadow_validation.py` maintains `data/shadow_validation.db`. It counts only hash-verified, complete market/pipeline/result days; failed, partial and missed dates do not count. Once five complete dates exist, it writes exactly one immutable-window `SHADOW_5D_REVIEW` to SQLite and `data/shadow_reviews/`, using saved daily reports and mature `effective_forward` rows only. The independent Stock listener sends one short review Telegram **after** the fifth day's main summary is confirmed delivered; ambiguous sends remain `UNKNOWN` and are not retried. The review is descriptive, includes matched SPY/QQQ and an explicit NOT_MATURED state, and does not tune any frozen parameter. The initial verified counter is two complete dates (2026-09-30 and 2026-10-01); no review is generated yet.

The following Phase 6 sections describe the older money-ledger workflow retained only for historical audit and offline compatibility; they are not the current Telegram workflow. The old `scan`, `serve`, `phase6-scan`, and `phase6-from-entry` CLI modes are disabled so they cannot issue monetary suggestions.

## Phase 6 — virtual portfolio and position sizing (historical)

Phase 6 remains monitor-only: no broker login, order API, Robinhood connection, or automatic execution. The ETF Agent, its token, database, process and LaunchAgent are separate and unchanged. A new virtual portfolio starts with $0; only `/deposit AMOUNT` adds money. `SIMULATION_MODE=true` is the default. Even outside simulation, `/bought` and `/sold` only update the virtual ledger after an explicit user command.

The independent Stock Bot accepts `/start`, `/help`, `/deposit 2000`, `/balance`, `/portfolio`, `/bought AMD 200 [ACTUAL_PRICE]`, `/sold AMD 100 [ACTUAL_PRICE]`, `/skip [TICKER]`, `/history`, `/candidates`, and `/status`. A supplied fill is `USER_CONFIRMED_PRICE`; an absent fill uses a fresh quote and is marked `ESTIMATED_PRICE`. Missing/stale quotes reject amount-only trades with `PRICE_STALE`. Telegram update IDs make money commands idempotent across redelivery, and only `STOCK_TELEGRAM_CHAT_ID` is authorized. The append-only ledger stores source, note, resulting cash and realized P/L. Suggestions are stored separately and never reduce cash.

The initial limits in `phase6_config.json` are a 5–15% target, 15% single-name maximum, 30% sector maximum, 30% highly correlated-group maximum, and 20% cash reserve. They are configurable research heuristics, not optimized weights. `phase6_portfolio.py` ranks conviction/entry quality after volatility, drawdown, chase and regime adjustment, then jointly applies caps against existing and planned positions. Correlation uses at least 20 aligned daily returns; same-industry names are conservatively grouped. Delayed/free data produces `BUY_TOMORROW_CANDIDATE` or `WATCH`, never a Telegram `BUY_NOW_CANDIDATE`. High-quality pending opportunities can reserve additional cash. Held names can be monitored as `HOLD`, `ADD_CANDIDATE`, `NO_ADD`, `REDUCE_CANDIDATE` or `EXIT_CANDIDATE`; all require user judgment.

Phase 6 can consume existing completed Phase 5 output without refreshing upstream data: `python3 stock_agent.py phase6-from-entry` reads `data/entry_data.db` and `data/market_data.db` read-only, then writes only to the Stock Agent's own portfolio DB. Do not run it against the real portfolio for a dry run; use `phase6_simulation.py` instead. The current frozen CDNA/AMD/META Phase 5 pilot has only WAIT decisions, so this bridge produces WATCH/$0, not buy suggestions. Future completed Phase 5 runs can be consumed the same way. Alternatively, run `python3 stock_agent.py phase6-scan /absolute/path/candidates.json` on a prepared local JSON packet. The JSON needs `asof` and `candidates`; each item needs US common-stock identity (`ticker`, `asset_type=COMMON_STOCK`, `country=US`, `exchange=NYSE/NASDAQ/AMEX`, `sector`), conviction, entry, risk score, state, price and timestamped `price_at`. ATR%, realized volatility%, drawdown%, chase risk, regime, industry and aligned returns are used when provided. Neither path calls an LLM or fetches market data. Decisions and inputs, suggested snapshots, transactions and daily portfolio snapshots live in the Stock Agent SQLite DB. Scheduled Phase 6 notifications consume a prepared file via `STOCK_PHASE6_CANDIDATES_FILE` or the latest completed Phase 5 run via `STOCK_PHASE6_ENTRY_DB`, but only when `STOCK_ALERTS_ENABLED=true`. This phase does not start that service. The legacy alert path is blocked without either Phase 6 source. Telegram polling retains 5→15→30-second retry; a possibly delivered `sendMessage` is not blindly retried because Telegram lacks a message idempotency key. Failed sends never duplicate portfolio writes.

Daily snapshots hold cash, positions, weights, realized/unrealized P/L, valuation status and optional SPY/QQQ prices. Once actual dated endpoints exist, `Portfolio.performance(snapshot_id)` stores 1M/3M/6M/1Y flow-adjusted returns, benchmark comparisons and drawdown. No performance claim is made from this pilot; a stale quote is explicitly marked and prevents complete market valuation.

Run `python3 phase6_simulation.py` for a disposable ticker-only confirmation scenario. Its temporary database is deleted automatically, with no Telegram, LLM, market-data or broker API call. Run offline tests with `python3 -m unittest discover -s tests -q` from this directory.

## Quick start

Requirements: Python 3.11+; no third-party Python packages. The built-in simulation needs no credentials or network.

```sh
cd averra-stock-agent
python3 stock_agent.py simulate
python3 -m unittest discover -s tests -v
```

`simulate` uses a disposable ticker-only holdings database and exits. No deposit is created. For the live Stock Bot, use Telegram after a manual purchase; the command listener uses cached validated securities.

```sh
cp .env.example .env
python3 stock_agent.py simulate
```

To run Telegram, create a **new** bot token and set `STOCK_TELEGRAM_TOKEN` and your own numeric `STOCK_TELEGRAM_CHAT_ID` in `.env`. Then double-click `start.command` or run `./start.command`. The bot accepts only messages from the configured chat. It checks for commands every 2 seconds and prevents a second copy from running. `STOCK_ALERTS_ENABLED=false` initially permits ledger commands without scheduled mock alerts; enable it only after a real feed is configured. A separate `com.averra.stock-agent.plist` is provided, but it is not installed automatically and no ETF Agent LaunchAgent is changed.

## Telegram commands

- `/bought TICKER [PER_SHARE_PRICE]` confirms that you own the stock and starts daily sell monitoring; a price enables return and profit-protection metrics. It does not place an order.
- `/sold TICKER [PER_SHARE_PRICE]` confirms that you fully exited and stops monitoring that ticker. If both prices are known, it saves the per-share realized return, not a dollar P/L.
- `/portfolio` lists tracked tickers. `/history` lists recent confirmations.
- `/candidates` shows the latest saved buy candidates without allocation amounts.
- `/skip [TICKER]` marks an open suggestion skipped. `/status` shows monitor health; `/help` lists syntax.
- `/deposit` and `/balance` are retired and do not change the old ledger.

The bot cannot detect your broker trades. Send `/bought TICKER` after a real purchase and `/sold TICKER` after a full exit. Partial-sale quantities are not tracked; keep the ticker marked held while any shares remain.

## Data and operating modes

### First-layer, offline whole-market screen

`quant_screen.py` is the new standalone first layer. It imports a **complete security master** and historical/incremental **daily OHLCV** into `data/market_data.db`, then writes an auditable, ranked `data/top100.json` for later research stages. It does not import the Telegram runner, use an API, access a broker, or call an LLM: this entire stage uses **0 AI tokens**. Existing scheduled alerts and event analysis are not part of this path; keep `STOCK_ALERTS_ENABLED=false` while building the data feed. The portfolio database remains separate.

The security-master CSV must have columns `ticker,asset_type,country,exchange,is_etf,is_otc,is_preferred,is_warrant`; optional `sector_etf` supplies each stock's sector benchmark for Phase 2. Use `COMMON_STOCK`, `US`, and `NYSE`/`NASDAQ`/`AMEX` for eligible listings; all four exclusion flags must be explicit `0` or `1` (also accepts `true/false` and `yes/no`). ETF, OTC, preferred, warrants, other security types, non-US issuers, and unsupported exchanges are excluded. Classification comes from the supplied master, **not a guess from the ticker suffix**. A replacement master must represent the entire intended universe, not just a watchlist.

The OHLCV CSV must have `ticker,date,open,high,low,close,volume`, with `date` in `YYYY-MM-DD`. Initial screening requires **200 daily observations per ticker**, including the requested `--asof` trading date. Each subsequent import upserts dates, so daily files can append/update the local history. Bars with invalid ranges, negative volume, or malformed dates fail the import. Minimum close is $3 and minimum average 20-day dollar volume is $2 million; zero-volume latest bars are rejected. The JSON records master size, eligible US common listings, data/liquidity pass count, rejection counts, and up to 100 candidates. `quant_rank` is a deterministic first-pass ordering from 20/60-day returns, SMA20/50/200 trend, and RVOL; it is **not** the later Stock Score or a buy decision.

```sh
cd averra-stock-agent
python3 quant_screen.py --master /path/to/security_master.csv --bars /path/to/daily_history.csv --asof YYYY-MM-DD
# Later days: keep the same database and import only the new daily bars.
python3 quant_screen.py --bars /path/to/new_daily_bars.csv --asof YYYY-MM-DD
python3 -m unittest discover -s tests -v
```

This CSV entry point remains available for offline imports. The separate Phase 2.5 command below connects a real provider to the same SQLite schema. Do not send a Top 100 alert from partial input. The existing `stock_agent.py scan` still consumes its older normalized snapshot and is **not wired to the real Top 100 file**; later Telegram decisions remain paused.

### Phase 2: Quant Ranking Engine

`quant_ranking.py` scores **every Phase 1-qualified stock** with local Python only. It writes a Top 100 JSON and stores *all* successful stock scores, raw features, failures, scan timestamps, and the exact scoring configuration in the same market-data SQLite database. There is no LLM/API/Telegram call in this executable, so its runtime AI-token use is zero. It does not produce BUY recommendations or position sizes.

The five component weights start at 25% Momentum, 25% Relative Strength, 20% Technical, 15% Volume, and 15% Risk. All normalization ranges, per-feature enabled flags/weights, feature parameters, and quality thresholds are in [`quant_config.json`](quant_config.json). Each component is normalized 0–100. A **higher Risk Score means lower measured risk**. Relative-strength returns are stock return *minus* benchmark return, so an underperforming rising stock is scored as relatively weak. SPY and QQQ daily bars are mandatory; an optional `sector_etf` in the master enables sector-relative features when that ETF's bars are available. Unavailable sector features are saved as `null` and excluded from that component's weight denominator; they are never fabricated.

```sh
cd averra-stock-agent
# After importing a complete master and at least 252 completed daily bars per stock,
# plus SPY/QQQ bars (and optional sector ETF bars) into data/market_data.db:
python3 quant_ranking.py --asof YYYY-MM-DD
python3 quant_ranking.py --show AAPL
python3 quant_ranking.py --history AAPL
```

The output is `data/quant_top100.json`; the full ranking history lives in `quant_runs`, `quant_scores`, and `quant_failures` in `data/market_data.db`. `--show` displays all saved raw features for a ticker, while `--history` returns its compact rank/score trajectory. Positive rank change means the rank improved. 1D and 5D changes compare with the latest prior **distinct scan dates**; when too few prior dates exist, the change stays `null`. Repeated runs on the same date are retained but do not create a fake 1D change. The database retains all observations; a future AI stage should receive only a deliberately compressed summary, never the full history.

For a fully offline integration demo using the existing synthetic bar generator:

```sh
python3 seed_quant_mock.py --db data/phase2_mock.db
python3 quant_ranking.py --db data/phase2_mock.db --asof YYYY-MM-DD --mode mock --output data/phase2_mock_top100.json
```

The demo creates 120 `MOCKQ*` stocks. It refuses to overwrite an existing database and must **never** be mistaken for real market output. Use the distinct Phase 2.5 command below for real data.

### Phase 2.5: real daily data and Yahoo VIX/VXN

The separate `real_scan.py` command connects the existing Phase 1 and Phase 2 code to real data. It does **not** start the Telegram bot, generate BUY messages, use Robinhood, or call an LLM. The existing ETF Agent is unrelated and untouched. Keep `STOCK_ALERTS_ENABLED=false`; this command is an on-demand daily research scan, not a four-times-a-day Telegram decision service.

Set `MASSIVE_API_KEY` in the ignored `.env`. Massive Stocks Basic supplies the active US-stock reference list (`type=CS` only), split-adjusted daily group bars, SPY/QQQ and all 11 sector ETFs. The first fill requests 400 completed trading sessions; newly listed or illiquid stocks may have fewer bars and are never padded. The free tier's five requests/minute means approximately 80–90 minutes. A stopped run can be restarted and skips completed dates. Subsequent runs request only missing dates plus a short SPY calendar query. The initial fill also stores in-window split events; later split events trigger a historical refresh **only for affected symbols**. The `adjusted_close` column contains split-adjusted close, not a dividend-adjusted total-return close.

Nasdaq's public stock-screener JSON adds issuer country, sector and industry. Only issuers explicitly marked `United States` pass the original Phase 1 country check. Unmatched/unknown countries are excluded; missing sectors remain `UNKNOWN` and are never guessed. Nasdaq's screener feed is an undocumented public endpoint and can change; sector completeness and the source are reported. Its sector labels are **not GICS**: for example, it classifies some lab-instrument makers as Industrials. Mapping these labels to the 11 SPDR sector ETFs is therefore heuristic. Sector rankings and sector-relative scores must be interpreted with that caveat; the overall Quant formula itself is unchanged.

Yahoo's chart JSON supplies `^VIX` and `^VXN` daily history and latest quote. The dated daily bar immediately before the latest bar supplies the **true previous close**; Yahoo's `chartPreviousClose` is not assumed to mean the previous trading day. The local `market_regime.py` validates timestamps, numeric values, bar/quote agreement and freshness using `real_market_config.json`. Missing/invalid/stale indices do not crash Phase 1/2: one valid index gives a single-index regime; if neither is usable, the engine uses real SPY/QQQ realized volatility, ATR, drawdown and available market breadth/dispersion. If even the proxy lacks current enough data, the regime is `UNAVAILABLE` with confidence `NONE`, never a fabricated value. Yahoo's public interface has no service guarantee; no HTML scraping is used.

```sh
cd averra-stock-agent
python3 real_scan.py
# After a completed backfill, rerun scoring without re-downloading daily OHLCV:
python3 real_scan.py --asof YYYY-MM-DD --no-sync
python3 -m unittest discover -s tests -v
```

The default rank date is the preceding NYSE trading day, because Massive Basic does not grant the current day's bars before its end-of-day release. `real_top100.json` states both the ranking date and the separately timestamped latest market-regime date; the latter does not feed back into historical Quant Scores. The report contains Overall Top 100, each known sector's Top 10, score-bias diagnostics and three saved raw-feature samples. The complete ranking and feature history remains in `data/market_data.db`. `market_sync_runs`, `market_sync_days`, `market_regime_runs` and `volatility_daily` retain provenance and quality history. Runtime request counts exclude manual connectivity probes outside the command. All real-data failures say `REAL DATA FAILED`; there is no mock fallback.

This is a current-universe research scan, not a point-in-time backtest: an old date replayed later would use today's active-security/sector snapshot and provider-adjusted history, creating possible survivorship or corporate-action look-ahead. Historical simulation needs point-in-time master and actions before its results can be treated as unbiased.

### Phase 2.6: quality calibration and forward tracking

`phase26_run.py` uses only locally stored real daily bars and split events; it never calls an LLM, broker, or Telegram. `data/phase25_baseline.json` is the frozen Phase 2.5 comparison. The offline command is:

```sh
python3 phase26_run.py                         # latest stored, fully synced SPY date
python3 phase26_run.py --asof YYYY-MM-DD       # must equal latest stored SPY date
python3 phase26_run.py --sync-first            # existing real-data sync first, then Phase 2.6
```

`--sync-first` retains the existing Massive + Nasdaq + Yahoo VIX/VXN/proxy implementation and will make market-data API requests, but no AI requests. It refuses missing real data and never falls back to mock. The Phase 2.6 result is `data/phase26_report.json`; the Phase 2.5 baseline is not overwritten. This is an on-demand one-command workflow, **not** a newly installed scheduler or Telegram decision path.

The split validator cross-checks each large jump with stored corporate actions, records its verdict in `action_validations`, and leaves source bars unchanged. Only a split-ratio-matching, continuity-restoring seam may be corrected in memory; a known split with a residual anomaly stays quarantined. Non-split jumps require strong volume and next-day persistence. Those checks are useful safeguards, not an independent price-feed confirmation. Thresholds are in `phase26_config.json`.

Nasdaq sector labels remain non-GICS. `raw_sector`, `normalized_sector`, `industry`, `mapped_sector_etf`, `sector_source`, `mapping_source`, and `mapping_confidence` preserve the provenance. Known ambiguous industry labels are `LOW` and cannot use a sector ETF in Relative Strength; they fall back to SPY/QQQ. Unverified known Nasdaq sectors are `MEDIUM`, never automatically `HIGH`. META is an explicitly source-dated XLC holding override; other exceptions need the same evidence before being promoted. The five overall Quant component weights are unchanged. `sector_rs_used`, `sector_rs_weight`, and `fallback_benchmark` are stored in each score's raw features.

Technical attributions are exact additive points relative to a neutral 50 baseline, with each contributing raw value and normalization retained. The redundancy matrix is descriptive cross-sectional Pearson correlation; same-date SPY-relative returns are a constant shift of stock returns, so their correlation is mechanically 1.0 and is not independent evidence of predictive duplication. No weight is automatically tuned or feature removed.

Each formal scan stores an immutable `forward_snapshots` row per scored stock. `forward_observations` adds 1/5/10/20/60-session outcomes only after all required later stock, SPY, and QQQ bars exist. MFE/MAE use future daily high/low over 5/10/20 sessions. Repeated scans on the same date remain auditable but only the latest run for each date enters descriptive bucket statistics. Current-universe survivorship and retrospective adjusted-bar revisions still limit historical causal claims; predictive conclusions are suppressed until the configured minimum number of independent scan dates is reached.

### Phase 3: SEC fundamental research (no AI)

`phase3_run.py` reads only the latest **real** Phase 2 Quant Top 100; it does not rescore the wider universe, alter frozen Quant weights, call an LLM, touch Robinhood, or send Telegram recommendations. Before running, set `SEC_USER_AGENT` in the ignored `.env` to an organization/name and a contactable **public** email alias. SEC's JSON APIs are free and do not require an API key, but automated requests require a declared User-Agent. Do not use a fabricated or private address. If the contact is absent, the command stops with `REAL FUNDAMENTAL DATA FAILED` and does not create a mock report.

```sh
python3 phase3_run.py
python3 -m unittest discover -s tests -q
```

The first scan obtains SEC Submissions and Company Facts for at most 100 Quant candidates. It checks SEC ticker/CIK/company identity, respects the configured request spacing and 5→15→30 second transient retry, and caches compressed source JSON in `data/fundamental_data.db`. Later scans refresh Submissions after the cache TTL and re-fetch Company Facts only when a new 10-K/10-Q or amendment appears. `phase3_config.json` contains concept aliases, units, period/quality rules, feature weights, confidence cutoffs, and candidate-score weights. No credentials are stored in the cache.

For each fact, the engine saves its concept, unit, fiscal/period dates, filing and acceptance information, accession, source and quality. If the SEC acceptance time is unavailable, it conservatively waits until the next weekday after filing. Quarterly, annual and TTM values are kept separate; cash-flow year-to-date values produce a quarterly value only when a matching earlier cumulative period exists. Missing quarters, non-USD units, negative denominators and unverifiable valuation inputs remain unavailable, not zero. A later amendment can affect a new scan but never updates an earlier `fundamental_scores` row.

`data/phase3_report.json` contains Fundamental Top 30, Candidate Top 30, all Top 100 scores, major flags, effective Quant/Fundamental weights, component explanations, and three raw-fact samples. `fundamental_runs` and `fundamental_scores` retain immutable history in the separate database. The Phase 2.6 Forward Tracker remains in `data/market_data.db`; Phase 3 links future outcomes by the original Quant run ID without overwriting either score history. Until sufficient new trading days and independent scans exist, its predictive-comparison status remains pending. This is research ranking, not a BUY/SELL signal or a point-in-time survivorship-free backtest.

Quality gates require 252 aligned daily observations, an as-of-date bar, complete SPY/QQQ calendars, finite/consistent OHLCV, and Phase 1 price/liquidity thresholds. Suspected unadjusted splits, extreme jumps/prices, missing dates, stale data, and insufficient history are recorded as per-stock failures without stopping other stocks. In `live` mode a same-day daily bar is not used before 16:15 ET. Historical/mock modes allow reproducible as-of scans, and all feature queries use dates at or before `--asof`. **Data providers must still supply point-in-time historical data**; a later revised or retrospectively split-adjusted feed can introduce bias that date filtering alone cannot eliminate.

`STOCK_MODE=mock` produces deterministic synthetic bars and is suitable only for simulation. `STOCK_MODE=file` reads `STOCK_DATA_FILE` each scan and event poll. These older Telegram modes remain separate from Phase 2.5's real daily ranking database and still require their own normalized JSON snapshot. They reject missing fundamentals and benchmark coverage rather than fabricating scores. Do not interpret mock alerts as market recommendations.

Example snapshot shape (repeat daily bars to at least 200 observations for each stock; benchmarks need 61):

```json
{
  "asof": "2026-09-25T14:05:00+00:00",
  "session": "OPEN",
  "market": {"vix": 18.0, "vxn": 22.0},
  "benchmarks": {"SPY": [{"close": 500}], "QQQ": [{"close": 440}], "XLK": [{"close": 230}]},
  "stocks": [{
    "ticker": "EXAMPLE", "asset_type": "COMMON_STOCK", "country": "US",
    "exchange": "NASDAQ", "sector": "Technology", "sector_benchmark": "XLK",
    "bars": [{"date": "2026-09-24", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000000}],
    "fundamentals": {"revenue_growth_yoy": 0.2, "earnings_growth_yoy": 0.3,
                     "operating_margin": 0.2, "debt_to_equity": 0.5,
                     "estimate_revision_90d": 0.02},
    "earnings_date": "2026-10-20",
    "events": [{"date": "2026-09-25", "type": "ANALYST_UPGRADE"}]
  }]
}
```

All return, growth and revision fields use decimals (0.2 = 20%). Event types supported: `EARNINGS_BEAT`, `EARNINGS_MISS`, `GUIDANCE_RAISE`, `GUIDANCE_CUT`, `ANALYST_UPGRADE`, `ANALYST_DOWNGRADE`, `CATALYST_POSITIVE`, `CATALYST_NEGATIVE`. Source provenance and licensing remain the data producer's responsibility. The snapshot must be timestamped within 15 minutes of use and each stock must have a current New York trading-date bar. `file` mode stops new suggestions when stale. Portfolio balance and deposit commands still work with stored quotes; a confirmed buy can use the last validated universe snapshot during a feed outage.

### Phase 4: research pre-screen and bounded three-stock pilot

`python3 phase4_run.py pre` uses the latest real Phase 3 Candidate Top 30 and cached SEC filing metadata to rank AI research priority locally. It produces `data/phase4_pre_report.json` and stores all 30 compact research packets in the separate `data/research_data.db`. This command makes **no AI call**. Each packet is limited by `phase4_config.json`; old full OHLCV, Company Facts and ranking history are never sent to the model. Source filing acceptance dates are checked against the research cutoff. The Candidate Score date and later research timestamp remain distinct.

Paid Phase 4 calls are currently disabled with `paid_ai_enabled: false` in `phase4_config.json`; `python3 phase4_run.py pilot` stops before any request. A later explicit authorization is required before this hold is lifted. The existing pilot selection is one high Candidate Score, one near Candidate rank 15, and one with risk flags. It does not research the remaining Top 15, alter Quant/Fundamental scores, send Telegram BUY messages, or trade.

Phase 4.1 splits research into 4A (local evidence collection and SQLite cache) and 4B (reasoning over Quant/Fundamental summaries plus the evidence packet). The 4B request has **no Web Search by default**. Search is a separate, disabled-by-default escalation (`web_search_escalation_enabled`) only for insufficient or conflicting evidence; when enabled it remains limited to one low-context, trusted-domain call. `evidence_feeds` can list HTTPS RSS sources for a ticker, and company IR hosts must be listed in `verified_ir_domains`. With no configured feed, 4A uses the existing SEC filing index only, which proves filing metadata **not its contents**; insufficient evidence blocks a paid reasoner call. Events record type, date, title, factual summary, source/type, materiality, official status, duplicate group, URL and retrieval time. Low-materiality items are compressed before material risks/catalysts. The target is 8,000 input tokens; the 16,000 hard guard remains.

Phase 4.2 adds a separate, **evidence-only** collector over the stored AI Priority Top 15. It fetches and caches bounded SEC filing bodies (recent 8-K and 10-Q/10-K, plus the latest periodic baseline), EX-99.1/99.2, and explicitly verified company IR RSS sources. It uses local HTML/XML parsing and deterministic event rules; it never imports or calls Phase 4B or an OpenAI API. The 90-day window, filing limits, source request spacing, retry delays, materiality rules, and 8,000-token packet target are in `phase4_2_config.json`. Use the three-stock validation subset without running paid research:

```sh
python3 phase4_2_run.py --tickers CDNA,AMD,META
python3 -m unittest tests.test_phase4_2
```

Omit `--tickers` to collect for the latest stored Top 15 only. This is on-demand, not a scheduler. It saves compressed source documents, normalized events, source errors, and final packets in `data/research_data.db`; `data/phase4_2_evidence_report.json` is a human-readable audit. Repeating a scan reads filing/article bodies from cache and refreshes only SEC submissions and IR feeds after 12 hours. SEC acceptance and IR publication timestamps exclude future sources; an explicit historical `--asof` additionally requires a cached IR page snapshot retrieved by that cutoff, since an IR page fetched later may have been edited. CDNA has a verified IR domain but no verified feed, so IR is `UNAVAILABLE`; the remaining Top 15 names also need individual IR feed verification before IR can be marked available. Coverage and confidence report these gaps instead of substituting unverified news or inventing analyst consensus.

Phase 4B's separate `phase4b_pilot.py` reads the cached Phase 2/3 summary and Phase 4A packet, verifies cached source hashes locally, and never fetches SEC/IR pages or enables Web Search. The default command is an **offline** preflight showing each expected request and the current safe daily budget. Only an explicit one-off `--execute` attempts paid requests. `paid_ai_enabled` stays `false` in `phase4_config.json` before, during, and after this command; this is not a scheduler or an unattended research path. The response is committed to `research_response_archive` before usage parsing and score validation. Unknown-cost requests are never retried automatically. Invalid model evidence IDs invalidate the parsed result but never erase the raw response. All scores and traceable claims are stored separately in `evidence_reasoner_results`; `data/phase4b_evidence_pilot_report.json` preserves the completed META baseline.

The deferred continuation is **CDNA and AMD only**. It cannot execute before 2026-09-28 00:15 UTC, so the earlier UTC daily AI budget window cannot be reused. It writes a distinct `data/phase4b_cdna_amd_report.json` and never overwrites the META baseline. `phase4_config.json` now sets evidence-packet target/soft/hard estimates to 6,000/8,000/10,000 tokens. The policy builds a smaller model-facing copy without editing cached evidence: duplicate facts and redundant audit fields are removed, and only unprotected LOW events may be omitted; all removed IDs are reported. MATERIAL/HIGH and named risk, earnings, guidance, regulatory, contract, M&A, financing, accounting and executive events are retained. If protected evidence remains above the hard maximum, the request is blocked as `EVIDENCE_PACKET_OVERSIZE`. Packet token counts are local UTF-8-byte/4 **estimates**, not exact provider counts; the independent 8,000 target and 16,000 hard total-input guards still apply. Official OpenAI documentation notes that exact request counts require its token-counting API, which this offline preflight intentionally does not call.

```sh
python3 phase4b_pilot.py                # no OpenAI API call; CDNA/AMD preflight only
python3 phase4b_pilot.py --execute      # CDNA/AMD only, after the new UTC daily window
```

Budget reports now separate `confirmed_cost_usd`, `pending_unknown_cost_usd`, `reserved_cost_usd`, and `available_safe_budget_usd`. Provider usage multiplied by configured rates is a cost estimate, not an invoice; unknown Web Search charges and unanswered requests remain unknown. Request states are `RESERVED`, `IN_FLIGHT`, `COMPLETED`, `FAILED_NO_REQUEST`, `FAILED_UNKNOWN`, `BUDGET_BLOCKED`, and reconciliation outcomes. Generic transport failures are `FAILED_UNKNOWN` and stop further calls; they are never automatically retried. A reasoner-only retry must read the cached evidence and cannot proceed while the earlier request is `FAILED_UNKNOWN`. Reconciliation requires traceable request-level or manually verified evidence; aggregate organization totals alone do not prove one request's bill. OpenAI API usage/cost endpoints are documented [here](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/web_search_calls).

Run `python3 phase4_run.py budget` for an offline UTC-day breakdown saved to `data/phase4_budget_report.json` (or add `--day YYYY-MM-DD`). This reads local SQLite only and does not run research. Configured IR/news feeds are currently empty; until verified feeds or filing contents are available, SEC index metadata alone is marked insufficient and the paid reasoner remains blocked.

The current SEC cache supplies recent filing metadata and past earnings announcements, but it is **not** a complete news feed or reliable next-earnings calendar. Unknown next earnings dates remain unavailable; the engine does not invent them. Without an independently verified no-news check, a previous thesis receives at least a light refresh rather than an unjustified zero-token cache hit. Forward-return attribution remains pending because the Phase 4 research timestamp is later than the frozen Quant snapshot. Phase 5 entry decisions and Telegram recommendations are not part of this command.

### Future Phase 4 two-tier policy (offline scaffold)

`phase4_tiers.py`, `phase4_tiers_config.json`, and `phase4_tiers_schema.sql` define the frozen Phase 4 V1 model policy: `INITIAL_RESEARCH_MODEL=gpt-5.6-luna`, then deterministic escalation to `DEEP_RESEARCH_MODEL=gpt-6-sol` only when selected. Architecture status is `FROZEN_V1`; `enabled` remains false and daily tier limits remain unset, so no paid research can start. Web Search remains off. There is **no API client or scheduled runner for this path**. GPT-6 Luna A/B artifacts and all Sol/Luna baselines remain immutable and untouched.

The escalation score and hard triggers operate only on INITIAL results. Deep results are terminal for this path: unresolved material/high-risk coverage, evidence conflicts, insufficient evidence or high-risk uncertainty are persisted as `DEEP_RESEARCH_LIMITATION` reasons and never cause a Sol→Sol loop. Final behavior is `INITIAL` for no escalation, `DEEP` after successful Sol, `INITIAL_FALLBACK` after Sol failure, and `UNAVAILABLE` when no reliable Initial exists. A valid existing research result is reused as `CACHED_RESEARCH` when no material evidence arrives and the candidate score change is below the configured threshold; otherwise the flow calls for `INITIAL_REFRESH`. Initial/deep/final scores and tier-separated cost accounting remain separate. All daily limits remain unset and paid research disabled.

Forward snapshots store INITIAL, DEEP and FINAL scores separately. The anchor is the first common stock/SPY/QQQ close *after* research completion (the next trading close if research finished at or after 16:00 ET); 1/5/10/20/60-session outcomes are only added when all future bars exist. They do not reuse an earlier Quant scan close or assert predictive value. A/B validation requires the same ticker, evidence hash, Quant/Fundamental summaries, task, schema and archived request body with only `model` different, and Web Search off. The local rubric checks evidence-ID recall, grounding, citation accuracy, unsupported claims, uncertainty calibration and cost/latency ratios. Semantic thesis and bull/bear quality remain explicitly marked for independent human review, not falsely graded by the model being tested. The old Sol baseline may lack the exact archived request/body fields needed by this strict comparison; the A/B case must not be claimed valid until those can be verified without changing the baseline.

Run only the offline tests now: `python3 -m unittest tests.test_phase4_tiers -v`. The new schema is initialized by `phase4_tiers.init_db()` when a future controlled workflow is authorized; no existing database or automation is migrated by these tests. The current CDNA/AMD baseline plan remains separate and unchanged.

### Phase 5: Entry Engine

`phase5_entry.py` calculates a separate 0–100 Entry Score for completed Phase 4 research candidates. It never edits Quant, Fundamental, Candidate, Research, or Conviction scores. A ranked Top 5–10 can be supplemented by existing WATCH/position names through `select_entry_candidates`. The first pilot is explicitly limited to CDNA, AMD and META. Phase 5 has no scheduler, Telegram output, Robinhood code, or LLM client.

The nine configurable components are trend quality (18%), momentum confirmation (12%), session VWAP/price location (14%), volume (12%), relative strength against SPY/QQQ/sector (12%), support/resistance (8%), event confirmation (10%), chase control (10%), and market regime (4%). The sector benchmark comes from the frozen Phase 4 candidate packet, avoiding drift in the older security master. Daily and 1H define structure; 15M confirms entry; 5M checks timing/overheat. Raw MACD, KDJ, RSI, EMA, SMA, ADX, Bollinger, ATR and volume features are saved for all four timeframes. A missing or incomplete bar does not become a synthetic candle. Event mode requires a separately supplied, credible MATERIAL/HIGH positive event; old Phase 4 catalyst scores alone do not activate it.

The bounded data source is Massive Stocks Basic historical aggregates. Only selected candidates and SPY/QQQ/available sector ETFs are downloaded, with one 5M history per symbol; 15M and 1H are aggregated locally. Official daily bars are cached separately in `data/entry_data.db`, leaving Phase 1–4 databases unchanged. The initial window is 45 calendar days; later requests overlap the last cached day and fetch newer dates. Massive's free tier provides end-of-day availability, so `BUY_NOW_CANDIDATE`, premarket gating and opening confirmation remain offline decision functions until a reliable timely feed and fresh event checks exist. Premarket uncertainty returns WAIT. Valid Entry decisions record a reference price and future 1/5/10/20-session return, MFE and MAE once those bars exist.

The CDNA/AMD/META pilot uses existing frozen Phase 4 results and real market bars. When research was completed after the latest available bar date, its computed score is explicitly `HISTORICAL_DIAGNOSTIC_ONLY`, the state is WAIT, and the row is excluded from forward performance attribution. This prevents look-ahead claims. Run it manually:

```sh
python3 phase5_entry.py --pilot
python3 -m unittest tests.test_phase5 -v
```

The result is `data/phase5_entry_pilot.json`. No AI API call or Telegram message is made by this command. Thresholds and weights are in `phase5_config.json` and are not automatically optimized from the three-stock pilot.

### Day 0.2 grounding and Deep triage

`day0_2_plan.py` audits the saved Day 0.1 HHH response and ranks the eight pending Deep cases entirely offline. `data/deep_research_plan_day0_2.json` is a plan, not Sol authorization; it does not fetch evidence, call an AI model, or start a scheduler. The 2026-09-25 market close remains the latest actionable-data boundary. HHH's unsupported citation remains `RESEARCH_FAILED_GROUNDING`; the original response is retained and no automatic retry is permitted.

Future Phase 4B requests can use `cached_inputs(..., citation_mode="short")`. The evidence presented to the model uses E1/E2 references, while `day02_citation_aliases` keeps the immutable per-packet mapping to canonical event IDs. `validate_response` rejects unknown short IDs and stores validated citations as canonical IDs. Historical frozen A/B requests keep the default canonical-ID mode. Legacy long IDs are repaired only by unique, syntax-only normalization (date separators, controlled event-type aliases, accession punctuation); semantic similarity is never enough. `day0_1_telegram.validate_summary` checks research-state totals and hard-escalation classification before any future Day 0.1-style Telegram send.

### Shadow Day 1 date gate and one-time close run

`python3 shadow_day1_gate.py` is read-only. It requires the 2026-09-28 normal close, a completed incremental `ALPACA_SIP` sync, 2026-09-28 Alpaca daily bars for SPY/QQQ/all 11 sector ETFs, and 2026-09-28 VIX/VXN records. Waiting prohibits Entry decisions and paid Initial Research. `python3 alpaca_market.py --asof 2026-09-28` imports only missing current-session SIP daily bars; older Massive history remains. V1 source duties are Massive for 400-day history/backfill/fallback, Alpaca historical SIP for current Daily and candidate 5M, local aggregation for 15M/1H, and Yahoo for VIX/VXN. Every imported bar stores its source. Historical SIP access does not imply real-time SIP entitlement; `BUY_NOW` remains disabled.

Day 1 execution state is stored separately in `data/shadow_day1_state.db` using `shadow_day1_state.sql`. Before **any** Day 1 work, run `python3 shadow_day1_state.py status`: `ALREADY_COMPLETE`, `FAILED_STOP`, and `RUNNING_STOP` mean exit without another scan, model call, or Telegram send. After the incremental daily and volatility updates, run `python3 shadow_day1_state.py refresh`; only `READY` may advance through `python3 shadow_day1_state.py begin`, which rechecks the date gate and atomically claims the one execution. These commands do not themselves run a scan, call a model, or send Telegram. The 2026-09-28 run was manually resumed once from `FAILED / RESEARCH_PLANNING` using saved Phase 1–3 results and is now `COMPLETE`; never rerun it. The one-run research path is `shadow_day1_research.py` (offline plan), `shadow_day1_initial.py` (budgeted GPT-5.6 Luna only, Web Search off), `shadow_day1_deep.py` (offline triage, no Sol), `shadow_day1_entry.py` (Alpaca historical SIP CLOSE only), `shadow_day1_finalize.py`, and `shadow_day1_telegram.py`. The frozen global Phase 4 configuration remains unchanged. This is not a Day 2 scheduler.

The pipeline must write a final JSON artifact with `trading_date` and `market_data_as_of` both `2026-09-28`, `status: PIPELINE_COMPLETE`, every required `phase_statuses` entry marked `COMPLETE`, and `telegram_summary: READY`; only then call `pipeline_complete(...)` with the actual token/cost summary. This records the result hash and leaves only Telegram delivery pending. `claim_telegram(...)` atomically marks the deterministic `SHADOW_DAY1:2026-09-28:SUMMARY` key in flight. Save a successful `message_id` immediately with `telegram_sent(...)`; only then does the run become `COMPLETE`. If sending was definitively not attempted, `telegram_failed(..., certain_no_send=True)` allows a Telegram-only retry. A timeout or uncertain send becomes `UNKNOWN` and needs manual reconciliation; the pipeline and Telegram message are never automatically replayed. A pipeline exception is recorded with `fail_pipeline(...)` and requires manual authorization before any retry. The Massive-waiting heartbeat was removed after the Alpaca switch to prevent duplicate checks.

### Shadow V1.1 calibration and Day 2–5 preparation (not enabled)

`shadow_v11_config.json` defines a separate research-eligibility and data-quality policy without changing Phase 2, Phase 3 or Phase 5 scoring weights. `FINAL_RESEARCH_AVAILABLE` keeps a valid Initial/Deep result. `RESEARCH_LIMITED` requires valid Quant/Fundamental data, sufficient official SEC evidence, no unresolved material/high-risk issue, and no grounding failure. It has no invented Research Score: effective conviction is `min(72, candidate_score × 0.82)` and its BUY TOMORROW threshold is the existing threshold plus 8 points. Its proposed position size is capped at half the normal size. Material evidence/risk gaps, material conflicts, accounting/auditor/regulatory/fraud issues and factual misreads stay `DEEP_REQUIRED`; failed grounding stays `RESEARCH_FAILED`. Only Full and Limited are eligible for Entry observation and both are recorded as separate research-quality tiers in forward snapshots.

`ENTRY_DATA_INSUFFICIENT` is separate from a scored `REJECT_ENTRY`: the former has no Entry Score when daily, 5M, aggregated 15M/1H, current-session VWAP or volume is inadequate. Historical SIP close-only processing never emits BUY NOW. Day 2–5 Initial Research stays GPT-5.6 Luna, Web Search off, at most ten calls and $0.05 locally estimated cost per UTC budget day; Deep/Sol is triage-only and never called automatically. Existing valid research/evidence is reused unless a material filing/evidence or candidate change requires refresh. Tier cost and token usage are tracked; no trading API is used.
An earlier `FAILED_UNKNOWN` Initial request remains blocked from automatic retry, even when candidate data changes; it needs manual reconciliation.

The project-local `launchd/com.averra.stock.shadow-v11.plist` was installed as the stock-only user LaunchAgent `com.averra.stock.shadow-v11` on 2026-09-29. It runs the standalone `shadow_daily_scheduler.py` at 16:20, 16:35, 16:50 and 17:05 America/New_York on 2026-09-29 through 2026-10-02. A 17:10 check-only trigger marks an unstarted day `MISSED_DATA_WINDOW`; it never starts a new pipeline. The 17:10 cutoff is rechecked inside the atomic `READY → RUNNING` transaction, so a slow data update cannot start late, but a run already in `RUNNING` can finish research, Entry, forward tracking and Telegram after 17:10. It needs no open Terminal or Codex heartbeat. The script has an explicit 2026 date/window guard, so the calendar entries cannot run a later year's Shadow day. Each invocation incrementally checks the day's Alpaca Daily/sector data and VIX/VXN; only a complete date gate can claim `READY → RUNNING` in `data/shadow_daily_state.db`. `COMPLETE`, `FAILED`, `MISSED_DATA_WINDOW`, and `RUNNING` stop repeat work. A completed pipeline with unsent Telegram is `PIPELINE_COMPLETE_TELEGRAM_PENDING`; only Telegram is retried, and an uncertain send requires manual reconciliation to avoid duplicates. Logs go to the project `data/` directory. Daily results and Full/Limited forward snapshots are in `data/shadow_daily_results.db`. Day 2 was installed after its 2026-09-29 window and is recorded as `MISSED_DATA_WINDOW`, with no market or AI call; Day 3–5 remain scheduled.

LaunchAgents are per-user services that resume after login/restart, but the Mac must be powered on and the user logged in during a scheduled slot. A sleeping Mac may run a missed calendar event when it wakes; a powered-off Mac will not replay every missed event. A later same-day slot can still check data, provided it launches before 17:10 ET. If all four data slots are missed, the check-only trigger closes the day instead of silently running on a later date; manual review is needed. No system sleep setting is changed. To inspect without running market or AI, use `launchctl print gui/$(id -u)/com.averra.stock.shadow-v11`, `sqlite3 data/shadow_daily_state.db 'SELECT trading_date,state FROM shadow_daily_runs'`, `plutil -lint launchd/com.averra.stock.shadow-v11.plist`, or `python3 -m unittest discover -s tests -q`.

Day 3 (2026-09-30) completed after two audited, narrowly scoped manual repairs. The first fixed a historical SIP daily request that ended at 20:00 ET, in the future at the 16:20 check; historical SIP now ends at 16:01 ET and requires the 15-minute delay. Its coverage baseline now uses the latest actually stored session when a prior Shadow day was missed. The second repaired final candidate comparison to use the latest saved Top 30 (9/28, because Day 2 was missed), then resumed **finalization only** from saved Quant/Fundamental/Research/Entry/Forward artifacts. It did not replay any paid Luna calls. `shadow_daily_recovery_audit` retains both failure/recovery reasons; the final state is `COMPLETE` with Stock Telegram message ID 25. The missing 9/29 Daily bars were subsequently backfilled for Day 4–5; Day 3 snapshots remain unchanged and must be interpreted with their original 9/29 historical-data gap, not silently recomputed.

## Research rules

Universe filtering requires `COMMON_STOCK`, US, NYSE/NASDAQ/AMEX, 200 bars, price at least $3, average dollar volume at least $2 million. ETF, preferred, warrant, and OTC flags are excluded. Quant scanning calculates 20/60-day momentum, liquidity and all technical fields. The technical set is MACD, KDJ, RSI, a **20-day daily VWAP proxy**, EMA 9/21, SMA 20/50/200, ATR 14, Bollinger bands, ADX 14 and RVOL. A true intraday VWAP requires intraday bars and is not claimed here.

Scores are deterministic 0–100 research heuristics: fundamentals/growth, momentum including 20-day relative strength vs SPY, QQQ and sector, technical, event/revision, and risk. There is no calibration or backtest claim. VIX/VXN classify NORMAL, CAUTION or STRESS; STRESS prevents new buy candidates. Holdings are labeled HOLD, REDUCE or EXIT for review. Other outputs are BUY NOW CANDIDATE, BUY TOMORROW, WATCH or REJECT. `REJECT` is internal and is not sent as a Telegram alert to limit noise.

Suggested allocation starts from total virtual equity, then enforces 15% minimum cash, 20% per name, 35% per sector and 35% in names with 60-day return correlation at least 0.8. It accounts for earlier suggestions in the same scan, but reservations are only proposals and do not alter the ledger. Values are editable in `config.json`.

The dormant service checks 10:05, 12:30, 15:30 and 16:05 America/New_York on weekdays. The 16:05 pass may save a daily portfolio snapshot. This V1 uses a weekday calendar, not an exchange holiday calendar. Phase 6 service scans only a prepared local candidate packet; the old event-detector CLI is not part of the service path and cannot make an AI call during Phase 6 scheduling. Telegram polling retries transient reads after 5, 15 and 30 seconds. A potentially delivered `sendMessage` is not retried blindly because duplicate notifications could result; redelivered user commands remain idempotent.

SQLite schema is in `schema.sql`. Logs rotate in `data/stock_agent.log`; database, logs and tokens are ignored by Git. This project contains no broker or order execution code and has no code path to the existing ETF Agent.
