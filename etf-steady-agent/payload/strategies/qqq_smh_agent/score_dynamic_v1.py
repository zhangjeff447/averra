"""Score Based Dynamic Allocation V1: pure planning; no order calls."""

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_DOWN


POLICY_VERSION = "score_dynamic_v1"
CENT = Decimal("0.01")
BROKER_DOCUMENTED_MINIMUM = Decimal("1.00")
FRESH_PLAN_AMOUNT_TOLERANCE = Decimal("5.00")
TARGETS = {"SMH": Decimal("0.80"), "QQQ": Decimal("0.20")}
# The sole V1 score table. Upper endpoints are exclusive, except 100.
SCORE_TIERS = (
    (Decimal("40"), "STRONG_WAIT", Decimal("0")),
    (Decimal("55"), "WAIT", Decimal("0")),
    (Decimal("60"), "VERY_LIGHT_BUY", Decimal("0.20")),
    (Decimal("65"), "LIGHT_BUY", Decimal("0.35")),
    (Decimal("70"), "MODERATE_BUY", Decimal("0.50")),
    (Decimal("75"), "BUY", Decimal("0.65")),
    (Decimal("80"), "STRONG_BUY", Decimal("0.75")),
    (Decimal("85"), "STRONG_BUY_PLUS", Decimal("0.85")),
    (Decimal("90"), "VERY_STRONG_BUY", Decimal("0.95")),
    (Decimal("101"), "MAX_BUY", Decimal("1.00")),
)
OPEN_STATES = {"open", "pending", "submitted", "partially_filled", "queued", "confirmed", "unconfirmed"}
CLOSED_STATES = {"filled", "rejected", "failed", "cancelled", "canceled", "expired"}


class PlanningUnavailable(ValueError):
    pass


def decimal(value, field):
    if isinstance(value, bool) or value is None:
        raise PlanningUnavailable(field + "_unavailable")
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation):
        raise PlanningUnavailable(field + "_invalid") from None
    if not number.is_finite() or number < 0:
        raise PlanningUnavailable(field + "_invalid")
    return number


def down(value):
    return value.quantize(CENT, rounding=ROUND_DOWN)


def score_tier(raw_score):
    try:
        score = decimal(raw_score, "score")
    except PlanningUnavailable:
        return "SCORE_INVALID", Decimal("0")
    if score > 100:
        return "SCORE_INVALID", Decimal("0")
    for ceiling, name, multiplier in SCORE_TIERS:
        if score < ceiling:
            return name, multiplier
    return "SCORE_INVALID", Decimal("0")


def _position_value(position, symbol, quotes):
    if not isinstance(position, dict):
        raise PlanningUnavailable("position_unavailable:" + symbol)
    quantity = decimal(position.get("quantity", position.get("qty", 0)), "quantity")
    if position.get("market_value") is not None:
        value = decimal(position["market_value"], "market_value")
        if quantity > 0 and value <= 0:
            raise PlanningUnavailable("position_value_unavailable:" + symbol)
        return value
    if quantity == 0:
        return Decimal("0")
    quote = (quotes or {}).get(symbol) or {}
    price = decimal(quote.get("last"), "quote")
    if price <= 0 or quote.get("is_stale") is True:
        raise PlanningUnavailable("quote_unavailable:" + symbol)
    return quantity * price


def _open_reservations(snapshot):
    orders = snapshot.get("open_orders")
    if not isinstance(orders, list):
        raise PlanningUnavailable("orders_unavailable")
    by_symbol = {symbol: Decimal("0") for symbol in TARGETS}
    seen = {}
    for order in orders:
        if not isinstance(order, dict):
            raise PlanningUnavailable("order_unavailable")
        state = str(order.get("state") or order.get("status") or "").lower()
        if state in CLOSED_STATES:
            continue
        if state not in OPEN_STATES:
            raise PlanningUnavailable("order_state_unknown")
        if str(order.get("side") or "").lower() != "buy":
            continue
        symbol = str(order.get("symbol") or order.get("ticker") or "").upper()
        if symbol not in TARGETS:
            raise PlanningUnavailable("open_non_etf_buy_unavailable")
        order_id = order.get("order_id") or order.get("id")
        if not order_id:
            raise PlanningUnavailable("order_id_unavailable")
        amount = next((order[key] for key in ("remaining_notional", "open_notional", "reserved_amount")
                       if order.get(key) is not None), None)
        if amount is None:
            gross = order.get("amount", order.get("dollar_amount"))
            if gross is None:
                raise PlanningUnavailable("order_reservation_unavailable")
            filled = next((order[key] for key in ("filled_notional", "executed_notional", "filled_amount")
                           if order.get(key) is not None), None)
            if state == "partially_filled" and filled is None:
                raise PlanningUnavailable("partial_fill_unavailable")
            amount = decimal(gross, "order_amount") - decimal(filled or 0, "filled_amount")
        reserved = decimal(amount, "reserved_amount")
        identity = (symbol, str(reserved), state)
        if str(order_id) in seen:
            if seen[str(order_id)] != identity:
                raise PlanningUnavailable("duplicate_order_conflict")
            continue
        seen[str(order_id)] = identity
        by_symbol[symbol] += reserved
    return by_symbol


