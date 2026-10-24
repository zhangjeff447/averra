import json
import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import shadow_day1_initial as initial
from day0_1_research import SCHEMA


class InitialTransportSafetyTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.db.executescript(SCHEMA)
        self.plan={'run_id':'TEST','source_research_run':1}
        self.case={'ticker':'ABC','input_hash':initial.digest({}),
                   'estimated_input':1000,'estimated_output':100,'estimated_cost':0.001}
        self.limits={'max_daily_initial_calls':10,'max_daily_initial_cost_usd':0.05}
        self.stack=ExitStack()
        for name, value in [('cached_inputs',{'source_run_id':1,'prompt':{}}),
                            ('request_body',{'model':'gpt-5.6-luna'}),
                            ('token_capacity',True),('reserve','RESERVED')]:
            self.stack.enter_context(patch.object(initial,name,return_value=value))
        self.transition=self.stack.enter_context(patch.object(initial,'budget_transition'))

    def tearDown(self): self.stack.close(); self.db.close()

    def run_case(self):
        return initial.run_one(self.db,self.plan,self.case,{}, {},self.limits,'test-only',['ABC'])

    def test_truncated_json_becomes_unknown_and_never_retries(self):
        with patch.object(initial,'api_research',side_effect=json.JSONDecodeError('truncated','{',1)) as call:
            with self.assertRaisesRegex(RuntimeError,'FAILED_UNKNOWN'): self.run_case()
            self.assertEqual(call.call_count,1)
        row=self.db.execute('SELECT * FROM day01_attempts').fetchone()
        self.assertEqual(row['state'],'FAILED_UNKNOWN'); self.assertTrue(row['finished_at'])
        self.assertEqual(self.transition.call_args.args[4],'FAILED_UNKNOWN')

    def test_usage_missing_keeps_raw_and_marks_attempt_unknown(self):
        raw={'model':'gpt-5.6-luna','output':[]}
        with patch.object(initial,'api_research',return_value=(raw,{'request_id':'test-id'})):
            with self.assertRaisesRegex(RuntimeError,'USAGE_UNKNOWN'): self.run_case()
        row=self.db.execute('SELECT * FROM day01_attempts').fetchone()
        self.assertEqual(row['state'],'FAILED_UNKNOWN')
        self.assertEqual(json.loads(row['raw_response_json']),raw)
        self.assertEqual(row['raw_response_saved'],1); self.assertTrue(row['finished_at'])


if __name__=='__main__': unittest.main()
