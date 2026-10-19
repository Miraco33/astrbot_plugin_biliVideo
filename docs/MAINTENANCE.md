# 远端维护与更新

## 仓库定位

- 自用维护源：https://github.com/Miraco33/astrbot_plugin_biliVideo
- 上游：https://github.com/storyAura/astrbot_plugin_biliVideo
- 初始上游版本：v2.1.0
- 初始上游提交：7cfb5ef0c365e35949e8afd5ff0eca7a513924cb
- 首个 fork 版本：v2.1.1（仓库身份、维护规则与发布检查初始化，不宣称已修复下载或转写故障）。
- 正式源码和文档只保存在 GitHub；部署环境继续保存运行代码、凭据及持久化数据，不建立新的长期本地开发工作区。
- 本仓库公开可见，不向插件市场或包仓库分发；保留 fork 与原作者归属。

## 文件职责

- AGENTS.md：稳定维护规则和每次更新/发布的脱敏要求。
- docs/KNOWN_ISSUES.md：证据、源码风险、验证缺口和待处理事项。
- CHANGELOG.md：已经完成的正式变更及验证。
- 本文件：更新来源、稳定分支、维护与回滚流程。

不把会话描述直接升级为当前部署事实，不另建工具专属记忆文件。

## AstrBot 控制台更新来源

依据 AstrBot 官方 v4.28.2 源码核对：

1. 插件 name=astrbot_plugin_bilivideo、author=storyAura 保持不变，避免配置与插件身份迁移。
2. metadata.yaml 的 repo 指向本 fork，但 AstrBot 还会持久化独立的安装来源。
3. 已有 market 来源记录时，仅更改 metadata.repo 或重载不足以完成切源。
4. 官方切换方式是通过“从 GitHub/仓库安装”输入本 fork URL，覆盖同名已安装插件；不要先卸载。
5. 安装成功后核验 install_method=repository、安装来源 repo 指向本 fork、原目录和配置/订阅保留。
6. 后续更新下载本 fork 默认分支源码 ZIP。GitHub Release 或标签不自动决定部署版本。
7. 仓库直装插件可能没有市场版本更新提示，使用已安装插件的更新/强制更新入口。
8. 同名安装/更新会自动重载插件，不需要因此重启整个容器。

依赖边界：v4.28.2 会预检 requirements，全部满足时跳过安装；缺失、版本不符或预检失败可能调用安装器。先核验实际环境和依赖，再执行切源；requirements 相同不能单独证明依赖零变更。首个 fork 提交保持上游 requirements 原文。

源码依据：
- [PluginService](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/dashboard/services/plugin_service.py)
- [PluginManager](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/star/star_manager.py)
- [PluginUpdater](https://github.com/AstrBotDevs/AstrBot/blob/v4.28.2/astrbot/core/star/updater.py)

## 每次维护和发布

1. 读取本仓库规则、当前默认分支及未完成问题；先确认工作基线。
2. 比较上游变更，逐项决定是否移植；保留自用修复，避免无条件同步覆盖。
3. 修改后检查新增/修改的代码、文档、提交说明和附件，不上传运行数据。
4. 在写入 GitHub 前人工脱敏；运行 publication-check、相关测试及现有 CI。
5. 只把验证通过的变更合入默认分支，更新 metadata.yaml 版本及 CHANGELOG。
6. 按需在控制台更新；分别确认加载、配置保留、字幕/音频路径、生成、发送返回值与客户端收到。
7. 把脱敏后的部署结果、提交和未验证范围写回仓库。不要把任务完成写成用户已收到。

CI 在远端运行，不使用生产凭据。扫描不能证明历史或所有二进制无敏感信息，新增图片/附件仍需人工检查。
本轮继承的历史图片未做全面视觉审计；后续不要将它们作为脱敏已通过的示例凭证。

## 回滚

保留切换前的代码基线及部署环境受限备份。回滚代码不等同于回滚数据库或订阅状态。
不得为回滚直接覆盖运行中的数据；数据恢复和容器变更按部署环境授权执行。
稳定部署记录尚未建立前，不宣称 fork 已上线。

## 许可和归属

- LICENSE 保留 storyAura 与 BiliNote / Jeffery Huang 的两段 MIT 归属。
- Noto Sans SC 保留 fonts/NotoSansSC-LICENSE.txt（SIL OFL）。
- JetBrains Mono 保留 fonts/JetBrainsMono-LICENSE.txt（官方 SIL OFL 文本）。
- 字体许可不改写为 MIT；继承字体二进制来源尚未逐一复核。
