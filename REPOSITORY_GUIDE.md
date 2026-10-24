# Averra Agents

网站 Agent Mode 对应的脱敏源码，ETF与Stock版本独立归档，不混用策略。

- 趋势进攻版：`etf-trend-agent/`，发布2026.10.24-RF-50200-audit1，正式策略2026.10.23-RF-50200-RESTORED。网站第247版。源码与网站公开源码包逐文件一致；版本与压缩包SHA256见WEBSITE_RELEASE.json。
- 稳健配置版：`etf-steady-agent/`，SMH70/QQQ20/SGOV10，etf_cycle_v3_sgov及2026-10-15 operations发布。2026-10-24核对未发现后续策略更新，网站已有最新版本；Mac/Windows原下载包哈希见该目录WEBSITE_RELEASE.json。平台差异文件见该目录README。

- Stock Agent：`stock-agent/`，MONITOR_ONLY。网站桌面payload标识stock_validation_20261016，内部operations版本标识保留原文。2026-10-24核对162个本机源码/模板的脱敏结果与网站一致，无待发布代码更新；Mac/Windows共有165个相同payload文件，另附平台桥接器。原包哈希见该目录WEBSITE_RELEASE.json。

各目录SHA256SUMS.json保留逐文件校验。不含本机凭证、私人账户数据库、日志或验收许可。克隆不启动服务。独立离线指标工具不参与交易，TradingView实际数值一致性尚未验证。

网站桌面App均为只读。趋势版完整源码启用实盘需用户本机另行授权及最新验收；稳健公开入口始终只读。当前收录两个ETF版本与Stock，不是网站全部模式归档；Monitor/Finance未纳入本次归档，运行中的服务均未因此更改或启用。
