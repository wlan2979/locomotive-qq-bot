import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import app


class AppTests(unittest.TestCase):
    def test_window_is_embedded_without_launching_browser(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            url = "http://127.0.0.1:12345/#test-session"
            with patch("webview.create_window") as create, patch("app.subprocess.Popen") as launch:
                self.assertIs(app.open_window(url, directory), create.return_value)
                self.assertEqual(create.call_args.args, ('Locomotive', url))
                launch.assert_not_called()

    def test_window_failure_is_reported_instead_of_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("webview.create_window", side_effect=RuntimeError('WebView2 unavailable')):
                with self.assertRaises(RuntimeError):
                    app.open_window("http://127.0.0.1:12345/#test-session", Path(temp))
            with patch("app.subprocess.Popen") as launch:
                with self.assertRaises(ValueError):
                    app.open_window("https://example.com/", Path(temp))
                launch.assert_not_called()

    def test_reopen_verifies_existing_service_before_opening_window(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            controller = app.Controller(directory)
            server, url = app.serve(controller, open_browser=False)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            window = directory / "window.json"
            app.write_json(window, {"url": url})
            try:
                controller.window = MagicMock()
                with patch("app.open_window") as launch:
                    app.reopen_window(window, directory)
                    controller.window.restore.assert_called_once()
                    controller.window.show.assert_called_once()
                    launch.assert_not_called()
            finally:
                server.shutdown()
                server.server_close()
            with patch("app.open_window") as launch:
                with self.assertRaises(RuntimeError):
                    app.reopen_window(window, directory)
                launch.assert_not_called()

    def test_config_has_no_api_key_and_no_autonomous_actions(self):
        config = app.make_config(12345, "local-test-only")
        self.assertEqual(config["deepseek"]["api_key"], "")
        self.assertEqual(config["onebot"]["ws_url"], "ws://127.0.0.1:12345")
        for section, name in [("phone", "enable"), ("hands", "enable"), ("douyin", "enable"),
                              ("imagegen", "enable"), ("sticker_gen", "prewarm"), ("bot", "summary_enabled")]:
            self.assertIs(config[section][name], False)

    def test_saved_key_is_encrypted_and_never_returned(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = app.Controller(Path(temp))
            key = "fake-key-for-offline-tests-only"
            with patch("app.validate_model_key", return_value="ok"), \
                 patch.object(controller, "prepare_runtime", side_effect=InterruptedError):
                controller.start(key)
                controller.worker.join(2)
            settings_text = controller.settings_path.read_text("utf-8")
            self.assertNotIn(key, settings_text)
            self.assertNotIn(key, json.dumps(controller.snapshot()))
            self.assertTrue(controller.snapshot()["saved_key"])
            with patch.object(controller, "run") as run:
                controller.start("")
                controller.worker.join(2)
                run.assert_called_once_with(key)

    def test_invalid_key_preserves_previous_saved_key(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = app.Controller(Path(temp))
            controller.settings["key"] = "previous-encrypted-value"
            app.write_json(controller.settings_path, controller.settings)
            with patch("app.validate_model_key", side_effect=ValueError("Invalid key")):
                controller.start("wrong-fake-key-for-test")
                controller.worker.join(2)
            self.assertEqual(json.loads(controller.settings_path.read_text("utf-8"))["key"], "previous-encrypted-value")
            self.assertEqual(controller.snapshot()["stage"], "error")

    def test_start_is_rejected_while_stop_cleanup_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = app.Controller(Path(temp))
            controller.stopping = True
            with self.assertRaises(ValueError):
                controller.start("fake-test-key-long-enough")

    def test_config_updates_only_owned_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            controller = app.Controller(directory / "profile")
            runtime = directory / "runtime"
            runtime.mkdir()
            account = directory / "profile/napcat/config/onebot11_10000.json"
            app.write_json(account, {"network": {}})
            config = controller.setup(runtime)
            self.assertEqual(json.loads(account.read_text("utf-8")), config)
            self.assertTrue((directory / "profile/napcat/config/webui.json").exists())
            self.assertFalse((runtime / "napcat/config/webui.json").exists())
            self.assertEqual(json.loads((directory / "profile/config.json").read_text("utf-8"))["deepseek"]["api_key"], "")

    def test_napcat_protocol_matches_official_auth_and_renews(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, payload, self.headers.get("Authorization")))
                value = {"Credential": "fake-session"} if self.path == "/api/auth/login" else {"isLogin": False, "qrcodeurl": "test-qr"}
                content = json.dumps({"code": 0, "data": value}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            api = app.Napcat(server.server_port, "test-password")
            api.login()
            status = api.call("/QQLogin/CheckLoginStatus")
            self.assertFalse(status["isLogin"])
            self.assertEqual(calls[0][1]["hash"], hashlib.sha256(b"test-password.napcat").hexdigest())
            self.assertEqual(calls[1][2], "Bearer fake-session")
            api.authenticated_at -= 3601
            api.call("/QQLogin/CheckLoginStatus")
            self.assertEqual([row[0] for row in calls].count("/api/auth/login"), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_browser_api_requires_session_and_expected_origin(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = app.Controller(Path(temp))
            server, url = app.serve(controller, open_browser=False)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base, session = url.split("#")
            def post(payload, token=session, origin=None):
                headers = {"Content-Type": "application/json", "X-Session": token}
                if origin:
                    headers["Origin"] = origin
                request = urllib.request.Request(base + "start", json.dumps(payload).encode(), headers)
                return urllib.request.urlopen(request, timeout=3)
            try:
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post({"key": "fake-key-for-tests"}, token="")
                self.assertEqual(rejected.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post({"key": "fake-key-for-tests"}, origin="https://example.com")
                self.assertEqual(rejected.exception.code, 403)
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    post({"key": ["invalid"]})
                self.assertEqual(rejected.exception.code, 400)
                with patch.object(controller, "start") as start:
                    with post({"key": "fake-key-for-tests"}) as response:
                        self.assertTrue(json.load(response)["ok"])
                    start.assert_called_once_with("fake-key-for-tests")
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
