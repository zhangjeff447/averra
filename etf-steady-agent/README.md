# Averra ETF 稳健配置版

SMH70% / QQQ20% / SGOV10%，策略 etf_cycle_v3_sgov，2026-10-15 operations发布。
2026-10-24核对：网站源码与该版最新本地发布源码一致，未发现后续策略更新，所以不重新编号、不重打包或重复发布网站。当前归档对应网站第247版的稳健版下载。

`payload/`逐文件对应Mac公开下载包中的Python源码、配置模板、测试和既有公开历史数据。Windows共用这些文件，`platform/windows/payload/`包含三个平台差异文件，使用Windows时需以同路径覆盖。`platform/`保留实际包内桥接器和当前网站中的启动器源码；macOS启动器也服务其他模式，这不构成这些模式的完整归档。

网站原包的SHA256与下载链接在WEBSITE_RELEASE.json，逐文件哈希在SHA256SUMS.json。下载包保持只读；不会安装交易任务或授权实盘，克隆GitHub也不会开启交易。

策略与风控保持原样：正常70/20/10，回调SGOV上限20%，复苏上限5%；SMH/QQQ只买不卖，仅SGOV按既有条件卖出。日买入和单笔100美元等现有限制不变。不要把这些规则与趋势进攻版60/10/30混用。

本归档不含本机凭证、私人账户配置、真实金融数据库、日志或验收许可。历史CSV不是最新行情。完整策略和使用说明见payload/README_ZH.md与README_EN.md。未把新离线指标、做T或其他研究候选加入本版交易。

离线检查：准备Python及payload/requirements.txt依赖后，在payload目录执行 `python3 -m pytest -q`。Windows版本尚未进行真实Windows设备验收。
