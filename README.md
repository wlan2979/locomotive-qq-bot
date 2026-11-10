# Locomotive · 机器人工作台

Windows 桌面应用，用于创建、配置和连接 QQ 机器人。自己填写机器人的名字、人设和模型供应商，通过手机 QQ 扫码登录即可使用。

> **v4.0.0 · 本仓库第一个公开版本 · 源码与 Windows 二进制均已发布**

[更新日志](CHANGELOG.md) · [许可状态](LICENSE.md) · [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

---

> ⚠️ **源码可读 ≠ 已开源授权。** 源码已公开，但**尚未授予正式开源许可证**，详见 [LICENSE.md](LICENSE.md)。
> ⚠️ **非商业项目**：不收费、无广告与赞助、不用于为任何收费项目引流获客。底层 NapCat 的许可第 3 条明写「不得用于任何商业目的」。

## 当前状态

| 项 | 状态 |
| --- | --- |
| 应用源码 | ✅ 已公开（`src/`，40 个文件） |
| 二进制安装包 | ✅ [Locomotive-v4.0.0-Windows-x64.zip](https://github.com/wlan2979/locomotive-qq-bot/releases/download/v4.0.0/Locomotive-v4.0.0-Windows-x64.zip)（22,612,045 字节） |
| 版本号 | `4.0.0`（`src/app.py` 的 `VERSION`） |
| 仓库历史 | 已重置为**单一初始提交**，v4.0.0 是唯一版本起点 |
| 全新环境验证 | ❌ 未做。`requirements.txt` 取自原构建环境，最小可用集合未验证 |
| 正式开源许可证 | ❌ 未授予 |
| 协议层解耦 | ✅ 已完成（标准 OneBot 11 客户端） |

## 下载

**[Locomotive-v4.0.0-Windows-x64.zip](https://github.com/wlan2979/locomotive-qq-bot/releases/download/v4.0.0/Locomotive-v4.0.0-Windows-x64.zip)**（22,612,045 字节）

```text
SHA-256  92e2da471fda3b6658be3fb112331798bcb74e161050c0824d38b664f45509bc
```

解压后得到 `Locomotive/` 目录，含 `Locomotive.exe`（22,801,580 字节）、`Locomotive-使用说明.txt`、`THIRD_PARTY_NOTICES.txt`。

运行前需要 Microsoft Edge WebView2 运行时，并自备一个已登录的 OneBot 11 兼容协议端。

## 已解耦：不下载、不安装、不启动任何协议端

v4.0.0 起，Locomotive 是标准的 **OneBot 11 正向 WebSocket 客户端**。它不下载、不安装、不配置、不启动 NapCat 或任何其他协议实现。

你需要自己准备一个兼容 OneBot 11 的协议端，登录你的 QQ 账号，把它的 WebSocket 地址与访问令牌填进本工作台。

- **移除的代码**：`prepare_runtime()`（自动下载 124MB 组件）、`launch_qq()`（启动 `node.exe`）、`class Napcat`（WebUI 控制）、二维码轮询与生成、端口与令牌自动分配、NapCat 插件卡片。
- **新增**：「OneBot 11 连接」设置区，填写 `ws://...` 与可选访问令牌；`save_onebot()` 只写入本机设置，令牌不回传到界面。
- **自检**：新增「`setup()` 不触碰任何第三方配置目录」与「OneBot 地址校验」用例。

上游 NapCat 的 [Limited Redistribution License](https://github.com/NapNeko/NapCatQQ/blob/v4.18.42/LICENSE) 仍然约束**你自己**对它的使用，包括**禁止任何商业用途**。许可全文在 `src/THIRD_PARTY_NOTICES.txt`。

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

## 测试

```powershell
cd src
python -m unittest discover -p "test_*.py" -q
```

另附 `test_frozen_engine.py`（模拟 QQ / 模型服务的收发与停止）、`desktop_check.py`（原生窗口）。运行前确认没有真实机器人占用引擎端口。**旧版本留下的测试报告不能作为 v4.0.0 的验证证据。**

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
| `src/THIRD_PARTY_NOTICES.txt` | 第三方许可全文、NapCat 许可、LGPL 源码获取说明 |
| `requirements.txt` | 运行与打包依赖 |

## 历史遗留模块（不是 v4 功能）

`src/bot/` 中的 `douyin_*`、`phone.py`、`hands.py`、`seedance.py`、`sticker*`、`taotao_video.py`、`inspiration.py` 等来自早期「桃桃」机器人项目，与 Locomotive 工作台无关。`bot/core.py` 是引擎核心且与这些模块互相引用，为保证源码可运行暂未裁剪，后续会单独处理。

## 安全与隐私

- **需要扫码登录一个真实 QQ 账号**，底层是第三方协议端，存在账号风控风险。用小号、独立的低额度 API Key。
- **软件本身不再下载任何协议端**；ComfyUI / VoiceStudio 插件缺失时仍会自动下载安装，请自行判断网络环境是否可信。
- 本机 HTTP 服务只监听回环地址，界面请求带会话标识并校验来源；`window.json` 中的 URL 含会话凭据，不要外贴。
- 漏洞报告走 [SECURITY.md](SECURITY.md) 的私有渠道，别开公开 Issue 贴凭据。

## 路线图

- [x] 核对各上游许可证原文（2026-11-10，结论见 [LICENSE.md](LICENSE.md)）
- [x] 公开应用源码，仓库历史重置为 v4.0.0 单一初始提交
- [x] 补齐 NapCat 许可全文、LGPL 依赖源码说明，修正第三方声明措辞
- [x] **协议层解耦**：移除 NapCat 的自动下载 / 配置 / 启动，改为通用 OneBot 11 客户端，用户自备兼容协议端并填写 WebSocket 地址与 Token
- [ ] 裁剪 `bot/` 中的历史遗留模块
- [ ] 全新环境端到端验证，补 WebView2 缺失提示
- [ ] 打包并发布第一个可用二进制版本，重新生成校验值
- [ ] 补作者声明，确定正式开源许可证

## 反馈与贡献

源码已公开，欢迎提 Issue 和 PR，见 [CONTRIBUTING.md](CONTRIBUTING.md)。报问题请附 Windows 版本、Python 版本、WebView2 情况和复现步骤，**先删掉密钥、QQ 号、登录令牌和聊天正文**。

## 许可

**尚未授予应用代码的开源许可**。源码可阅读、可为本机试用而运行与构建，但在正式 `LICENSE` 发布前，请勿再分发、商用或替本项目指定许可证。**本项目为非商业项目**：不收费、不含广告与赞助、不用于为任何收费项目引流获客；软件内不提供协议端下载链接、安装向导或内嵌跳转。

完整结论、上游组件清单与非商业定位见 [LICENSE.md](LICENSE.md)。

上游项目：[NapCatQQ](https://github.com/NapNeko/NapCatQQ) · [ComfyUI](https://github.com/Comfy-Org/ComfyUI) · [VoiceStudio](https://github.com/debpalash/VoiceStudio)
