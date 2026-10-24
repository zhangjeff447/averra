# 2026-10-15 运行与通知更新

ETF 保留 SMH 70% / QQQ 20% / SGOV 10% 及原 SGOV 规则。新增 BUY / SELL / WAIT 通知格式、计划及成交金额、参考价和成交均价、剩余现金、市场行情、独立每日持仓组件、去重回执、运行与设置历史。每日持仓组件使用独立只读读取，交易日美东15:31起；公开入口仍仅允许配置检查和只读账户验证，不安装后台服务或自动发送 Telegram。已有实盘安装须由本人迁移和验收，不能覆盖账本。没有每月300美元额度，也不显示旧累计虚拟预算。

ETF keeps SMH 70% / QQQ 20% / SGOV 10% and the existing SGOV rules. Includes BUY / SELL / WAIT report formatting, plan/submission/fill amounts, reference/fill prices, remaining cash, market regime, a separate read-only daily-holdings component, delivery deduplication, run and setting history. Holdings reports are due from 15:31 ET on trading days after private setup. The public entry remains configuration/read-only account validation only; it does not install a background service or send Telegram automatically. Existing live installations require owner-managed migration and acceptance without replacing ledgers. No monthly $300 allowance or legacy cumulative virtual budget is displayed.

Local history: python agent_ops.py --db <your-own-operations.sqlite> status
