# 自用维护 fork

本仓库由 Miraco33 自用维护，源自 [storyAura/astrbot_plugin_biliVideo](https://github.com/storyAura/astrbot_plugin_biliVideo)。
正式代码、稳定规则和故障记录均在 GitHub 维护，不建立长期本地工作区，不向插件市场或包仓库分发。
仓库公开可见；每次提交、更新与发布前均须脱敏。

当前 fork 版本 **v2.2.0**：下载/转写有限恢复、订阅失败补偿、按目标确认投递、主对话读取工具结果和运行日志脱敏。显示名为 **biliVideo（自用维护）**，内部名称及作者标识保持原值。

- [维护规则](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/AGENTS.md)
- [控制台更新与回滚](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/docs/MAINTENANCE.md)
- [已知故障与修复待办](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/docs/KNOWN_ISSUES.md)
- [验证记录](https://github.com/Miraco33/astrbot_plugin_biliVideo/actions)

AstrBot 已安装插件的来源独立持久化；只改主页不代表切源完成。保持原插件名称与作者标识，通过仓库安装同名覆盖切换，步骤见维护说明。
以下保留上游使用文档与归属。

---

<div align="center">
  <img src="https://raw.githubusercontent.com/Miraco33/astrbot_plugin_biliVideo/main/logo.png" alt="biliVideo" width="120">

# biliVideo · AstrBot B 站视频解析与 AI 总结

**丢一个 B 站链接,AI 帮你秒出精华总结。**

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.0%2B-blueviolet)](https://astrbot.app/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/LICENSE)
[![Version](https://img.shields.io/badge/version-2.2.0-orange)](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/CHANGELOG.md)
[![Tests](https://github.com/Miraco33/astrbot_plugin_biliVideo/actions/workflows/test.yml/badge.svg)](https://github.com/Miraco33/astrbot_plugin_biliVideo/actions/workflows/test.yml)

</div>

---

## ✨ 为什么是 v2.0?

biliVideo v2.0 是一次**完全重写**的工程升级。主要目标:

- **可维护性**:`main.py` 从 2,000 行单一巨型文件瘦身到约 160 行。所有逻辑下放到 `bilivideo/` 子包,按职责严格分层。
- **健壮性**:HTTP 层采用共享 `aiohttp.ClientSession` + 指数退避重试;订阅文件原子写入 + `fsync`;Cookie 文件 0600 权限。
- **响应速度**:带 TTL 的 LRU 缓存避免同一 BV 重复请求 B 站;single-flight 让多人同时粘贴同一链接只触发一次工作。
- **可测试**:299 个 PyTest 单元/集成测试覆盖 URL 解析、分页、智能截断、结构化输出、订阅持久化、冷却、缓存、消息路由、渲染降级链等。
- **类型化**:所有 API 返回 `dataclass`(`VideoInfo` / `UploaderInfo` / …),配置读取经 `PluginConfig` 校验。

> 命令、配置项、行为对终端用户**完全向后兼容**。配置文件不需要改动即可升级。

**v2.1.0** 完善总结图片与时间戳([#27](https://github.com/storyAura/astrbot_plugin_biliVideo/pull/27), 感谢 [@Eco404](https://github.com/Eco404)):检测到 `wkhtmltoimage` 时继续用 HTML/CSS 并在服务端预渲染 LaTeX;未安装系统渲染工具时自动用纯 Python 后备(Markdown AST + MathText + Pillow)。开启时间戳且走 AstrBot Provider 时,优先强制工具调用拿到结构化章节并校验范围/顺序,失败或不支持工具时回退原 Markdown 提示词;`openai_compatible` 自建 Provider 行为不变。无需改配置即可升级。详见 [CHANGELOG](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/CHANGELOG.md)。

**v2.0.1** 是并发与健壮性修复版:手动 `/检查更新` 与定时推送不再可能重复推送同一视频(按订阅加锁);订阅数据写盘失败会回滚并提示、热重载不再可能被旧实例脏写;渲染与写盘全部移出事件循环;推送目标管理命令限制为管理员;版本号与配置默认值单一来源化。另合入引用消息中 QQ 小程序卡片的直接解析([#24](https://github.com/storyAura/astrbot_plugin_biliVideo/pull/24))。

---

## 🚀 快速开始

```text
1. AstrBot「安装插件 → 从链接安装」填 https://github.com/Miraco33/astrbot_plugin_biliVideo
   已安装上游时同名覆盖，保留配置与数据，无需因此重启容器
   依赖全部由 AstrBot 自动 pip 安装(含静态 ffmpeg + 内建中文字体),VPS / Docker 无需手动装系统依赖
2. /B站登录  → 扫码登录
3. /总结 https://www.bilibili.com/video/BV1xx411c7mD
```

---

## 🏆 核心特性

| 类别 | 特性 |
| --- | --- |
| 输出 | wkhtml HTML/CSS 卡片 / Python Markdown + LaTeX 后备 / 纯文本回退 / 合并转发模式 |
| 总结 | 简洁 / 详细 / 专业 三种风格;AstrBot 工具调用稳定约束章节时间戳 |
| 输入 | 完整链接、短链、BV 号、UID、空间链接、UP 主昵称 |
| 智能 | 自动识别小程序/链接/短链;**触发关键词**配置化 |
| 订阅 | 订阅 UP 主自动推送;支持指定推送群/QQ |
| 加速 | 优先平台字幕(无字幕再下载音频转写) |
| 长视频 | 按章节自动分图,LLM 生成的多章节内容不被截断 |
| 安全 | 群级黑/白名单;用户冷却;并发 BV 去重 |
| 工具 | AI function-call:`bilibili_search_list` / `bilibili_search_download` / `bilibili_search_result` |

---

## 📦 安装

### 前置要求

- AstrBot v4.0+
- Python 3.10+
- 至少配置一个 LLM Provider(DeepSeek、OpenAI、Claude 等),或在配置中切到 `openai_compatible` 自填 API。

### 开箱即用(VPS / Docker / Zeabur 一键安装)

本插件**无需手动安装任何系统依赖**。AstrBot 安装插件时会自动 `pip install -r requirements.txt`,其中已包含:

- **ffmpeg**:由 `imageio-ffmpeg` 提供静态二进制,无字幕视频走 ASR 转写时自动调用;系统若已装 ffmpeg 则优先用系统版。
- **图片渲染**:检测到 `wkhtmltoimage` 时使用 HTML/CSS 渲染并在服务端预渲染 LaTeX;未检测到时自动使用 Markdown AST + MathText + Pillow 后备。内建 Noto Sans SC 中文字型(GB2312 子集,SIL OFL),容器无图形环境也能出图。
- 装好后用 `/总结状态` 即可看到检测出的渲染后端与 ffmpeg 来源(系统 / 内建)。

### 可选增强:wkhtmltopdf HTML/CSS 渲染

若系统安装的 wkhtmltopdf 同时提供 `wkhtmltoimage` 且该命令位于 `PATH`,插件会优先使用原有 HTML/CSS 高保真渲染模式。不同 Docker 基础镜像的软件源可用性不同,请使用镜像发行版支持的安装方式或兼容二进制包。

```bash
# 软件源提供该软件包的 Debian / Ubuntu 版本
apt install -y wkhtmltopdf fonts-wqy-zenhei
```

未检测到 `wkhtmltoimage` 时无需额外配置,会自动启用纯 Python 后备渲染。

### 可选增强:系统中文字体

内建字型为 GB2312 子集,已覆盖绝大多数简体中文。若需繁体 / 日文 / 生僻字的完整覆盖,装一个系统 CJK 字体即可(会被优先使用):

```bash
apt install -y fonts-noto-cjk    # 或 fonts-wqy-zenhei
```

> 纯文本模式:把 `output_image` 设为 `false` 即可完全免图片渲染依赖。

> wkhtml 和纯 Python 后备模式共用 Matplotlib MathText 的 TeX 子集,覆盖分数、根式、上下标、求和、积分、希腊字母和 `\text{}` 等常见总结场景。wkhtml 模式会将公式预渲染为内嵌透明图片,不需要 JavaScript 或网络资源。

---

## 🔧 命令一览

### 基础

| 命令 | 说明 |
| --- | --- |
| `/总结帮助` | 显示帮助 + 当前登录状态 |
| `/总结 <链接 \| BV>` | 为单个视频生成 AI 总结 |
| `/最新视频 <UID \| 空间链接 \| 昵称>` | 获取 UP 主最新视频并总结 |

### 管理

| 命令 | 说明 |
| --- | --- |
| `/总结状态` | 查看版本 / 登录 / LLM / 渲染后端 / 缓存等 |
| `/总结清缓存` | 清除视频信息 + WBI + 总结结果缓存 |
| `/总结模型 [provider ID]` | 列出 / 切换 AstrBot 内置对话模型(仅 astrbot 模式) |

### 登录

| 命令 | 说明 |
| --- | --- |
| `/B站登录` | 扫码登录 |
| `/B站登出` | 清除登录状态 |

### 订阅

| 命令 | 说明 |
| --- | --- |
| `/订阅 <UP>` | 订阅 UP 主,新视频自动推送 |
| `/取消订阅 <UP>` | 取消订阅 |
| `/订阅列表` | 查看当前订阅 |
| `/检查更新` | 检查新视频并重试失败任务；按已配置目标投递 |

### 推送目标

| 命令 | 说明 |
| --- | --- |
| `/添加推送群 <群号>` | 加入推送群列表(仅 bot 管理员) |
| `/添加推送号 <QQ号>` | 加入推送私聊列表(仅 bot 管理员) |
| `/推送列表` | 查看推送目标 |
| `/移除推送 <id>` | 移除推送目标(仅 bot 管理员) |

### 自动识别

| 命令 | 说明 |
| --- | --- |
| `/识别开关` | 开关 B 站链接自动识别 |

> `<UP>` 支持纯数字 UID、空间链接、或 UP 主昵称。
> 设置推送目标后,所有订阅总结仅推送到指定目标;未设置则推到订阅来源。

---

## 🧠 智能消息过滤

引用消息默认**不**触发自动识别,除非引用回复中包含触发关键词。

```text
A: [分享 B 站视频]
B: 引用 A + "帮我总结一下"   ✅ 触发(命中"总结"关键词)
C: 引用 A + "好的"            ❌ 不触发(无关键词)
```

触发关键词在 `_conf_schema.json` 的 `trigger_keywords` 字段里,逗号分隔。
支持中英文混合自定义。

---

## 📦 合并转发模式

```json
{ "enable_forward_message": true }
```

视频信息 + AI 总结打包成聊天记录。

- 视频封面 + 标题
- UP 主、简介、发布时间、播放数据
- 长内容自动分页

---

## 🤖 AI 智能搜索

直接和 AI 对话,无需记忆命令:

```text
搜索 Python 教程视频
从结果里选播放量最高的 3 个,下载并总结
```

AI 先调用 `bilibili_search_list` 搜索，再调用 `bilibili_search_download` 转写。短任务直接返回内容；其余返回 `task_id`，模型应调用 `bilibili_search_result` 读取原视频文本后回答用户，可用 `video_index`、`offset` 和 `length` 分段读取。

结果仅原会话可读，内存保留一小时、最多 16 个任务，每任务最多 200000 字符；超长内容明确标记截断，重载后失效。后台通知不代表主对话已读内容，已结束的模型轮次需要用户续问或模型再次调用结果工具。转写文件仍保存在部署环境的 `<data_dir>/search_results/`，不上传本仓库。

---

## ⚙️ 配置项

配置面板已按功能分组(🔧 通用 / 🤖 LLM / 📝 总结 / 🎨 渲染 / 💬 消息 / 🔍 识别 / 📡 订阅 / 🔐 访问 / 🔎 搜索 / 🧪 实验)。部分常用配置:

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `output_image` | `true` | 总结以图片形式发送 |
| `note_style` | `professional` | `concise`:最多 8 个单句核心观点 / `detailed`:最多 20 章完整记录 / `professional`:最多 12 章结构化分析 |
| `enable_link` | `true` | 在总结中嵌入时间戳标记 |
| `enable_summary` | `true` | 末尾添加 AI 总结段落 |
| `download_quality` | `fast` | `fast`(32k) / `medium`(64k) / `slow`(128k)；同时用于源音轨排序和输出转码 |
| `download_timeout_seconds` | `150` | 下载阶段预算（含有限重试），30–1800 秒 |
| `asr_timeout_seconds` | `180` | 必剪与显式启用的备用转写共享预算，30–1800 秒 |
| `max_audio_duration_minutes` | `180` | 需要下载音频的视频时长上限，1–1440 分钟；超限失败，不截断视频 |
| `max_audio_size_mb` | `128` | 源下载及转写文件大小上限，1–512 MiB |
| `fallback_asr_provider_id` | 空 | 已配置的 AstrBot STT 提供商 ID；默认关闭，启用后会把音频交给所选服务，并可能产生其费用 |
| `prefer_subtitle` | `true` | 优先平台字幕,无字幕才下载音频 |
| `enable_multi_platform` | `false` | 🧪 实验:`/总结` 支持 YouTube/抖音(yt-dlp,字幕优先;非中文 ASR 质量不保证)。YouTube 登录用 `/YT登录` 在聊天里贴 cookies(机器人自动保存,无需建文件;有封号风险,请用小号) |
| `llm_provider_id` | `""` | 指定 AstrBot 内置模型 ID(留空=当前模型;用 `/总结模型` 查看可用 ID) |
| `enable_auto_push` | `false` | 启用定时推送新视频 |
| `auto_push_summary` | `true` | 推送新视频时是否附带 AI 总结 |
| `check_interval_minutes` | `600` | 定时检查间隔 |
| `max_subscriptions` | `20` | 每个会话最大订阅数 |
| `enable_miniapp_detect` | `false` | 自动识别群内 B 站链接 |
| `detect_auto_summary` | `false` | 识别后自动生成总结 |
| `enable_forward_message` | `false` | 总结合并转发(聊天记录形式) |
| `enable_auto_split` | `true` | 长视频自动分图 |
| `max_cards_per_image` | `6` | 每张图最大章节卡片数 |
| `image_width` | `900` | 图片宽度像素，推荐 850~1000 以适配聊天窗口 |
| `user_cooldown_seconds` | `8` | 用户冷却(防刷屏) |
| `trigger_keywords` | (默认列表) | 引用消息触发关键词,逗号分隔 |
| `access_mode` | `blacklist` | `blacklist` / `whitelist` |
| `group_list` | `""` | 黑/白名单群号,逗号分隔 |

下载与转写阶段预算仍受 `processing_timeout` 的单次总结总超时约束。

启用 `enable_link` 且使用 AstrBot 内置 Provider 时,插件会在模型声明支持工具调用后要求其提交结构化章节和整数秒时间戳,并校验时间范围与章节顺序。模型或 Provider 不支持工具调用、或结构化结果校验失败时,会自动回退到原有 Markdown 提示词。`openai_compatible` 自建 Provider 始终沿用原有 Markdown 路径,不会收到工具参数。

完整配置请见 [_conf_schema.json](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/_conf_schema.json)。

---

## 🧪 开发与测试

```bash
git clone https://github.com/Miraco33/astrbot_plugin_biliVideo
cd astrbot_plugin_biliVideo

python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio ruff mypy

python scripts/check_publication.py
python -m pytest -q
ruff check .
mypy bilivideo
```

测试覆盖了所有纯逻辑:URL 解析、分页、智能截断、消息路由、订阅持久化、
冷却追踪、LRU 缓存、in-flight 去重、访问控制、配置校验等。

---

## 📁 项目结构

```
bilivideo/
├── core/          # 配置、常量、错误、类型、日志
├── api/           # Bilibili HTTP 客户端 / WBI / endpoints
├── auth/          # Cookie 持久化 / QR 登录
├── parsing/       # URL/BV 提取、消息路由、触发关键词
├── transcription/ # 字幕→ASR pipeline / 必剪 BCut
├── downloader/    # yt-dlp 包装
├── llm/           # Provider 抽象 + AstrBot/OpenAI 实现 + Prompt
├── summarize/     # 总结编排 + 后处理
├── render/        # Markdown→PNG (主题、模板、分页)
├── messaging/     # 合并转发、文本切分、视频信息渲染
├── subscription/  # 原子 JSON 存储 / 管理器 / 调度器
├── access/        # 黑白名单 / 冷却 / in-flight dedup
├── cache/         # LRU + TTL 缓存
├── handlers/      # 命令处理器(每个命令一个文件)
├── tools/         # AI function-call 注册
├── search.py      # 批次搜索/转写服务
└── services.py    # 服务容器(composition root)
```

更多请阅 [CONTRIBUTING.md](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/CONTRIBUTING.md)。

---

## ⚠️ 注意事项

- 首次使用必须先 `/B站登录`。
- 视频总结约需 1-3 分钟(有字幕时更快)。
- 图片渲染失败会自动回退为纯文本。
- 引用消息时只识别用户实际发送的内容,引用部分仅在命中触发关键词时被提取。
- 合并转发需要平台支持(部分平台可能不支持,会自动降级)。

---

## 🔎 致谢

- 总结流程(音频下载、字幕获取、Prompt 构建)的核心思路参考自
  [BiliNote](https://github.com/JefferyHcool/BiliNote)。
- 旧版 v1.x 的实现由 [@storyAura](https://github.com/storyAura) 维护;
  v2.0 重构在保留所有功能的前提下完成。
- 引用消息硬拦截的 PR 灵感来自 [@Jeric-X](https://github.com/Jeric-X)。
- 引用消息中 QQ 小程序卡片解析由 [@pheasantgogogo](https://github.com/pheasantgogogo) 贡献(#24)。
- Markdown/LaTeX 图片渲染与结构化时间戳由 [@Eco404](https://github.com/Eco404) 贡献(#27)。

---

## 📄 License

代码：[MIT](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/LICENSE) © 2025 storyAura，并保留 BiliNote / Jeffery Huang 归属。字体分别遵循 SIL OFL，见 [Noto Sans SC](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/fonts/NotoSansSC-LICENSE.txt) 与 [JetBrains Mono](https://github.com/Miraco33/astrbot_plugin_biliVideo/blob/main/fonts/JetBrainsMono-LICENSE.txt)。
