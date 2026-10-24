# ETF 离线指标核对工具

本机新增工具；未接入实盘，也不自动运行或发送 Telegram。借鉴 tradingview-scraper 的按标的/周期提取指标思路，不安装或调用该爬虫，不抓取 TradingView 数据。不产生任何交易建议或订单。

功能：

- QQQ、SMH、TQQQ 的 SMA50/200、EMA20、Wilder RSI14、MACD(12/26/9)。
- 并列显示旧辅助模块简单平均 RSI14。差异属于公式口径，不能自动替换已验证策略。
- 输入 CSV 与原 manifest 的 SHA256、日期范围、重复、完整交易日、价格有效性核对。
- 必须明确截至哪个已完成交易日；排除之后的行，过期数据保留 STALE；缺数据不是0。
- 可比较用户已有授权的本地指标导出；日期、周期、复权、公式、历史哈希不一致时显示 NOT_COMPARABLE，没有导出则 NOT_VERIFIED。
- JSON 和中文 Markdown 报告输出；全程离线。无网络、凭证、券商、交易账本、实盘模块或发送器导入。

运行示例（在项目根目录，使用现有依赖环境，不安装新依赖）：

```sh
production/etf-return-first/.venv/bin/python3 -B tools/etf_indicator_audit/audit.py \
  --data-dir work/etf-intraday-backtest-2021-2026/daily \
  --through 2026-10-23 \
  --output-dir work/etf-indicator-audit-20261024/historical-smoke
```

该缓存只到2026-10-21，此例应显示 STALE，不能当作10月23日或当前行情。
`--through` 必须早于本机美东当天，保守排除正在形成的日线；无隐含自动“今天”。
当前运行依赖仅为现有 exchange_calendars（检查NYSE交易日）。计算逻辑使用标准库，无需新增包。

参考导出 `--reference local.json` 的格式如下。只能填真实一致的元数据，不能为通过检查而伪造历史哈希或公式。TradingView普通截图或扫描器快照通常不足以满足这些条件，因此可正常保留“未验证”。数值MATCH也不是平台认证、数据授权或执行许可。

```json
{
  "QQQ": {
    "symbol": "QQQ",
    "through": "2026-10-21",
    "timeframe": "1d",
    "price_basis": "Adj Close",
    "formula_profile": "SMA_SEEDED_EMA_WILDER_RSI14_MACD12_26_9_v1",
    "history_sha256": "actual-identical-input-hash",
    "source": "authorized export provenance",
    "values": {
      "SMA50": null,
      "SMA200": null,
      "EMA20": null,
      "RSI14_WILDER": null,
      "MACD": null,
      "MACD_SIGNAL": null,
      "MACD_HIST": null
    }
  }
}
```

RSI用首14个变动的均值初始化Wilder RMA，alpha=1/14；完全平价时分母为0，返回缺失值。旧辅助模块将该边界设为50，两种口径保留区别。EMA以首N期SMA为种子，alpha=2/(N+1)；MACD信号以首9个有效MACD的SMA初始化。有限预热长度、行情源和复权设置仍会影响平台比对；不声称逐位复制Pine内建函数。

当前正式 Return First 的主趋势使用SMA50/200。检查旧RSI模块不代表当前主策略使用RSI，更不意味着将其改为Wilder后收益会提高。

来源：

- [tradingview-scraper](https://github.com/mnwato/tradingview-scraper)
- [TradingView RSI 公式](https://www.tradingview.com/support/solutions/43000502338-relative-strength-index-rsi/)
- [TradingView 均线说明](https://www.tradingview.com/support/solutions/43000502589-moving-averages/)
- [TradingView MACD 说明](https://www.tradingview.com/support/solutions/43000502344-moving-average-convergence-divergence-macd-indicator/)
- [TradingView 使用条款](https://www.tradingview.com/policies/)

核对报告与测试证据：`work/etf-indicator-audit-20261024/`。缺少同数据的外部授权导出时，本次交付只能证明本地计算与输入检查，不证明与TradingView实际输出一致。正式交易源码、配置、调度不变。
