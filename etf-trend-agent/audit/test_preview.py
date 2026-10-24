import copy,json,unittest,types,tempfile,hashlib
from pathlib import Path
from decimal import Decimal as D
import planner
W=Path(__file__).resolve().parent
class PreviewTests(unittest.TestCase):
 def setUp(self):self.x=json.loads((W/'example-hypothetical.json').read_text())
 def p(self):return planner.preview(self.x['snapshot'],self.x['signal'],weekly_funding_session=self.x['weekly_funding_session'])
 def test_original_no_cash_floor(self):
  result=self.p();self.assertEqual(result['safe_cash_after'],'0.00');self.assertEqual(result['orders'][0]['symbol'],'TQQQ');self.assertEqual(result['orders'][0]['amount'],'200.00')
 def test_no_mutation(self):
  before=copy.deepcopy(self.x);self.p();self.assertEqual(before,self.x)
 def test_core_never_sold_to_reach_target(self):self.assertTrue(all(o['side']=='BUY' for o in self.p()['orders']))
 def test_no_fast_exit_on_short_price_decline(self):
  self.x['signal'].update(qqq_below50=True,tqqq_drawdown=.5);self.assertEqual(self.p()['orders'][0]['symbol'],'TQQQ')
 def test_long_trend_off_new_cash_waits(self):
  self.x['signal']['sma50']='99';self.assertEqual(self.p()['orders'],[]);self.assertEqual(self.p()['safe_cash_after'],'200')
 def test_sma_equality_is_off(self):
  self.x['signal']['sma50']='100';self.assertEqual(self.p()['orders'],[])
 def test_own_cash_unverified_blocks(self):
  self.x['snapshot']['own_settled_cash_verified']=False
  with self.assertRaises(ValueError):self.p()
 def test_pending_orders_block(self):
  self.x['snapshot']['unresolved_orders']=True
  with self.assertRaises(ValueError):self.p()
 def test_same_day_signal_block(self):
  self.x['signal']['date']=self.x['snapshot']['date']
  with self.assertRaises(ValueError):self.p()
 def test_sale_not_immediately_reused(self):
  s=self.x['snapshot'];s['quantities']['TQQQ']='2';s['safe_cash']=s['settled_cash']='0';self.x['signal']['sma50']='99'
  r=self.p();self.assertEqual(len(r['orders']),1);self.assertEqual(r['orders'][0]['side'],'SELL');self.assertEqual(D(r['safe_cash_after']),0);self.assertGreater(D(r['new_unsettled_sale_proceeds']),0)
 def test_settled_rotation_reinvests_even_when_off(self):
  self.x['signal']['sma50']='99';self.x['snapshot']['settled_rotation']='200';r=self.p();self.assertEqual([o['symbol'] for o in r['orders']],['SMH','QQQ']);self.assertEqual(sum(D(o['amount']) for o in r['orders']),200)
 def test_rotation_is_not_extra_cash(self):
  self.x['snapshot']['settled_rotation']='201'
  with self.assertRaises(ValueError):self.p()
 def test_promised_deposit_not_used(self):
  self.x['snapshot']['promised_deposit']='100000';self.assertEqual(self.p()['orders'][0]['amount'],'200.00')
 def test_execution_cannot_be_enabled(self):
  self.x['snapshot']['live']=True;self.assertFalse(self.p()['execution_supported']);self.assertEqual(self.p()['mode'],'OFFLINE_PREVIEW_ONLY')
 def test_cash_inconsistency_blocks(self):
  self.x['snapshot']['safe_cash']='201'
  with self.assertRaises(ValueError):self.p()

def replay_parity():
 source=(W/'reference_engine.py').read_text()
 # Instrument the unchanged original model at its decision boundary only.
 before="""   _preview=preview(dict(date=day,quantities={s:str(qty[s]) for s in SYMS},prices={s:str(px[s]) for s in SYMS},net_assets=str(nav),settled_cash=str(cash),safe_cash=str(cash),settled_rotation=str(rotation),unsettled_sales=str(unsettled),dividend_receivable=str(ent),unresolved_orders=False,own_settled_cash_verified=True),dict(date=signal,sma50=f['ma50'],sma200=f['ma200']),weekly_funding_session=bool(incoming),bps=bps)
   _order_start=len(orders)
"""
 after="""   _actual=[{k:o[k] for k in _preview['orders'][j]} for j,o in enumerate(orders[_order_start:])]
   assert _actual==_preview['orders'],(day,_actual,_preview)
   assert D(_preview['safe_cash_after'])==cash,(day,cash,_preview)
   assert D(_preview['settled_rotation_after'])==rotation
   assert all(D(_preview['projected_quantities'][s])==qty[s] for s in SYMS)
   checked.append(day)
"""
 source=source.replace("  if kind=='tactical':\n","  if kind=='tactical':\n"+before,1).replace("  elif kind=='dca' and day in planned:",after+"  elif kind=='dca' and day in planned:",1)
 module=types.ModuleType('instrumented_reference');module.__file__=str(W/'reference_engine.py');module.preview=planner.preview;module.checked=[]
 exec(compile(source,str(W/'reference_engine.py'),'exec'),module.__dict__)
 dates,data=module.prep.load();full=sorted(set.intersection(*(set(v) for v in data.values())))
 ref=json.loads((W/'reference-summary.json').read_text());cfg=ref['config'];results=[]
 for name,ds,kw in [('primary',dates,{}),('latest',full,dict(end='2026-10-14')),('cost50',dates,dict(bps=50)),('delay1',dates,dict(lag=1)),('restart2024',dates,dict(start='2024-01-01'))]:
  start=len(module.checked)
  with tempfile.TemporaryDirectory() as tmp:
   actual=module.replay(ds,data,module.features(ds,data),cfg,save=Path(tmp),**kw)
   if name=='primary':
    assert actual==ref
    assert json.loads((Path(tmp)/'orders.json').read_text())==json.loads((W/'reference-orders.json').read_text())
  results.append(dict(scenario=name,days=len(module.checked)-start,final_nav=actual['final_nav'],status='PASS'))
 return results
if __name__=='__main__':
 result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(PreviewTests));assert result.wasSuccessful()
 parity=replay_parity();evidence=dict(mode='OFFLINE_PREVIEW_ONLY',tests=result.testsRun,parity=parity,broker_calls=0,live_changes=0,primary_reference_sha256=hashlib.sha256((W/'reference_engine.py').read_bytes()).hexdigest())
 (W/'verification.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence,indent=2))
