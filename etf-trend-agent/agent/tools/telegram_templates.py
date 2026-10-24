"""Presentation only: never makes trading decisions or sends requests."""
import math


def _money(value):
    if value == "UNKNOWN":
        return "未知"
    try:
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return f"${number:,.2f}"
    except (TypeError, ValueError):
        pass
    return "数据不可用"


def _execution_state(value):
    return {"WAIT": "已跳过", "SKIPPED": "已跳过", "NOT_SUBMITTED": "尚未提交",
            "SUBMITTED": "已提交", "PARTIALLY_FILLED": "部分成交", "FILLED": "已成交",
            "REJECTED": "已拒绝", "CANCELED": "已取消", "BLOCKED": "已阻止"}.get(str(value).upper(), str(value))


def _reason(value):
    return {"Signal not strong enough": "买入信号不足，继续观察。",
            "BUY": "满足当前模式的候选条件",
            "WAIT_RISK_CASH_RETENTION": "等待风险恢复确认或下一档资金许可",
            "WAIT_SMH_NO_PULLBACK": "SMH尚未出现均线或RSI回调",
            "WAIT_SMH_WEAK_TREND": "SMH弱势趋势，等待有限分档许可",
            "WAIT_SMH_OVERHEAT": "SMH短期过热，暂不追涨",
            "WAIT_SMH_SIGNAL_UNVERIFIED": "SMH信号数据未核实",
            "No additional allocation needed": "当前无需追加配置。",
            "WAIT_SCORE_BELOW_THRESHOLD": "评分低于买入门槛",
            "no_v1_buy_candidate": "本轮没有买入计划",
            "BLOCKED_BY_UNRESOLVED_INTENT": "存在未闭环订单，等待对账",
            "SMH_DAILY_ONE_BUY_LIMIT": "SMH本交易日已提交买单，下一交易日9:30后重新判断",
            "SMH_DAILY_HISTORY_UNVERIFIED": "SMH交易记录尚未核实",
            "QQQ_DAILY_TWO_BUY_LIMIT": "QQQ本交易日已提交两笔买单，下一交易日9:30后重新判断",
            "QQQ_SECOND_BUY_SCORE_INCREASE_BELOW_10": "QQQ第二笔需比第一笔评分高至少10分，或两笔评分均≥50",
            "QQQ_FIRST_BUY_NOT_FILLED": "QQQ第一笔尚未确认成交，不追加买入",
            "QQQ_SCORE_UNVERIFIED": "QQQ本次或第一笔买入评分未核实",
            "QQQ_DAILY_HISTORY_UNVERIFIED": "QQQ当日交易记录尚未核实",
            "PLAN_INVALIDATED_BY_FRESH_SNAPSHOT": "提交前账户或行情更新，计划未通过复核；本轮不下单，下个窗口重新计算",
            "account_data_unavailable": "账户数据不可用",
            "REGIME_DATA_UNAVAILABLE:regime_cash_unverified": "可用资金尚未核实，暂停买入",
            "OBSERVED_SNAPSHOT_UNVERIFIED": "账户资金一致性尚未核实，未提交订单",
            "budget_reconciliation_failed": "策略预算尚未核实"}.get(str(value), str(value))


def _approval_message(result):
    from tools.approval_compatibility import status_message
    text = status_message(result)
    for original, translated in {
        "ROBINHOOD APPROVAL REQUIRED": "Robinhood 需要你审批",
        "ORDER STATUS REQUIRES REVIEW": "订单状态需要核查",
        "Ticker: ": "ETF：", "Planned Amount: ": "计划金额：",
        "Decision Time: ": "决策时间：", "Execution State: ": "执行状态：",
        "Reservation: ": "已预留金额：",
        "Robinhood requires manual approval.": "请在 Robinhood App 中查看并审批该请求。",
        "Please review the request in the Robinhood app.": "",
        "Robinhood approval visibility is not currently available through this MCP connection.": "当前接口无法读取 Robinhood 审批状态。",
        "Do not resubmit automatically.": "保留预留，不自动重提。",
        "No additional ETF orders will be submitted until this intent is reconciled.": "该订单意图闭环前，不会提交新的 ETF 订单。",
        "Approval is only available in Robinhood; Telegram cannot approve or clear this intent.": "只能在 Robinhood App 审批；Telegram 无法审批或清除该订单意图。",
    }.items():
        text = text.replace(original, translated)
    return "\n".join(line for line in text.splitlines() if line)