def _weights_and_cash(snapshot, reservations):
    normalized = snapshot.get("cash_normalization")
    if normalized is not None:
        if normalized.get("cash_semantics_verified") is not True:
            raise PlanningUnavailable(normalized.get("reconciliation_status", "cash_semantics_unverified"))
        values = {symbol: decimal(normalized["market_values"][symbol], "market_value") for symbol in TARGETS}
        cash = decimal(normalized["cash_for_weight"], "cash_for_weight")
        spendable = decimal(normalized["safe_spendable_cash"], "safe_spendable_cash")
        return values, cash, sum(values.values()) + cash, spendable, normalized["cash_basis"]
    positions = snapshot.get("positions")
    if not isinstance(positions, dict):
        raise PlanningUnavailable("positions_unavailable")
    values = {symbol: _position_value(positions.get(symbol, {}), symbol, snapshot.get("quotes"))
              for symbol in TARGETS}
    reported_cash = decimal(snapshot.get("cash"), "cash")
    broker_total = snapshot.get("portfolio_value")
    reserved_total = sum(reservations.values())
    gross_cash = reported_cash
    cash_basis = "reported_cash_includes_open_order_reservations"
    if broker_total is not None:
        broker_total = decimal(broker_total, "portfolio_value")
        observed = sum(values.values()) + reported_cash
        if abs(observed - broker_total) <= CENT:
            if reserved_total and snapshot.get("cash_includes_open_order_reservations") is not True:
                raise PlanningUnavailable("cash_reservation_basis_unavailable")
        elif reserved_total > 0 and abs(observed + reserved_total - broker_total) <= CENT:
            gross_cash += reserved_total
            cash_basis = "reported_cash_excludes_open_order_reservations"
        else:
            raise PlanningUnavailable("cash_total_reconciliation_failed")
    elif reserved_total:
        raise PlanningUnavailable("cash_reservation_basis_unavailable")
    total = sum(values.values()) + gross_cash
    buying_power = decimal(snapshot.get("buying_power"), "buying_power")
    # Gross cash may already have an open-order hold. Buying power is only a cap.
    spendable = min(max(Decimal("0"), gross_cash - reserved_total), buying_power)
    return values, gross_cash, total, spendable, cash_basis


def _safe_wait(reason, snapshot_time=None):
    return {"policy_version": POLICY_VERSION, "snapshot_time": snapshot_time,
            "overall": "WAIT", "reason": reason,
            "etfs": {symbol: {"decision": "WAIT", "reason": reason, "final_permitted_amount": "0.00"}
                     for symbol in TARGETS}}


