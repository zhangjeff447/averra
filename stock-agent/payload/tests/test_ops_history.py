import json
from agent_ops import Operations
from ops_tools import index_history,query


def test_index_is_incremental_and_history_is_read_only(tmp_path):
 folder=tmp_path/'data/shadow_daily/2026-10-14';folder.mkdir(parents=True)
 path=folder/'research.json';case={'ticker':'AMD','reason':'official earnings changed','cache_status':'VALID_CACHE','evidence_hash':'v1'}
 path.write_text(json.dumps({'cases':[case]}));before=path.read_bytes()
 index_history(tmp_path);index_history(tmp_path)
 s=Operations(tmp_path/'data/operations.sqlite');assert s.status()['research_documents']==1;s.close()
 reply=query('/research_history',['/research_history','AMD','earnings'],tmp_path)
 assert 'official earnings changed' in reply and '不参与当前评分' in reply
 assert path.read_bytes()==before
 path.write_text(json.dumps({'cases':[dict(case,evidence_hash='v2',reason='official earnings revised')]}));index_history(tmp_path)
 s=Operations(tmp_path/'data/operations.sqlite');assert s.status()['research_documents']==2;s.close()
 assert '没有匹配' in query('/research_history',['/research_history','AMD','nonexistent'],tmp_path)


def test_scope_and_help_do_not_allow_broker_or_model_calls(tmp_path):
 (tmp_path/'data').mkdir()
 assert '仅监测' in query('/ops',['/ops'],tmp_path)
 assert '仅本 Agent' in query('/settings_history',['/settings_history'],tmp_path)
 s=Operations(tmp_path/'data/operations.sqlite');v=s.versions()[0]['payload']
 assert v['broker_access'] is False and v['model_history_is_explanatory_only'] is True;s.close()


def test_history_renders_saved_claims_without_raw_json():
 from ops_tools import research_summary
 text=research_summary({'claims':{'KEY_RISK':[{'kind':'KEY_RISK','text':'Official earnings risk','evidence_ids':['e1']}]},'research_score':70})
 assert '主要风险：Official earnings risk' in text and '研究评分：70' in text and '{' not in text
