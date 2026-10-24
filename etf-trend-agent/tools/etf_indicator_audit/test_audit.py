import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from copy import deepcopy
from audit import indicators, legacy_rsi, audit, read_history, compare, PROFILE

class Tests(unittest.TestCase):
    def test_wilder_known_seed(self):
        prices = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
        self.assertAlmostEqual(indicators(prices)['RSI14_WILDER'], 70.46413502109705)
        prices += [46, 45, 48]
        self.assertNotAlmostEqual(indicators(prices)['RSI14_WILDER'], legacy_rsi(prices))
    def test_trend_and_flat_boundaries(self):
        self.assertEqual(indicators(list(range(1,50)))['RSI14_WILDER'], 100)
        self.assertEqual(indicators(list(range(50,1,-1)))['RSI14_WILDER'], 0)
        self.assertIsNone(indicators([20]*50)['RSI14_WILDER'])
    def test_warmup_is_missing_not_zero(self):
        v = indicators([1,2,3])
        self.assertTrue(all(x is None for x in v.values()))
    def test_linear_ema_and_macd(self):
        v = indicators(list(range(1,251)))
        self.assertAlmostEqual(v['EMA20'], 240.5)
        self.assertAlmostEqual(v['MACD'], 7)
        self.assertAlmostEqual(v['MACD_SIGNAL'], 7)
        self.assertAlmostEqual(v['MACD_HIST'], 0)
        self.assertAlmostEqual(v['SMA50'],225.5)
    def fixture(self, root):
        rows = [dict(Date=f'2026-01-{d:02d}',Open=10,High=12,Low=9,Close=11,**{'Adj Close':11}) for d in [5,6,7]]
        p = root/'QQQ.csv'
        with p.open('w') as f:
            writer=csv.DictWriter(f,rows[0].keys());writer.writeheader();writer.writerows(rows)
        m=dict(first='2026-01-05',last='2026-01-07',rows=3,source='unit fixture',sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        (root/'manifest.json').write_text(json.dumps({'QQQ':m}))
        sessions=lambda a,b:[r['Date'] for r in rows if a<=r['Date']<=b]
        return p,m,sessions
    def test_stale_and_symbol_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,m,ss=self.fixture(Path(tmp))
            sessions=lambda a,b:ss(a,b)+(['2026-01-08'] if a<= '2026-01-08'<=b else [])
            result=audit(p.parent,'2026-01-08',sessions)
            self.assertEqual(result['symbols']['QQQ']['status'],'STALE')
            self.assertEqual(result['symbols']['SMH']['status'],'UNAVAILABLE')
            self.assertFalse(result['reference_verified'])
    def test_forming_future_rows_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,m,ss=self.fixture(Path(tmp));rows,_=read_history(p,m,'2026-01-06',ss)
            self.assertEqual([r['Date'] for r in rows],['2026-01-05','2026-01-06'])
    def test_hash_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,m,ss=self.fixture(Path(tmp));p.write_text(p.read_text()+'\n')
            with self.assertRaisesRegex(ValueError,'HASH'):read_history(p,m,'2026-01-07',ss)
    def test_session_gap_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p,m,ss=self.fixture(Path(tmp));sessions=lambda a,b:['2026-01-05','2026-01-06','2026-01-06a','2026-01-07']
            with self.assertRaisesRegex(ValueError,'SESSION'):read_history(p,m,'2026-01-07',sessions)
    def test_incompatible_reference_cannot_pass(self):
        local=dict(symbol='QQQ',through='2026-01-07',timeframe='1d',price_basis='Adj Close',formula_profile=PROFILE,history_sha256='abc',values={'RSI14_WILDER':50})
        ref=dict(local,source='authorized fixture');self.assertEqual(compare(local,ref)['status'],'MATCH')
        for field in ['through','timeframe','price_basis','formula_profile','history_sha256','symbol']:
            bad=deepcopy(ref);bad[field]='other';self.assertEqual(compare(local,bad)['status'],'NOT_COMPARABLE')
        self.assertEqual(compare(local,None)['status'],'NOT_VERIFIED')
    def test_missing_and_nonfinite_not_zero(self):
        a=dict(symbol='QQQ',through='2026-01-07',timeframe='1d',price_basis='Adj Close',formula_profile=PROFILE,history_sha256='abc',values={'RSI14_WILDER':50})
        for value in [None,float('nan'),True]:
            ref=dict(a,source='fixture',values={'RSI14_WILDER':value});self.assertEqual(compare(a,ref)['status'],'INSUFFICIENT')
        ref=dict(a,source='fixture',values={'RSI14_WILDER':0});self.assertEqual(compare(a,ref)['status'],'DIFFERENT')

if __name__=='__main__':unittest.main()
