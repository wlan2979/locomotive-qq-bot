# Locomotive · 机器人工作台

Windows 桌面应用，用于创建、配置和连接 QQ 机器人。自己填写机器人的名字、人设和模型供应商。

本软件是**通用 OneBot 11 客户端**：不做 QQ 登录、不显示二维码，也不下载或启动任何协议端。你需要自备一个已登录的 OneBot 11 兼容协议端，把它的 WebSocket 地址与访问令牌填进来。

> **v4.1.2 · 源码与 Windows 二进制均已发布**

[更新日志](CHANGELOG.md) · [许可证](LICENSE) · [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

---

> ✅ **许可证已明确**：采用 [PolyForm Noncommercial 1.0.0](LICENSE) —— **除商业用途外，其他都允许**。通俗说明见 [docs/licensing.md](docs/licensing.md)。
> ⚠️ **非商业项目**：不收费、无广告与赞助、不用于为任何收费项目引流获客。这是本项目的主动定位。你自备的协议端可能另有其自身的非商用限制，使用前请自行确认。

## 当前状态

| 项 | 状态 |
| --- | --- |
| 应用源码 | ✅ 已公开（`src/`，30 个源码文件，不含构建产物） |
| 二进制安装包 | ✅ [Locomotive-v4.1.2-Windows-x64.zip](https://github.com/wlan2979/locomotive-qq-bot/releases/download/v4.1.2/Locomotive-v4.1.2-Windows-x64.zip)（22,439,899 字节） |
| 版本号 | `4.1.2`（`src/app.py` 的 `VERSION`） |
| 仓库历史 | 已重置为**单一初始提交**，v4.0.0 是唯一版本起点 |
| 全新环境验证 | ❌ 未做。`requirements.txt` 取自原构建环境，最小可用集合未验证 |
| 许可证 | ✅ PolyForm Noncommercial 1.0.0（除商业用途外均允许） |
| 协议层解耦 | ✅ 已完成（标准 OneBot 11 客户端） |
| 真实链路验证 | ⚠️ 单次冒烟通过（2026-11-10，本机 OneBot 11 协议端 + 真实 QQ 群 + DeepSeek），未覆盖多轮/图片/重连 |

## 下载

**[Locomotive-v4.1.2-Windows-x64.zip](https://github.com/wlan2979/locomotive-qq-bot/releases/download/v4.1.2/Locomotive-v4.1.2-Windows-x64.zip)**（22,439,899 字节）

```text
SHA-256  9e6d683a80339658e5384ddffbc7d4de7e2b1d09e7800137e2d4c3476c5c978a
```

解压后得到 `Locomotive/` 目录，含 `Locomotive.exe`（22,628,241 字节）、`Locomotive-使用说明.txt`、`THIRD_PARTY_NOTICES.txt`、`LICENSE`。

运行前需要 Microsoft Edge WebView2 运行时，并自备一个已登录的 OneBot 11 兼容协议端。

## 已解耦：不下载、不安装、不启动任何协议端

v4.0.0 起，Locomotive 是标准的 **OneBot 11 正向 WebSocket 客户端**。它不下载、不安装、不配置、不启动任何协议端实现。

> **边界**：这里说的「不下载」只针对**协议端**。ComfyUI / VoiceStudio 插件在缺失时仍会自动下载安装（见下方「插件与资源」）。

你需要自己准备一个兼容 OneBot 11 的协议端，登录你的 QQ 账号，把它的 WebSocket 地址与访问令牌填进本工作台。

- **移除的能力**：内置协议端的自动下载、解压、配置与启动，二维码登录流程，端口与访问令牌自动分配，以及对应的插件卡片。
- **新增**：「OneBot 11 连接」设置区，填写 `ws://...` 与可选访问令牌；`save_onebot()` 只写入本机设置，令牌不回传到界面。
- **自检**：新增「`setup()` 不触碰任何第三方配置目录」与「OneBot 地址校验」用例。

你自备的协议端适用其**自身**许可，可能包含非商用或其他限制，请在使用前自行确认。`src/THIRD_PARTY_NOTICES.txt` 里附了本软件实际打包的第三方组件声明。

## 从源码运行

要求：Windows 10 / 11 64 位、Python 3.13.x、Microsoft Edge WebView2 Runtime。

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd src
python app.py
```

- 先自行准备并登录一个 OneBot 11 兼容协议端，在「模型」页底部填写它的 WebSocket 地址与访问令牌。
- 模型 API Key 在「模型」页填写，由 Windows 当前账户 DPAPI 加密保存在本机。**不要写进任何文件并提交。**
- 应用数据在 `%LOCALAPPDATA%\TaoTaoBot`，含密钥、QQ 登录状态与聊天记录，**绝不分享或提交**。
- 程序生成的 `config.json`、`window.json`、`data/`、`logs/` 均已在 `.gitignore` 中排除。
- 自备模型 API Key，调用费用由使用者承担；仓库不含任何密钥。

## 打包

```powershell
cd src
python -m PyInstaller --noconfirm Locomotive.spec
```

`Locomotive.spec` 已改为全相对路径，迁移机器无需改动。

## 无头运行（不打开窗口）

桌面程序关窗即退出，这是它的形态。如果你希望它常驻后台，用引擎模式：

```bat
Locomotive.exe --engine --data-dir "%LOCALAPPDATA%\TaoTaoBot"
```

这个模式不创建窗口，只跑 OneBot 连接和聊天引擎；日志写到数据目录的 `bot-process.log`。停止方式：在数据目录下创建一个空文件 `engine-stop`，进程会优雅退出并保存历史。

想开机自启，用「任务计划程序」：触发器设为登录时，操作填 `Locomotive.exe`，参数 `--engine --data-dir "<你的数据目录>"`。注意 DPAPI 密钥绑定当前 Windows 账户，别换账户运行。

**这个模式没有托盘、没有自动重启**。进程崩了不会自己起来，需要任务计划的「失败后重新启动」，或第三方守护工具。

## 测试

```powershell
cd src
python -m unittest discover -p "test_*.py" -q
```

另附 `test_frozen_engine.py`（模拟 QQ / 模型服务的收发与停止）、`desktop_check.py`（原生窗口）。运行前确认没有真实机器人占用引擎端口。**旧版本留下的测试报告不能作为 v4.1.2 的验证证据。**

## 源码结构

| 路径 | 职责 |
| --- | --- |
| `src/app.py` | 主入口、设置、机器人管理、本机 HTTP 接口、原生窗口、子进程编排 |
| `src/index.html` | 机器人 / 模型 / 插件三个页面的前端 |
| `src/resources.py` | ComfyUI、VoiceStudio 的本地识别、下载校验、安装与启动 |
| `src/engine_main.py` | 聊天引擎入口 |
| `src/bot/onebot.py` | 标准 OneBot 11 正向 WebSocket 客户端 |
| `src/bot/llm.py` | 模型请求、供应商兼容与网络代理处理 |
| `src/bot/config.py` | 引擎配置结构（已含 `ws_url` / `access_token`） |
| `src/Locomotive.spec` | PyInstaller 打包配置（相对路径） |
| `src/THIRD_PARTY_NOTICES.txt` | 第三方许可全文、LGPL 依赖源码获取说明 |
| `requirements.txt` | 运行与打包依赖 |

## 已移除的历史遗留模块

早期「桃桃」项目带来的抖音、手机自动化、视频生成等模块已全部移除，共 9 个文件：

- `douyin_commenter.py`、`douyin_publisher.py`、`douyin_scheduler.py`、`douyin_share.py`
- `phone.py`（手机/模拟器自动化）、`hands.py`（真机屏幕操作）
- `seedance.py`（图生视频）、`taotao_video.py`（视频合成）、`inspiration.py`（灵感库）

`bot/core.py` 里对应的工具注册、工具执行分支、定时循环与主动发言逻辑也一并删除；配置里的 `phone` / `hands` / `douyin` / `seedance` 四段全部去掉。`bot/` 从 24 个模块降到 16 个，总行数减少约一半。

保留的只有 QQ 聊天相关能力：OneBot 连接、消息处理、模型调用、记忆、联网搜索、生图、表情包、语音、用量统计、对话摘要。

## 安全与隐私

- **需要扫码登录一个真实 QQ 账号**，底层是第三方协议端，存在账号风控风险。用小号、独立的低额度 API Key。
- **软件本身不再下载任何协议端**；ComfyUI / VoiceStudio 插件缺失时仍会自动下载安装，请自行判断网络环境是否可信。
- 本机 HTTP 服务只监听回环地址，界面请求带会话标识并校验来源；`window.json` 中的 URL 含会话凭据，不要外贴。
- 漏洞报告走 [SECURITY.md](SECURITY.md) 的私有渠道，别开公开 Issue 贴凭据。

## 常见问题

**连接失败 / 一直显示「正在连接 OneBot 服务」？** 见 [OneBot 11 接入指南](docs/ONEBOT-SETUP.md)。最常见的三个原因：协议端没运行、没开启正向 WebSocket、地址或端口填错。

**为什么没有二维码？** 本软件不做 QQ 登录，登录在你自己的协议端里完成。

**支持群聊吗？** 支持。群里需要 @ 机器人才会触发回复。

**能同时连多个机器人吗？** 不能。一次只能连一个，切换或改设置前先停止，且不在近期路线图内。

**数据存在哪？** `%LOCALAPPDATA%\TaoTaoBot`，含加密密钥、聊天记录与设置，**不要分享或提交**。目录名沿用早期版本，改名会导致旧数据丢失，所以保留。

**怎么自己打包？** 见上文「打包」，`src/Locomotive.spec` 是全相对路径的。

## 尚未验证的场景

「单次冒烟通过」只说明一条路径能走通。下面这些**没测过**，遇到问题请开 Issue：

- 多轮上下文与记忆摘要（超过历史窗口后的行为）
- 图片消息理解、群聊 @ 判定、引用回复
- 断线重连、协议端重启后的恢复
- 长回复切分、并发消息处理
- 其他 Windows 版本 / 没有开发环境的干净机器
- 实际绘图（ComfyUI）与语音合成（VoiceStudio）

## 已知取舍

- **形态**：桌面 GUI 单体进程，关窗即停，没有守护与看门狗。这是「看得见的桌面工作台」这个定位带来的代价，改成常驻服务等于重写 UI 层。无头场景请用上面的 `--engine` 模式。
- **数据可迁移性**：人设、记忆、设置存在 `%LOCALAPPDATA%\TaoTaoBot` 的私有结构里，与内部 config schema 绑定，**目前没有导出功能**。想迁到其他机器人框架只能手工重建。
- **平台**：Windows 独占，依赖 WebView2 运行时。启动前会查注册表主动探测，缺失时直接弹窗给出下载地址并退出，不会退化成白屏；但不会自动安装。
- **维护**：单人项目，issue 与第三方验证都很少，遇到问题主要靠读源码。

## 路线图

- [x] 核对各上游许可证原文（2026-11-10，结论见 [docs/licensing.md](docs/licensing.md)）
- [x] 公开应用源码，仓库历史重置为 v4.0.0 单一初始提交
- [x] 补齐第三方许可声明与 LGPL 依赖源码说明，修正第三方声明措辞
- [x] **协议层解耦**：移除内置协议端的自动下载 / 配置 / 启动，改为通用 OneBot 11 客户端，用户自备兼容协议端并填写 WebSocket 地址与 Token
- [x] 裁剪 `bot/` 中的历史遗留模块：抖音、手机自动化、视频生成、灵感库共 9 个文件已移除，`bot/` 降到 16 个模块
- [ ] 全新环境端到端验证，补 WebView2 缺失提示
- [x] 打包并发布二进制版本（v4.1.2，SHA-256 已写入 Release 与 README）
- [ ] 数据导出：把人设、记忆、设置导出为通用格式，降低迁移成本
- [ ] 补作者声明，确定正式开源许可证

## 反馈与贡献

源码已公开，欢迎提 Issue 和 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md)。报问题请附 Windows 版本、Python 版本、WebView2 情况和复现步骤，**先删掉密钥、QQ 号、登录令牌和聊天正文**。

## 许可

本项目采用 **[PolyForm Noncommercial License 1.0.0](LICENSE)**：**除商业用途外，其他都允许**。

- **允许**：阅读、运行、修改、fork、分发（需附许可全文或链接，以及 `Required Notice` 行）、个人学习与研究、非商业组织使用
- **禁止**：任何商业用途（收费、广告变现、集成进商业产品、为商业项目引流）、移除 `Required Notice`、再授权

**本项目是非商业项目**：不收费、不含广告与赞助、不用于为任何收费项目引流获客；软件内不提供协议端下载链接、安装向导或内嵌跳转。

通俗说明、第三方组件清单与历史沿革见 [docs/licensing.md](docs/licensing.md)。

本项目使用的第三方组件：[ComfyUI](https://github.com/Comfy-Org/ComfyUI) · [VoiceStudio](https://github.com/debpalash/VoiceStudio)
