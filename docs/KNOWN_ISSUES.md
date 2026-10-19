# 已知问题与验证状态

核对日期：2026-10-19。此文记录脱敏故障事实和上游源码风险，不包含生产原始日志或真实会话。

## 近期总结失败

证据来源：2026-10-19 通过已登录的真实 Chrome，只读检查 AstrBot Dashboard 和现有容器日志。
以下时间为 Asia/Shanghai，视频、会话、任务标识及接口完整查询参数均省略。

| 时间 | 已观察证据 | 可得结论与限制 |
|---|---|---|
| 2026-10-12 21:41 | 11.86 MiB 音频已下载；必剪结果查询返回 HTTP 412 | ASR 结果查询失败，尚未进入模型总结；412 的触发条件未核实，不能推断账号被封 |
| 2026-10-17 21:39 | 源音频约 112.11 MiB，仅收到 655360 字节；连接结束时报仍有数据未读 | 音频下载中断，随后总结失败；未完成同视频重试验证 |
| 2026-10-12 至 2026-10-15 | 多次视频信息请求返回 Bilibili code=-400 | 自动识别元数据请求失败；缺少对应输入，未定根因 |
| 2026-10-14 | 模型工具选择请求被拒，插件随后回退 Markdown | 该记录本身不证明整次总结失败 |

当次 Dashboard 显示 AstrBot 4.28.2、插件 2.1.0。未通过 SSH 复核镜像 ID、云端代码哈希、依赖版本及订阅游标。
文中日志事实不等同于 fork 已部署或修复有效。

## 已确认的上游源码风险

基线：storyAura/astrbot_plugin_biliVideo@7cfb5ef0c365e35949e8afd5ff0eca7a513924cb。

1. 下载器未显式设置有限下载/分片/提取重试；网络中断会进入失败路径。
2. 必剪使用普通 requests.Session，各阶段单次请求异常会终止处理；错误记录可能带完整 URL。
3. scheduled_push 将总结异常转换为错误提示；发送后仍可能推进 last_bvid，造成该视频不再自动补总结。
4. scheduled_push 未检查 send_message 的布尔返回值；无异常不等于发送成功。
5. 音频质量选项用于下载后的转码，源下载仍选择 bestaudio；降低该选项不能直接降低源文件下载量。
6. 现有 TaggedLogger 没有统一脱敏；原始异常和目标标识不得直接用于公开诊断。

这是代码静态风险，不证明当次云端游标已经推进，也不证明仓库已经发生凭据泄露。

源码：
- [下载器](https://github.com/storyAura/astrbot_plugin_biliVideo/blob/7cfb5ef0c365e35949e8afd5ff0eca7a513924cb/bilivideo/downloader/ytdlp_downloader.py)
- [必剪](https://github.com/storyAura/astrbot_plugin_biliVideo/blob/7cfb5ef0c365e35949e8afd5ff0eca7a513924cb/bilivideo/transcription/bcut_provider.py)
- [订阅推送](https://github.com/storyAura/astrbot_plugin_biliVideo/blob/7cfb5ef0c365e35949e8afd5ff0eca7a513924cb/bilivideo/handlers/scheduled_push.py)
- [上游问题 #29](https://github.com/storyAura/astrbot_plugin_biliVideo/issues/29)
- [结果未回主对话的独立问题 #31](https://github.com/storyAura/astrbot_plugin_biliVideo/issues/31)

## fork 初始化与生产切源状态

2026-10-19 已建立自用 fork，维护版本 v2.1.1 已进入默认分支。
代码基线：[4d492df](https://github.com/Miraco33/astrbot_plugin_biliVideo/commit/4d492df60dd35a911ec30365cc5664ee6adeae45)。
[远端验证](https://github.com/Miraco33/astrbot_plugin_biliVideo/actions/runs/39207755179)：
Python 3.10、3.11、3.12 的 pytest，Ruff 和 publication-check 均成功。
requirements 与上游基线逐字相同；本轮没有部署业务可靠性修复。

只读查看实际 Dashboard 来源绑定窗口：当前插件仍为 v2.1.0，
选中“直接使用仓库地址”，安装目的地仍是上游仓库，不是本 fork。
尚未执行生产覆盖安装或更换来源。

切源需使用正式同名覆盖安装。该路径在依赖缺失、版本不满足或预检失败时可能调用安装器。
当前无法只读运行容器依赖预检，而部署环境要求依赖安装/升级单独授权，因此生产切源待该授权后执行。
待切源后复核来源持久化记录、插件身份、配置/订阅保留、加载日志与实际收发，再更新本节。

## 后续修复与验收

- [ ] 完成生产安装来源切换与依赖/配置/收发验证。

- [ ] 下载有限重试、续传、取消及必要时重新获取媒体地址。
- [ ] 必剪阶段化脱敏错误、有限恢复；HTTP 412 单独分类，不无限重试。
- [ ] 总结成功、失败通知、目标投递和重试时间分开持久化。
- [ ] 检查各目标发送返回值，仅按明确成功推进对应状态。
- [ ] 可配置备用 ASR；外部服务、成本和音频传输范围需先明确。
- [ ] 长视频源音轨选择和处理预算。
- [ ] 复核 code=-400 的真实输入，不根据错误码猜测原因。
- [ ] 需要主对话读取视频时，单独处理工具结果回传回归。
- [ ] 复核继承字体二进制来源及历史图片。

每项修复需有模拟失败场景测试；部署后再核验真实下载/转写/生成/发送/接收及持久化状态。
不通过改订阅游标、数据库或凭据验证猜测。完成事项更新 CHANGELOG 并删除相应待办。
