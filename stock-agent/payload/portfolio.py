"""User-confirmed virtual accounting; suggestions never change cash."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

CENT = Decimal("0.01")
TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def money(value: str | int | float | Decimal) -> int:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Amount must be positive")
    cents = int((amount / CENT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if cents <= 0 or Decimal(cents) / 100 != amount:
        raise ValueError("Amount must be at least $0.01 with at most two decimal places")
    return cents


def usd(cents: int) -> str:
    return f"${Decimal(cents) / 100:,.2f}"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price_is_fresh(observed_at: str, max_age_hours: float) -> bool:
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return False
        current = datetime.now(timezone.utc)
        age = (current - observed.astimezone(timezone.utc)).total_seconds()
        if not -300 <= age <= max_age_hours * 3600:
            return False
        day = observed.date()
        weekdays = 0
        while day < current.date():
            day += timedelta(days=1)
            weekdays += day.weekday() < 5
        return weekdays <= 1
    except (TypeError, ValueError, OverflowError):
        return False


class Portfolio:
    def __init__(self, path: str | Path, max_quote_age_hours: float = 72):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        self.max_quote_age_hours = max_quote_age_hours
        self._savepoint = 0
        self._migrate_ledger()
        for table, additions in {
            "tracked_holdings": {"entry_date": "TEXT", "entry_price": "REAL",
                                 "entry_price_source": "TEXT", "entry_thesis_json": "TEXT",
                                 "exit_price": "REAL", "exit_price_source": "TEXT"},
            "holding_events": {"price": "REAL", "price_source": "TEXT", "metrics_json": "TEXT"},
            "exit_decisions": {"lifecycle_id": "TEXT"},
        }.items():
            existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, kind in additions.items():
                if name not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        snapshot_cols = {r["name"] for r in self.db.execute("PRAGMA table_info(portfolio_snapshots)")}
        if "ledger_max_id" not in snapshot_cols:
            with self.atomic():
                self.db.execute("ALTER TABLE portfolio_snapshots ADD COLUMN ledger_max_id INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def atomic(self):
        self._savepoint += 1
        key = f"p{self._savepoint}"
        self.db.execute(f"SAVEPOINT {key}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {key}")
            self.db.execute(f"RELEASE SAVEPOINT {key}")
            raise
        else:
            self.db.execute(f"RELEASE SAVEPOINT {key}")

    def _migrate_ledger(self):
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(ledger)")}
        if {"source", "resulting_cash_cents", "realized_pl_cents"} - columns:
            with self.atomic():
                for name, kind in (("source", "TEXT"), ("resulting_cash_cents", "INTEGER"), ("realized_pl_cents", "INTEGER")):
                    if name not in columns:
                        self.db.execute(f"ALTER TABLE ledger ADD COLUMN {name} {kind}")
        definition = self.db.execute("SELECT sql FROM sqlite_master WHERE name='ledger'").fetchone()[0]
        if "'ADJUSTMENT'" not in definition:
            with self.atomic():
                self.db.execute("ALTER TABLE ledger RENAME TO ledger_v1_migration")
                self.db.execute("""CREATE TABLE ledger (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('DEPOSIT','BUY','SELL','ADJUSTMENT')),
                    ticker TEXT, amount_cents INTEGER NOT NULL CHECK(amount_cents != 0),
                    shares TEXT, fill_price TEXT, price_source TEXT, note TEXT,
                    source TEXT, resulting_cash_cents INTEGER, realized_pl_cents INTEGER)""")
                self.db.execute("""INSERT INTO ledger SELECT id,created_at,kind,ticker,amount_cents,
                    shares,fill_price,price_source,note,source,resulting_cash_cents,realized_pl_cents
                    FROM ledger_v1_migration""")
                self.db.execute("DROP TABLE ledger_v1_migration")
        self.db.executescript("""CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
            BEGIN SELECT RAISE(ABORT,'ledger is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
            BEGIN SELECT RAISE(ABORT,'ledger is immutable'); END;""")

    def close(self):
        self.db.close()

    def cash_cents(self) -> int:
        return self.db.execute("""SELECT COALESCE(SUM(CASE kind
            WHEN 'DEPOSIT' THEN amount_cents WHEN 'SELL' THEN amount_cents
            WHEN 'ADJUSTMENT' THEN amount_cents ELSE -amount_cents END),0) FROM ledger""").fetchone()[0]

    def _record(self, kind, amount, ticker=None, shares=None, price=None, price_source=None,
                note=None, source="USER_COMMAND", realized=None):
        self.db.execute("""INSERT INTO ledger(created_at,kind,ticker,amount_cents,shares,fill_price,
            price_source,note,source,resulting_cash_cents,realized_pl_cents) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), kind, ticker, amount, str(shares) if shares is not None else None,
             str(price) if price is not None else None, price_source, note, source, self.cash_cents(), realized))

    def deposit(self, amount: str, source="TELEGRAM") -> int:
        cents = money(amount)
        with self.atomic():
            self.db.execute("INSERT INTO ledger(created_at,kind,amount_cents,source,resulting_cash_cents) VALUES(?,?,?,?,?)",
                            (now(), "DEPOSIT", cents, source, self.cash_cents() + cents))
        return self.cash_cents()

    def adjust(self, amount: str, note: str, source="USER_COMMAND") -> int:
        if not note.strip():
            raise ValueError("Adjustment requires a note")
        delta = -money(amount[1:]) if amount.startswith("-") else money(amount)
        with self.atomic():
            cash = self.cash_cents() + delta
            if cash < 0:
                raise ValueError("Adjustment exceeds virtual cash")
            self.db.execute("INSERT INTO ledger(created_at,kind,amount_cents,note,source,resulting_cash_cents) VALUES(?,?,?,?,?,?)",
                            (now(), "ADJUSTMENT", delta, note, source, cash))
        return cash

    def quote(self, ticker: str, price: str | float, observed_at: str):
        ticker = ticker.upper()
        if not TICKER.fullmatch(ticker):
            raise ValueError("Invalid ticker")
        p = Decimal(str(price))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if not p.is_finite() or p <= 0 or observed.tzinfo is None:
            raise ValueError("Invalid quote")
        with self.atomic():
            self.db.execute("INSERT INTO quotes VALUES(?,?,?) ON CONFLICT(ticker) DO UPDATE SET price=excluded.price, observed_at=excluded.observed_at",
                            (ticker, str(p), observed_at))

    def get_quote(self, ticker: str):
        return self.db.execute("SELECT * FROM quotes WHERE ticker=?", (ticker.upper(),)).fetchone()

    def fresh_quote(self, ticker: str):
        quote = self.get_quote(ticker)
        if quote is None:
            return None
        return quote if price_is_fresh(quote["observed_at"], self.max_quote_age_hours) else None

    def positions(self):
        return list(self.db.execute("SELECT * FROM positions ORDER BY ticker"))

    def summary(self):
        cash = self.cash_cents()
        deposited = self.db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE kind='DEPOSIT'").fetchone()[0]
        realized = self.db.execute("SELECT COALESCE(SUM(realized_pl_cents),0) FROM ledger WHERE kind='SELL'").fetchone()[0]
        rows, stale = [], []
        market = cost = 0
        for pos in self.positions():
            q = self.fresh_quote(pos["ticker"])
            price = Decimal(q["price"]) if q else None
            value = _cents(Decimal(pos["shares"]) * price * 100) if q else None
            if not q:
                stale.append(pos["ticker"])
            market += value if value is not None else pos["cost_cents"]
            cost += pos["cost_cents"]
            rows.append({"ticker": pos["ticker"], "shares": pos["shares"], "sector": pos["sector"],
                         "cost_cents": pos["cost_cents"], "avg_cost": f"{Decimal(pos['cost_cents']) / 100 / Decimal(pos['shares']):.2f}",
                         "price": str(price) if price else None, "market_value_cents": value,
                         "unrealized_pl_cents": value - pos["cost_cents"] if value is not None else None})
        total = cash + market
        for row in rows:
            val = row["market_value_cents"] if row["market_value_cents"] is not None else row["cost_cents"]
            row["weight_pct"] = round(100 * val / total, 2) if total else 0
        return {"deposited_cents": deposited, "cash_cents": cash, "invested_market_cents": market,
                "portfolio_value_cents": total, "realized_pl_cents": realized,
                "unrealized_pl_cents": market - cost if not stale else None,
                "positions": rows, "stale_tickers": stale, "valuation_status": "PRICE_STALE" if stale else "OK"}

    def equity_cents(self) -> tuple[int, list[str]]:
        state = self.summary()
        return state["portfolio_value_cents"], state["stale_tickers"]

    def _execution_price(self, ticker, fill_price):
        q = self.fresh_quote(ticker) if fill_price is None else None
        if fill_price is None and q is None:
            raise ValueError("PRICE_STALE: supply actual fill price")
        price = Decimal(fill_price) if fill_price is not None else Decimal(q["price"])
        if not price.is_finite() or price <= 0:
            raise ValueError("Invalid fill price")
        return price

    def buy(self, ticker: str, amount: str, sector: str, fill_price: str | None = None, source="USER_COMMAND"):
        ticker = ticker.upper()
        if not TICKER.fullmatch(ticker):
            raise ValueError("Invalid ticker")
        cents = money(amount)
        price = self._execution_price(ticker, fill_price)
        shares = Decimal(cents) / (price * 100)
        with self.atomic():
            if cents > self.cash_cents():
                raise ValueError("Insufficient virtual cash")
            old = self.db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
            existing = Decimal(old["shares"]) if old else Decimal(0)
            existing_cost = old["cost_cents"] if old else 0
            self.db.execute("INSERT INTO positions VALUES(?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET shares=excluded.shares,cost_cents=excluded.cost_cents,sector=excluded.sector",
                            (ticker, str(existing + shares), existing_cost + cents, old["sector"] if old else sector))
            self._record("BUY", cents, ticker, shares, price,
                         "USER_CONFIRMED_PRICE" if fill_price else "ESTIMATED_PRICE", source=source)
        return shares, bool(fill_price)

    def sell(self, ticker: str, amount: str, fill_price: str | None = None, source="USER_COMMAND"):
        ticker = ticker.upper()
        if not TICKER.fullmatch(ticker):
            raise ValueError("Invalid ticker")
        cents = money(amount)
        price = self._execution_price(ticker, fill_price)
        shares = Decimal(cents) / (price * 100)
        with self.atomic():
            old = self.db.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if old is None or shares > Decimal(old["shares"]) + Decimal("0.00000001"):
                raise ValueError("Sale exceeds virtual position")
            remaining = max(Decimal(0), Decimal(old["shares"]) - shares)
            remaining_cost = _cents(Decimal(old["cost_cents"]) * remaining / Decimal(old["shares"]))
            if remaining < Decimal("0.00000001"):
                remaining_cost = 0
                self.db.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
            else:
                self.db.execute("UPDATE positions SET shares=?,cost_cents=? WHERE ticker=?", (str(remaining), remaining_cost, ticker))
            self._record("SELL", cents, ticker, shares, price,
                         "USER_CONFIRMED_PRICE" if fill_price else "ESTIMATED_PRICE", source=source,
                         realized=cents - (old["cost_cents"] - remaining_cost))
        return shares, bool(fill_price)

    def add_suggestion(self, ticker: str, action: str, cents: int, reasons: str):
        with self.atomic():
            self.db.execute("INSERT INTO suggestions(created_at,ticker,action,suggested_cents,reasons) VALUES(?,?,?,?,?)",
                            (now(), ticker, action, cents, reasons))

    def skip(self, ticker: str | None = None) -> bool:
        query = "SELECT id FROM suggestions WHERE status='OPEN'"
        args: tuple = ()
        if ticker:
            query += " AND ticker=?"
            args = (ticker,)
        row = self.db.execute(query + " ORDER BY id DESC LIMIT 1", args).fetchone()
        if row is None:
            return False
        with self.atomic():
            self.db.execute("UPDATE suggestions SET status='SKIPPED' WHERE id=?", (row["id"],))
        return True

    def history(self, limit: int = 10):
        return list(self.db.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)))

    def daily_snapshot(self, asof: str, spy_close=None, qqq_close=None):
        state = self.summary()
        with self.atomic():
            cursor = self.db.execute("""INSERT INTO portfolio_snapshots(asof,captured_at,cash_cents,
                market_value_cents,portfolio_value_cents,unrealized_pl_cents,realized_pl_cents,
                positions_json,spy_close,qqq_close,status,ledger_max_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (asof, now(), state["cash_cents"], state["invested_market_cents"] if not state["stale_tickers"] else None,
                 state["portfolio_value_cents"] if not state["stale_tickers"] else None,
                 state["unrealized_pl_cents"], state["realized_pl_cents"], json.dumps(state["positions"]),
                 spy_close, qqq_close, state["valuation_status"],
                 self.db.execute("SELECT COALESCE(MAX(id),0) FROM ledger").fetchone()[0]))
            return cursor.lastrowid

    def performance(self, snapshot_id: int):
        end = self.db.execute("SELECT * FROM portfolio_snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if end is None or end["status"] != "OK":
            return []
        results = []
        for horizon, days in (("1M", 30), ("3M", 90), ("6M", 180), ("1Y", 365)):
            start_date = (datetime.fromisoformat(end["asof"]) - timedelta(days=days)).date().isoformat()
            start = self.db.execute("""SELECT * FROM portfolio_snapshots WHERE asof<=? AND status='OK'
                ORDER BY asof DESC,id DESC LIMIT 1""", (start_date,)).fetchone()
            if start is None or not start["portfolio_value_cents"] or (datetime.fromisoformat(start_date).date() - datetime.fromisoformat(start["asof"]).date()).days > 7:
                continue
            points = [start] + list(self.db.execute("""SELECT * FROM portfolio_snapshots
                WHERE id>? AND id<=? AND status='OK' ORDER BY id""", (start["id"], end["id"])))
            factor = peak = 1.0
            max_drawdown = 0.0
            for previous, current in zip(points, points[1:]):
                flows = self.db.execute("""SELECT COALESCE(SUM(CASE kind WHEN 'DEPOSIT' THEN amount_cents
                    WHEN 'ADJUSTMENT' THEN amount_cents ELSE 0 END),0) FROM ledger
                    WHERE id>? AND id<=?""", (previous["ledger_max_id"], current["ledger_max_id"])).fetchone()[0]
                if not previous["portfolio_value_cents"]:
                    factor = None
                    break
                factor *= 1 + (current["portfolio_value_cents"] - previous["portfolio_value_cents"] - flows) / previous["portfolio_value_cents"]
                peak = max(peak, factor)
                max_drawdown = min(max_drawdown, factor / peak - 1)
            if factor is None:
                continue
            ret = factor - 1
            spy = end["spy_close"] / start["spy_close"] - 1 if end["spy_close"] and start["spy_close"] else None
            qqq = end["qqq_close"] / start["qqq_close"] - 1 if end["qqq_close"] and start["qqq_close"] else None
            row = {"horizon": horizon, "return": ret, "spy_return": spy, "qqq_return": qqq,
                   "spy_alpha": ret - spy if spy is not None else None,
                   "qqq_alpha": ret - qqq if qqq is not None else None}
            with self.atomic():
                self.db.execute("""INSERT OR REPLACE INTO portfolio_performance VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (snapshot_id, horizon, start["id"], ret, spy, qqq, row["spy_alpha"], row["qqq_alpha"],
                     max_drawdown, "FLOW_ADJUSTED_CHAINED_V1"))
            results.append(row)
        return results
