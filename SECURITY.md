# 安全政策

## 受支持的版本

| 版本 | 是否支持 |
| --- | --- |
| v4.0.0（源码） | ✅ |
| 更早的内部构建与测试版 | ❌ 已下线，不再维护 |

这是个人维护的非商业项目，修复以尽力而为为原则，不承诺固定响应时间或 SLA。

## 如何报告漏洞

**不要开公开 Issue。** 请使用 [GitHub 私有漏洞报告](https://github.com/wlan2979/locomotive-qq-bot/security/advisories/new)（Security → Advisories → Report a vulnerability）。如果入口不可用，请先开一个不含任何细节的 Issue 询问联系方式，由维护者提供私密渠道。

报告请包含：

- 受影响的提交 / 版本、Windows 版本、Python 版本、WebView2 情况。
- 复现步骤与最小 PoC。
- 影响面判断（能否读到密钥 / 登录态 / 聊天记录，能否远程触发）。

**提交前必须脱敏**：删掉 API Key、QQ 账号与密码、登录令牌、二维码截图、`window.json` 内容（其中的 URL 含会话凭据）、聊天正文，以及 `%LOCALAPPDATA%\TaoTaoBot` 目录下的任何文件。

## 已知的安全边界（设计如此，通常不算漏洞）

如实列出，方便使用者自行判断风险：

- **需要扫码登录一个真实 QQ 账号**，底层是第三方协议端，存在账号风控风险。建议用小号试用。
- **当前源码仍含自动下载并启动 NapCat 组件的遗留逻辑**（`src/app.py` 的 `prepare_runtime()` / `setup()` / `launch_qq()`），下一版将移除。下载物会做大小与 SHA-256 校验，但供应链信任仍需用户自行判断。插件缺失时还可能自动下载安装 ComfyUI / VoiceStudio 安装包。
- **本机 HTTP 服务只监听回环地址**，界面请求带会话标识并校验来源。若发现可绕过该限制的路径，请按上面的渠道报告。
- **API Key 由 Windows DPAPI 加密**，绑定当前 Windows 账户。这是本机静态保护，不是跨机器传输加密，也不防御同账户下的恶意进程。
- 停止聊天引擎走 `engine-stop` 优雅退出，不应强杀用户其他 QQ 或资源软件。
- 源码已公开可读，但**尚未授予开源许可**，见 [LICENSE.md](LICENSE.md)。

## 不在本项目受理范围

- NapCatQQ、ComfyUI、VoiceStudio 或模型供应商自身的漏洞 —— 请报告给对应上游。
- 因使用第三方协议端导致的 QQ 账号限制或封禁 —— 这是使用风险，见 README「安全与隐私」。
- 模型 API 的额度消耗与计费争议 —— 请与供应商联系。
