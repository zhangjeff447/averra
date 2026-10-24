"""Validate large daily jumps against stored corporate actions; never alter source bars."""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timezone


def inspect_jumps(db, ticker, bars, cfg):
    """Return corrected in-memory bars, validations, and safe large-move dates."""
    bars = [dict(bar) for bar in bars]
    checks, allowed = [], set()
    actions = db.execute("""SELECT action_date,action_type,source,details_json FROM corporate_actions
        WHERE ticker=? ORDER BY action_date""", (ticker,)).fetchall()
    for index in range(1, len(bars)):
        prev, current = bars[index - 1], bars[index]
        jump = current["close"] / prev["close"] - 1
        if abs(jump) <= cfg["jump_threshold"]:
            continue
        day = current["date"]
        nearby = [a for a in actions if abs((date.fromisoformat(a[0]) - date.fromisoformat(day)).days)
                  <= cfg["event_date_tolerance_days"]]
        nearby.sort(key=lambda a: abs((date.fromisoformat(a[0]) - date.fromisoformat(day)).days))
        event = nearby[0] if nearby else None
        kind = "UNEXPLAINED_ANOMALY"
        status = "QUARANTINED"
        ratio = None
        source = None
        event_day = None
        if event:
            event_day, action_type, source = event[0], event[1], event[2]
            detail = json.loads(event[3])
            if action_type in {"forward_split", "reverse_split"}:
                kind = "CONFIRMED_SPLIT" if action_type == "forward_split" else "CONFIRMED_REVERSE_SPLIT"
                try:
                    ratio = float(detail["split_from"]) / float(detail["split_to"])
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    ratio = None
                # Massive bars are already adjusted. Correct only a provably unadjusted seam.
                if ratio and ratio > 0 and abs(current["close"] / prev["close"] / ratio - 1) <= cfg["raw_split_ratio_tolerance"]:
                    residual = current["close"] / (prev["close"] * ratio) - 1
                    if abs(residual) <= cfg["adjusted_continuity_tolerance"]:
                        for old in bars[:index]:
                            for field in ("open", "high", "low", "close"):
                                old[field] *= ratio
                            old["volume"] = round(old["volume"] / ratio)
                        status = "RECOVERED_IN_MEMORY"
            else:
                kind = "OTHER_CORPORATE_ACTION"
        elif index + 1 < len(bars):
            lookback = [b["volume"] for b in bars[max(0, index - 20):index] if b["volume"] > 0]
            rvol = current["volume"] / statistics.mean(lookback) if lookback else 0
            next_reversal = abs(bars[index + 1]["close"] / current["close"] - 1)
            if (rvol >= cfg["true_move_min_rvol"] and next_reversal <= cfg["true_move_max_next_day_reversal"]
                    and abs(jump) <= cfg["extreme_jump_quarantine"]):
                kind, status = "TRUE_PRICE_MOVE", "VALIDATED"
                allowed.add(day)
        else:
            kind = "INSUFFICIENT_DATA"
        checks.append({"ticker": ticker, "jump_date": day, "corporate_action_type": kind,
                       "corporate_action_date": event_day, "split_ratio": ratio, "source": source,
                       "validation_status": status,
                       "details": {"original_return": jump, "source_bars": "MASSIVE_ADJUSTED",
                                   "adjusted_in_memory": status == "RECOVERED_IN_MEMORY"}})
    return bars, checks, allowed


def persist_checks(db, checks):
    if not checks:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db:
        db.executemany("""INSERT INTO action_validations VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker,jump_date) DO UPDATE SET corporate_action_type=excluded.corporate_action_type,
            corporate_action_date=excluded.corporate_action_date,split_ratio=excluded.split_ratio,
            source=excluded.source,validation_status=excluded.validation_status,
            details_json=excluded.details_json,checked_at=excluded.checked_at""",
            [(c["ticker"], c["jump_date"], c["corporate_action_type"], c["corporate_action_date"],
              c["split_ratio"], c["source"], c["validation_status"], json.dumps(c["details"]), now)
             for c in checks])
