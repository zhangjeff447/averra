from datetime import datetime,timezone
import hashlib
import pandas as pd
from etf import history


def test_signal_uses_last_100_completed_closes(tmp_path,monkeypatch):
    now=datetime(2026,10,22,16,tzinfo=timezone.utc)
    dates=['2026-10-21']*300
    values=list(range(1,301))
    monkeypatch.setattr(history,'load',lambda _: (dates,{'QQQ':pd.DataFrame({'Adj Close':values})}))
    (tmp_path/'QQQ.csv').write_text('test-history')
    result=history.signal(tmp_path,now=now)
    assert float(result['sma50'])==275.5
    assert float(result['sma100'])==250.5
    assert 'sma200' not in result
    assert result['history_sha256']==hashlib.sha256(b'test-history').hexdigest()
