# Averra ETF Agent · 趋势进攻版

发布：2026.10.24-RF-50200-audit1。策略：2026.10.23-RF-50200-RESTORED。

上一个完整交易日QQQ复权SMA50>SMA200时，账户目标SMH60%/QQQ10%/TQQQ30%；原趋势退出和每周TQQQ超过35%减至30%保留。SMH/QQQ只买不卖；已结算净卖款按核心80/20补正缺口。只用核实的自有已结算资金，不借款。不设每月300、每日100、每窗口20策略限额，也不设固定6.6%价格止损；券商最低额、0.25执行缓冲、资金与订单保护保留。三个窗口10:05/12:30/15:30美东。不是每个窗口保证购买。

本次同步当前正式源码、手动外部订单归属、持仓/通知与QQQ/SMH支撑突破展示；展示不改变交易。保留全局一个未闭环Intent、UNKNOWN不重提、审批由Robinhood处理、对账失败停止。

附带tools/etf_indicator_audit：仅离线核对SMA/EMA/RSI/MACD。旧简单RSI和Wilder口径并列，不加入实盘。没有TradingView实际导出，不宣称平台一致性；不抓取TradingView或运行LLM做交易。没有把做T、Supertrend或其他研究候选自动上线。

本包不含凭证、账户配置、私人账本、状态、日志或本机验收许可。网站macOS/Windows App保持只读，不自动安装交易任务；此源码仅在用户自行完成本机完整对账、授权和安全验收后才可启用实盘。Windows App不因此取得完整macOS实盘安装能力。下载、解压或上传GitHub都不会启动交易。

已有兼容本机升级方式见INSTALL.txt；不要将压缩包当作账本备份或覆盖现有账本。首次运行配置检查：`python3 agent/run_agent.py --check-config`。离线回归：安装agent/requirements.txt依赖后在agent目录执行`python3 -m pytest tests_return_first -q`。辅助工具使用方法见tools/etf_indicator_audit/README.md。

历史数据与audit/为旧公开研究复现材料，不是当前行情、真实成交或未来收益证明；最新策略以agent/etf/policy.json和tests_return_first/test_restored_50200.py为准。尚未证明新增指标或做T提高收益。本次实盘源码不增加新策略。
