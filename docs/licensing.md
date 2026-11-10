# 许可说明

本项目的代码采用 **PolyForm Noncommercial License 1.0.0**，全文见仓库根目录的 [LICENSE](../LICENSE)。

## 一句话

**除商业用途外，其他都允许。**

## 明确允许

- 阅读、运行、修改源码
- 自己用，给朋友用
- 分发副本 —— 需附带 LICENSE 全文或它的 URL，以及 `Required Notice` 行
- 分发你修改后的版本 —— 同样受非商业限制约束
- fork 出去自己维护
- 个人学习、研究、实验、爱好项目
- 慈善机构、教育机构、公共研究机构、公共安全或卫生机构、环保组织、政府机构使用（不论资金来源）

## 明确禁止

- **任何商业用途**：收费、付费服务、广告变现、内购、集成进商业产品，或拿它给自己的商业项目引流获客
- 移除或篡改 `Required Notice` 行
- 再授权（sublicense）或转让你的许可

## 与「非商业项目」定位的关系

两件独立的事：

- **许可证**管的是**你**怎么用这份代码 —— PolyForm 禁止商业用途。
- **项目定位**是**作者自己**的选择 —— 不收费、无广告、不引流。

作者不靠这个项目赚钱，也不阻止你在非商业范围内随便用。

## 第三方组件

打包进 EXE 的组件适用各自许可，不受 PolyForm 约束：

| 组件 / 版本 | 许可 | 与本项目关系 |
| --- | --- | --- |
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) v0.44.0 | **GPL-3.0** | 运行时下载官方便携包或复用本机安装，独立进程 + 本机 HTTP API；不打包、不修改、不链接 |
| [VoiceStudio](https://github.com/debpalash/VoiceStudio) 0.5.7 | **AGPL-3.0** | 运行时下载官方安装包或复用本机安装，独立进程；本地桌面单用户不触发 §13 |
| [workflow_templates](https://github.com/Comfy-Org/workflow_templates) | **MIT** | 仅参考 Z-Image 模板参数，代码自行实现并注明来源 |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) 6.22.3 | **GPLv2-or-later + Bootloader Exception** | 打包工具；例外条款明确允许嵌入闭源程序分发 |
| py7zr / pyppmd / pybcj / inflate64 / multivolumefile | **LGPL-2.1-or-later** | 打包进单文件 EXE；已在 `src/THIRD_PARTY_NOTICES.txt` 列出精确版本与源码获取方式 |
| aiohttp / Pillow / pywebview / pythonnet / Python 运行时 | Apache-2.0 / HPND / BSD-3-Clause / MIT / PSF | 打包进 EXE；许可全文见 `src/THIRD_PARTY_NOTICES.txt` |

`src/THIRD_PARTY_NOTICES.txt` 里还保留了历史版本曾集成过的组件的许可全文，用于履行已发布版本的告知义务；这不代表当前版本包含该组件。

## 与协议端的关系

自 v4.0.0 起，本软件是**通用 OneBot 11 客户端**：不下载、不安装、不配置、不启动任何协议端实现，也不内嵌其二进制。用户自行准备并登录兼容协议端，只把 WebSocket 地址与访问令牌填入本工作台。

你自备的协议端适用其**自身**许可，可能包含非商用或其他限制，与本仓库无关。

## 历史

- v4.0.0 – v4.1.1：仓库只有「许可状态说明」，没有许可证。源码仅授权阅读、本机试用与自建自用。
- **v4.1.2 起**：正式采用 PolyForm Noncommercial 1.0.0。此前版本的使用者按当时的声明执行。

## 这不是法律意见

以上是对许可文本的通俗说明，不是法律意见。以 [LICENSE](../LICENSE) 全文为准。涉及商业使用或授权范围有疑问时，请咨询律师。
