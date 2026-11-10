"""Pre-login runtime check only: no account login and no AI request."""
import json
import os
from pathlib import Path
import subprocess
import time

import app

directory = Path(__file__).resolve().parent.parent / "runtime-smoke"
controller = app.Controller(directory)
runtime = directory / ("runtime-" + app.NAPCAT_VERSION)
archive = Path(__file__).resolve().parent.parent / "napcat-research/NapCat.Shell.Windows.Node.zip"
if not (runtime / ".ready").exists():
    assert app.hashlib.file_digest(archive.open("rb"), "sha256").hexdigest() == app.RUNTIME_HASH
    app.unpack_checked(archive, runtime)
    (runtime / ".ready").write_text(app.RUNTIME_HASH, encoding="ascii")
controller.setup(runtime)
environment = os.environ.copy()
environment["NAPCAT_QQ_DATA_DIR"] = str(directory / "qq")
environment["NAPCAT_WORKDIR"] = str(directory / "napcat")
environment["NAPCAT_WEBUI_SECRET_KEY"] = controller.api.password
for name in ("DEEPSEEK_API_KEY", "BOCHA_API_KEY", "IMAGE_API_KEY", "ONEBOT_ACCESS_TOKEN"):
    environment.pop(name, None)
result = {"passed": False, "login_performed": False, "ai_api_called": False}
try:
    with (directory / "native-smoke.log").open("wb") as output:
        controller.node = subprocess.Popen([str(runtime / "node.exe"), str(runtime / "index.js")],
                                          cwd=runtime, env=environment, stdout=output,
                                          stderr=subprocess.STDOUT, creationflags=app.NO_WINDOW)
    for _ in range(90):
        if controller.node.poll() is not None:
            raise RuntimeError("Portable runtime exited with code " + str(controller.node.returncode))
        try:
            controller.api.login()
            state = controller.api.call("/QQLogin/CheckLoginStatus")
            qr = state.get("qrcodeurl") or controller.api.call("/QQLogin/GetQQLoginQrcode").get("qrcode")
            if qr:
                assert state.get("isLogin") is False
                result.update(passed=True, auth_api=True, qr_available=True, account_logged_in=False)
                break
        except (OSError, ValueError, RuntimeError, KeyError):
            time.sleep(1)
    else:
        raise RuntimeError("Runtime did not provide a QR code within 90 seconds")
finally:
    controller.cleanup()
    app.write_json(directory / "result.json", result)
print(json.dumps(result))
assert result["passed"]