def score_line(symbol, decision=None, *, account_unavailable=False):
    """Display the recorded buy score only; zero for missing data is a placeholder."""
    block = decision if isinstance(decision, dict) else {}
    items = block.get("decisions", {})
    item = items.get(symbol, {}) if isinstance(items, dict) else {}
    item = item if isinstance(item, dict) else {}
    reasons = item.get("reason") or []
    unavailable = account_unavailable or block.get("data_ready") is False or "account_data_unavailable" in reasons
    score = item.get("buy_score")
    if unavailable:
        return f"{symbol} 评分：0（账户/必要数据不可用，非实际评分）"
    if symbol == 'SMH' and ('REGIME_DATA_UNAVAILABLE:regime_cash_unverified' in reasons
                            or 'WAIT_SMH_SIGNAL_UNVERIFIED' in reasons):
        return 'SMH 评分：未生成（独立评分尚未完成）'
    if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score):
        return f"{symbol} 评分：{score:g}"
    return f"{symbol} 评分：0（本轮评分未生成，非实际评分）"


def with_scores(message, decision=None):
    if message.startswith("Cycle "):
        for original, translated in {"Cycle ": "周期 ", ": mode=": "：运行模式=", ", status=": "，执行状态=",
                ", mcp_connected=": "，MCP连接=", ", blocked=": "，数据/连接受限=", "=skipped": "=已跳过",
                "=blocked": "=已阻止", "=live": "=LIVE_TRADING",
                "=True": "=是", "=False": "=否"}.items():
            message = message.replace(original, translated)
    if all(f"{symbol} 评分：" in message for symbol in ("QQQ", "SMH")):
        return message
    unavailable = "ACCOUNT DATA UNAVAILABLE" in message or "账户未同步" in message
    return message + "\n\n" + "\n".join(
        score_line(symbol, decision, account_unavailable=unavailable)
        for symbol in ("QQQ", "SMH")
    )


def _volatility_line(label, item):
    if not isinstance(item, dict) or item.get("available") is not True:
        return f"{label} 数据不可用"
    level = item.get("level")
    change = item.get("change_pct_1d")
    arrow = "↑" if isinstance(change, (int, float)) and change > 0 else "↓" if isinstance(change, (int, float)) and change < 0 else "→"
    change_text = f"{arrow}{abs(float(change)):.1f}%" if isinstance(change, (int, float)) else "涨跌数据不可用"
    stale = " | 数据过期" if item.get("stale") else ""
    regime = str(item.get('regime', 'UNKNOWN')).upper()
    regime = {"LOW": "低", "NORMAL": "正常", "ELEVATED": "偏高", "HIGH": "高", "EXTREME": "极高", "UNKNOWN": "未知"}.get(regime, regime)
    return f"{label} {float(level):.1f} {change_text} | {regime}{stale}"


