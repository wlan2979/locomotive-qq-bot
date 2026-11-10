"""Install official optional resources without replacing existing installations."""
import hashlib
import ctypes
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

PACKAGES = {
    "comfyui": {"version": "0.44.0", "size": 2054382457,
        "url": "https://github.com/Comfy-Org/ComfyUI/releases/download/v0.44.0/ComfyUI_windows_portable_nvidia.7z",
        "sha256": "7170c2db49c182a81adcc051e80234f99dd891656fb200737f87797876348f96"},
    "voicestudio": {"version": "0.5.7", "size": 222263756,
        "url": "https://github.com/debpalash/VoiceStudio/releases/download/v0.5.7/VoiceStudio-0.5.7-win-x64.exe",
        "sha256": "76c6385d404ae9f11b9e47d42996432ea4cd6c065afb06718c4842b39020cd80"},
}


def external_process(arguments, **options):
    frozen = getattr(sys, 'frozen', False)
    if frozen:
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(arguments, **options)
    finally:
        if frozen:
            ctypes.windll.kernel32.SetDllDirectoryW(str(sys._MEIPASS))


def installed_voice():
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view) as root:
                    for index in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                if winreg.QueryValueEx(key, "DisplayName")[0].strip().lower().split(" ", 1)[0] != "voicestudio":
                                    continue
                                for field in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                    try:
                                        value = str(winreg.QueryValueEx(key, field)[0]).strip()
                                        candidate = Path(value.split('"')[1] if value.startswith('"') else value.split(",")[0])
                                        candidate = candidate / "VoiceStudio.exe" if candidate.is_dir() else candidate.parent / "VoiceStudio.exe"
                                        if candidate.is_file():
                                            return str(candidate)
                                    except (OSError, IndexError):
                                        continue
                        except OSError:
                            continue
            except OSError:
                continue
    return ""


def comfy_record(root):
    root = Path(root)
    for source in (root, root / "ComfyUI"):
        if not (source / "main.py").is_file() or not (source / "nodes.py").is_file():
            continue
        for python in (source / "venv/Scripts/python.exe", source / ".venv/Scripts/python.exe",
                       source.parent / "python_embeded/python.exe"):
            if python.is_file():
                launcher = source / "start_comfyui.bat"
                launch_text = launcher.read_text('utf-8', errors='replace')[:65536] if launcher.is_file() else ""
                launch_text = '\n'.join(line for line in launch_text.splitlines() if 'main.py' in line and not line.lstrip().lower().startswith(('rem ', '::')))
                # Preserve local VRAM calibration without executing launcher scripts.
                flags = [flag for flag in ("--fp8_e4m3fn-unet", "--lowvram", "--novram", "--disable-nvml-pressure") if flag in launch_text]
                reserve = re.search(r"--reserve-vram\s+(\d+(?:\.\d+)?)", launch_text)
                if reserve:
                    flags += ["--reserve-vram", reserve[1]]
                return {"entry": str(python), "main": str(source / "main.py"),
                        "portable": python.parent.name == "python_embeded", "args": flags, "version": "本地安装"}
    return None


def installed_comfy(folder):
    home = Path.home()
    roots = [home, home / "Desktop", home / "Downloads", home / "Documents",
             Path(os.environ.get("LOCALAPPDATA", home)) / "Programs"]
    # shortcut: scan common locations, not entire disks; the folder picker covers custom locations.
    roots += [Path(f"{drive}:/") / sub for drive in "CDEFG" for sub in ("", "AI", "tools")]
    candidates = [base / name for base in roots for name in ("ComfyUI", "ComfyUI_windows_portable", "comfyui")]
    if (folder / "comfyui").is_dir():
        candidates = [path for path in (folder / "comfyui").glob("*/*") if not path.parent.name.endswith(".staging")] + candidates
    for candidate in candidates:
        record = comfy_record(candidate)
        if record:
            return record
    return None


