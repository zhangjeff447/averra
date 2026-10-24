# ETF Return First · 2026.10.15-RF-rc2

This candidate implements the user-selected `sma50-200-cash0` strategy with a native broker execution path. Core SMH/QQQ are BUY-only; TQQQ supports notional BUY and share-based SELL. Settled net TQQQ proceeds rotate toward the whole-account 80/20 core deficits.

The actual runtime is `run_agent.py`, not the preserved legacy scripts. The operator deployment and rollback procedure is in `../INSTALL.txt`. `deployment.py` creates a separate runtime with local activation disabled. It preserves every legacy ledger table, references the existing credential configuration, and renders—but does not load—two LaunchAgents.

The old production code remains separate. Old rollback release: `2026.10.15-8020-r1`. This candidate is not enabled on the real account. Native read-only discovery and account/history/three-symbol reads have succeeded; fresh quote/funds/valuation acceptance remains blocked by after-close stale quotes. Historical parity is not live acceptance.

Execution requires fresh operator-recorded read-only acceptance, unchanged policy and implementation hashes, no other ETF executor/report service, verified own funds, fresh quotes, complete reconciliation, no pending approvals/unresolved intents, and normal scheduled market windows. Unknown submissions never retry; pre-submit abandoned reservations may recover only with zero submission attempts. Core sells, borrowing, other assets and automated approvals remain prohibited.

Run tests from this folder using `python -m pytest`. The suite uses fake transports and historical public inputs; it never sends real orders or Telegram messages. Actual API schema verification uses read-only discovery only.

The preserved old modules and operations documentation are compatibility references; use only the new entry points described in INSTALL.txt for this candidate.
