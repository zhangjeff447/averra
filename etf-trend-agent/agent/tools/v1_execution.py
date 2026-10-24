"""Flagged V1 execution lifecycle. All broker writes go through the injected adapter."""

import copy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from strategies.qqq_smh_agent.score_dynamic_v1 import (
    plan, revalidate_before_submission, select_one_for_existing_executor,
)
from tools.v1_cash import BROKER_OPEN, normalize_cash
from tools.v1_observed_snapshot import TERMINAL_ORDERS as BROKER_TERMINAL


from tools.v1_state_store import ACTIVE, TERMINAL
from tools.approval_compatibility import COMPATIBILITY_MODE, NATIVE_MODE, approval_required, order_from_response, response_from_error

LOCAL_ACTIVE = set(ACTIVE)


def approval_block_reason(snapshot, *, mode=NATIVE_MODE):
    """No inferred approvals from an empty ordinary order list."""
    if mode == COMPATIBILITY_MODE:
        pending = snapshot.get('v1_pending_approvals') or {}
        if (pending.get('source') == 'broker_mcp' and pending.get('account_hash') == snapshot.get('v1_account_hash') and pending.get('complete') is True and pending.get('statuses_verified') is True and type(pending.get('active_count')) is int and pending['active_count'] > 0):
            return 'WAIT_PENDING_APPROVAL'
        return None  # No proof of absence; one unresolved intent is the execution fence.
    account = snapshot.get("v1_account_hash")
    pending = snapshot.get("v1_pending_approvals") or {}
    setting = snapshot.get("v1_approval_setting") or {}
    if (not account or pending.get("source") != "broker_mcp"
            or pending.get("account_hash") != account or pending.get("complete") is not True
            or pending.get("statuses_verified") is not True
            or type(pending.get("active_count")) is not int or pending["active_count"] < 0
            or setting.get("source") != "broker_mcp" or setting.get("account_hash") != account
            or type(setting.get("enabled")) is not bool):
        return "APPROVAL_STATE_UNVERIFIED"
    if pending["active_count"] or setting["enabled"]:
        return "WAIT_PENDING_APPROVAL" if pending["active_count"] else "TRADE_APPROVAL_REQUIRED"
    return None


def allocation_block_reason(snapshot, store, *, exclude_intent_id=None):
    """No new allocation until every prior order and local intent is terminal."""
    if any(row["intent_id"] != exclude_intent_id for row in store.open_intents()):
        return "BLOCKED_BY_UNRESOLVED_INTENT"
    orders = snapshot.get("v1_broker_orders")
    if snapshot.get("orders_readable") is not True or not isinstance(orders, list):
        return "ORDERS_INCOMPLETE"
    for order in orders:
        if not isinstance(order, dict):
            return "ORDERS_INCOMPLETE"
        state = str(order.get("state") or order.get("status") or "").lower()
        if state in BROKER_OPEN:
            return "WAIT_OPEN_ORDER"
        if state not in BROKER_TERMINAL:
            return "RECONCILIATION_REQUIRED"
    return None


