# 许可状态说明 / Licensing Status

> ⚠️ **这不是一份开源许可证。** v4.0.0 起应用源码已公开可读，但**尚未授予正式开源许可**。
> 本文件是事实核对与风险归类，**不是法律意见**；权利边界的最终判断以各权利人书面答复为准。

## 结论速览

2026-11-10 已逐个核对上游许可证原文，结论：

- ⛔ **本软件不得用于任何商业目的。** 这是 [NapCat Limited Redistribution License](https://github.com/NapNeko/NapCatQQ/blob/v4.18.42/LICENSE) 第 3 条的硬性约束，源码公开也改变不了。
- ✅ **应用源码的公开不受 GPL / AGPL 阻碍**：ComfyUI、VoiceStudio 以独立进程运行，PyInstaller 有 Bootloader Exception；这些许可既不要求也不阻止本项目公开自己写的代码。
- ⚠️ **核对不等于上游书面授权**。NapCat 许可文本第 1 条与第 2 条互相矛盾，「基于 NapCat 代码开发」的边界也未定义。
- 📌 **本项目定位为非商业项目**：不收费、不含广告与赞助入口、不用于为任何收费产品或服务引流获客。软件内不提供协议端的下载链接、安装向导或内嵌跳转，也不内置任何商业推广。

## 应用代码

自 v4.0.0 起，应用源码位于 `src/`，**可供阅读、为本机试用而运行与构建**。但：

- 仓库**没有** `LICENSE` 文件，**未授予**任何开源许可。著作权默认保留给各自权利人。
- 「源码公开」只代表可读、可自建自用，**不代表**可以再分发、商用、或替本项目指定许可证。
- 正式 `LICENSE` 将在补完作者声明、裁剪历史遗留模块后选定。

当前可以做的：

- 阅读源码、从源码在本机运行与打包，用于**个人非商业**试用与评估。
- 提交 Issue 与 PR（见 [CONTRIBUTING.md](CONTRIBUTING.md)）。
- 引用本仓库文档，注明出处。

当前**未获授权**的事：

- **任何商业使用**：收费、广告、内购、赞助、企业部署、整合进商业产品，或**为任何收费项目／服务引流、获客、导流**。免费不等于非商业使用。
- 再分发本项目的源码或二进制；把 NapCat / ComfyUI / VoiceStudio 打包进发布物。
- 移除或篡改上游许可与版权声明；反编译、逆向工程（适用法律另有强制规定的除外）。
- 声称本项目是开源软件，或替本项目指定某个许可证。

**作者声明尚未补齐**：本地证据未发现复制第三方代码的痕迹（无版权头、无 fork 声明、无 vendored 目录），但原项目没有 git 历史，也没有书面权属声明。这是正式许可证落地前必须补的一步。

## 上游组件许可（已核对一手文本）

| 组件 / 版本 | 许可 | 与本项目关系 | 约束 |
| --- | --- | --- | --- |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) v4.18.42 | **Limited Redistribution License**（自定义，非 OSI）<br>Copyright © 2024 Mlikiowa | **已解耦**：不下载、不安装、不启动，也不内嵌；由用户自备 | 禁商用；禁未获授权「基于 NapCat 代码开发」；再分发须附许可全文与来源版权 |
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) v0.44.0 | **GPL-3.0** | 运行时下载官方便携包或复用本机安装，独立进程 + 本机 HTTP API | 不打包、不修改、不链接其代码 → 不构成衍生作品，不要求 Locomotive 源码开源 |
| [VoiceStudio](https://github.com/debpalash/VoiceStudio) 0.5.7 | **AGPL-3.0** | 运行时下载官方安装包或复用本机安装，独立进程 | 本地桌面单用户不触发 AGPL §13；改成对外网络服务会触发源码提供义务 |
| [workflow_templates](https://github.com/Comfy-Org/workflow_templates) | **MIT** | 仅参考 Z-Image 模板参数，代码自行实现并注明来源 | 无额外义务 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) 6.22.3 | **GPLv2-or-later + Bootloader Exception** | 打包工具 | 例外条款明确允许嵌入闭源程序并分发，**不传染**；runtime hooks 为 Apache-2.0 |
| py7zr / pyppmd / pybcj / inflate64 / multivolumefile | **LGPL-2.1-or-later** | 打包进单文件 EXE | 已按 LGPL §6 在 `src/THIRD_PARTY_NOTICES.txt` 列出精确版本与源码获取方式 |
| aiohttp / Pillow / pywebview / pythonnet / qrcode / Python 运行时 | Apache-2.0 / HPND / BSD-3-Clause / MIT / BSD / PSF | 打包进 EXE | 各许可全文见 `src/THIRD_PARTY_NOTICES.txt` |

## 与协议端的关系（v4.0.0 起已解耦）

自 v4.0.0 起，本软件是**通用 OneBot 11 客户端**：不下载、不安装、不配置、不启动 NapCat 或任何其他协议实现，也不内嵌其二进制。用户自行准备并登录兼容协议端，只把 WebSocket 地址与访问令牌填入本工作台。

早前的内部构建曾自动下载 NapCat 官方原版运行组件，该逻辑已从源码中移除。这使本项目与 NapCat 的集成关系断开，落到上游在 [#2017](https://github.com/NapNeko/NapCatQQ/issues/2017) 认可的形态（不捆绑、只走 OneBot 11 协议）。

**任何把 NapCat / ComfyUI / VoiceStudio 打包进发布物、或分发其修改版的做法，都会改变上述合规结论。**

## 已履行的告知义务

v4.0.0 的 `src/THIRD_PARTY_NOTICES.txt` 已补齐：

1. NapCat `Limited Redistribution License` **全文**与 `Copyright © 2024 Mlikiowa`。
2. LGPL-2.1+ 五个库的上游地址、精确版本与源码获取／替换说明（LGPL §6）。
3. 去掉与公开发布矛盾的 `This private-use build` 措辞，写明非商用约束与遗留逻辑的移除计划。

## 教程与文档边界

为保证「非商业」与「不集成」两条都成立：

- **可以写**（放 README / 独立文档页，不内置进软件界面）：OneBot 11 是什么、正向 WebSocket 地址与 Token 怎么填、通用排错；链接指向 OneBot 11 标准或中立文档。
- **不写**：点名某个协议端并附具体下载链接、图文安装步骤、一键安装入口。
- **软件界面内不出现**：收费项目、课程、社群、赞助链接、广告，或任何指向下载页的按钮／内嵌跳转。

依据：NapCat 上游在 [#2147](https://github.com/NapNeko/NapCatQQ/issues/2147) 对「本地免费 + 云端收费」明确答复「任何商业用途都不允许」，且未认可「改成引导用户自行下载安装」；[#2017](https://github.com/NapNeko/NapCatQQ/issues/2017) 则对「不捆绑、只走 OneBot 11 协议」回了「ok」。

## 本仓库文档

`README.md`、`CHANGELOG.md`、`SECURITY.md`、`CONTRIBUTING.md` 及 Issue / PR 模板，允许为反馈、引用与协作目的复制和分享。文档尚未单独指定正式许可，会与源码许可一并明确。

## 反馈许可相关线索

如果你掌握原始机器人代码的上游出处、NapCat 主作者的授权渠道，或某个依赖更准确的许可信息，欢迎开 Issue 提供线索。请勿在 Issue 中粘贴他人未授权的代码。