def download(package, destination, cancel, progress):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file():
        with destination.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() == package["sha256"]:
                return
    partial = destination.with_suffix(destination.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset >= package["size"]:
        with partial.open("rb") as handle:
            complete = offset == package["size"] and hashlib.file_digest(handle, "sha256").hexdigest() == package["sha256"]
        if complete:
            partial.replace(destination)
            return
        partial.unlink()
        offset = 0
    headers = {"User-Agent": "Locomotive/0.2"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with urllib.request.urlopen(urllib.request.Request(package["url"], headers=headers), timeout=60) as response:
        append = offset > 0 and response.status == 206 and response.headers.get("Content-Range", "").startswith(f"bytes {offset}-")
        if not append:
            offset = 0
        with partial.open("ab" if append else "wb") as output:
            while chunk := response.read(1024 * 1024):
                if cancel.is_set():
                    raise InterruptedError()
                offset += len(chunk)
                if offset > package["size"]:
                    raise ValueError("下载大小异常，已停止。")
                output.write(chunk)
                progress(int(offset / package["size"] * 100))
    with partial.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if offset != package["size"] or digest != package["sha256"]:
        partial.unlink(missing_ok=True)
        raise ValueError("下载文件未通过完整性校验，请重试。")
    partial.replace(destination)


def extract_7z(archive, target):
    import py7zr
    target = target.resolve()
    with py7zr.SevenZipFile(archive, max_extract_size=30_000_000_000) as bundle:
        total = 0
        for item in bundle.list():
            resolved = (target / item.filename).resolve()
            if not resolved.is_relative_to(target) or item.is_symlink:
                raise ValueError("压缩包包含不安全的路径，已取消安装。")
            total += item.uncompressed
            if total > 30_000_000_000:
                raise ValueError("解压大小超出安全范围。")
    target.mkdir(parents=True, exist_ok=True)
    # ComfyUI uses BCJ2; Windows libarchive handles it, py7zr's decoder does not.
    extractor = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/tar.exe"
    if not extractor.is_file():
        raise ValueError("此系统缺少 Windows tar 解压工具，请更新 Windows 10/11 后重试。")
    result = subprocess.run([str(extractor), "-xf", str(archive), "-C", str(target)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError("资源解压未完成，请检查磁盘空间和 Windows 解压组件。")


class Resources:
    def __init__(self, directory):
        self.folder = (directory / "resources").resolve()
        self.manifest = self.folder / "installed.json"
        self.records = json.loads(self.manifest.read_text("utf-8")) if self.manifest.exists() else {}
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.processes = {}
        self.job = {"resource": "", "phase": "idle", "progress": 0, "message": ""}
        for name in PACKAGES:
            if not self.records.get(name, {}).get("removed"):
                self.scan(name)

    def update(self, **values):
        with self.lock:
            self.job.update(values)

    def snapshot(self):
        with self.lock:
            return {"installed": {name: {**value, "installed": not value.get("removed") and Path(value.get("entry", "")).is_file()} for name, value in self.records.items()},
                    "job": dict(self.job), "packages": {name: {"version": package["version"], "size": package["size"]} for name, package in PACKAGES.items()}}

    def scan(self, name, directory=None):
        if name not in PACKAGES:
            raise ValueError("未知插件。")
        with self.lock:
            previous = self.records.get(name, {})
            if not directory and Path(previous.get("entry", "")).is_file():
                record = {**previous, "removed": False}
            elif name == "comfyui":
                record = comfy_record(directory) if directory else installed_comfy(self.folder)
            else:
                entry = str(Path(directory) / "VoiceStudio.exe") if directory else installed_voice()
                record = {"entry": entry, "version": "本地安装"} if entry and Path(entry).is_file() else None
            if directory and not record:
                raise ValueError("此目录中没有找到可运行的插件。请选择 ComfyUI 主目录或 VoiceStudio 安装目录。")
            if record:
                self.records[name] = record
                self._save()
            return record

    def remove(self, name):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("请先等待插件准备完成，或暂停下载，再移除插件。")
            self.records.setdefault(name, {})["removed"] = True
            self._save()

    def install(self, name, url=None):
        if name not in PACKAGES:
            raise ValueError("未知资源。")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("另一项资源正在安装，请稍候。")
            self.cancel.clear()
            self.update(resource=name, phase="scanning", progress=0, message="正在优先检查本地服务和安装…")
            self.thread = threading.Thread(target=self._install, args=(name, url), daemon=True)
            self.thread.start()

    def _install(self, name, url=None):
        try:
            if url:
                try:
                    self.probe(name, url)
                    self.update(phase="done", progress=100, message="已连接本机正在运行的服务，无需安装。")
                    return
                except (OSError, ValueError):
                    pass
            package = PACKAGES[name]
            record = self.scan(name)
            if record:
                message = self.start(name, url)["message"] if url else "检测到本地安装，无需重复下载，可以直接启动。"
                self.update(phase="done", progress=100, message=message)
                return
            self.update(phase="downloading", message="本地未找到，正在自动下载官方安装包…")
            self.folder.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.folder).free < (12_000_000_000 if name == "comfyui" else 2_000_000_000):
                raise ValueError("安装目录所在磁盘的可用空间不足。")
            destination = self.folder / "downloads" / Path(urllib.parse.urlsplit(package["url"]).path).name
            download(package, destination, self.cancel, lambda value: self.update(progress=value, message=f"正在下载 {value}%"))
            if self.cancel.is_set():
                raise InterruptedError()
            target = self.folder / name / package["version"]
            if name == "comfyui":
                self.update(phase="extracting", message="正在解压 ComfyUI 运行环境，请稍候…")
                staging = target.with_name(target.name + ".staging").resolve()
                if not staging.is_relative_to(self.folder) or staging == self.folder:
                    raise ValueError("安装目录无效。")
                if staging.exists():
                    shutil.rmtree(staging)
                extract_7z(destination, staging)
                candidates = list(staging.glob("*/python_embeded/python.exe"))
                if len(candidates) != 1 or not (candidates[0].parent.parent / "ComfyUI/main.py").is_file():
                    raise ValueError("安装包缺少 ComfyUI 启动文件。")
                relative = candidates[0].relative_to(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    raise ValueError("目标版本目录已存在，保留现有文件，请检查安装目录。")
                staging.rename(target)
                entry = str(target / relative)
            else:
                # Existing installs were detected above; never upgrade their modified runtime.
                self.update(phase="installing", message="正在自动安装 VoiceStudio…")
                target.mkdir(parents=True, exist_ok=True)
                process = external_process([str(destination), "/S", "/D=" + str(target)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                result = process.wait(timeout=900)
                if result not in (0, 3010):
                    raise ValueError("VoiceStudio 安装未完成，请重试或使用现有安装。")
                candidate = target / "VoiceStudio.exe"
                entry = str(candidate) if candidate.is_file() else installed_voice()
                if not entry:
                    raise ValueError("安装完成后没有找到 VoiceStudio。")
            with self.lock:
                self.records[name] = {"entry": entry, "version": package["version"]}
            self._save()
            message = self.start(name, url)["message"] if url else "安装完成，可以启动并检测连接。"
            self.update(phase="done", progress=100, message=message)
        except InterruptedError:
            self.update(phase="cancelled", message="下载已暂停，再次安装将继续下载。")
        except Exception as error:
            self.update(phase="error", message=str(error)[:200])

    def _save(self):
        with self.lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary = self.manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.manifest)

    def probe(self, name, url):
        suffix = "/system_stats" if name == "comfyui" else "/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url.rstrip("/") + suffix, timeout=4) as response:
            result = json.loads(response.read(2_000_000))
        if not isinstance(result, dict):
            raise ValueError("资源接口返回了无法识别的内容。")
        return {"connected": True, "message": "连接正常", "version": str(result.get("version", result.get("system", {}).get("comfyui_version", "")))[:80]}

    def start(self, name, url):
        try:
            return self.probe(name, url)
        except (OSError, ValueError):
            pass
        entry = Path(self.records.get(name, {}).get("entry", ""))
        if not entry.is_file():
            raise ValueError("请先自动安装这项资源。")
        previous = self.processes.get(name)
        if previous and previous.poll() is None:
            return {"message": "资源正在启动，请稍后检测连接。"}
        if name == "comfyui":
            record = self.records[name]
            main = Path(record.get("main", entry.parent.parent / "ComfyUI/main.py"))
            root = main.parent
            port = urllib.parse.urlsplit(url).port or 8188
            arguments = [str(entry), "-s", str(main), "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch"]
            if record.get("portable", entry.parent.name == "python_embeded"):
                arguments.append("--windows-standalone-build")
            arguments.extend(record.get("args", []))
            if not shutil.which("nvidia-smi"):
                arguments.append("--cpu")
            self.folder.mkdir(parents=True, exist_ok=True)
            with (self.folder / "comfyui-start.log").open("ab") as output:
                self.processes[name] = external_process(arguments, cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 1
            self.processes[name] = external_process([str(entry)], cwd=entry.parent, startupinfo=startup)
        return {"message": "已使用本地安装发起启动，请稍后检测连接。"}
