"""Local onboarding UI for a standard OneBot 11 bot."""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from resources import Resources


ROOT = Path(__file__).resolve().parent
VERSION = "4.2.2"
DEFAULT_MODEL = {"provider": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"}
DEFAULT_PLUGINS = {"comfyui": {"enabled": False, "url": "http://127.0.0.1:8188", "checkpoint": ""},
                   "voicestudio": {"enabled": False, "url": "http://127.0.0.1:3900", "voice": "", "model": ""}}
DEFAULT_ONEBOT = {"ws_url": "ws://127.0.0.1:3001", "access_token": ""}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DATA_DIR_NAME = "Locomotive"
LEGACY_DATA_DIR_NAME = "TaoTaoBot"


def default_data_dir() -> Path:
    """默认数据目录。旧版本用的是另一个名字，首次启动时复制过来，旧目录保留不动。

    复制失败就继续用旧目录 —— 宁可名字不好看，也不能让用户丢数据。
    """
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    current = base / DATA_DIR_NAME
    legacy = base / LEGACY_DATA_DIR_NAME
    if not current.exists() and legacy.exists():
        # 先复制到临时目录再改名：中途失败不会留下「半套数据」的 current，
        # 否则下次启动会以为迁移已完成，直接用一个不完整的目录。
        staging = base / (DATA_DIR_NAME + ".migrating")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            shutil.copytree(legacy, staging)
            staging.replace(current)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            return legacy
    return current


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def endpoint(value, local_only=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("接口地址无效。")
    parsed = urllib.parse.urlsplit(value.strip())
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if not parsed.hostname or parsed.scheme not in ("http", "https") or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("请填写完整的 HTTP/HTTPS 接口地址，不要在地址里填写密钥。")
    if parsed.scheme == "http" and not local or local_only and (not local or parsed.path.strip("/")):
        raise ValueError("资源接口请使用本机地址；远程模型接口请使用 HTTPS。")
    parsed.port
    return value.strip().rstrip("/")



RELEASE_FEED = "https://github.com/wlan2979/locomotive-qq-bot/releases.atom"


def _version_tuple(text):
    try:
        return tuple(int(part) for part in text.split("."))
    except (AttributeError, ValueError):
        return ()


def parse_release_version(text):
    """从 releases.atom 里取最新版本号。第一个 title 是 feed 标题，要跳过。"""
    for title in re.findall(r"<title>([^<]+)</title>", text or "")[1:]:
        match = re.search(r"v?(\d+\.\d+\.\d+)", title)
        if match:
            return match.group(1)
    return None


def latest_release_version(timeout=3.0):
    """查 GitHub Releases 的最新版本号；任何失败都返回 None，不影响启动。

    只读取公开的 releases.atom，不发送任何本地数据。默认走系统代理。
    """
    request = urllib.request.Request(RELEASE_FEED, headers={"User-Agent": "Locomotive/" + VERSION})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read(300_000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    return parse_release_version(text)


def onebot_endpoint(value):
    if not isinstance(value, str) or len(value) > 512:
        raise ValueError("OneBot 地址无效。")
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请填写 ws:// 或 wss:// 开头的 OneBot 11 正向 WebSocket 地址。")
    parsed.port
    return value.strip().rstrip("/")

def validate_model_key(model, key):
    class SameOrigin(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            before, after = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(new_url)
            if (before.scheme, before.hostname, before.port) != (after.scheme, after.hostname, after.port):
                raise ValueError("模型接口跳转到了其他地址，请填写供应商提供的最终接口地址。")
            return super().redirect_request(request, fp, code, message, headers, new_url)

    request = urllib.request.Request(endpoint(model["base_url"]) + "/models", headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.build_opener(SameOrigin()).open(request, timeout=20) as response:
            result = json.loads(response.read(1_000_000))
        if not isinstance(result, (dict, list)):
            raise ValueError("模型接口返回了无法识别的内容。")
        return "模型接口连接正常。"
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise ValueError("API Key 验证失败，请检查供应商、地址和密钥。") from None
        if error.code in (404, 405):
            return "接口未提供模型列表，设置已保存；模型可用性将在聊天时验证。"
        raise ValueError("模型接口暂时无法连接，请稍后重试。") from None


def protect(data: bytes, decrypt=False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise OSError("Windows 无法读取或保护保存的密钥，请重新输入 API Key。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)




WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def webview2_version():
    """返回已安装的 WebView2 Runtime 版本；未安装返回 None。

    直接查 EdgeUpdate 的客户端注册项，比等 pywebview 抛异常再猜文案可靠。
    """
    import winreg

    locations = (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
    )
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
        except OSError:
            continue
        if isinstance(version, str) and version and version != "0.0.0.0":
            return version
    return None


def require_webview2():
    """没有 WebView2 就直接说明白，别让它退化成白屏或一句英文异常。"""
    if webview2_version() is not None:
        return
    message = (
        "Locomotive 需要 Microsoft Edge WebView2 运行时才能显示界面，\n"
        "但这台电脑上没有检测到它。\n\n"
        "安装后重新启动即可：\n" + WEBVIEW2_URL
    )
    ctypes.windll.user32.MessageBoxW(None, message, "Locomotive", 0x30 | 0x10000 | 0x40000)
    raise SystemExit(2)


def open_window(url: str, directory: Path):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path != "/" or parsed.username or parsed.query:
        raise ValueError("程序窗口地址无效，请重新启动。")
    import webview
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    return webview.create_window("Locomotive", url, width=1100, height=850, min_size=(800, 650),
                                 background_color="#f5f6f8", text_select=True)


def reopen_window(window: Path, directory: Path):
    if not window.is_file():
        raise RuntimeError("程序启动端口已被占用，但没有找到登录窗口。请退出旧程序后重试。")
    previous = json.loads(window.read_text("utf-8")).get("url", "")
    parsed = urllib.parse.urlsplit(previous)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path != "/" or parsed.username or parsed.query or not parsed.fragment:
        raise ValueError("已保存的窗口地址无效，请退出旧程序后重试。")
    base = urllib.parse.urlunsplit(parsed._replace(fragment=""))
    request = urllib.request.Request(base + "activate", data=b"{}", headers={"X-Session": parsed.fragment, "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=3) as response:
            state = json.loads(response.read(2_000_000))
        if not isinstance(state, dict) or not state.get("ok"):
            raise ValueError()
    except (OSError, ValueError) as error:
        raise RuntimeError("旧窗口已无法连接，请退出旧程序后重新打开。") from error


def show_startup_error(error):
    directory = default_data_dir()
    detail = str(error) or type(error).__name__
    lowered = detail.lower()
    if any(k in lowered for k in ("webview", "edgechromium", "mshtml", "runtime")):
        hint = ("\n\n这通常是缺少 Microsoft Edge WebView2 运行时。\n"
                "安装后重新启动即可：https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    else:
        hint = ("\n\n如果窗口一直没有出现，请先确认 Microsoft Edge WebView2 运行时已安装：\n"
                "https://go.microsoft.com/fwlink/p/?LinkId=2124703")
    message = "Locomotive 未能启动。\n\n" + detail[:600] + hint
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "startup.log").open("a", encoding="utf-8") as output:
            output.write(time.strftime("%Y-%m-%d %H:%M:%S ") + type(error).__name__ + ": " + detail + "\n")
    except OSError:
        pass
    ctypes.windll.user32.MessageBoxW(None, message, "Locomotive", 0x10 | 0x10000 | 0x40000)



def make_config(ws_url: str, token: str, model=None, plugins=None, name="我的机器人"):
    result = {
        "onebot": {"ws_url": ws_url, "access_token": token},
        "deepseek": {"api_key": "", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "thinking": False},
        "bot": {"system_prompt": "@persona.txt", "private_enabled": True, "group_trigger": "at", "group_activate_minutes": 0,
                "owner_ids": [], "enable_search": False, "history_persist": True, "summary_enabled": False},
        "imagegen": {"enable": False, "comfyui_enable": False},
        "sticker_gen": {"enable": False, "generate": False, "prewarm": False},
        "tts": {"enabled": False},
        "log": {"level": "INFO", "dir": "logs"},
    }
    result["deepseek"].update({key: value for key, value in (model or DEFAULT_MODEL).items() if key != "provider"})
    result["bot"]["self_names"] = [name]
    plugins = plugins or DEFAULT_PLUGINS
    drawing, voice = plugins["comfyui"], plugins["voicestudio"]
    result["imagegen"].update(enable=drawing["enabled"], comfyui_enable=drawing["enabled"], local_only=True,
                              comfyui_url=drawing["url"], comfyui_checkpoint=drawing.get("checkpoint", ""))
    result["tts"].update(enabled=voice["enabled"], base_url=voice["url"], voice=voice.get("voice", ""),
                         model=voice.get("model", ""), language="zh", release_comfyui=drawing["enabled"])
    return result



class Controller:
    def __init__(self, directory: Path):
        self.root_directory = directory.resolve()
        self.directory = self.root_directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.directory / "settings.json"
        self.settings = json.loads(self.settings_path.read_text("utf-8")) if self.settings_path.exists() else {}
        self.settings.setdefault("model", dict(DEFAULT_MODEL))
        self.settings.setdefault("plugins", json.loads(json.dumps(DEFAULT_PLUGINS)))
        self.settings.setdefault("onebot", dict(DEFAULT_ONEBOT))
        persona_path = self.root_directory / "persona.txt"
        default_persona = persona_path.read_text("utf-8") if persona_path.exists() else (ROOT / "persona.txt").read_text("utf-8")
        self.settings.setdefault("bots", [{"id": "default", "name": "我的机器人", "persona": default_persona}])
        self.settings.setdefault("selected_bot", "default")
        # 旧版默认机器人的 id 是历史字符串，一次性改名到 default。
        # 数据本来就在数据目录根，不需要搬文件；这里保留旧字符串仅为识别老配置。
        for bot in self.settings["bots"]:
            if bot.get("id") == "taotao":
                bot["id"] = "default"
        if self.settings.get("selected_bot") == "taotao":
            self.settings["selected_bot"] = "default"
        if not any(bot["id"] == self.settings["selected_bot"] for bot in self.settings["bots"]):
            self.settings["selected_bot"] = self.settings["bots"][0]["id"] if self.settings["bots"] else None
        self.directory = self.bot_directory(self.settings["selected_bot"]) if self.settings["selected_bot"] else self.root_directory
        write_json(self.settings_path, self.settings)
        self.resources = Resources(self.root_directory)
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.cleanup_lock = threading.Lock()
        self.state = {"stage": "idle", "message": "配置模型与 OneBot 地址后连接。", "progress": 0, "saved_key": bool(self.settings.get("key"))}
        self.worker = None
        self.engine = None
        self.secret_values = []
        self.stopping = False
        self.model_for_run = dict(self.settings["model"])
        self.window = None
        self.latest_version = None


    def memory_snapshot(self):
        """读 history.json，给出会话概览。引擎在跑时也能读，但删除必须停机。"""
        path = self.directory / "data" / "history.json"
        if not path.is_file():
            return {"exists": False, "sessions": [], "path": str(path)}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {"exists": False, "sessions": [], "path": str(path), "error": "history.json 无法解析"}
        sessions = []
        for key, messages in (data.get("sessions") or {}).items():
            if not isinstance(messages, list):
                continue
            last = messages[-1] if messages else {}
            content = last.get("content") if isinstance(last, dict) else ""
            sessions.append({"key": key, "count": len(messages),
                             "last": str(content or "")[:160]})
        sessions.sort(key=lambda item: item["key"])
        return {"exists": True, "sessions": sessions, "path": str(path),
                "summary_count": len(data.get("summaries") or {})}

    def clear_memory(self, key=None):
        with self.lock:
            self.require_idle()
            path = self.directory / "data" / "history.json"
            if not path.is_file():
                return {"message": "没有可清理的记忆。"}
            data = json.loads(path.read_text("utf-8"))
            if key:
                if key not in (data.get("sessions") or {}):
                    raise ValueError("找不到这个会话。")
                data["sessions"].pop(key, None)
                (data.get("summaries") or {}).pop(key, None)
                message = "已清空该会话的记忆。"
            else:
                data["sessions"] = {}
                data["summaries"] = {}
                message = "已清空全部记忆。"
            write_json(path, data)
            return {"message": message}

    def check_updates(self):
        """后台查一次最新版本；比当前新就记下来，供界面提示。"""
        latest = latest_release_version()
        if latest and _version_tuple(latest) > _version_tuple(VERSION):
            self.latest_version = latest
            self.update()

    def bot_directory(self, bot_id):
        if not isinstance(bot_id, str) or not bot_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in bot_id):
            raise ValueError("机器人标识无效。")
        # 默认机器人直接用数据目录根，其余按 id 分子目录。
        return self.root_directory if bot_id == "default" else self.root_directory / "bots" / bot_id

    def selected_bot(self):
        return next((bot for bot in self.settings["bots"] if bot["id"] == self.settings["selected_bot"]), None)

    def require_idle(self):
        # shortcut: one active QQ bot at a time; give engines separate locks when parallel bots are requested.
        if self.stopping or self.worker and self.worker.is_alive():
            raise ValueError("请先停止当前机器人，再修改设置或切换机器人。")

    def save_bot(self, body):
        name, persona = body.get("name"), body.get("persona")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or not isinstance(persona, str) or len(persona) > 20000:
            raise ValueError("机器人名称须为 1—40 字，人设最多 20000 字。")
        with self.lock:
            self.require_idle()
            bot_id = body.get("id")
            if bot_id:
                bot = next((bot for bot in self.settings["bots"] if bot["id"] == bot_id), None)
                if bot is None:
                    raise ValueError("找不到这个机器人。")
                bot.update(name=name.strip(), persona=persona)
            else:
                if len(self.settings["bots"]) >= 50:
                    raise ValueError("当前最多保存 50 个机器人。")
                bot_id = secrets.token_hex(8)
                self.settings["bots"].append({"id": bot_id, "name": name.strip(), "persona": persona})
            self.settings["selected_bot"] = bot_id
            self.directory = self.bot_directory(bot_id)
            write_json(self.settings_path, self.settings)
            self.update(stage="idle", message="设置已保存，可以连接。")

    def select_bot(self, bot_id):
        with self.lock:
            self.require_idle()
            if not any(bot["id"] == bot_id for bot in self.settings["bots"]):
                raise ValueError("找不到这个机器人。")
            self.settings["selected_bot"] = bot_id
            self.directory = self.bot_directory(bot_id)
            write_json(self.settings_path, self.settings)
            self.update(stage="idle", message="机器人已切换，可以连接。", progress=0)

    def remove_bot(self, bot_id):
        with self.lock:
            self.require_idle()
            if not any(bot["id"] == bot_id for bot in self.settings["bots"]):
                raise ValueError("找不到这个机器人。")
            self.settings["bots"] = [bot for bot in self.settings["bots"] if bot["id"] != bot_id]
            if self.settings["selected_bot"] == bot_id:
                self.settings["selected_bot"] = self.settings["bots"][0]["id"] if self.settings["bots"] else None
            self.directory = self.bot_directory(self.settings["selected_bot"]) if self.settings["selected_bot"] else self.root_directory
            write_json(self.settings_path, self.settings)
            self.update(stage="idle", message="机器人已移除，本地聊天记录和登录资料已保留。", progress=0)

    def change_plugin(self, name, removed):
        if name not in DEFAULT_PLUGINS:
            raise ValueError("未知插件。")
        with self.lock:
            self.require_idle()
            if removed:
                self.resources.remove(name)
                self.settings["plugins"][name]["enabled"] = False
            else:
                self.resources.scan(name)
            self.settings["plugins"][name]["removed"] = removed
            write_json(self.settings_path, self.settings)

    def save_onebot(self, body):
        url = onebot_endpoint(body.get("ws_url"))
        token = body.get("access_token", "")
        if not isinstance(token, str) or len(token) > 512 or any(c.isspace() for c in token):
            raise ValueError("访问令牌格式无效。")
        with self.lock:
            self.require_idle()
            previous = self.settings.get("onebot", {})
            self.settings["onebot"] = {"ws_url": url,
                                       "access_token": token.strip() or previous.get("access_token", "")}
            write_json(self.settings_path, self.settings)
            self.update(stage="idle", message="OneBot 地址已保存，可以连接。")
        return {"message": "OneBot 地址已保存。"}

    def save_model(self, body, test_only=False):
        with self.lock:
            self.require_idle()
            provider, model_id = body.get("provider"), body.get("model")
            if not isinstance(provider, str) or not 1 <= len(provider.strip()) <= 64 or not isinstance(model_id, str) or not 1 <= len(model_id.strip()) <= 200:
                raise ValueError("请填写供应商名称和模型名称。")
            model = {"provider": provider.strip(), "model": model_id.strip(), "base_url": endpoint(body.get("base_url"))}
            key = body.get("key", "")
            if not isinstance(key, str):
                raise ValueError("API Key 格式无效。")
            key = key.strip()
            if not key:
                if model["base_url"] != self.settings["model"]["base_url"] and self.settings.get("key"):
                    raise ValueError("更换接口地址时，请重新输入对应供应商的 API Key。")
                if not self.settings.get("key"):
                    raise ValueError("请填写 API Key。")
                key = protect(base64.b64decode(self.settings["key"]), decrypt=True).decode()
            if len(key) > 4096 or any(c.isspace() for c in key):
                raise ValueError("API Key 格式无效。")
            message = validate_model_key(model, key)
            if not test_only:
                self.settings.update(model=model, key=base64.b64encode(protect(key.encode())).decode())
                write_json(self.settings_path, self.settings)
                self.update(saved_key=True)
            return {"message": message}

    def save_plugins(self, body):
        values = {}
        for name, defaults in DEFAULT_PLUGINS.items():
            supplied = body.get(name, {})
            if not isinstance(supplied, dict) or not isinstance(supplied.get("enabled"), bool):
                raise ValueError("插件配置无效。")
            value = {"enabled": supplied["enabled"], "url": endpoint(supplied.get("url"), local_only=True)}
            for field in ("checkpoint",) if name == "comfyui" else ("voice", "model"):
                text = supplied.get(field, "")
                if not isinstance(text, str) or len(text) > 300:
                    raise ValueError("插件配置无效。")
                value[field] = text.strip()
            values[name] = value
        with self.lock:
            self.require_idle()
            for name, value in values.items():
                value["removed"] = bool(self.settings["plugins"][name].get("removed"))
                if value["removed"]:
                    value["enabled"] = False
            self.settings["plugins"] = values
            write_json(self.settings_path, self.settings)

    def update(self, **fields):
        with self.lock:
            self.state.update(fields)

    def snapshot(self):
        with self.lock:
            return {**self.state, "saved_key": bool(self.settings.get("key")), "model": dict(self.settings["model"]),
                    "bots": [{"id": bot["id"], "name": bot["name"]} for bot in self.settings["bots"]],
                    "bot": dict(self.selected_bot()) if self.selected_bot() else None,
                    "onebot": {"ws_url": self.settings["onebot"]["ws_url"], "token_saved": bool(self.settings["onebot"].get("access_token"))}, "latest_version": self.latest_version, "plugins": json.loads(json.dumps(self.settings["plugins"])),
                    "resources": self.resources.snapshot(), "version": VERSION}

    def start(self, api_key: str):
        with self.lock:
            if self.stopping or self.worker and self.worker.is_alive():
                raise ValueError("正在连接，请稍候。")
            if not self.selected_bot():
                raise ValueError("请先创建一个机器人。")
            if not self.settings["onebot"].get("ws_url"):
                raise ValueError("请先在「OneBot 连接」页填写 WebSocket 地址。")
            api_key = api_key.strip()
            if not api_key:
                if not self.settings.get("key"):
                    raise ValueError("请先在模型页填写 API Key。")
                api_key = protect(base64.b64decode(self.settings["key"]), decrypt=True).decode()
            if not api_key or len(api_key) > 4096 or any(c.isspace() for c in api_key):
                raise ValueError("API Key 格式不正确，请检查后重试。")
            self.secret_values = [api_key]
            if self.settings["onebot"].get("access_token"):
                self.secret_values.append(self.settings["onebot"]["access_token"])
            self.model_for_run = dict(self.settings["model"])
            self.cancel.clear()
            self.update(stage="connecting", message="正在连接 OneBot 服务…", progress=0)
            self.worker = threading.Thread(target=self.run, args=(api_key,), daemon=True)
            self.worker.start()


    def setup(self):
        # 只写本工作台自己的配置，不接触任何协议端的安装或配置。
        onebot = self.settings["onebot"]
        bot = self.selected_bot()
        write_json(self.directory / "config.json",
                   make_config(onebot["ws_url"], onebot.get("access_token", ""),
                               self.model_for_run, self.settings["plugins"], bot["name"]))
        prompt = f"你当前的名字是『{bot['name']}』，如详细设定里出现旧名字，以当前名字为准。\n\n{bot['persona']}"
        (self.directory / "persona.txt").write_text(prompt, encoding="utf-8")


    def launch_engine(self, key):
        arguments = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
        environment = os.environ.copy()
        for name in ("DEEPSEEK_API_KEY", "BOCHA_API_KEY", "IMAGE_API_KEY", "ONEBOT_ACCESS_TOKEN"):
            environment.pop(name, None)
        environment["DEEPSEEK_API_KEY"] = key
        environment["PYTHONUTF8"] = "1"
        ready_path = self.directory / "engine-ready.json"
        ready_path.unlink(missing_ok=True)
        (self.directory / "engine-stop").unlink(missing_ok=True)
        self.engine = subprocess.Popen(arguments + ["--engine", "--data-dir", str(self.directory)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       env=environment, cwd=self.directory, creationflags=NO_WINDOW)
        for _ in range(100):
            if self.cancel.wait(0.2):
                raise InterruptedError()
            if self.engine.poll() is not None:
                raise RuntimeError("聊天程序未能启动。请查看日志；原来的机器人如果正在运行，请先关闭后重试。")
            if ready_path.exists():
                return
        raise RuntimeError("聊天程序连接超时，请检查 OneBot 地址与服务状态，然后重新连接。")


    def run(self, key):
        try:
            self.update(message="正在验证 API Key…")
            validate_model_key(self.model_for_run, key)
            with self.lock:
                self.settings["key"] = base64.b64encode(protect(key.encode())).decode()
                write_json(self.settings_path, self.settings)
            self.update(saved_key=True)
            for name, plugin in self.settings["plugins"].items():
                if not plugin["enabled"]:
                    continue
                self.resources.install(name, plugin["url"])
                while self.resources.thread.is_alive():
                    if self.cancel.wait(0.25):
                        self.resources.cancel.set()
                        raise InterruptedError()
                    job = self.resources.snapshot()["job"]
                    self.update(message=job["message"], progress=job["progress"])
                job = self.resources.snapshot()["job"]
                if job["phase"] != "done":
                    raise ValueError(job["message"])
            if self.cancel.is_set():
                raise InterruptedError()
            self.setup()
            self.update(stage="connecting", message="正在连接 OneBot 服务…", progress=100)
            self.launch_engine(key)
            online_message = self.selected_bot()["name"] + "已连接。可以私聊，或在群里 @ 机器人。"
            self.update(stage="online", message=online_message, progress=100)
            while not self.cancel.wait(3):
                if self.engine.poll() is not None:
                    raise RuntimeError("聊天程序已停止，请重新连接。")
        except InterruptedError:
            self.update(stage="idle", message="已停止，可以重新连接。", progress=0)
        except Exception as error:
            message = str(error)
            for value in self.secret_values:
                message = message.replace(value, "[已隐藏]")
            self.update(stage="error", message=message[:240])
        finally:
            self.cleanup()


    def cleanup(self):
        with self.cleanup_lock:
            self._cleanup()

    def _cleanup(self):
        if self.engine and self.engine.poll() is None:
            try:
                (self.directory / "engine-stop").touch()
                self.engine.wait(timeout=12)
            except (OSError, subprocess.TimeoutExpired):
                self.engine.terminate()
                self.engine.wait(timeout=5)


    def stop(self):
        with self.lock:
            if self.stopping:
                return
            self.stopping = True
        try:
            self.cancel.set()
            self.update(stage="stopping", message="正在停止并保存聊天记录…")
            if self.worker and self.worker.is_alive():
                self.worker.join(timeout=25)
            self.cleanup()
            if not self.worker or not self.worker.is_alive():
                self.update(stage="idle", message="已停止，可以重新连接。", progress=0)
        finally:
            with self.lock:
                self.stopping = False



def _note(data_dir: Path, text: str) -> None:
    """记一行守护日志。

    写文件用 UTF-8，一定成功；stdout 只在编码支持时才打 —— CI 的 stdout
    是 cp1252，直接 print 中文会抛 UnicodeEncodeError 把进程带崩。
    """
    try:
        with (data_dir / "supervise.log").open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        pass


def supervise(data_dir: Path):
    """守护 --engine 子进程：退出就重启，退避 3 → 6 → 12 → 30 → 60 秒。

    `engine-stop` 出现表示用户主动停止，此时守护进程也退出，不再拉起。
    跑满 60 秒后退出视为「曾经正常工作过」，退避重置回 3 秒。
    """
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    stop = data_dir / "engine-stop"
    delay = 3.0
    while True:
        if stop.exists():
            _note(data_dir, "检测到 engine-stop，守护进程退出。")
            return 0
        arguments = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
        child = subprocess.Popen(arguments + ["--engine", "--data-dir", str(data_dir)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        started = time.monotonic()
        try:
            child.wait()
        except KeyboardInterrupt:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
            return 0
        if stop.exists():
            _note(data_dir, "检测到 engine-stop，守护进程退出。")
            return 0
        ran = time.monotonic() - started
        if ran >= 60:
            delay = 3.0
        _note(data_dir, f"引擎退出（退出码 {child.returncode}，运行 {ran:.0f} 秒），{delay:.0f} 秒后重启…")
        time.sleep(delay)
        delay = min(delay * 2, 60.0)


def run_engine(directory):
    import logging
    import engine_main
    import bot.sticker
    import bot.sticker_gen
    import bot.tts

    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "bot-process.log"
    if log_path.is_file() and log_path.stat().st_size > 5_000_000:
        log_path.replace(log_path.with_suffix(".log.1"))
    # 追加而不是截断：--supervise 会在崩溃后立刻重启，截断模式会把上一次的
    # 崩溃现场清掉，看门狗越勤快越看不到崩在哪。
    output = log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = output
    bot.sticker.STICKER_ROOT = directory / "assets/stickers"
    bot.sticker_gen.STICKER_ROOT = directory / "assets/stickers"
    bot.sticker_gen.LORA_TRIGGER = getattr(cfg.imagegen, "comfyui_lora_trigger", "") or ""

    class Ready(logging.Handler):
        def emit(self, record):
            if record.getMessage() == "OneBot 连接就绪":
                write_json(directory / "engine-ready.json", {"ready": True})

    logging.getLogger("bot.onebot").addHandler(Ready())
    config_path = directory / "config.json"
    cfg = engine_main.Config.load(config_path)
    bot.tts.PAUSE_FILE = directory / ".speech-paused"
    bot.tts._COMFYUI_FREE_URL = cfg.imagegen.comfyui_url.rstrip("/") + "/free"
    lock = engine_main._acquire_single_instance(cfg.data_dir)
    if lock is None:
        # 这行必须在 return 之前：界面提示用户「请查看日志」，不写就等于指向空文件。
        print(f"数据目录 {cfg.data_dir} 已有引擎在运行，本次启动取消。", flush=True)
        return 3

    async def run():
        task = asyncio.create_task(engine_main.amain(argparse.Namespace(config=str(config_path), check=False)))

        async def wait_for_stop():
            while not (directory / "engine-stop").exists():
                await asyncio.sleep(0.25)
            task.cancel()

        stopper = asyncio.create_task(wait_for_stop())
        try:
            return await task
        except asyncio.CancelledError:
            return 0
        finally:
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)

    try:
        return asyncio.run(run())
    finally:
        lock.close()
        output.flush()


def serve(controller, open_browser=True):
    session = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, value, code=200, kind="application/json; charset=utf-8"):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def valid(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            origin = self.headers.get("Origin")
            return self.headers.get("Host") == expected and (not origin or origin == "http://" + expected)

        def do_GET(self):
            if not self.valid():
                return self.send({"error": "请求来源无效"}, 403)
            if self.path == "/":
                return self.send((ROOT / "index.html").read_bytes(), kind="text/html; charset=utf-8")
            if not secrets.compare_digest(self.headers.get("X-Session", ""), session):
                return self.send({"error": "请从程序打开的窗口访问。"}, 403)
            if self.path == "/state":
                return self.send(controller.snapshot())
            if self.path == "/memory":
                return self.send(controller.memory_snapshot())
            if self.path.startswith("/logs"):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                try:
                    tail = max(20, min(2000, int(query.get("tail", ["200"])[0])))
                except (TypeError, ValueError):
                    tail = 200
                path = controller.directory / "bot-process.log"
                text = ""
                if path.is_file():
                    with path.open("rb") as handle:
                        handle.seek(0, os.SEEK_END)
                        size = handle.tell()
                        handle.seek(max(0, size - 400_000))
                        text = handle.read().decode("utf-8", "replace")
                return self.send({"lines": text.splitlines()[-tail:], "path": str(path),
                                  "exists": path.is_file()})
            return self.send({"error": "未找到页面"}, 404)

        def do_POST(self):
            if not self.valid() or not secrets.compare_digest(self.headers.get("X-Session", ""), session):
                return self.send({"error": "请求来源无效"}, 403)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 131072 or self.headers.get_content_type() != "application/json":
                    raise ValueError("请求格式无效。")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效。")
                result = {}
                if self.path == "/bots/save":
                    controller.save_bot(body)
                elif self.path == "/bots/select":
                    controller.select_bot(body.get("id"))
                elif self.path == "/bots/remove":
                    controller.remove_bot(body.get("id"))
                elif self.path in ("/plugins/remove", "/plugins/add"):
                    controller.change_plugin(body.get("name"), self.path == "/plugins/remove")
                elif self.path in ("/model/save", "/model/test"):
                    result = controller.save_model(body, test_only=self.path == "/model/test")
                elif self.path == "/plugins/save":
                    controller.save_plugins(body)
                elif self.path == "/onebot/save":
                    result = controller.save_onebot(body)
                elif self.path == "/memory/clear":
                    result = controller.clear_memory(body.get("key"))
                elif self.path == "/open-data-dir":
                    os.startfile(str(controller.directory))
                    result = {"message": "已打开数据目录。"}
                elif self.path in ("/plugins/install", "/plugins/probe", "/plugins/start"):
                    name = body.get("name")
                    if name not in DEFAULT_PLUGINS:
                        raise ValueError("未知插件。")
                    if controller.settings["plugins"][name].get("removed"):
                        raise ValueError("请先重新添加这个插件。")
                    url = endpoint(body.get("url", controller.settings["plugins"][name]["url"]), local_only=True)
                    if self.path == "/plugins/probe":
                        result = controller.resources.probe(name, url)
                    else:
                        controller.resources.install(name, url)
                        result = {"message": "正在检查本机服务和安装；没有找到时会自动安装并启动。"}
                elif self.path == "/plugins/local":
                    name = body.get("name")
                    if name not in DEFAULT_PLUGINS or controller.settings["plugins"][name].get("removed") or not controller.window:
                        raise ValueError("请在桌面窗口中选择已添加的插件。")
                    import webview
                    paths = controller.window.create_file_dialog(webview.FileDialog.FOLDER, allow_multiple=False)
                    if paths:
                        controller.resources.scan(name, paths[0])
                        result = {"message": "已关联本地安装，无需下载。"}
                elif self.path == "/plugins/cancel":
                    if controller.resources.snapshot()["job"]["phase"] != "downloading":
                        raise ValueError("当前已进入安装阶段，请等待安装完成。")
                    controller.resources.cancel.set()
                elif self.path == "/start":
                    if not isinstance(body.get("key", ""), str):
                        raise ValueError("API Key 格式无效。")
                    controller.start(body.get("key", ""))
                elif self.path == "/stop":
                    threading.Thread(target=controller.stop, daemon=True).start()
                elif self.path == "/exit":
                    def close():
                        controller.resources.cancel.set()
                        controller.stop()
                        self.server.shutdown()
                        if controller.window:
                            controller.window.destroy()
                    threading.Thread(target=close, daemon=True).start()
                elif self.path == "/activate":
                    if controller.window:
                        controller.window.restore()
                        controller.window.show()
                else:
                    return self.send({"error": "未知操作"}, 404)
                self.send({"ok": True, **result})
            except (ValueError, OSError, RuntimeError, KeyError) as error:
                message = str(error) if isinstance(error, ValueError) else "操作未完成，请检查输入并重试。"
                self.send({"error": message}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/#{session}"
    if open_browser:
        open_window(url, controller.directory)
    return server, url


def self_test(report):
    import tempfile
    from bot.config import Config

    checks = []
    def verify(condition, message):
        if not condition:
            raise AssertionError(message)

    sample = b"self-test-only-no-real-credential"
    encrypted = protect(sample)
    verify(encrypted != sample and protect(encrypted, decrypt=True) == sample, "Credential encryption failed")
    checks.append("Windows credential encryption roundtrip")
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        config = make_config("ws://127.0.0.1:3001", "test-token-only")
        write_json(directory / "config.json", config)
        (directory / "persona.txt").write_text("你叫小北。", encoding="utf-8")
        cfg = Config.load(directory / "config.json")
        verify(not cfg.sticker_gen.prewarm and not cfg.imagegen.enable and not cfg.bot.summary_enabled, "Background calls enabled")
        verify(cfg.base_dir == directory, "Config data path mismatch")
        checks.append("Starter config loads; autonomous features disabled")
        verify(config["onebot"]["ws_url"] == "ws://127.0.0.1:3001", "OneBot URL lost")
        for bad in ("http://127.0.0.1:3001", "ws://", "ws://user:pass@127.0.0.1:3001"):
            try:
                onebot_endpoint(bad)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid OneBot URL accepted: " + bad)
        checks.append("User-supplied OneBot endpoint validation")
        controller = Controller(directory / "app")
        server, url = serve(controller, open_browser=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base, token = url.split("#")
            request = urllib.request.Request(base + "state", headers={"X-Session": token})
            with urllib.request.urlopen(request, timeout=3) as response:
                state = json.load(response)
            verify(state["stage"] == "idle" and "key" not in state, "UI state leak")
            try:
                urllib.request.urlopen(base + "state", timeout=3)
            except urllib.error.HTTPError as error:
                verify(error.code == 403, "Wrong authentication rejection")
            else:
                raise AssertionError("Unauthenticated request was accepted")
            checks.append("Local UI authentication; no API key in state")
        finally:
            server.shutdown()
            server.server_close()
    write_json(report, {"passed": True, "version": VERSION, "checks": checks, "real_qq_login_tested": False, "paid_api_called": False})


def runtime_self_test(directory, report):
    controller = Controller(directory)
    write_json(report, {"passed": True, "account_logged_in": False, "paid_api_called": False,
                        "ws_url": controller.settings["onebot"]["ws_url"],
                        "note": "仅校验 OneBot 配置，不启动任何协议端、不登录 QQ、不调用模型"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", action="store_true")
    parser.add_argument("--supervise", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--self-test", type=Path, metavar="REPORT")
    parser.add_argument("--runtime-self-test", type=Path, metavar="REPORT")
    parser.add_argument("--desktop-self-test", type=Path, metavar="REPORT")
    args = parser.parse_args()
    if args.self_test:
        self_test(args.self_test)
        return 0
    if args.runtime_self_test:
        runtime_self_test(args.data_dir, args.runtime_self_test)
        return 0
    if args.desktop_self_test:
        from desktop_check import check
        return check(args.desktop_self_test)
    if args.supervise:
        return supervise(args.data_dir)
    if args.engine:
        return run_engine(args.data_dir)
    window = args.data_dir / "window.json"
    # 命名互斥量做单实例锁：不受端口占用影响，进程退出由系统回收。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    # 锁按数据目录区分：同一个 data-dir 严格单实例，不同 data-dir 可以并行多开。
    key = hashlib.sha256(str(args.data_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    mutex = kernel32.CreateMutexW(None, False, f"Local\\Locomotive.{key}")
    if not mutex:
        raise OSError("无法创建单实例互斥量")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(mutex)
        reopen_window(window, args.data_dir)
        return 0
    controller = Controller(args.data_dir)
    threading.Thread(target=controller.check_updates, daemon=True).start()
    server, url = serve(controller, open_browser=False)
    write_json(window, {"url": url, "pid": os.getpid()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    require_webview2()
    try:
        import webview
        controller.window = open_window(url, args.data_dir)
        def closing():
            controller.resources.cancel.set()
            controller.stop()
        controller.window.events.closing += closing
        webview.start(gui="edgechromium", private_mode=True, storage_path=str(args.data_dir / "webview"))
    finally:
        controller.resources.cancel.set()
        controller.stop()
        server.shutdown()
        server.server_close()
        kernel32.CloseHandle(mutex)
        window.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as error:
        if not any(flag in sys.argv for flag in ("--engine", "--self-test", "--runtime-self-test", "--desktop-self-test")):
            show_startup_error(error)
        exit_code = 1
    raise SystemExit(exit_code)