def format_decision(decision):
    items = decision.get("decisions", {}) if isinstance(decision, dict) else {}
    lines = ["📊 ETF Agent｜决策通知", ""]
    any_buy = False
    cash_blocked = False
    for symbol in ("QQQ", "SMH"):
        item = items.get(symbol, {})
        action = str(item.get("action", "UNKNOWN")).upper()
        lines += [symbol, "决策：" + (action if action in {"BUY", "WAIT"} else "数据不可用")]
        lines.append(score_line(symbol, decision))
        reasons = item.get("reason", [])
        if isinstance(reasons, str):
            reasons = [reasons]
        item_cash_blocked = 'REGIME_DATA_UNAVAILABLE:regime_cash_unverified' in reasons
        cash_blocked = cash_blocked or item_cash_blocked
        reason = "；".join(_reason(r) for r in reasons[:2]) if isinstance(reasons, list) else ""
        lines.append("原因：" + (reason[:180] or "未提供原因。"))
        if action == "BUY":
            any_buy = True
        if item.get("execution_eligibility"):
            lines.append("执行资格：" + item["execution_eligibility"])
            lines.append("条件性计划金额：$" + str(item.get("conditional_preapproval_amount")) + "；许可金额 UNKNOWN，未提交。")
        if "selected_for_execution" in item and action == "BUY":
            lines.append("本轮候选：" + ("已选中（仍需全部执行检查）" if item["selected_for_execution"] else "本轮不提交"))
        proposal = (item.get('metadata') or {}).get('score_dynamic_v1') or (
            ((decision.get('plan_diagnostic') or {}).get('plan') or {}).get('etfs') or {}).get(symbol, {})
        if proposal.get('regime_mode'):
            lines.append('策略模式：' + {'EARLY_INVEST':'轻度择时','RISK_RETENTION':'现金保留／分档择时'}.get(proposal['regime_mode'],proposal['regime_mode']))
            if proposal.get('multiplier') is not None:
                lines.append(f"买入倍率：{float(proposal['multiplier']):.1%}")
            if proposal.get('stage_id'):
                lines.append('回调档位：' + {'D5':'5%','D10':'10%','D20':'20%'}.get(proposal['stage_id'],proposal['stage_id']))
        known_wait = action == 'WAIT' and decision.get('data_ready') is not False and decision.get('allocation_data_ready') is not False and 'account_data_unavailable' not in reasons
        planned = '0.00' if known_wait else proposal.get('score_adjusted_amount', item.get('amount'))
        if proposal.get('regime_mode') and not known_wait:
            planned = proposal.get('final_permitted_amount',planned)
        if item_cash_blocked:
            planned = 'UNAVAILABLE'
        lines += ['计划金额：' + _money(planned),
                  '提交金额：' + _money(proposal.get('actual_submitted_amount', '0.00')),
                  '成交金额：' + _money(proposal.get('actual_filled_amount', '0.00')),
                  '执行状态：' + ('资金核验未通过（未提交）' if item_cash_blocked else
                      _execution_state(item.get('execution_state', proposal.get('execution_state', 'WAIT' if known_wait else 'NOT_SUBMITTED'))))]
        lines.append("")
    volatility = decision.get("volatility_context", {}) if isinstance(decision, dict) else {}
    if isinstance(volatility, dict) and volatility:
        relative = volatility.get("relative") if isinstance(volatility.get("relative"), dict) else {}
        lines.append("波动率")
        lines.append(_volatility_line("VIX", volatility.get("vix")))
        lines.append(_volatility_line("VXN", volatility.get("vxn")))
        if relative.get("available") is True:
            relative_state = str(relative.get('nasdaq_volatility_state', 'UNKNOWN')).upper()
            relative_state = {"NORMAL": "正常", "NASDAQ_STRESS": "纳斯达克波动压力偏高",
                              "SYSTEMIC_HIGH_VOL": "整体波动偏高", "EXTREME_FEAR": "极度恐慌",
                              "BROAD_STRESS": "大盘波动压力偏高", "RECOVERY": "波动回落"}.get(relative_state, relative_state)
            lines.append(
                f"VXN/VIX {float(relative.get('ratio')):.2f} | "
                f"{relative_state}"
            )
        impacts = []
        for symbol in ("QQQ", "SMH"):
            metadata = items.get(symbol, {}).get("metadata", {}) if isinstance(items.get(symbol), dict) else {}
            contribution = metadata.get("volatility_contribution", {}) if isinstance(metadata, dict) else {}
            if isinstance(contribution, dict) and contribution.get("direction"):
                direction = str(contribution['direction'])
                direction = {"CAUTION": "谨慎", "NEUTRAL": "中性", "ACCUMULATION_OPPORTUNITY": "分批买入机会",
                             "POSITIVE_CONFIRMATION": "正向确认"}.get(direction, direction)
                impacts.append(f"{symbol} {direction}")
        lines.append("波动率影响：" + (" | ".join(impacts) if impacts else "数据不可用"))
        lines.append("")
    if decision.get('unresolved_intent'):
        lines += ['', _approval_message({'audit': decision['unresolved_intent']}), '']
    lines.append("BUY 是计划，提交结果将另行通知，不代表已成交。" if any_buy else
                 "本轮未下单；资金核验通过后再按规则判断。" if cash_blocked else
                 "本轮未产生买入计划，等待下次检查。")
    return "\n".join(lines)