def _base_plan(snapshot, scores, *, daily_limit, per_order_limit, round_limit=None,
         pending_submission=None, snapshot_id=None, run_id=None, target_weights=None):
    """Pure simulation from one immutable account/order/score snapshot."""
    targets = TARGETS if target_weights is None else target_weights
    if set(targets) != set(TARGETS):
        raise ValueError("core_target_symbols_invalid")
    scores = scores if isinstance(scores, dict) else {}
    regime = snapshot.get('cash_regime_v2')
    cash_v2 = isinstance(regime, dict) and regime.get('policy_version') == 'cash_regime_v2'
    snapshot_time = (snapshot.get("account_sync") or {}).get("account_data_timestamp")
    if snapshot_id is None:
        fingerprint = {key: snapshot.get(key) for key in (
            "cash", "buying_power", "portfolio_value", "positions", "open_orders",
            "monthly_budget_limit", "monthly_filled_buys", "monthly_open_buy_amount",
            "daily_filled_buys", "daily_open_buy_amount", "quotes",
            "cash_normalization", "v1_portfolio_fields", "portfolio_provider_as_of")}
        fingerprint['cash_regime_v2'] = regime
        fingerprint["scores"] = scores
        fingerprint["time"] = snapshot_time
        snapshot_id = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, default=str).encode()).hexdigest()[:24]
    if pending_submission:
        return _safe_wait("EXECUTION_UNKNOWN", snapshot_time)
    normalized = snapshot.get("cash_normalization")
    if normalized is not None and normalized.get("cash_semantics_verified") is not True:
        return _safe_wait(normalized.get("reconciliation_status", "CASH_SEMANTICS_UNVERIFIED"), snapshot_time)
    for field in ("connected", "positions_readable", "orders_readable", "budget_readable", "quotes_readable"):
        if snapshot.get(field) is not True:
            return _safe_wait("DATA_UNAVAILABLE:" + field, snapshot_time)
    try:
        reservations = _open_reservations(snapshot)
        values, cash, total, spendable, cash_basis = _weights_and_cash(snapshot, reservations)
        monthly_limit = Decimal('0') if cash_v2 else decimal(snapshot.get("monthly_budget_limit"), "monthly_budget_limit")
        new_allowance_raw = snapshot.get("monthly_new_allowance")
        new_allowance = None if cash_v2 else (decimal(new_allowance_raw, "monthly_new_allowance") if new_allowance_raw is not None else None)
        monthly_filled = decimal(snapshot.get("monthly_filled_buys"), "monthly_filled_buys")
        daily_filled = decimal(snapshot.get("daily_filled_buys"), "daily_filled_buys")
        daily_cap = decimal(daily_limit, "daily_limit")
        per_order_cap = decimal(per_order_limit, "per_order_limit")
        round_cap = daily_cap if round_limit is None else decimal(round_limit, "round_limit")
        reported_open = decimal(snapshot.get("monthly_open_buy_amount"), "monthly_open_buy_amount")
        reported_daily_open = decimal(snapshot.get("daily_open_buy_amount"), "daily_open_buy_amount")
        local_unreflected = decimal(normalized.get("local_unreflected_reservations", 0), "local_reservation") if normalized else Decimal("0")
        local_by_symbol = {symbol: decimal((normalized.get("local_unreflected_by_symbol") or {}).get(symbol, 0),
                                          "local_reservation") if normalized else Decimal("0") for symbol in targets}
        # Source aggregates only agentic orders. Count all visible reservations
        # conservatively without counting the same agent order twice.
        occupied = max(reported_open, sum(reservations.values())) + local_unreflected
        daily_occupied = max(reported_daily_open, sum(reservations.values())) + local_unreflected
        budget_available = max(Decimal("0"), monthly_limit - monthly_filled - occupied)
        daily_available = max(Decimal("0"), daily_cap - daily_filled - daily_occupied)
        gaps = {symbol: max(Decimal("0"), total * weight - values[symbol]) for symbol, weight in targets.items()}
        adjusted = {symbol: max(Decimal("0"), gaps[symbol] - reservations[symbol] - local_by_symbol[symbol])
                    for symbol in targets}
        gap_total = sum(adjusted.values())
        if cash_v2:
            budget_available = spendable
        shared = min(spendable, budget_available, daily_available, round_cap, gap_total)
        scale = min(Decimal("1"), shared / gap_total) if gap_total else Decimal("0")
    except PlanningUnavailable as exc:
        return _safe_wait("DATA_UNAVAILABLE:" + str(exc), snapshot_time)

    etfs = {}
    for symbol, target in targets.items():
        base = adjusted[symbol] * scale
        tier, multiplier = score_tier(scores.get(symbol))
        adjusted_amount = base * multiplier
        stage_id = None
        if cash_v2:
            from .cash_regime_v2 import light_tier, stage_amount
            if regime.get('mode') == 'EARLY_INVEST':
                tier, multiplier = light_tier(scores.get(symbol))
                base = down(base)
                adjusted_amount = base * multiplier
            elif regime.get('mode') == 'RISK_RETENTION':
                tier, multiplier, stage_id, allowed = stage_amount(regime, symbol, scores.get(symbol))
                stage = next((x for x in regime.get('stages', {}).get(symbol, []) if x['id'] == stage_id), None)
                base = Decimal(stage['authorized_cents']) / 100 if stage else Decimal('0')
                adjusted_amount = min(allowed, adjusted[symbol], shared)
            else:
                return _safe_wait('REGIME_STATE_UNVERIFIED', snapshot_time)
        final = down(min(adjusted_amount, per_order_cap))
        if reservations[symbol] > 0:
            reason = "WAIT_PENDING_ORDER"
        elif tier == "SCORE_INVALID":
            reason = "SCORE_INVALID"
        elif tier in ("STRONG_WAIT", "WAIT"):
            reason = "WAIT_SCORE_BELOW_THRESHOLD"
        elif cash_v2 and regime.get('mode') == 'RISK_RETENTION' and stage_id is None:
            reason = 'WAIT_RISK_CASH_RETENTION'
        elif total <= 0:
            reason = "WAIT_CASH"
        elif adjusted[symbol] <= 0:
            reason = "WAIT_PENDING_ORDER" if reservations[symbol] else "WAIT_NO_TARGET_GAP"
        elif budget_available <= 0:
            reason = "WAIT_BUDGET"
        elif spendable <= 0:
            reason = "WAIT_CASH"
        elif daily_available <= 0 or round_cap <= 0:
            reason = "WAIT_DAILY_LIMIT"
        elif final <= 0:
            reason = "ZERO_EXECUTABLE_AMOUNT"
        elif final < BROKER_DOCUMENTED_MINIMUM:
            reason = "WAIT_BROKER_MINIMUM"
        else:
            reason = "BUY"  # Final brokerage acceptance still needs preview.
        etfs[symbol] = {
            "final_score": str(scores.get(symbol)) if scores.get(symbol) is not None else None,
            "score_tier": tier, "multiplier": str(multiplier),
            "market_value": str(down(values[symbol])),
            "current_weight": str(values[symbol] / total) if total else None,
            "target_weight": str(target), "target_dollar_gap": str(down(gaps[symbol])),
            "reserved_unfilled_buy": str(down(reservations[symbol])),
            "adjusted_gap": str(down(adjusted[symbol])),
            "base_suggested_amount": str(down(base)),
            "score_adjusted_amount": str(down(adjusted_amount)),
            "final_permitted_amount": str(final if reason == "BUY" else Decimal("0.00")),
            "actual_submitted_amount": "0.00", "actual_filled_amount": "0.00",
            "decision": "BUY" if reason == "BUY" else "WAIT", "reason": reason,
            "execution_state": "PLANNED" if reason == "BUY" else "WAIT",
        }
        if cash_v2:
            etfs[symbol].update(regime_mode=regime['mode'], stage_id=stage_id,
                               regime_context={k:regime.get(k) for k in ('mode','episode_id','observation_date')})
            etfs[symbol]['regime_context']['stage_id'] = stage_id
    return {
        "policy_version": 'cash_regime_v2' if cash_v2 else POLICY_VERSION, "snapshot_id": snapshot_id, "run_id": run_id,
        "snapshot_time": snapshot_time, "overall": "BUY" if any(x["decision"] == "BUY" for x in etfs.values()) else "WAIT",
        "market_values": {k: str(down(v)) for k, v in values.items()},
        "cash_for_weight": str(down(cash)), "cash_basis": cash_basis,
        "total_assets": str(down(total)), "cash_weight": str(cash / total) if total else None,
        "spendable_cash": str(down(spendable)),
        "monthly_new_allowance": str(down(new_allowance)) if new_allowance is not None else None,
        "carryover": str(down(max(Decimal("0"), monthly_limit - new_allowance))) if new_allowance is not None else None,
        "executed_this_month": str(down(monthly_filled)),
        "reserved": str(down(occupied)), "available_budget": str(down(budget_available)),
        "daily_remaining_limit": str(down(daily_available)), "shared_round_budget": str(down(shared)),
        "etfs": etfs,
        "execution_selection": "one_highest_score_buy_per_cycle",
        "funding_mode": 'ACCOUNT_CASH' if cash_v2 else 'MONTHLY_STRATEGY_BUDGET',
        "regime": regime if cash_v2 else None,
    }


