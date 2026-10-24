# Averra Stock Agent

网站Stock Agent Mode的脱敏源码归档。独立于两个ETF版本，保持MONITOR_ONLY；不连接券商，不具备真实下单能力。

2026-10-24核对：网站Mac/Windows包含完全相同的165个payload文件，其中162个源码/模板与本机当前源码的正式脱敏结果一致，另3个为公开发布说明。未发现未发布代码更新，网站原包保留，不重复发布。

`payload/`逐文件对应网站两种平台下载包内的源码、默认配置、测试和文档。`platform/`包含实际包内的Stock桥接器、构建信息以及网站源码中的通用启动器。通用启动器服务其他模式，不代表这些模式的完整归档。

版本区分：桌面payload标识为stock_validation_20261016，内部VERSION与release.json保留其原有2026-10-15 operations标识；没有为本次GitHub归档伪造新策略版本。原包哈希与下载地址见WEBSITE_RELEASE.json，逐文件哈希见SHA256SUMS.json。

公开包intraday_config.json保持disabled，不自动安装或运行服务；作者本机的运行状态和授权不随源码转移。完整使用规则见payload/README.md与VALIDATION_GUIDE.md。历史研究/Shadow结果不能当作实盘成交或未来收益证明。

本仓库不含.env、真实API Key、Telegram Token、聊天收件人、真实持仓、账本、运行数据库或日志。需要自行配置独立的Stock凭证，不复用ETF的机器人身份。下载和克隆不会启用后台任务、发送Telegram或调用付费模型。

离线测试在payload目录执行 `python3 -m unittest discover -s tests -v`；不要把单元测试中的模拟记录当真实数据。Windows桌面包仍需真实设备验收。