def reconcile_orders(store, orders, *, complete_history):
    """Read-only broker rows to persistent fill/intent state; never submit."""
    if not complete_history or not isinstance(orders, list):
        return {"status": "RECONCILIATION_REQUIRED", "reason": "order_history_incomplete"}
    by_id, by_ref = {}, {}
    for order in orders:
        if not isinstance(order, dict):
            return {"status": "RECONCILIATION_REQUIRED", "reason": "malformed_order"}
        oid = str(order.get("id") or order.get("order_id") or "")
        ref = str(order.get("ref_id") or "")
        if oid:
            if oid in by_id and by_id[oid] != order:
                return {"status": "RECONCILIATION_REQUIRED", "reason": "duplicate_broker_id"}
            by_id[oid] = order
        if ref:
            if ref in by_ref and by_ref[ref] != order:
                return {"status": "RECONCILIATION_REQUIRED", "reason": "duplicate_broker_ref"}
            by_ref[ref] = order
    unresolved = []
    for intent in store.all_intents():
        if intent["status"] == "REJECTED":
            continue
        order = (by_id.get(intent["broker_order_id"]) if intent["broker_order_id"] else None) or by_ref.get(intent["ref_id"])
        if order is None:
            if intent["status"] == "SUBMITTING":
                store.mark_unknown(intent["intent_id"], "submission_result_missing")
            if intent['status'] not in TERMINAL and intent['status'] != 'RESERVED':
                store.hold(intent['intent_id'], 'RECONCILIATION_REQUIRED', 'no_uniquely_linked_broker_order')
                unresolved.append(intent['intent_id'])
            continue
        oid = str(order.get("id") or order.get("order_id") or "")
        if not oid or (intent["broker_order_id"] and intent["broker_order_id"] != oid):
            unresolved.append(intent["intent_id"])
            continue
        if (str(order.get("symbol") or "").upper() != intent["ticker"]
                or str(order.get("side") or "").lower() != "buy"
                or (not intent["broker_order_id"] and order.get("ref_id") and str(order["ref_id"]) != intent["ref_id"])):
            unresolved.append(intent["intent_id"])
            continue
        store.record_response(intent['intent_id'], 'reconcile', order)
        state = str(order.get('state') or order.get('status') or '').lower()
        if intent['status'] not in TERMINAL:
            if approval_required(order):
                if intent['status'] != 'RESERVED':
                    store.mark_submitted(intent['intent_id'], oid, broker_status=state)
                store.hold(intent['intent_id'], 'AWAITING_USER_APPROVAL', 'explicit_broker_approval_label', broker_status=state)
                continue
            if state not in BROKER_OPEN and state not in BROKER_TERMINAL:
                store.hold(intent['intent_id'], 'RECONCILIATION_REQUIRED', 'broker_state_unrecognized', broker_status=state)
                unresolved.append(intent['intent_id'])
                continue
            if intent['status'] not in ('RESERVED', 'SUBMITTED', 'PARTIALLY_FILLED'):
                store.mark_submitted(intent['intent_id'], oid, broker_status=state)
        fills = order.get("executions")
        if isinstance(fills, list) and fills:
            for fill in fills:
                if not isinstance(fill, dict) or not fill.get("id"):
                    unresolved.append(intent["intent_id"])
                    break
                try:
                    if fill.get("currency") and str(fill["currency"]).upper() != "USD":
                        raise ValueError("execution_currency_mismatch")
                    executed_at = datetime.fromisoformat(str(fill.get("timestamp", "")).replace("Z", "+00:00"))
                    if executed_at.tzinfo is None or (executed_at - datetime.now(timezone.utc)).total_seconds() > 5:
                        raise ValueError("execution_time_unverified")
                    price = Decimal(str(fill["price"]))
                    qty = Decimal(str(fill["quantity"]))
                    fees = Decimal(str(fill.get("fees") or 0))
                    if not all(value.is_finite() and value >= 0 for value in (price, qty, fees)):
                        raise ValueError("invalid_execution")
                    cost = (price * qty + fees).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    result = store.record_fill(intent["intent_id"], fill_id=str(fill["id"]),
                                               amount=cost, executed_at=fill.get("timestamp"))
                    if result.get("status") == "RECONCILIATION_REQUIRED":
                        unresolved.append(intent["intent_id"])
                except (KeyError, ValueError, ArithmeticError):
                    unresolved.append(intent["intent_id"])
                    break
        elif order.get("cumulative_filled_amount") is not None:
            # An aggregate alone cannot assign fills to their actual budget
            # month. Keep the reservation until dated executions are available.
            try:
                aggregate = Decimal(str(order["cumulative_filled_amount"]))
                verified_zero = aggregate.is_finite() and aggregate == 0
            except (ValueError, ArithmeticError):
                verified_zero = False
            if not verified_zero:
                unresolved.append(intent["intent_id"])
        if isinstance(fills, list) and order.get("cumulative_filled_amount") is not None:
            try:
                reported = Decimal(str(order["cumulative_filled_amount"]))
                observed = Decimal(store.get(intent["intent_id"])["cumulative_filled_cents"]) / 100
                if not reported.is_finite() or reported < 0 or reported != observed:
                    unresolved.append(intent["intent_id"])
            except (ValueError, ArithmeticError):
                unresolved.append(intent["intent_id"])
        state = str(order.get("state") or order.get("status") or "").lower()
        current = store.get(intent["intent_id"])
        if state == 'pending_cancelled' and current['status'] not in TERMINAL:
            store.hold(intent['intent_id'], 'CANCEL_PENDING', 'broker_cancel_not_terminal', broker_status=state)
        elif state in ("canceled", "cancelled", "rejected", "failed", "expired", "voided", "partially_filled_rest_cancelled") and current["status"] not in TERMINAL:
            # A terminal label does not prove all executions have arrived.
            # Keep the remaining reservation until the cumulative total agrees.
            verified = intent['intent_id'] not in unresolved
            try:
                if order.get('cumulative_quantity') is not None:
                    quantity = Decimal(str(order['cumulative_quantity']))
                    executed = sum((Decimal(str(f['quantity'])) for f in (fills or [])), Decimal('0'))
                    verified = verified and quantity.is_finite() and quantity >= 0 and quantity == executed
                elif order.get('cumulative_filled_amount') is not None:
                    amount = Decimal(str(order['cumulative_filled_amount']))
                    verified = verified and amount.is_finite() and amount == Decimal(current['cumulative_filled_cents']) / 100
                else:
                    verified = False
            except (TypeError, KeyError, ArithmeticError):
                verified = False
            if verified:
                store.mark_terminal(intent["intent_id"], "CANCELED" if state in ("canceled", "cancelled", "expired", "voided", "partially_filled_rest_cancelled") else "REJECTED",
                                    broker_confirmed=True)
            else:
                unresolved.append(intent['intent_id'])
        elif state == "filled" and current["status"] != "FILLED":
            # A dollar order may finish below its requested notional. Release the
            # residual only when the broker's completed quantities match all fills.
            try:
                quantity = Decimal(str(order.get("cumulative_quantity")))
                fill_quantity = sum(Decimal(str(f["quantity"])) for f in fills)
                valid = quantity > 0 and quantity == fill_quantity and intent["intent_id"] not in unresolved
            except (TypeError, KeyError, ArithmeticError):
                valid = False
            if valid:
                store.confirm_filled(intent["intent_id"])
            else:
                unresolved.append(intent["intent_id"])
    for intent_id in set(unresolved):
        row = store.get(intent_id)
        if row and row['status'] not in TERMINAL and row['status'] != 'RESERVED':
            store.hold(intent_id, 'RECONCILIATION_REQUIRED', 'broker_execution_evidence_incomplete')
    if store.halt_reason():
        return {"status": "RECONCILIATION_REQUIRED", "reason": store.halt_reason()}
    return {"status": "RECONCILIATION_REQUIRED" if unresolved else "RECONCILED",
            "unresolved_intent_count": len(set(unresolved))}