def plan(snapshot, scores, **kwargs):
    """Apply the approved QQQ curve and independently tested SMH rule."""
    import copy
    from .smh_long_term import POLICY_VERSION as SMH_POLICY, normal_signal, risk_score, qqq_normal_tier
    result = _base_plan(snapshot, scores, **kwargs)
    regime = snapshot.get('cash_regime_v2') or {}
    if result.get('reason') or regime.get('smh_policy_version') != SMH_POLICY:
        return result
    if regime['mode'] == 'EARLY_INVEST':
        item = result['etfs']['QQQ']
        if item['reason'] in ('BUY', 'WAIT_BROKER_MINIMUM', 'ZERO_EXECUTABLE_AMOUNT'):
            tier, factor = qqq_normal_tier(scores.get('QQQ'))
            adjusted = Decimal(item['base_suggested_amount']) * factor
            amount = down(min(adjusted, Decimal(str(kwargs['per_order_limit']))))
            reason = 'BUY' if amount >= BROKER_DOCUMENTED_MINIMUM else 'WAIT_BROKER_MINIMUM'
            item.update(score_tier=tier, multiplier=str(factor), score_adjusted_amount=str(down(adjusted)),
                        final_permitted_amount=str(amount if reason == 'BUY' else Decimal('0.00')),
                        decision='BUY' if reason == 'BUY' else 'WAIT', reason=reason,
                        execution_state='PLANNED' if reason == 'BUY' else 'WAIT')
    try:
        feature = regime['smh_feature']
        if not isinstance(feature, dict) or feature.get('date') != regime.get('observation_date'):
            raise ValueError('smh_feature_date_unverified')
        new_score, factor, reason = normal_signal(feature)
        if type(regime.get('smh_local_clear')) is not bool:
            raise ValueError('smh_recovery_unverified')
        normal = regime['mode'] == 'EARLY_INVEST' or regime['smh_local_clear']
        modified = copy.deepcopy(regime)
        if normal:
            modified['mode'] = 'EARLY_INVEST'
        else:
            new_score = risk_score(feature)
            for stage in modified['stages']['SMH']:
                stage['eligible'] = feature['drawdown60'] >= float(stage['drawdown'])
                if stage['id'] == 'D20':stage['authorized_cents'] //= 2
        candidate = _base_plan(dict(snapshot, cash_regime_v2=modified), dict(scores, SMH=new_score), **kwargs)
        if candidate.get('reason'):
            return candidate
        item = candidate['etfs']['SMH']
        if normal and item['reason'] in ('BUY', 'WAIT_SCORE_BELOW_THRESHOLD', 'WAIT_BROKER_MINIMUM', 'ZERO_EXECUTABLE_AMOUNT'):
            adjusted = Decimal(item['base_suggested_amount']) * factor
            amount = down(min(adjusted, Decimal(str(kwargs['per_order_limit']))))
            reason = reason if factor == 0 else 'BUY' if amount >= BROKER_DOCUMENTED_MINIMUM else 'WAIT_BROKER_MINIMUM'
            item.update(score_tier=SMH_POLICY, multiplier=str(factor), score_adjusted_amount=str(down(adjusted)),
                        final_permitted_amount=str(amount if reason == 'BUY' else Decimal('0.00')),
                        decision='BUY' if reason == 'BUY' else 'WAIT', reason=reason,
                        execution_state='PLANNED' if reason == 'BUY' else 'WAIT')
        item['smh_research_policy'] = SMH_POLICY
        result['etfs']['SMH'] = item
    except (KeyError, ValueError, TypeError):
        result['etfs']['SMH'].update(decision='WAIT', reason='WAIT_SMH_SIGNAL_UNVERIFIED', final_permitted_amount='0.00', execution_state='WAIT')
    result['overall'] = 'BUY' if any(x['decision'] == 'BUY' for x in result['etfs'].values()) else 'WAIT'
    return result


