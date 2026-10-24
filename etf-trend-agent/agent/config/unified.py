"""The single policy loader for the unified entry and runtime."""
import hashlib
import json
import os
from pathlib import Path

from config.production import ProductionConfigError, _validate_values

ROOT = Path(__file__).resolve().parents[1]
_loaded = None
READ_ONLY_VALIDATION = 'READ_ONLY_VALIDATION'
LIVE_TRADING = 'LIVE_TRADING'
APPROVAL_MODES = {'APPROVAL_SAFE_COMPATIBILITY_MODE', 'NATIVE_APPROVAL_VISIBILITY_MODE'}


def load_config():
    global _loaded
    if _loaded is not None:
        return _loaded
    from strategies.qqq_smh_agent.score_dynamic_v1 import SCORE_TIERS, TARGETS
    from tools.ops_utils import load_env_file
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProductionConfigError('duplicate_policy_key:' + key)
            result[key] = value
        return result
    policy = json.loads((ROOT / 'config/unified_policy.json').read_text(), object_pairs_hook=unique)
    if any(not isinstance(value, str) for value in policy.values()):
        raise ProductionConfigError('policy_values_must_be_strings')
    if (ROOT / '.env.production').exists():
        raise ProductionConfigError('legacy_config_source_present')
    conflicts = [key for key, value in policy.items() if key in os.environ and os.environ[key] != value]
    if conflicts:
        raise ProductionConfigError('loaded_policy_conflict:' + ','.join(sorted(conflicts)))
    cash_v2 = policy.get('CASH_REGIME_V2_ENABLED') == 'true'
    fixed = {'SCORE_DYNAMIC_ALLOCATION_V1_ENABLED': 'true', 'RESTRICTED_NO_OPEN_ORDER_MODE': 'true',
             'MONTHLY_BUY_LIMIT': '0' if cash_v2 else '300', 'TIMEZONE': 'America/New_York', 'MIN_ORDER_AMOUNT': '0',
             'REPORT_ONLY_ENABLED': 'true', 'FRESH_PLAN_AMOUNT_TOLERANCE': '5.00',
             'QQQ_MAX_BUYS_PER_ET_DAY': '2', 'QQQ_SECOND_BUY_MIN_SCORE_INCREASE': '10.00',
             'QQQ_SECOND_BUY_BOTH_SCORES_AT_LEAST': '50.00'}
    if any(policy.get(key) != value for key, value in fixed.items()):
        raise ProductionConfigError('unified_policy_invariant')
    if policy.get('APPROVAL_MODE') not in APPROVAL_MODES:
        raise ProductionConfigError('approval_mode_unverified')
    operation = policy.get('AGENT_MODE')
    if operation == READ_ONLY_VALIDATION:
        gates = {'TRADING_MODE': 'read_only', 'ALLOW_LIVE_ORDERS': 'false',
                 'V1_SUBMISSION_APPROVED': 'false', 'FINAL_DEPLOYMENT_AUTHORIZED': 'false'}
    elif operation == LIVE_TRADING:
        gates = {'TRADING_MODE': 'live', 'ALLOW_LIVE_ORDERS': 'true',
                 'V1_SUBMISSION_APPROVED': 'true', 'FINAL_DEPLOYMENT_AUTHORIZED': 'true',
                 'TELEGRAM_ENABLED': 'true'}
    else:
        raise ProductionConfigError('explicit_agent_mode_required')
    if any(policy.get(key) != value for key, value in gates.items()):
        raise ProductionConfigError('ambiguous_mode_or_deployment_not_authorized')
    os.environ.update(policy)
    credentials = load_env_file(ROOT / '.env.secrets')
    allowed = {'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'ROBINHOOD_MCP_AUTH_TOKEN', 'ROBINHOOD_MCP_COOKIE'}
    if set(credentials) - allowed:
        raise ProductionConfigError('noncredential_key_in_secrets_file')
    for key, value in credentials.items():
        if key in os.environ and os.environ[key] != value:
            raise ProductionConfigError('credential_source_conflict:' + key)
        os.environ[key] = value
    config = _validate_values(dict(policy))
    if config.daily_buy_limit > 100 or config.max_order_amount > 100:
        raise ProductionConfigError('existing_safety_limit_must_not_increase')
    config.approval_mode = policy['APPROVAL_MODE']
    config.operation_mode = operation
    config.final_deployment_authorized = policy['FINAL_DEPLOYMENT_AUTHORIZED'] == 'true'
    config.cash_regime_v2_enabled = cash_v2
    config.cash_regime_policy = None
    if cash_v2:
        raw = (ROOT / 'config/cash_regime_policy.json').read_bytes()
        if policy.get('FUNDING_MODE') != 'ACCOUNT_CASH' or hashlib.sha256(raw).hexdigest() != policy.get('CASH_REGIME_POLICY_HASH'):
            raise ProductionConfigError('cash_regime_policy_unverified')
        spec = json.loads(raw)
        from decimal import Decimal
        if (spec.get('version') != 'cash_regime_v2' or spec.get('minimum_buy_score') != 25
                or Decimal(spec.get('light_intercept','0')) != Decimal('.25')
                or Decimal(spec.get('light_slope','0')) != Decimal('.75')
                or any(type(spec.get(k)) is not int or spec[k] < 1 for k in (
                    'entry_confirmation_sessions','exit_confirmation_sessions','recovery_confirmation_sessions'))
                or sum(Decimal(x['fraction']) for x in spec.get('stages',[])) != 1):
            raise ProductionConfigError('cash_regime_policy_invalid')
        if (spec.get('smh_policy_version') != 'SMH_EMA20_PULLBACK'
                or spec.get('qqq_normal_curve') != 'QQQ_SCORE25_50_EXP75_FULL'
                or spec.get('smh_d20_release_fraction') != '0.25'):
            raise ProductionConfigError('long_term_policy_unverified')
        config.cash_regime_policy = spec
        config.cash_regime_policy_hash = policy['CASH_REGIME_POLICY_HASH']
    effective = dict(policy, targets={s: str(w) for s, w in TARGETS.items()},
                     score_tiers=[[str(a), b, str(c)] for a, b, c in SCORE_TIERS],
                     slots_et=['10:05', '12:30', '15:30'], orders_per_cycle=1,
                     fixed_cash_reserve='0', max_unresolved_trade_intents=1, broker_minimum='1.00', amount_precision='0.01',
                     symbol_daily_gates_independent=True,
                     execution_selection='highest_score_among_symbol_eligible_candidates',
                     cooldown_hours_by_symbol={'SMH':None, 'QQQ':None},
                     smh_daily_buy_rule={'timezone':'America/New_York','reset_at':'09:30',
                                         'max_submitted_buys':1,'reference':'submitted_orders_and_dated_fills'},
                     qqq_daily_buy_rule={'timezone':'America/New_York','reset_at':'09:30','max_submitted_buys':2,
                                         'second_buy_min_score_increase':'10.00','reference':'first_confirmed_buy_score',
                                         'waive_increase_when_both_scores_at_least':'50.00'})
    if cash_v2:
        effective.update(active_monthly_strategy_cap=None, historical_monthly_ledger='PRESERVED_AUDIT_ONLY',
                         strategy_version='cash_regime_v2', cash_regime_policy=config.cash_regime_policy,
                         qqq_normal_score_rule='WAIT below25;50% at25, exponential to100% at75',
                         smh_score_rule='SMH_EMA20_PULLBACK; normal eligibleScore50to100 factor100%Base; risk finite20/30/25')
    effective['policy_hash'] = hashlib.sha256(json.dumps(effective, sort_keys=True).encode()).hexdigest()
    _loaded = (config, effective)
    return _loaded
