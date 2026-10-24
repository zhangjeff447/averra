"""Exchange sessions, half days and conservative settled-cash eligibility."""
from datetime import datetime,timedelta,time,timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
import exchange_calendars as xc
import pandas as pd
from pandas.tseries.holiday import USFederalHolidayCalendar
ET=ZoneInfo('America/New_York')
@lru_cache(None)
def exchange():return xc.get_calendar('XNYS',start='2015-01-01',end='2036-12-31')
def previous_session(day):return exchange().date_to_session(day,direction='previous').strftime('%Y-%m-%d') if not exchange().is_session(day) else exchange().previous_session(day).strftime('%Y-%m-%d')
def is_session(day):return exchange().is_session(day)
def funding_session(day):
 d=pd.Timestamp(day);monday=d-timedelta(days=d.weekday())
 return exchange().date_to_session(monday,direction='next').date()==d.date()
def scheduled(now):
 local=now.astimezone(ET);day=local.date().isoformat()
 if not is_session(day):return None
 opened=exchange().session_open(day).to_pydatetime();closed=exchange().session_close(day).to_pydatetime()
 if not opened<=now<closed:return None
 for hour,minute in [tuple(map(int,value.split(':'))) for value in __import__('etf.cycle',fromlist=['POLICY']).POLICY['slots_et']]:
  slot=local.replace(hour=hour,minute=minute,second=0,microsecond=0)
  if 0<=(local-slot).total_seconds()<300:return f'{hour:02d}:{minute:02d}'
 return None
def settlement_day(executed_at):
 # An execution never creates spendable cash. This is only a minimum hold date;
 # the current broker cash/BP and unsettled-funds evidence still cap spending.
 day=datetime.fromisoformat(executed_at).astimezone(ET).date().isoformat()
 d=exchange().next_session(day)
 holidays=USFederalHolidayCalendar().holidays(d-timedelta(days=3),d+timedelta(days=15))
 while d in holidays:d=exchange().next_session(d)
 return d.strftime('%Y-%m-%d')


def risk_session(now):
 local=now.astimezone(ET);day=local.date().isoformat()
 return is_session(day) and exchange().session_open(day).to_pydatetime()<=now<exchange().session_close(day).to_pydatetime()
