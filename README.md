# Locomotive · 机器人工作台

Windows 桌面应用，用于创建、配置和连接 QQ 机器人。自己填写机器人的名字、人设和模型供应商，通过手机 QQ 扫码登录即可使用。

> **v4.0.0 · 本仓库第一个公开版本 · 应用源码已公开 · 暂无二进制下载**

[更新日志](CHANGELOG.md) · [许可状态](LICENSE.md) · [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

---

> ⚠️ **源码可读 ≠ 已开源授权。** 源码已公开，但**尚未授予正式开源许可证**，详见 [LICENSE.md](LICENSE.md)。
> ⚠️ **非商业项目**：不收费、无广告与赞助、不用于为任何收费项目引流获客。底层 NapCat 的许可第 3 条明写「不得用于任何商业目的」。

## 当前状态

| 项 | 状态 |
| --- | --- |
| 应用源码 | ✅ 已公开（`src/`，40 个文件） |
| 二进制安装包 / Release | ❌ **无**。此前的测试版已全部下线，不再提供下载 |
| 版本号 | `4.0.0`（`src/app.py` 的 `VERSION`） |
| 仓库历史 | 已重置为**单一初始提交**，v4.0.0 是唯一版本起点 |
| 全新环境验证 | ❌ 未做。`requirements.txt` 取自原构建环境，最小可用集合未验证 |
| 正式开源许可证 | ❌ 未授予 |
| 协议层解耦 | ⏳ **未完成**，见下 |

**别找下载链接了**：这一版只有源码。想用就从源码运行或自行打包，见下方。

## 重要：当前源码仍含遗留的 NapCat 集成

`src/app.py` 中的 `prepare_runtime()` / `setup()` / `launch_qq()` 仍会在首次连接时**自动下载、解压、配置并启动 NapCat 运行组件**。这与项目既定的「通用 OneBot 11 客户端」方向不符，是下一版要移除的部分。

因此：

- 别把当前源码当成「已解耦」的版本使用或再分发。
- NapCat 的使用仍受其上游 [Limited Redistribution License](https://github.com/NapNeko/NapCatQQ/blob/v4.18.42/LICENSE) 约束，**禁止任何商业用途**。许可全文已附在 `src/THIRD_PARTY_NOTICES.txt`。
- 协议层解耦方案见[路线图](#路线图)。

## 从源码运行

要求：Windows 10 / 11 64 位、Python 3.13.x、Microsoft Edge WebView2 Runtime。

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
cd src
python app.py
```

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
- **首次连接会联网下载并执行外部组件**；缺失插件时还可能自动下载安装。请自行判断网络环境是否可信。这部分正是下一版要移除的。
- 本机 HTTP 服务只监听回环地址，界面请求带会话标识并校验来源；`window.json` 中的 URL 含会话凭据，不要外贴。
- 漏洞报告走 [SECURITY.md](SECURITY.md) 的私有渠道，别开公开 Issue 贴凭据。

## 路线图

- [x] 核对各上游许可证原文（2026-11-10，结论见 [LICENSE.md](LICENSE.md)）
- [x] 公开应用源码，仓库历史重置为 v4.0.0 单一初始提交
- [x] 补齐 NapCat 许可全文、LGPL 依赖源码说明，修正第三方声明措辞
- [ ] **协议层解耦**：移除 NapCat 的自动下载 / 配置 / 启动，改为通用 OneBot 11 客户端，用户自备兼容协议端并填写 WebSocket 地址与 Token
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
