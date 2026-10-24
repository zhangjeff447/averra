"""Bridge the actual integrated pure planner to all five original replay scenarios."""
import importlib.util,sys,types
from pathlib import Path
from etf import reference_planner

def test_unchanged_allocation_with_frozen_supplied_signal():
 folder=Path(__file__).resolve().parents[2]/'audit'
 # Frozen replay supplies a field named sma200. Map its numeric value to
 # the new signal key to verify allocation/execution parity, not 100-day returns.
 def preview(snapshot,signal,**kwargs):
  signal=dict(signal);signal['sma100']=signal.pop('sma200')
  return reference_planner.preview(snapshot,signal,**kwargs)
 bridge=types.SimpleNamespace(**{k:getattr(reference_planner,k) for k in dir(reference_planner) if not k.startswith('__')})
 bridge.preview=preview
 old_planner=sys.modules.get('planner');sys.modules['planner']=bridge;sys.path.insert(0,str(folder))
 try:
  spec=importlib.util.spec_from_file_location('reference_audit',folder/'test_preview.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  rows=module.replay_parity();assert len(rows)==5;assert sum(r['days'] for r in rows)==6467
 finally:
  sys.path.remove(str(folder))
  if old_planner is None:sys.modules.pop('planner',None)
  else:sys.modules['planner']=old_planner