def select_one_for_existing_executor(result, eligible_symbols=None):
    """Choose the highest-scoring candidate that passes its own symbol gate."""
    eligible = set(TARGETS) if eligible_symbols is None else set(eligible_symbols)
    candidates = [(decimal(item["final_score"], "score"),
                   decimal(item["adjusted_gap"], "adjusted_gap"), symbol, item)
                  for symbol in TARGETS
                  if symbol in eligible
                  for item in (result.get("etfs", {}).get(symbol, {}),) if item.get("decision") == "BUY"]
    if not candidates:
        return None
    _, _, symbol, item = max(candidates)
    return {"symbol": symbol, "amount": item["final_permitted_amount"], "policy_version": result.get('policy_version', POLICY_VERSION)}


def revalidate_before_submission(original_result, fresh_result, symbol):
    """Only same-score plans in the authorized dollar band can be refreshed."""
    old = original_result.get("etfs", {}).get(symbol, {})
    new = fresh_result.get("etfs", {}).get(symbol, {})
    if (old.get("decision") != "BUY" or new.get("decision") != "BUY"
            or original_result.get("policy_version") not in (POLICY_VERSION, 'cash_regime_v2')
            or fresh_result.get("policy_version") != original_result.get('policy_version')
            or old.get('stage_id') != new.get('stage_id')
            or old.get('regime_context') != new.get('regime_context')):
        return False, "PLAN_INVALIDATED_BY_FRESH_SNAPSHOT"
    try:
        for ticker in TARGETS:
            old_score = decimal(original_result['etfs'][ticker].get('final_score'), 'score')
            new_score = decimal(fresh_result['etfs'][ticker].get('final_score'), 'score')
            if old_score != new_score or old_score > 100:
                return False, 'PLAN_INVALIDATED_BY_FRESH_SNAPSHOT'
        previous = decimal(old.get('final_permitted_amount'), 'amount')
        latest = decimal(new.get('final_permitted_amount'), 'amount')
        selected = select_one_for_existing_executor(fresh_result)
        if (abs(latest - previous) > FRESH_PLAN_AMOUNT_TOLERANCE
                or latest < BROKER_DOCUMENTED_MINIMUM or not selected or selected['symbol'] != symbol):
            return False, 'PLAN_INVALIDATED_BY_FRESH_SNAPSHOT'
    except (PlanningUnavailable, KeyError):
        return False, 'PLAN_INVALIDATED_BY_FRESH_SNAPSHOT'
    return True, "OK"


