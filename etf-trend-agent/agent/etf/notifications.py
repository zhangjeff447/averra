"""Durable notifications; every format is also available without sending."""
from datetime import datetime,timezone
from pathlib import Path
from .calendar import ET
from .telegram_reports import decision as report
ROOT=Path(__file__).resolve().parents[1]
def enqueue(result,journal):
 from agent_ops import Operations
 now=datetime.now(timezone.utc).astimezone(ET)
 import hashlib,json
 event=dict(day=now.date().isoformat(),version=result.get('version'),phase=result.get('phase'),selected=result.get('selected'),reason=result.get('reason'),executions=result.get('executions',[]))
 key=hashlib.sha256(json.dumps(event,sort_keys=True).encode()).hexdigest()
 from .price_levels import append_to
 text=append_to(report(result),ROOT.parent/'data',now)
 with __import__('contextlib').closing(Operations(ROOT.parent/'state/operations.sqlite')) as store:
  return store.enqueue('rf-decision:'+key,'DECISION',text)


def configure_sender(broker):
 from tools.ops_utils import load_env_file
 secrets=load_env_file(Path(broker.adapter.credential_config).parent.parent/'.env.secrets')
 token=secrets.get('TELEGRAM_BOT_TOKEN');chat=secrets.get('TELEGRAM_CHAT_ID')
 if not token or not chat:raise ValueError('existing_etf_sender_missing')
 broker.adapter.telegram_bot_token=token;broker.adapter.telegram_chat_id=chat