def compact_message(message):
    if message.startswith(("系统启动:", "ACCOUNT SYNC OK:", "Decision received:")):
        return None
    if message.startswith("订单预览:"):
        return "🔎 Trade Bot｜正在核验订单预览，尚未提交。"
    if message.startswith("订单提交:"):
        return "📨 Trade Bot｜已收到订单提交响应，正在核验状态；不代表已成交。"
    if message.startswith("订单状态:"):
        return "📋 Trade Bot｜订单状态已查询。请以 Robinhood 订单记录确认成交情况。"
    return message[:3500]


def format_daily_summary(day_key, mode, snapshot, decision, *, risk_triggered, has_open_orders, halted, allow_live, funding_mode=None):
    def number(value):
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) and parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    def money(value):
        parsed = number(value)
        return f"${parsed:,.2f}" if parsed is not None else "数据不可用"

    def status(value):
        return "是" if value is True else "否" if value is False else "数据不可用"

    sync = snapshot.get("account_sync") or {}
    positions = snapshot.get("positions") or {}
    valued = (decision.get("portfolio") or {}).get("holdings") or {}
    cash = number(snapshot.get("cash", sync.get("cash")))
    values = {symbol: number((positions.get(symbol) or {}).get("market_value",
              (valued.get(symbol) or {}).get("market_value"))) for symbol in ("QQQ", "SMH")}
    # Native portfolio_value may be holdings-only. Use the strategy's explicit
    # cash-inclusive denominator; missing cash or valuation stays unavailable.
    total = cash + sum(values.values()) if cash is not None and all(v is not None for v in values.values()) else None
    lines = [f"📊 ETF Agent｜每日持仓｜{day_key}",
             f"账户：{status(snapshot.get('connected'))} | Robinhood 现金：{money(cash)}"]
    for symbol in ("QQQ", "SMH"):
        value = values[symbol]
        percent = f"{value / total:.2%}" if value is not None and total is not None and total > 0 and value <= total else "数据不可用"
        quantity = (positions.get(symbol) or {}).get("quantity", "数据不可用")
        lines.append(f"{symbol}：{quantity} 股 | {money(value)} | {percent}")
    cash_percent = f"{cash / total:.2%}" if cash is not None and total is not None and total > 0 else "数据不可用"
    lines.append(f"现金：{money(cash)} | {cash_percent} | 总资产：{money(total)}")
    budget = decision.get("budget") or {}
    plan = decision.get('score_dynamic_v1') or ((decision.get('plan_diagnostic') or {}).get('plan') or {})
    reserved = plan.get('reserved', budget.get('reserved'))
    available = plan.get('available_budget', budget.get('available'))
    if available is None and number(budget.get('remaining')) is not None and number(reserved) is not None:
        available = number(budget['remaining']) - number(reserved)
    if (funding_mode or decision.get('funding_mode')) == 'ACCOUNT_CASH':
        funding = decision.get('cash_funding') or {}
        lines += ['Agent 资金规则','月度策略额度：不设上限',
                  '可用非融资现金：' + (money(funding.get('available')) if number(funding.get('available')) is not None else '未核实'),
                  '已预留：' + (money(funding.get('reserved')) if number(funding.get('reserved')) is not None else '未核实'),
                  '本月 Agent 成交：' + money(budget.get('spent')),
                  '每日／单笔上限：$100；历史策略预算账本保留审计。']
    else:
        lines += ["Agent 策略预算", "总额度：" + money(budget.get('monthly_limit')), "已使用：" + money(budget.get('spent')),
                  "已预留：" + money(reserved), "可用额度：" + money(available),
                  "策略预算与 Robinhood 账户现金分开核算。"]
    lines += [f"风控触发：{status(risk_triggered)} | 未完成订单：{status(has_open_orders)}",
              f"交易暂停：{status(halted)} | 实盘许可：{status(allow_live)}",
              "QQQ 评分：" + score_line("QQQ", decision).split("：", 1)[1],
              "SMH 评分：" + score_line("SMH", decision).split("：", 1)[1]]
    return "\n".join(lines)


