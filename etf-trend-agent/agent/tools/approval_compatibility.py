"""Approval-safe labels and status presentation; never approves or submits."""
from datetime import datetime
from zoneinfo import ZoneInfo

COMPATIBILITY_MODE = 'APPROVAL_SAFE_COMPATIBILITY_MODE'
NATIVE_MODE = 'NATIVE_APPROVAL_VISIBILITY_MODE'
MAX_UNRESOLVED_TRADE_INTENTS = 1
# Explicit semantic labels requested by the owner. These are NOT a claim that
# Robinhood has published this enum. Unknown/new alert shapes remain unknown.
APPROVAL_LABELS = {'approval required', 'trade approval required', 'pending approval',
                   'awaiting user approval', 'user action required'}

def approval_required(response):
    if not isinstance(response, dict):
        return False
    data = response.get('data') if isinstance(response.get('data'), dict) else response
    order = data.get('order') if isinstance(data.get('order'), dict) else {}
    checks = data.get('order_checks') if isinstance(data.get('order_checks'), dict) else {}
    for row in (response, data, order, checks):
        if row.get('approval_required') is True or row.get('customer_approval_required') is True:
            return True
        for key in ('alert_type', 'state', 'status', 'message', 'error'):
            value = row.get(key)
            if isinstance(value, str) and value.strip().lower().replace('_', ' ') in APPROVAL_LABELS:
                return True
    return False

def order_from_response(response):
    if not isinstance(response, dict) or response.get('isError') or response.get('ok') is False:
        return None
    data = response.get('data') if isinstance(response.get('data'), dict) else response
    if isinstance(data.get('order'), dict):
        return data['order']
    # Retained adapter-normalized legacy envelope; native data.order is preferred.
    if data.get('order_id'):
        return dict(data, id=data['order_id'])
    return None

def status_message(result):
    audit = result.get('audit') or {}
    state = audit.get('state', result.get('status', 'UNKNOWN'))
    approval = state == 'AWAITING_USER_APPROVAL'
    heading = 'ROBINHOOD APPROVAL REQUIRED' if approval else 'ORDER STATUS REQUIRES REVIEW'
    created = audit.get('created_at')
    try:
        when = datetime.fromisoformat(created).astimezone(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M:%S ET')
    except (TypeError, ValueError):
        when = 'UNKNOWN'
    lines = [heading, 'Ticker: ' + str(audit.get('ticker', 'UNKNOWN')),
             'Planned Amount: $' + str(audit.get('requested_amount', 'UNKNOWN')),
             'Decision Time: ' + when, 'Execution State: ' + str(state),
             'Reservation: $' + str(audit.get('reserved_remaining', 'UNKNOWN'))]
    if approval:
        lines += ['Robinhood requires manual approval.',
                  'Please review the request in the Robinhood app.']
    else:
        lines += ['Robinhood approval visibility is not currently available through this MCP connection.',
                  'Do not resubmit automatically.']
    lines += ['No additional ETF orders will be submitted until this intent is reconciled.',
              'Approval is only available in Robinhood; Telegram cannot approve or clear this intent.']
    return '\n'.join(lines)


def response_from_error(error):
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        response = getattr(error, 'mcp_response', None)
        if isinstance(response, dict):
            return response
        error = error.__cause__
    return None
