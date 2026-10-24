"""Regime state in the same transaction boundary as trade reservations."""
import json
from decimal import Decimal, ROUND_DOWN

from strategies.qqq_smh_agent.cash_regime_v2 import POLICY_VERSION, release_context, stage_amount, light_tier


def cash_mode(store):
    row = store.db.execute("SELECT value FROM meta WHERE key='funding_mode'").fetchone()
    return bool(row and row[0] == POLICY_VERSION)


def activate(store, policy_hash):
    if not store.initialized() or store.open_intents() or store.halt_reason():
        raise ValueError('cash_mode_migration_requires_reconciled_ledger')
    with store.transaction() as db:
        if store.open_intents() or store.halt_reason():
            raise ValueError('cash_mode_migration_requires_reconciled_ledger')
        old = db.execute("SELECT value FROM meta WHERE key='cash_policy_hash'").fetchone()
        if old and old[0] != policy_hash:
            raise ValueError('cash_policy_migration_hash_conflict')
        db.execute("CREATE TABLE IF NOT EXISTS regime_intents (intent_id TEXT PRIMARY KEY, episode_id TEXT, stage_id TEXT, symbol TEXT NOT NULL)")
        for key, value in [('funding_mode', POLICY_VERSION), ('cash_policy_hash', policy_hash)]:
            db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)', (key, value))
        if not old:
            store._audit(db, None, 'CASH_MODE_ACTIVATED_HISTORY_PRESERVED', json.dumps(store.summary(db), sort_keys=True))


def load(store):
    row = store.db.execute("SELECT value FROM meta WHERE key='cash_regime_state'").fetchone()
    return json.loads(row[0]) if row else None


def save(store, state):
    if not cash_mode(store):
        raise ValueError('cash_mode_not_migrated')
    raw = json.dumps(state, sort_keys=True)
    with store.transaction() as db:
        old = db.execute("SELECT value FROM meta WHERE key='cash_regime_state'").fetchone()
        if not old or old[0] != raw:
            db.execute("INSERT INTO meta VALUES('cash_regime_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (raw,))
            store._audit(db, None, 'CASH_REGIME_OBSERVATION', json.dumps({k:state.get(k) for k in ('last_date','mode','episode_id','entry_count','exit_count')}, sort_keys=True))


def usage(store, episode_id, exclude_intent_id=None):
    result = {}
    for row in store.db.execute('''SELECT r.symbol,r.stage_id,i.intent_id,i.cumulative_filled_cents,i.reserved_remaining_cents
        FROM regime_intents r JOIN intents i ON i.intent_id=r.intent_id WHERE r.episode_id=?''', (episode_id,)):
        if row[2] == exclude_intent_id:
            continue
        item = result.setdefault(row[0], {}).setdefault(row[1], {'filled_cents':0,'reserved_cents':0})
        item['filled_cents'] += row[3]
        item['reserved_cents'] += row[4]
    return result


def context(store, policy, exclude_intent_id=None):
    state = load(store)
    if not state:
        raise ValueError('regime_observation_missing')
    result = release_context(state, usage(store, state.get('episode_id'), exclude_intent_id), policy)
    if policy.get('smh_policy_version'):
        result.update(smh_policy_version=policy['smh_policy_version'], smh_feature=state.get('smh_feature'),
                      smh_local_clear=state.get('smh_clear_count', 0) >= 2)
    return result


def bind_reservation(store, db, intent_id, symbol, score, multiplier, requested, base_cents, stage_context, policy):
    """Called inside reserve's BEGIN IMMEDIATE, before any broker interaction."""
    ctx = context(store, policy)
    independent = policy.get('smh_policy_version') == 'SMH_EMA20_PULLBACK'
    if independent and symbol == 'SMH':
        from strategies.qqq_smh_agent.smh_long_term import normal_signal, risk_score
        feature = ctx['smh_feature']
        if not feature or feature.get('date') != ctx['observation_date']:
            raise ValueError('smh_feature_date_unverified')
        if ctx['mode'] == 'EARLY_INVEST' or ctx['smh_local_clear']:
            ctx['mode'] = 'EARLY_INVEST'
            expected_score, smh_factor, signal_reason = normal_signal(feature)
            if signal_reason != 'BUY' or Decimal(str(score)) != Decimal(str(expected_score)):
                raise ValueError('smh_signal_changed_before_reservation')
        else:
            if Decimal(str(score)) != Decimal(str(risk_score(feature))):
                raise ValueError('smh_risk_score_changed_before_reservation')
            for stage in ctx['stages']['SMH']:
                stage['eligible'] = feature['drawdown60'] >= float(stage['drawdown'])
                if stage['id'] == 'D20':stage['authorized_cents'] //= 2
    expected = (stage_context or {})
    if (ctx['mode'] != expected.get('mode') or ctx['episode_id'] != expected.get('episode_id')
            or ctx['observation_date'] != expected.get('observation_date')):
        raise ValueError('cash_regime_changed_before_reservation')
    if ctx['mode'] == 'EARLY_INVEST':
        if independent:
            if symbol == 'SMH':factor = smh_factor
            else:
                from strategies.qqq_smh_agent.smh_long_term import qqq_normal_tier
                _, factor = qqq_normal_tier(score)
        else:
            _, factor = light_tier(score)
        stage_id = None
    else:
        _, factor, stage_id, allowed = stage_amount(ctx, symbol, score)
        if stage_id is None or requested > int((allowed * 100).to_integral_value(rounding=ROUND_DOWN)):
            raise ValueError('risk_stage_release_exceeded')
    if factor <= 0 or Decimal(str(multiplier)) != factor or expected.get('stage_id') != stage_id:
        raise ValueError('cash_score_or_stage_invalid')
    if requested > int((Decimal(base_cents) * factor).to_integral_value(rounding=ROUND_DOWN)):
        raise ValueError('cash_score_amount_exceeded')
    db.execute('INSERT INTO regime_intents VALUES(?,?,?,?)',
               (intent_id, ctx['episode_id'] if stage_id else None, stage_id, symbol))


def verified_policy(store, policy_hash):
    row = store.db.execute("SELECT value FROM meta WHERE key='cash_policy_hash'").fetchone()
    return cash_mode(store) and bool(row and row[0] == policy_hash)