class V1ExecutionService:
    def __init__(self, runtime, store, adapter, *, submission_authorized=False):
        self.runtime = runtime
        self.store = store
        self.adapter = adapter
        self.submission_authorized = submission_authorized

    def _normalize(self, snapshot, *, exclude_intent_id=None):
        if getattr(self.runtime, "restricted_no_open_order_mode", False):
            if "cash_normalization" in snapshot:
                return snapshot
            return dict(snapshot, cash_normalization={"cash_semantics_verified": False,
                            "reconciliation_status": "OBSERVED_SNAPSHOT_UNVERIFIED"})
        fields = snapshot.get("v1_portfolio_fields") or {}
        candidate = copy.copy(snapshot)
        if fields:
            candidate.update({"cash": fields.get("cash"), "portfolio_value": fields.get("total_value"),
                              "equity_value": fields.get("equity_value"),
                              "unleveraged_buying_power": fields.get("unleveraged_buying_power")})
            candidate.update({key: fields.get(key) for key in (
                "options_value", "futures_value", "event_contracts_value", "crypto_value",
                "mutual_funds_value", "fixed_income_value")})
        active = [row for row in self.store.open_intents() if row["intent_id"] != exclude_intent_id]
        candidate["cash_normalization"] = normalize_cash(
            candidate, active,
            cash_basis=snapshot.get("verified_cash_basis"),
            buying_power_reflects_open_orders=snapshot.get("verified_buying_power_hold_basis"),
        )
        return candidate

    def run(self, base):
        if not self.store.initialized():
            return {"status": "blocked", "reason": "V1_LEDGER_MIGRATION_REQUIRED"}
        if not self.store.account_matches_hash(base.get("snapshot", {}).get("v1_account_hash")):
            return {"status": "blocked", "reason": "V1_LEDGER_ACCOUNT_MISMATCH"}
        snapshot = base['snapshot']
        self.store.recover_uncertain()
        if snapshot.get('orders_readable') is not True or not isinstance(snapshot.get('v1_broker_orders'), list):
            return {'status': 'blocked', 'reason': 'ORDERS_INCOMPLETE'}
        reconcile = reconcile_orders(self.store, snapshot['v1_broker_orders'],
                                     complete_history=snapshot.get('budget_history_complete') is True)
        if self.store.open_intents():
            row = self.store.open_intents()[0]
            return {'status': 'blocked', 'reason': 'BLOCKED_BY_UNRESOLVED_INTENT',
                    'detail': reconcile, 'audit': self.store.audit_report(row['intent_id'])}
        if reconcile['status'] != 'RECONCILED':
            return {'status': 'blocked', 'reason': 'RECONCILIATION_REQUIRED', 'detail': reconcile}
        block_reason = allocation_block_reason(snapshot, self.store)
        if block_reason:
            return {'status': 'blocked', 'reason': block_reason}
        score_at = base.get('decision', {}).get('score_received_at')
        try:
            score_age = (datetime.now(timezone.utc) - datetime.fromisoformat(score_at)).total_seconds()
        except (TypeError, ValueError):
            score_age = None
        if score_age is None or score_age < 0 or score_age > 60:
            return {'status': 'blocked', 'reason': 'SCORE_STALE'}
        guard = self.runtime._v1_execution_guard(snapshot)
        if guard:
            return {'status': 'blocked', 'reason': guard}
        snapshot = self._normalize(snapshot)
        cash = snapshot["cash_normalization"]
        if not cash["cash_semantics_verified"]:
            return {"status": "blocked", "reason": cash["reconciliation_status"]}
        original = base["decision"].get("score_dynamic_v1") or {}
        selected = select_one_for_existing_executor(original)
        if selected is None:
            return {"status": "skipped", "reason": original.get("reason", "no_v1_buy_candidate")}
        symbol, wanted = selected["symbol"], selected["amount"]
        decision = dict(base["decision"]["decisions"][symbol], symbol=symbol)
        decision["amount"] = float(wanted)
        guard = self.runtime._v1_execution_guard(snapshot, decision)
        if guard:
            return {"status": "blocked", "reason": guard}
        ok, reason = self.runtime._risk_check(decision, snapshot)
        if not ok:
            return {"status": "blocked", "reason": reason}
        broker_ids = {str(o.get("id") or o.get("order_id")) for o in snapshot.get("open_orders", [])}
        broker_symbols = {str(o.get("symbol") or "").upper() for o in snapshot.get("open_orders", [])}
        item = original["etfs"][symbol]
        try:
            intent = self.store.reserve(
                decision_id=self.runtime._decision_id(decision),
                run_id=original.get("run_id") or "", snapshot_id=original["snapshot_id"],
                ticker=symbol, requested_amount=wanted, score=item["final_score"],
                base_amount=item["base_suggested_amount"], multiplier=item["multiplier"],
                policy_version=original["policy_version"], safe_cash=cash["safe_spendable_cash"],
                daily_limit=self.runtime.daily_limit,
                broker_daily_filled=snapshot.get("daily_filled_buys"),
                broker_daily_open=snapshot.get("daily_open_buy_amount"),
                local_unmatched=cash["local_unreflected_reservations"],
                max_order=self.runtime.max_order,
                broker_open_symbols=broker_symbols, broker_open_ids=broker_ids,
                regime_context=item.get('regime_context'))
        except (ValueError, KeyError) as exc:
            return {"status": "blocked", "reason": str(exc)}

        intent_id = intent["intent_id"]
        try:
            fresh = (self.runtime.fetch_restricted_observed_snapshot(exclude_intent_id=intent_id)
                     if getattr(self.runtime, "restricted_no_open_order_mode", False)
                     else self.runtime.fetch_live_account_snapshot())
            if fresh.get("orders_readable") is not True or not isinstance(fresh.get("v1_broker_orders"), list):
                self.store.reject_before_submit(intent_id, "fresh_orders_incomplete")
                return {"status": "blocked", "reason": "ORDERS_INCOMPLETE"}
            refreshed = reconcile_orders(self.store, fresh.get("v1_broker_orders"),
                                         complete_history=fresh.get("budget_history_complete") is True)
            if refreshed["status"] != "RECONCILED":
                self.store.reject_before_submit(intent_id, "fresh_order_sync_unavailable")
                return {"status": "blocked", "reason": "RECONCILIATION_REQUIRED"}
            block_reason = allocation_block_reason(fresh, self.store, exclude_intent_id=intent_id)
            if block_reason:
                self.store.reject_before_submit(intent_id, "fresh_prior_order_or_intent")
                return {"status": "blocked", "reason": block_reason}
            fresh = self._normalize(fresh, exclude_intent_id=intent_id)
            if not fresh["cash_normalization"]["cash_semantics_verified"]:
                self.store.reject_before_submit(intent_id, "fresh_cash_unavailable")
                return {"status": "blocked", "reason": fresh["cash_normalization"]["reconciliation_status"]}
            if (datetime.now(timezone.utc) - datetime.fromisoformat(score_at)).total_seconds() > 60:
                self.store.reject_before_submit(intent_id, "score_stale_before_submit")
                return {"status": "blocked", "reason": "SCORE_STALE"}
            scores = self.runtime.score_fresh_snapshot(fresh)
            if getattr(self.runtime, 'cash_regime_v2_enabled', False):
                fresh = self.runtime.prepare_cash_regime(fresh, exclude_intent_id=intent_id)
            fresh_plan = plan(fresh, scores, daily_limit=self.runtime.daily_limit,
                              per_order_limit=self.runtime.max_order, run_id=original.get("run_id"))
            valid, reason = revalidate_before_submission(original, fresh_plan, symbol)
            if not valid:
                self.store.reject_before_submit(intent_id, reason)
                fields = ('decision', 'final_score', 'final_permitted_amount', 'stage_id', 'regime_context')
                comparison = {key: {'planned': original['etfs'][symbol].get(key),
                                    'fresh': fresh_plan.get('etfs', {}).get(symbol, {}).get(key)}
                              for key in fields
                              if original['etfs'][symbol].get(key) != fresh_plan.get('etfs', {}).get(symbol, {}).get(key)}
                return {"status": "blocked", "reason": reason, "intent_id": intent_id,
                        "audit": self.store.audit_report(intent_id), "plan_changes": comparison}
            latest_item = fresh_plan['etfs'][symbol]
            if latest_item['final_permitted_amount'] != wanted:
                wanted = latest_item['final_permitted_amount']
                decision['amount'] = float(wanted)
                decision.setdefault('metadata', {})['score_dynamic_v1'] = latest_item
                self.store.refresh_reserved_amount(intent_id, requested_amount=wanted,
                    base_amount=latest_item['base_suggested_amount'], multiplier=latest_item['multiplier'],
                    snapshot_id=fresh_plan['snapshot_id'], safe_cash=fresh['cash_normalization']['safe_spendable_cash'],
                    daily_limit=self.runtime.daily_limit, max_order=self.runtime.max_order,
                    broker_daily_filled=fresh.get('daily_filled_buys'),
                    broker_daily_open=fresh.get('daily_open_buy_amount'), regime_context=latest_item.get('regime_context'))
            ok, reason = self.runtime._risk_check(decision, fresh)
            if not ok:
                self.store.reject_before_submit(intent_id, reason)
                return {"status": "blocked", "reason": reason}
            quote = fresh.get("quotes", {}).get(symbol)
            if not isinstance(quote, dict) or not self.runtime._is_quote_fresh_for_live(quote):
                self.store.reject_before_submit(intent_id, "quote_stale")
                return {"status": "blocked", "reason": "quote_stale"}
            params, reason = self.runtime._build_order_params(decision, quote, fresh, fresh.get("capabilities", {}))
            if params is None or params.get("dollar_amount") != wanted:
                self.store.reject_before_submit(intent_id, "order_amount_changed")
                return {"status": "blocked", "reason": "order_amount_changed"}
            preview = self.adapter.call_tool_exact("review_equity_order", params)
            self.store.record_response(intent_id, 'preview', preview)
            preview_data = preview.get('data') if isinstance(preview, dict) and isinstance(preview.get('data'), dict) else preview
            # The documented customer flag describes approval after placement;
            # preview creates no request in the app. All ordinary preview,
            # Telegram, freshness and single-submission gates still apply.
            if approval_required(preview) and not (isinstance(preview_data, dict) and preview_data.get('customer_approval_required') is True):
                self.store.hold(intent_id, 'AWAITING_USER_APPROVAL', 'preview_explicitly_requires_approval')
                return {'status': 'AWAITING_USER_APPROVAL', 'intent_id': intent_id,
                        'audit': self.store.audit_report(intent_id)}
            data = preview.get("data") if isinstance(preview, dict) and isinstance(preview.get("data"), dict) else preview
            if not isinstance(data, dict) or data.get("order_checks") != {} or str(data.get("dollar_amount")) != wanted:
                self.store.reject_before_submit(intent_id, "broker_preview_rejected")
                return {"status": "blocked", "reason": "broker_preview_rejected"}
            guard = (self.runtime._v1_execution_guard(fresh, decision)
                     or self.runtime._v1_preview_guard(preview, params, fresh, quote))
            if guard:
                self.store.reject_before_submit(intent_id, guard)
                return {"status": "blocked", "reason": guard}
            if not self.submission_authorized:
                self.store.reject_before_submit(intent_id, "v1_submission_not_approved")
                return {"status": "blocked", "reason": "v1_submission_not_approved"}
            if not self.runtime._v1_notify_preview(preview, params):
                self.store.reject_before_submit(intent_id, "preview_notification_unconfirmed")
                return {"status": "blocked", "reason": "PREVIEW_NOTIFICATION_UNCONFIRMED"}
            if not 0 <= (datetime.now(timezone.utc) - datetime.fromisoformat(score_at)).total_seconds() <= 60:
                self.store.reject_before_submit(intent_id, "score_stale_after_preview")
                return {"status": "blocked", "reason": "SCORE_STALE"}
            self.store.mark_submitting(intent_id)
            # Stable documented broker idempotency reference. Never retry a
            # transport-uncertain write even with this reference available.
            place_params = dict(params, ref_id=intent["ref_id"])
            try:
                placed = self.adapter.call_tool_exact("place_equity_order", place_params)
            except Exception as exc:
                response = response_from_error(exc)
                if response is not None:
                    self.store.record_response(intent_id, 'submit', response)
                if approval_required(response):
                    self.store.hold(intent_id, 'AWAITING_USER_APPROVAL', 'submit_error_explicitly_requires_approval')
                    return {'status': 'AWAITING_USER_APPROVAL', 'intent_id': intent_id,
                            'audit': self.store.audit_report(intent_id)}
                self.store.mark_unknown(intent_id, type(exc).__name__)
                return {"status": "EXECUTION_UNKNOWN", "intent_id": intent_id,
                        "audit": self.store.audit_report(intent_id)}
            self.store.record_response(intent_id, 'submit', placed)
            order = order_from_response(placed)
            if order and ((order.get('symbol') and order['symbol'] != symbol)
                    or (order.get('side') and str(order['side']).lower() != 'buy')):
                order = None
            oid = (order.get('id') or order.get('order_id')) if order else None
            if oid:
                self.store.mark_submitted(intent_id, oid, broker_status=order.get('state', order.get('status')))
            if approval_required(placed) or (isinstance(preview_data, dict) and preview_data.get('customer_approval_required') is True):
                self.store.hold(intent_id, 'AWAITING_USER_APPROVAL', 'submit_explicitly_requires_approval')
            elif not oid:
                self.store.hold(intent_id, 'APPROVAL_STATE_UNKNOWN', 'incomplete_or_unrecognized_submit_response')
            elif (order.get('state') or order.get('status')) and self.store.get(intent_id)['status'] not in TERMINAL:
                # Facts from this uniquely-bound response may establish partial or
                # terminal execution, never an empty-list inference.
                row = dict(order, id=oid, symbol=symbol, side='buy')
                reconcile_orders(self.store, [row], complete_history=True)
            current = self.store.get(intent_id)
            return {'status': current['status'], 'intent_id': intent_id, 'order_id': oid,
                    'requested_amount': wanted, 'filled_amount': f"{current['cumulative_filled_cents'] / 100:.2f}",
                    'audit': self.store.audit_report(intent_id)}
        except Exception as exc:
            response = response_from_error(exc)
            if approval_required(response):
                self.store.record_response(intent_id, 'preview', response)
                self.store.hold(intent_id, 'AWAITING_USER_APPROVAL', 'preview_error_explicitly_requires_approval')
                return {'status': 'AWAITING_USER_APPROVAL', 'intent_id': intent_id,
                        'audit': self.store.audit_report(intent_id)}
            state = self.store.get(intent_id)
            if state and state["status"] == "RESERVED":
                self.store.reject_before_submit(intent_id, "pre_submit_exception:" + type(exc).__name__)
            elif state and state["status"] == "SUBMITTING":
                self.store.mark_unknown(intent_id, "exception_after_submit_mark:" + type(exc).__name__)
            return {"status": "blocked", "reason": "V1_EXECUTION_ERROR:" + type(exc).__name__}
