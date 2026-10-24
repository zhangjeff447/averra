"""SQLite credit, reservation and fill journal for the flagged ETF V1 path."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo


ACTIVE = ("RESERVED", "SUBMITTING", "SUBMITTED", "AWAITING_USER_APPROVAL",
          "APPROVAL_STATE_UNKNOWN", "EXECUTION_UNKNOWN", "PARTIALLY_FILLED",
          "CANCEL_PENDING", "RECONCILIATION_REQUIRED")
TERMINAL = ("FILLED", "REJECTED", "CANCELED")


def cents(value):
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("invalid_money")
    if number != number.quantize(Decimal("0.01")):
        raise ValueError("money_precision_exceeds_cent")
    return int(number * 100)


def dollars(value):
    return f"{Decimal(value) / 100:.2f}"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class V1StateStore:
    @classmethod
    def open_read_only(cls, path):
        """Inspect an existing stage ledger without creating or changing it."""
        store = cls.__new__(cls)
        store.path = Path(path)
        store.db = sqlite3.connect("file:" + str(store.path.resolve()) + "?mode=ro", uri=True)
        store.db.row_factory = sqlite3.Row
        return store

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credits (month_id TEXT PRIMARY KEY, cents INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS baseline_spend (month_id TEXT PRIMARY KEY, cents INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY, decision_id TEXT UNIQUE NOT NULL,
                run_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, policy_version TEXT NOT NULL,
                ticker TEXT NOT NULL, requested_cents INTEGER NOT NULL,
                reserved_remaining_cents INTEGER NOT NULL, cumulative_filled_cents INTEGER NOT NULL DEFAULT 0,
                reported_cumulative_cents INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, broker_order_id TEXT UNIQUE, ref_id TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL, last_reconciled_at TEXT,
                score TEXT NOT NULL, base_cents INTEGER NOT NULL, multiplier TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL,
                cents INTEGER NOT NULL, observed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, intent_id TEXT,
                event TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(intents)')}
        for name, declaration in (('submit_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('last_broker_status', 'TEXT')):
            if name not in columns:
                self.db.execute('ALTER TABLE intents ADD COLUMN ' + name + ' ' + declaration)
                if name == 'submit_attempts':
                    self.db.execute("UPDATE intents SET submit_attempts=1 WHERE status IN ('SUBMITTING','SUBMITTED','PARTIALLY_FILLED','EXECUTION_UNKNOWN','FILLED','CANCELED')")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _audit(self, db, intent_id, event, detail=""):
        db.execute("INSERT INTO audit(intent_id,event,detail,at) VALUES(?,?,?,?)",
                   (intent_id, event, detail, utc_now()))

    def initialize_from_legacy(self, ledger_path, *, account=None, verified_account_hash=None):
        """One-time import from a verified, consistent copy of the legacy JSON."""
        raw = Path(ledger_path).read_bytes()
        old = json.loads(raw)
        if (account is None) == (verified_account_hash is None):
            raise ValueError('one_verified_account_identity_required')
        expected = (hashlib.sha256(str(account).encode()).hexdigest() if account is not None
                    else str(verified_account_hash))
        if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError('verified_account_hash_invalid')
        if old.get("version") != 1 or old.get("account") != expected:
            raise ValueError("legacy_account_or_version_mismatch")
        if not old.get("start_month") or not old.get("active_month"):
            raise ValueError("legacy_month_missing")
        source_hash = hashlib.sha256(raw).hexdigest()
        with self.transaction() as db:
            prior = db.execute("SELECT value FROM meta WHERE key='migration_source_hash'").fetchone()
            if prior:
                if prior[0] != source_hash:
                    raise ValueError("legacy_source_changed_after_migration")
                return self.summary(db)
            if db.execute("SELECT COUNT(*) FROM credits").fetchone()[0] or db.execute("SELECT COUNT(*) FROM intents").fetchone()[0]:
                raise ValueError("nonempty_database_cannot_import")
            for month, value in old.get("credits", {}).items():
                db.execute("INSERT INTO credits VALUES(?,?)", (month, cents(value)))
            for month, value in old.get("spent", {}).items():
                db.execute("INSERT INTO baseline_spend VALUES(?,?)", (month, cents(value)))
            for key, value in (("account_hash", expected), ("start_month", old["start_month"]),
                               ("active_month", old["active_month"]), ("migration_source_hash", source_hash)):
                db.execute("INSERT INTO meta VALUES(?,?)", (key, value))
            self._audit(db, None, "LEGACY_MIGRATED", source_hash)
            return self.summary(db)

    def credit_through(self, month_id, allowance):
        datetime.strptime(month_id, "%Y-%m")
        active = self.db.execute("SELECT value FROM meta WHERE key='active_month'").fetchone()
        if not active or month_id < active[0]:
            raise ValueError("budget_clock_or_migration_invalid")
        cursor = active[0]
        self.credit_month(cursor, allowance)
        while cursor < month_id:
            year, month = map(int, cursor.split("-"))
            cursor = f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
            self.credit_month(cursor, allowance)
        return self.summary()

    def credit_month(self, month_id, allowance):
        if len(month_id) != 7 or month_id[4] != "-":
            raise ValueError("invalid_month_id")
        value = cents(allowance)
        with self.transaction() as db:
            old = db.execute("SELECT cents FROM credits WHERE month_id=?", (month_id,)).fetchone()
            if old and old[0] != value:
                raise ValueError("monthly_credit_conflict")
            if not old:
                active = db.execute("SELECT value FROM meta WHERE key='active_month'").fetchone()
                if not active or month_id <= active[0]:
                    raise ValueError("retroactive_credit_prohibited")
                year, month = map(int, active[0].split("-"))
                expected = f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
                if month_id != expected:
                    raise ValueError("missing_intervening_month_credit")
                db.execute("INSERT INTO credits VALUES(?,?)", (month_id, value))
                db.execute("UPDATE meta SET value=? WHERE key='active_month'", (month_id,))
                self._audit(db, None, "MONTHLY_CREDIT", month_id + ":" + dollars(value))
            return self.summary(db)

    def summary(self, db=None):
        db = db or self.db
        credits = db.execute("SELECT COALESCE(SUM(cents),0) FROM credits").fetchone()[0]
        baseline = db.execute("SELECT COALESCE(SUM(cents),0) FROM baseline_spend").fetchone()[0]
        filled = db.execute("SELECT COALESCE(SUM(cents),0) FROM fills").fetchone()[0]
        reserved = db.execute("SELECT COALESCE(SUM(reserved_remaining_cents),0) FROM intents WHERE status NOT IN (?,?,?)", TERMINAL).fetchone()[0]
        return {"balance": dollars(credits - baseline - filled), "reserved": dollars(reserved),
                "available": dollars(credits - baseline - filled - reserved),
                "credits": dollars(credits), "baseline_spend": dollars(baseline),
                "v1_filled": dollars(filled)}

    def initialized(self):
        return self.db.execute("SELECT 1 FROM meta WHERE key='migration_source_hash'").fetchone() is not None

    def account_matches_hash(self, account_hash):
        saved = self.db.execute("SELECT value FROM meta WHERE key='account_hash'").fetchone()
        return bool(saved and account_hash and saved[0] == account_hash)

    def month_snapshot(self, now=None):
        current = now or datetime.now(timezone.utc)
        month = current.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m")
        credits = self.db.execute("SELECT COALESCE(SUM(cents),0) FROM credits WHERE month_id<=?", (month,)).fetchone()[0]
        baseline_rows = self.db.execute("SELECT month_id,cents FROM baseline_spend").fetchall()
        prior = sum(row[1] for row in baseline_rows if row[0] < month)
        this_month = sum(row[1] for row in baseline_rows if row[0] == month)
        for row in self.db.execute("SELECT cents,observed_at FROM fills"):
            trade_month = datetime.fromisoformat(row[1]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m")
            if trade_month < month:
                prior += row[0]
            elif trade_month == month:
                this_month += row[0]
        current_credit = self.db.execute("SELECT cents FROM credits WHERE month_id=?", (month,)).fetchone()
        return {"monthly_budget_limit": dollars(credits - prior),
                "monthly_filled_buys": dollars(this_month),
                "monthly_new_allowance": dollars(current_credit[0] if current_credit else 0)}

    def matches_broker_spend(self, broker_spend):
        if not isinstance(broker_spend, dict):
            return False
        expected = {row[0]: row[1] for row in self.db.execute("SELECT month_id,cents FROM baseline_spend")}
        for row in self.db.execute("SELECT cents,observed_at FROM fills"):
            month = datetime.fromisoformat(row[1]).astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m")
            expected[month] = expected.get(month, 0) + row[0]
        start = self.db.execute("SELECT value FROM meta WHERE key='start_month'").fetchone()[0]
        try:
            return all(expected.get(month, 0) == cents(broker_spend.get(month, 0))
                       for month in set(expected) | {m for m in broker_spend if m >= start})
        except (ValueError, ArithmeticError):
            return False

    def confirm_filled(self, intent_id):
        with self.transaction() as db:
            row = db.execute("SELECT cumulative_filled_cents FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row[0] <= 0:
                raise ValueError("fill_proof_missing")
            db.execute("UPDATE intents SET status='FILLED',reserved_remaining_cents=0,last_reconciled_at=? WHERE intent_id=?",
                       (utc_now(), intent_id))
            self._audit(db, intent_id, "BROKER_FILLED_CONFIRMED")

    def open_intents(self):
        rows = self.db.execute("SELECT * FROM intents WHERE status NOT IN (?,?,?)", TERMINAL).fetchall()
        return [dict(row, reserved_remaining=dollars(row["reserved_remaining_cents"])) for row in rows]

    def all_intents(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM intents")]

    def get(self, intent_id):
        row = self.db.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def qqq_buy_gate(self, score, *, now=None):
        """Reset at 09:30 ET; second buy needs +10 unless both scores are >=50."""
        current = now or utc_now()
        if not _valid_time(current):
            return 'QQQ_DAILY_HISTORY_UNVERIFIED'
        instant = datetime.fromisoformat(current)
        local = instant.astimezone(ZoneInfo('America/New_York'))
        start = local.replace(hour=9, minute=30, second=0, microsecond=0)
        if local < start:
            start -= timedelta(days=1)
        today = []
        for row in self.db.execute("SELECT * FROM intents WHERE ticker='QQQ' AND (submit_attempts>0 OR broker_order_id IS NOT NULL OR cumulative_filled_cents>0) ORDER BY created_at,intent_id"):
            if not _valid_time(row['created_at']) or datetime.fromisoformat(row['created_at']) > instant:
                return 'QQQ_DAILY_HISTORY_UNVERIFIED'
            if datetime.fromisoformat(row['created_at']) >= start:
                today.append(row)
        if len(today) >= 2:
            return 'QQQ_DAILY_TWO_BUY_LIMIT'
        try:
            value = Decimal(str(score))
            if not value.is_finite() or not 0 <= value <= 100:
                return 'QQQ_SCORE_UNVERIFIED'
            if today:
                first = today[0]
                if first['status'] not in TERMINAL or first['cumulative_filled_cents'] <= 0:
                    return 'QQQ_FIRST_BUY_NOT_FILLED'
                reference = Decimal(first['score'])
                if not reference.is_finite() or not 0 <= reference <= 100:
                    return 'QQQ_SCORE_UNVERIFIED'
                both_at_least_50 = reference >= Decimal('50') and value >= Decimal('50')
                if not both_at_least_50 and value < reference + Decimal('10'):
                    return 'QQQ_SECOND_BUY_SCORE_INCREASE_BELOW_10'
        except (ArithmeticError, ValueError, TypeError):
            return 'QQQ_SCORE_UNVERIFIED'
        return None

    def smh_buy_gate(self, *, now=None, last_buy_time=None):
        """One SMH submission per session, reset at 09:30 America/New_York."""
        current = now or utc_now()
        if not _valid_time(current):
            return 'SMH_DAILY_HISTORY_UNVERIFIED'
        instant = datetime.fromisoformat(current)
        local = instant.astimezone(ZoneInfo('America/New_York'))
        start = local.replace(hour=9, minute=30, second=0, microsecond=0)
        if local < start:
            start -= timedelta(days=1)
        times = [row[0] for row in self.db.execute("SELECT created_at FROM intents WHERE ticker='SMH' AND (submit_attempts>0 OR broker_order_id IS NOT NULL OR cumulative_filled_cents>0)")]
        times.extend(row[0] for row in self.db.execute("SELECT f.observed_at FROM fills f JOIN intents i ON i.intent_id=f.intent_id WHERE i.ticker='SMH'"))
        if last_buy_time is not None:
            times.append(last_buy_time)
        for value in times:
            if not _valid_time(value):
                return 'SMH_DAILY_HISTORY_UNVERIFIED'
            at = datetime.fromisoformat(value)
            if at > instant:
                return 'SMH_DAILY_HISTORY_UNVERIFIED'
            if at >= start:
                return 'SMH_DAILY_ONE_BUY_LIMIT'
        return None

    def audit_report(self, intent_id):
        row = self.get(intent_id)
        if row is None:
            raise ValueError("intent_not_found")
        events = [dict(item) for item in self.db.execute(
            "SELECT event,detail,at FROM audit WHERE intent_id=? ORDER BY seq", (intent_id,))]
        return {"intent_id": intent_id, "decision_id": row["decision_id"],
                "run_id": row["run_id"], "snapshot_id": row["snapshot_id"],
                "policy_version": row["policy_version"], "ticker": row["ticker"],
                "requested_amount": dollars(row["requested_cents"]),
                "filled_amount": dollars(row["cumulative_filled_cents"]),
                "reserved_remaining": dollars(row["reserved_remaining_cents"]),
                "broker_order_id": row["broker_order_id"], "state": row["status"],
                "last_reconciled_at": row["last_reconciled_at"], "created_at": row["created_at"],
                "submit_attempts": row["submit_attempts"], "known_broker_status": row["last_broker_status"], "events": events}

    def reserve(self, *, decision_id, run_id, snapshot_id, ticker, requested_amount,
                score, base_amount, multiplier, policy_version, safe_cash,
                daily_limit, broker_daily_filled, broker_daily_open,
                local_unmatched, max_order, broker_open_symbols=(), broker_open_ids=(), now=None,
                regime_context=None):
        from tools.cash_regime_state import cash_mode
        cash_v2 = cash_mode(self)
        if ticker not in ("SMH", "QQQ") or policy_version != ('cash_regime_v2' if cash_v2 else 'score_dynamic_v1'):
            raise ValueError("intent_policy_invalid")
        requested = cents(requested_amount)
        if requested < 100 or requested > min(cents(max_order), 10000):
            raise ValueError("broker_minimum_or_order_cap")
        if requested > cents(safe_cash):
            raise ValueError("cash_limit")
        current = now or utc_now()
        if not _valid_time(current):
            raise ValueError("invalid_intent_time")
        with self.transaction() as db:
            if db.execute("SELECT value FROM meta WHERE key='halt_reason'").fetchone():
                raise ValueError("ledger_halted")
            if db.execute("SELECT 1 FROM intents WHERE decision_id=? OR run_id=?", (decision_id, run_id)).fetchone():
                raise ValueError("duplicate_decision_or_run")
            if db.execute("SELECT 1 FROM intents WHERE status NOT IN (?,?,?)", TERMINAL).fetchone():
                raise ValueError("BLOCKED_BY_UNRESOLVED_INTENT")
            if ticker in broker_open_symbols:
                raise ValueError("broker_pending_order")
            if ticker == 'QQQ':
                reason = self.qqq_buy_gate(score, now=current)
                if reason:
                    raise ValueError(reason)
            else:
                reason = self.smh_buy_gate(now=current)
                if reason:
                    raise ValueError(reason)
            summary = self.summary(db)
            if not cash_v2 and requested > cents(summary["available"]):
                raise ValueError("budget_limit")
            matched = set(map(str, broker_open_ids))
            open_rows = db.execute("SELECT reserved_remaining_cents,broker_order_id FROM intents WHERE status NOT IN (?,?,?)", TERMINAL).fetchall()
            db_unmatched = sum(row[0] for row in open_rows if not row[1] or row[1] not in matched)
            newly_reserved = max(0, db_unmatched - cents(local_unmatched))
            if requested + newly_reserved > cents(safe_cash):
                raise ValueError("cash_reserved_by_concurrent_intent")
            occupied = cents(broker_daily_open) + db_unmatched
            day = datetime.fromisoformat(current).astimezone(ZoneInfo("America/New_York")).date()
            local_daily_filled = sum(
                fill["cents"] for fill in db.execute("SELECT cents,observed_at FROM fills")
                if datetime.fromisoformat(fill["observed_at"]).astimezone(ZoneInfo("America/New_York")).date() == day)
            if requested + max(cents(broker_daily_filled), local_daily_filled) + occupied > cents(daily_limit):
                raise ValueError("daily_limit")
            # A run can reserve at most one candidate, even across processes.
            intent_id = str(uuid.uuid4())
            db.execute("""INSERT INTO intents(intent_id,decision_id,run_id,snapshot_id,policy_version,ticker,
                       requested_cents,reserved_remaining_cents,status,ref_id,created_at,score,base_cents,multiplier)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (intent_id, decision_id, run_id, snapshot_id, policy_version, ticker,
                        requested, requested, "RESERVED", intent_id, current,
                        str(score), cents(base_amount), str(multiplier)))
            if cash_v2:
                from config.unified import load_config
                from tools.cash_regime_state import bind_reservation, verified_policy
                config, _ = load_config()
                if not verified_policy(self, getattr(config, 'cash_regime_policy_hash', None)):
                    raise ValueError('cash_policy_hash_mismatch')
                bind_reservation(self, db, intent_id, ticker, score, multiplier, requested, cents(base_amount),
                                 regime_context, config.cash_regime_policy)
            self._audit(db, intent_id, "RESERVED", dollars(requested))
            return self.get(intent_id)

    def refresh_reserved_amount(self, intent_id, *, requested_amount, base_amount, multiplier,
                                snapshot_id, safe_cash, daily_limit, max_order,
                                broker_daily_filled, broker_daily_open, regime_context):
        """Atomically replace an unsubmitted reservation with the verified fresh plan."""
        from tools.cash_regime_state import cash_mode
        from strategies.qqq_smh_agent.score_dynamic_v1 import FRESH_PLAN_AMOUNT_TOLERANCE
        requested, base = cents(requested_amount), cents(base_amount)
        cash_v2 = cash_mode(self)
        with self.transaction() as db:
            row = self.get(intent_id)
            if (not row or row['status'] != 'RESERVED' or row['submit_attempts'] != 0
                    or row['cumulative_filled_cents'] != 0 or row['broker_order_id']):
                raise ValueError('reservation_refresh_requires_unsubmitted_intent')
            if abs(requested - row['requested_cents']) > cents(FRESH_PLAN_AMOUNT_TOLERANCE):
                raise ValueError('reservation_refresh_exceeds_tolerance')
            if db.execute("SELECT 1 FROM audit WHERE intent_id=? AND event='PLAN_AMOUNT_REFRESHED_WITHIN_5_USD'", (intent_id,)).fetchone():
                raise ValueError('reservation_already_refreshed')
            factor = Decimal(str(multiplier))
            if (not factor.is_finite() or factor <= 0 or factor > 1
                    or factor != Decimal(row['multiplier']) or requested > int(Decimal(base) * factor)):
                raise ValueError('reservation_refresh_exceeds_score_amount')
            if db.execute("SELECT value FROM meta WHERE key='halt_reason'").fetchone():
                raise ValueError('ledger_halted')
            if db.execute("SELECT 1 FROM intents WHERE intent_id<>? AND status NOT IN (?,?,?)", (intent_id, *TERMINAL)).fetchone():
                raise ValueError('BLOCKED_BY_UNRESOLVED_INTENT')
            if requested < 100 or requested > min(cents(max_order), 10000):
                raise ValueError('broker_minimum_or_order_cap')
            if requested > cents(safe_cash):
                raise ValueError('cash_limit')
            summary = self.summary(db)
            if not cash_v2 and requested > cents(summary['available']) + row['reserved_remaining_cents']:
                raise ValueError('budget_limit')
            day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York')).date()
            filled = sum(fill['cents'] for fill in db.execute('SELECT cents,observed_at FROM fills')
                         if datetime.fromisoformat(fill['observed_at']).astimezone(ZoneInfo('America/New_York')).date() == day)
            if requested + max(cents(broker_daily_filled), filled) + cents(broker_daily_open) > cents(daily_limit):
                raise ValueError('daily_limit')
            if cash_v2:
                from config.unified import load_config
                from tools.cash_regime_state import bind_reservation, verified_policy
                config, _ = load_config()
                if not verified_policy(self, config.cash_regime_policy_hash):
                    raise ValueError('cash_policy_hash_mismatch')
                db.execute('DELETE FROM regime_intents WHERE intent_id=?', (intent_id,))
                bind_reservation(self, db, intent_id, row['ticker'], row['score'], multiplier,
                                 requested, base, regime_context, config.cash_regime_policy)
            detail = json.dumps({'previous_amount': dollars(row['requested_cents']),
                                 'fresh_amount': dollars(requested), 'previous_snapshot_id': row['snapshot_id'],
                                 'fresh_snapshot_id': snapshot_id, 'score': row['score']}, sort_keys=True)
            db.execute('UPDATE intents SET requested_cents=?,reserved_remaining_cents=?,base_cents=?,snapshot_id=? WHERE intent_id=?',
                       (requested, requested, base, snapshot_id, intent_id))
            self._audit(db, intent_id, 'PLAN_AMOUNT_REFRESHED_WITHIN_5_USD', detail)
            return self.get(intent_id)

    def mark_submitting(self, intent_id):
        with self.transaction() as db:
            row = db.execute("SELECT status,submit_attempts FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row[0] != "RESERVED" or row[1] != 0:
                raise ValueError("submit_state_invalid_or_already_attempted")
            if db.execute("SELECT 1 FROM intents WHERE intent_id<>? AND status NOT IN (?,?,?)", (intent_id, *TERMINAL)).fetchone():
                raise ValueError("BLOCKED_BY_UNRESOLVED_INTENT")
            db.execute("UPDATE intents SET status='SUBMITTING',submit_attempts=1 WHERE intent_id=?", (intent_id,))
            self._audit(db, intent_id, "SUBMITTING")

    def reject_before_submit(self, intent_id, reason):
        with self.transaction() as db:
            row = db.execute("SELECT status,submit_attempts FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row[0] != "RESERVED" or row[1] != 0:
                raise ValueError("pre_submit_release_invalid")
            db.execute("UPDATE intents SET status='REJECTED',reserved_remaining_cents=0 WHERE intent_id=?", (intent_id,))
            self._audit(db, intent_id, "REJECTED", reason)

    def hold(self, intent_id, state, reason, *, broker_status=None):
        if state not in ACTIVE or state == 'RESERVED':
            raise ValueError('invalid_hold_state')
        with self.transaction() as db:
            row = db.execute("SELECT status FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row[0] in TERMINAL:
                raise ValueError('hold_state_invalid')
            db.execute("UPDATE intents SET status=?,last_broker_status=COALESCE(?,last_broker_status),last_reconciled_at=? WHERE intent_id=?",
                       (state, broker_status, utc_now(), intent_id))
            self._audit(db, intent_id, state, reason)

    def record_response(self, intent_id, stage, response):
        from tools.ops_utils import redact_payload
        def private(value):
            if isinstance(value, dict):
                return {k: '[REDACTED]' if k.lower() in {'account', 'account_number'} else private(v) for k, v in value.items()}
            if isinstance(value, list):
                return [private(v) for v in value]
            return value
        payload = json.dumps(private(redact_payload(response)), sort_keys=True, default=str)
        with self.transaction() as db:
            if stage == 'reconcile' and isinstance(response, dict):
                db.execute('UPDATE intents SET last_broker_status=? WHERE intent_id=?', (str(response.get('state') or response.get('status') or 'UNKNOWN'), intent_id))
            self._audit(db, intent_id, 'BROKER_' + stage.upper() + '_RESPONSE', payload)

    def mark_unknown(self, intent_id, reason):
        self.hold(intent_id, 'EXECUTION_UNKNOWN', reason)

    def mark_submitted(self, intent_id, order_id, *, broker_status=None):
        if not order_id:
            return self.mark_unknown(intent_id, "order_id_missing")
        with self.transaction() as db:
            row = db.execute("SELECT status,broker_order_id FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row[0] in TERMINAL or row[0] == 'RESERVED':
                raise ValueError("submission_state_invalid")
            if row[1] and row[1] != str(order_id):
                raise ValueError('broker_order_identity_conflict')
            db.execute("UPDATE intents SET status='SUBMITTED',broker_order_id=?,last_broker_status=COALESCE(?,last_broker_status) WHERE intent_id=?",
                       (str(order_id), broker_status, intent_id))
            self._audit(db, intent_id, "SUBMITTED", str(order_id))

    def record_fill(self, intent_id, *, fill_id, amount=None, cumulative=None, executed_at=None):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row["status"] == "REJECTED":
                raise ValueError("fill_state_invalid")
            if cumulative is not None:
                target = cents(cumulative)
                previous = max(row["reported_cumulative_cents"], row["cumulative_filled_cents"])
                if target < previous or target < row["cumulative_filled_cents"]:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('halt_reason','cumulative_fill_regression')")
                    self._audit(db, intent_id, "RECONCILIATION_REQUIRED", "cumulative_fill_regression")
                    return {"status": "RECONCILIATION_REQUIRED"}
                delta = target - previous
                db.execute("UPDATE intents SET reported_cumulative_cents=? WHERE intent_id=?", (target, intent_id))
                fill_id = fill_id or f"cumulative:{intent_id}:{target}"
            else:
                delta = cents(amount)
                if not fill_id:
                    raise ValueError("fill_id_missing")
            if delta == 0:
                return self.get(intent_id)
            prior = db.execute("SELECT cents,intent_id FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
            if prior:
                if prior[0] != delta or prior[1] != intent_id:
                    db.execute("INSERT OR REPLACE INTO meta VALUES('halt_reason','fill_id_revision')")
                    self._audit(db, intent_id, "RECONCILIATION_REQUIRED", "fill_id_revision:" + fill_id)
                    return {"status": "RECONCILIATION_REQUIRED"}
                return self.get(intent_id)
            trade_time = executed_at or utc_now()
            if not _valid_time(trade_time):
                raise ValueError("invalid_fill_time")
            db.execute("INSERT INTO fills VALUES(?,?,?,?)", (fill_id, intent_id, delta, trade_time))
            filled = row["cumulative_filled_cents"] + delta
            remaining = max(0, row["requested_cents"] - filled) if row["status"] not in ("CANCELED", "FILLED") else 0
            state = row["status"] if row["status"] in ("CANCELED", "FILLED") else "PARTIALLY_FILLED"
            db.execute("UPDATE intents SET cumulative_filled_cents=?,reserved_remaining_cents=?,status=?,last_reconciled_at=? WHERE intent_id=?",
                       (filled, remaining, state, utc_now(), intent_id))
            self._audit(db, intent_id, "FILL", fill_id + ":" + dollars(delta))
            if row['status'] == 'FILLED':
                db.execute("INSERT OR REPLACE INTO meta VALUES('halt_reason','terminal_fill_revision')")
                self._audit(db, intent_id, 'HALTED', 'new_execution_after_confirmed_fill')
            if filled > row["requested_cents"] or Decimal(self.summary(db)["available"]) < 0:
                db.execute("INSERT OR REPLACE INTO meta VALUES('halt_reason','fill_exceeds_verified_budget_or_request')")
                self._audit(db, intent_id, "HALTED", "fill_exceeds_verified_budget_or_request")
            return self.get(intent_id)

    def mark_terminal(self, intent_id, state, *, broker_confirmed=False):
        if state not in ("CANCELED", "REJECTED") or not broker_confirmed:
            raise ValueError("terminal_not_confirmed")
        with self.transaction() as db:
            row = db.execute("SELECT status FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if not row or row[0] in TERMINAL:
                raise ValueError("terminal_state_invalid")
            db.execute("UPDATE intents SET status=?,reserved_remaining_cents=0,last_reconciled_at=? WHERE intent_id=?",
                       (state, utc_now(), intent_id))
            self._audit(db, intent_id, state, "broker_confirmed")

    def recover_uncertain(self):
        with self.transaction() as db:
            rows = db.execute("SELECT intent_id FROM intents WHERE status='SUBMITTING'").fetchall()
            for row in rows:
                db.execute("UPDATE intents SET status='EXECUTION_UNKNOWN' WHERE intent_id=?", (row[0],))
                self._audit(db, row[0], "EXECUTION_UNKNOWN", "recovered_after_restart")
            return len(rows)

    def halt_reason(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='halt_reason'").fetchone()
        return row[0] if row else None


def _valid_time(value):
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except (TypeError, ValueError):
        return False