def format_v1_execution(result):
    audit = result.get("audit") or {}
    status = str(result.get("status", "UNKNOWN")).upper()
    lines = ["ETF Agent｜订单执行"]
    if audit.get("ticker"):
        lines.append("ETF：" + audit["ticker"])
    planned = result.get('requested_amount', audit.get('requested_amount', 'UNAVAILABLE'))
    submitted = planned if audit.get('broker_order_id') else 'UNKNOWN' if status == 'EXECUTION_UNKNOWN' else '0.00'
    filled = audit.get('filled_amount', result.get('filled_amount', 'UNAVAILABLE'))
    known_wait = status == 'SKIPPED' and not audit and not result.get('intent_id') and not result.get('order_id') and result.get('requested_amount') is None
    cash_blocked_before_submit = (status == 'BLOCKED' and result.get('reason') == 'OBSERVED_SNAPSHOT_UNVERIFIED'
                                 and not audit and not result.get('intent_id') and not result.get('order_id')
                                 and result.get('requested_amount') is None)
    if known_wait:
        planned = submitted = filled = '0.00'
    if cash_blocked_before_submit:
        submitted = filled = '0.00'
    invalidated_before_submit = (status == 'BLOCKED'
        and result.get('reason') == 'PLAN_INVALIDATED_BY_FRESH_SNAPSHOT'
        and audit.get('state') == 'REJECTED' and audit.get('submit_attempts') == 0
        and not audit.get('broker_order_id'))
    action = 'WAIT' if known_wait else 'BUY' if audit.get('ticker') or result.get('requested_amount') is not None or result.get('intent_id') else 'UNKNOWN'
    if invalidated_before_submit or cash_blocked_before_submit:
        action = 'WAIT'
    lines += ['决策：' + action, ('原计划金额：' if invalidated_before_submit else '计划金额：') + _money(planned), '提交金额：' + _money(submitted),
              '成交金额：' + _money(filled), '执行状态：' + ('已跳过（未提交）' if invalidated_before_submit
                  else '资金核验未通过（未提交）' if cash_blocked_before_submit
                  else _execution_state(audit.get('state', status)))]
    if status == "SUBMITTED":
        lines.append("已提交，尚未核实成交；预算继续预留。")
    elif status == "EXECUTION_UNKNOWN":
        lines.append("提交结果未知；保留预留，不重试，等待券商对账。")
    elif result.get("reason"):
        lines.append("原因：" + _reason(result["reason"]))
    if audit.get('state') in {'AWAITING_USER_APPROVAL', 'APPROVAL_STATE_UNKNOWN', 'EXECUTION_UNKNOWN', 'RECONCILIATION_REQUIRED', 'CANCEL_PENDING'}:
        lines += ['', *[line for line in _approval_message(result).splitlines()
                        if not line.startswith(('ETF：', '计划金额：', '执行状态：'))]]
    return "\n".join(lines)
