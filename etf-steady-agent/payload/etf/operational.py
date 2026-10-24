"""Observations and version history; never a source of trading instructions."""
import json
from pathlib import Path
from agent_ops import Operations,safe_event,category
ROOT=Path(__file__).resolve().parents[1]
DB=ROOT.parent/'state/operations.sqlite'


def settings():
    try:
        store=Operations(DB)
        try:store.version('ETF_PUBLIC_SGOV',dict(policy=json.loads((ROOT/'etf/policy.json').read_text()),scope='网站下载版，只读默认',permissions=['QQQ BUY','SMH BUY','SGOV BUY','SGOV SELL'],timezone='America/New_York'))
        finally:store.close()
    except Exception:pass


def record(run,stage,payload):safe_event(DB,run,stage,payload)


def failure(run,reason):
    try:
        store=Operations(DB)
        try:store.fault(run,category(reason));store.event(run,'FAILED',dict(category=category(reason)))
        finally:store.close()
    except Exception:pass
