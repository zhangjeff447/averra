"""Bounded reconnection for reads; submission is never retried here."""
import time
from datetime import datetime,timezone
from urllib.error import HTTPError,URLError
from .calendar import ET,scheduled,risk_session


def transient(exc):
    chain=[]
    while exc is not None and exc not in chain:
        chain.append(exc);exc=exc.__cause__
    for error in chain:
        if isinstance(error,HTTPError):return error.code in (404,408,429,500,502,503,504)
    text=' '.join(str(error).lower() for error in chain)
    if any(word in text for word in ('unauthorized','forbidden','permission','authentication','oauth','schema','policy')):
        return False
    return (any(isinstance(error,(TimeoutError,ConnectionError,URLError)) for error in chain)
            or any(word in text for word in ('session disconnected','mcp disconnected','session not found',
                'invalid session','connection reset','broken pipe','timed out','timeout','getaddrinfo',
                'network is unreachable','temporary failure in name resolution','nodename nor servname')))


def retry_read(operation,*,reconnect=None,sleep=time.sleep):
    for attempt in range(3):
        try:
            if attempt and reconnect:reconnect()
            return operation()
        except Exception as exc:
            if attempt==2 or not transient(exc):raise
            sleep((2,5)[attempt])


def window_key(now):
    slot=scheduled(now)
    return now.astimezone(ET).date().isoformat()+':'+slot if slot else None


def require_window(window):
    now=datetime.now(timezone.utc)
    if window and window.startswith('STOP:'):
        if not risk_session(now) or window!='STOP:'+now.astimezone(ET).date().isoformat():raise ValueError('recovery_window_expired')
        return
    if window is not None and window_key(now)!=window:
        raise ValueError('recovery_window_expired')