def format_simulated_daily_report(result, existing_daily_summary=None):
    """A Telegram-shaped offline artifact; never sends a message."""
    lines = ["AVERRA | SCORE ALLOCATION V1 | SIMULATION", "DRY RUN — NO ORDER PLACED",
             "Snapshot Time: " + str(result.get("snapshot_time") or "Unavailable"),
             "Snapshot ID: " + str(result.get("snapshot_id") or "Unavailable"),
             "Run ID: " + str(result.get("run_id") or "Offline Simulation"),
             "Data Basis: PRE-TRADE ACCOUNT SNAPSHOT", "Policy: " + POLICY_VERSION]
    if existing_daily_summary:
        lines = [existing_daily_summary, ""] + lines
    if "total_assets" not in result:
        return "\n".join(lines + ["Decision: WAIT", "Reason: " + str(result.get("reason"))])
    lines += [f"SMH Market Value: ${result['market_values']['SMH']}",
              f"QQQ Market Value: ${result['market_values']['QQQ']}",
              f"Cash: ${result['cash_for_weight']}", f"Total Assets: ${result['total_assets']}",
              "Current Allocation: " + " | ".join(
                  f"{s} {Decimal(result['etfs'][s]['current_weight']):.2%}" for s in TARGETS)
              + f" | Cash {Decimal(result['cash_weight']):.2%}",
              "Target Allocation: SMH 80% | QQQ 20%",
              "Cash may remain under score, budget, or risk constraints; targets do not require immediate full investment.",
              f"Monthly New Allowance: {result['monthly_new_allowance']} | Carryover: {result['carryover']}",
              f"Executed This Month: ${result['executed_this_month']} | Reserved: ${result['reserved']}",
              f"Available Budget: ${result['available_budget']} | Spendable Cash: ${result['spendable_cash']}",
              f"Daily Remaining Limit: ${result['daily_remaining_limit']}"]
    selected = select_one_for_existing_executor(result)
    for symbol, item in result["etfs"].items():
        lines += [f"{symbol}: Final Score {item['final_score']} | Tier {item['score_tier']} | Multiplier {item['multiplier']}",
                  f"Weight {Decimal(item['current_weight']):.2%} | Target {Decimal(item['target_weight']):.2%}",
                  f"Target Gap ${item['target_dollar_gap']} | Base ${item['base_suggested_amount']} | Score-Adjusted ${item['score_adjusted_amount']}",
                  f"Final Permitted ${item['final_permitted_amount']} | Submitted ${item['actual_submitted_amount']} | Filled ${item['actual_filled_amount']}",
                  f"Selected For Execution: {'YES' if selected and selected['symbol'] == symbol else 'NO'}",
                  f"Decision {item['decision']} | Execution {item['execution_state']} | Reason {item['reason']}"]
    return "\n".join(lines)
