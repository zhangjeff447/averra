"""Append an isolated report after trading fields have been finalized."""
import copy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def append_to(message, snapshot, now, *, fetcher=None):
    from tools.drawdown_stress_monitor import report_once, unavailable_section
    try:
        result = (fetcher or report_once)(now, ROOT / 'state/report_cache',
            ROOT / 'state/report_history.json',
            volatility_context=copy.deepcopy(snapshot.get('volatility_context')),
            runtime_quotes=copy.deepcopy(snapshot.get('quotes')))
        section = result['section']
    except Exception as exc:
        result = {'status': 'DATA_UNAVAILABLE', 'reason': type(exc).__name__}
        section = unavailable_section('DATA_UNAVAILABLE:' + type(exc).__name__)
    return message + '\n\n' + section, result
