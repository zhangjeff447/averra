"""Research replay: completed closes -> following session open, equal cash flows.

This is a price-only comparison. Provider dividend-adjusted prices imply same-
ETF reinvestment; it DOES NOT validate the user's cash-dividend routing. Broker
approvals, tax, actual intraday fills and payable-date settlement are not modeled.
"""
import copy
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from .cycle import plan,phase_observation,advance,money
from .history import load
from strategies.qqq_smh_agent.cash_regime_v2 import observations,advance as old_advance,add_cash_capacity,release_context
from strategies.qqq_smh_agent.smh_long_term import features
from strategies.qqq_smh_agent.indicators import rsi_from_closes
from strategies.qqq_smh_agent.strategy_agent import StrategyAgent
from tools.volatility_context import unavailable_context

ROOT=Path(__file__).resolve().parents[1]
OLD_POLICY=json.loads((ROOT/'config/cash_regime_policy.json').read_text())


def run(destination,output):
    Path(output).mkdir(parents=True,exist_ok=True)
    dates,frames=load(destination)
    closes={s:[float(x) for x in frames[s]['Adj Close']] for s in ('SMH','QQQ')}
    volatility={s:[float(x) for x in frames['^'+s]['Close']] for s in ('VIX','VXN')}
    obs=[o for o in observations(dates,closes,volatility,OLD_POLICY) if dates.index(o["date"])>=271]
    signal_inputs={};strategy=StrategyAgent()
    for o in obs:
        i=dates.index(o['date']);recent={s:closes[s][max(0,i-349):i+1] for s in ('SMH','QQQ')}
        signal=strategy.evaluate_signals({},0,recent,volatility_context=unavailable_context('historical_intraday_context_not_modeled'))
        scores={s:d['metadata']['signal_score'] for s,d in signal['decisions'].items()}
        feature=features(dates[max(0,i-349):i+1],recent['SMH'],rsi_from_closes(recent['SMH']))
        phase_obs=phase_observation(o,dates[:i+1],{s:closes[s][:i+1] for s in closes})
        signal_inputs[o['date']]=(scores,feature,phase_obs)
    reports={}
    for kind in ('dynamic_cycle','fixed_70_20_10_buy_only','legacy_80_20'):
        qty={s:Decimal(0) for s in ('SMH','QQQ','SCHD')};cash=Decimal(0);external=Decimal(0);buys=Decimal(0);sales=Decimal(0)
        phase=None;legacy=None;usage={};counts=Counter();curve=[];peak=Decimal(0);worst=0.;last_month=None;last_schd_side=None;last_schd_i=-100
        for o in obs[:-1]:
            i=dates.index(o['date']);day=dates[i+1]
            if day[:7]!=last_month:cash+=300;external+=300;last_month=day[:7]
            close_prices={s:Decimal(str(frames[s].iloc[i]['Adj Close'])) for s in qty}
            opens={s:Decimal(str(frames[s].iloc[i+1]['Open']*frames[s].iloc[i+1]['Adj Close']/frames[s].iloc[i+1]['Close'])) for s in qty}
            values={s:qty[s]*close_prices[s] for s in qty};nav=sum(values.values())+cash
            scores,feature,phase_obs=signal_inputs[o['date']]
            phase=advance(phase,phase_obs,release_complete=values['SCHD']<=nav*Decimal('.05'))
            legacy=old_advance(legacy,o,OLD_POLICY);legacy['smh_feature']=feature
            legacy['smh_clear_count']=legacy.get('smh_clear_count',0)+1 if not feature['weak'] else 0
            gaps={s:max(Decimal(0),nav*w-values[s]) for s,w in [('SMH',Decimal('.70')),('QQQ',Decimal('.20'))]}
            legacy=add_cash_capacity(legacy,int(cash*100),int((buys-sales)*100),gaps,OLD_POLICY)
            context=release_context(legacy,usage.get(legacy.get('episode_id'),{}),OLD_POLICY)
            context.update(smh_policy_version='SMH_EMA20_PULLBACK',smh_feature=feature,
                           smh_local_clear=legacy['smh_clear_count']>=2,depth={s:max(0,1-closes[s][i]/max(closes[s][max(0,i-59):i+1])) for s in closes})
            daily_buy=Decimal(0);daily_sell=Decimal(0);bought=Counter();first_qqq=None
            for slot in range(3):
                values={s:qty[s]*opens[s] for s in qty};nav=sum(values.values())+cash
                snapshot=dict(values={s:str(v) for s,v in values.items()},net_assets=str(nav),safe_cash=str(max(Decimal(0),cash-Decimal('.25'))),cash_for_weight=str(cash))
                gates={}
                if bought['SMH']:gates['SMH']='daily'
                if bought['QQQ']>=2:gates['QQQ']='daily'
                elif first_qqq is not None and not ((first_qqq>=50 and money(scores['QQQ'])>=50) or money(scores['QQQ'])>=first_qqq+10):gates['QQQ']='score'
                if kind=='dynamic_cycle':
                    result=plan(snapshot,phase,scores,context,buy_remaining=max(0,100-daily_buy),sell_remaining=max(0,100-daily_sell),gates=gates,
                                schd_buy_cooled=last_schd_side!='SELL' or i-last_schd_i>=5,
                                schd_sell_cooled=last_schd_side!='BUY' or i-last_schd_i>=5)
                    order=result['selected'];counts['phase:'+phase['phase']]+=1
                elif kind=='fixed_70_20_10_buy_only':
                    target={'SMH':Decimal('.70'),'QQQ':Decimal('.20'),'SCHD':Decimal('.10')}
                    gaps={s:max(Decimal(0),nav*w-values[s]) for s,w in target.items()};eligible=[s for s in qty if s not in gates and gaps[s]>nav*Decimal('.005')]
                    amount=min(cash-Decimal('.25'),100-daily_buy,max(gaps.values())) if eligible else Decimal(0)
                    s=max(eligible,key=lambda s:gaps[s]) if eligible else None
                    order=dict(symbol=s,side='BUY',amount=str(amount)) if amount>=1 else None
                else:
                    from .cycle import _core_plan
                    # A research comparison uses the same completed inputs and
                    # original 80/20 rules, without SCHD or changed scoring.
                    _,candidates=_core_plan(snapshot,scores,context,{'SMH':Decimal('.8'),'QQQ':Decimal('.2')},max(Decimal(0),min(cash-Decimal('.25'),100-daily_buy)),daily_remaining=max(0,100-daily_buy),gates=gates)
                    order=candidates[0] if candidates else None
                if not order:counts['WAIT']+=1;continue
                s=order['symbol'];amount=money(order['amount']);side=order['side'];price=opens[s]*(Decimal('1.0005') if side=='BUY' else Decimal('.9995'))
                if side=='BUY':
                    amount=min(amount,cash);qty[s]+=amount/price;cash-=amount;daily_buy+=amount;buys+=amount;bought[s]+=1
                    if s=='QQQ' and first_qqq is None:first_qqq=money(order.get('score',scores['QQQ']))
                    ctx=order.get('legacy_context') or {}
                    if ctx.get('stage_id'):
                        u=usage.setdefault(ctx['episode_id'],{}).setdefault(s,{}).setdefault(ctx['stage_id'],dict(filled_cents=0,reserved_cents=0));u['filled_cents']+=int(amount*100)
                else:
                    shares=min(qty[s],amount/price);amount=shares*price;qty[s]-=shares;cash+=amount;daily_sell+=amount;sales+=amount
                if s=='SCHD':last_schd_side=side;last_schd_i=i
                counts[side+':'+s]+=1
                assert cash>=0 and all(q>=0 for q in qty.values()) and daily_buy<=Decimal('100.01') and daily_sell<=Decimal('100.01')
                assert side!='SELL' or s=='SCHD'
            # Contributions are neutralized using a time-weighted wealth index.
            end_nav=sum(qty[s]*Decimal(str(frames[s].iloc[i+1]['Adj Close'])) for s in qty)+cash
            curve.append(dict(date=day,nav=float(end_nav),contributions=float(external),cash=float(cash)))
        wealth=1.;peak=1.;worst=0.
        for n,row in enumerate(curve):
            prior=curve[n-1]['nav'] if n else 0
            contribution=row['contributions']-(curve[n-1]['contributions'] if n else 0)
            if prior+contribution>0:wealth*=row['nav']/(prior+contribution)
            peak=max(peak,wealth);worst=min(worst,wealth/peak-1)
        reports[kind]=dict(start=curve[0]['date'],end=curve[-1]['date'],contributed=float(external),end_nav=curve[-1]['nav'],
                           time_weighted_return=wealth-1,max_drawdown=worst,counts=dict(counts),
                           final_cash=curve[-1]['cash'],buy_turnover=float(buys),sell_turnover=float(sales),
                           assumptions=['$300 monthly completed contributions','three execution opportunities per day at next-session open',
                           '5 bp one-way slippage','price-only adjusted-price replay; same-ETF dividend reinvestment implied',
                           'intraday volatility adjustment unavailable, neutralized equally','no actual tax, fees, payable dates or broker approvals'])
        (Path(output)/(kind+'-curve.json')).write_text(json.dumps(curve))
    Path(output).mkdir(parents=True,exist_ok=True)
    (Path(output)/'research-comparison.json').write_text(json.dumps(reports,indent=2));return reports
