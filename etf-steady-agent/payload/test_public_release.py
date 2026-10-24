import unittest,subprocess,sys,json,os,hashlib,importlib.util,tempfile,types,io
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parent
class PublicTests(unittest.TestCase):
 def cli(self,*args,env=None):return subprocess.run([sys.executable,str(ROOT/'run_agent.py'),*args],capture_output=True,text=True,cwd=ROOT,env=env)
 def test_three_etf_frozen_defaults(self):
  r=self.cli('--check-config');self.assertEqual(r.returncode,0,r.stderr);p=json.loads(r.stdout)
  self.assertEqual(p['targets'],{'SMH':'0.70','QQQ':'0.20','SGOV':'0.10'});self.assertFalse(p['live_execution_available']);self.assertFalse(p['website_preferences_applied'])
 def test_live_denied_even_with_environment_authorization(self):
  env=dict(os.environ,ALLOW_LIVE_ORDERS='true',FINAL_DEPLOYMENT_AUTHORIZED='true',AGENT_MODE='LIVE_TRADING')
  r=self.cli('--live',env=env);self.assertEqual(r.returncode,2);self.assertEqual(json.loads(r.stdout)['reason'],'public_download_read_only')
 def test_no_automatic_default_operation(self):self.assertEqual(self.cli().returncode,2)
 def test_read_only_transport_rejects_order_before_underlying_transport(self):
  from etf.broker import GuardedTransport
  class Transport:
   def call_tool(self,*args):raise AssertionError('order reached transport')
  g=GuardedTransport(Transport())
  for name in ['review_equity_order','place_equity_order']:
   with self.assertRaises(PermissionError):g.call_tool(name,{'symbol':'SGOV','side':'buy'})
 def test_account_validation_stays_read_only_and_temporary(self):
  spec=importlib.util.spec_from_file_location('public_entry',ROOT/'run_agent.py');entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
  from tests.test_etf_cycle_v3 import limited_raw
  snapshot=limited_raw();snapshot['pending_approvals']=[];snapshot['account_hash']=hashlib.sha256(b'demo-account-test-only').hexdigest()
  calls=[]
  class Adapter:
   def __init__(self,workspace,config,live=False):self.assert_read_only=not live
   def _call_tool(self,name,*args,**kwargs):
    calls.append(name)
    return {'data':{'accounts':[{'account_number':'demo-account-test-only','agentic_allowed':True,'state':'active'}]}}
   def _unwrap_mcp_result(self,data):return data
  class Broker:
   def __init__(self,adapter,expected_account_hash):assert adapter.assert_read_only;assert expected_account_hash==snapshot['account_hash']
   def read(self):calls.append('read_only_account');return snapshot
  old=Path.cwd()
  with tempfile.TemporaryDirectory() as temp,patch('etf.broker.NativeAdapter',Adapter),patch('etf.broker.Broker',Broker),patch.object(sys,'argv',['run_agent.py','--validate-read-only']),patch('sys.stdout',new_callable=io.StringIO) as output:
   try:
    os.chdir(temp);self.assertEqual(entry.main(),0);result=json.loads(output.getvalue());self.assertEqual(result['broker_place_calls'],0);self.assertEqual(result['formal_ledger_writes'],0);self.assertFalse((Path(temp)/'state').exists())
   finally:os.chdir(old)
  self.assertEqual(calls,['get_accounts','read_only_account'])
if __name__=='__main__':unittest.main()
