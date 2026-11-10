"""Isolated smoke test of the real embedded Windows window and its controls."""
import json
from pathlib import Path
import tempfile
import threading
import time


def check(report):
    import app
    import webview
    result = {"passed": False, "paid_api_called": False, "real_qq_used": False}
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        controller = app.Controller(directory)
        server, url = app.serve(controller, open_browser=False)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        window = app.open_window(url, directory)
        controller.window = window
        def exercise():
            try:
                if not window.events.loaded.wait(30):
                    raise RuntimeError('Desktop page did not load')
                window.run_js((app.ROOT / 'desktop-check.js').read_text('utf-8'))
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    value = json.loads(window.run_js('JSON.stringify(window.__desktopResult || null)'))
                    if value:
                        result.update(value)
                        break
                    time.sleep(.2)
                else:
                    raise RuntimeError('Desktop controls timed out')
                result['renderer'] = window.gui.renderer
            except Exception as error:
                result['error'] = str(error)
            finally:
                app.write_json(report, result)
                window.destroy()
        try:
            webview.start(exercise, gui='edgechromium', private_mode=True, storage_path=str(directory / 'webview'))
        except Exception as error:
            result['error'] = str(error)
            app.write_json(report, result)
        finally:
            controller.stop()
            server.shutdown()
            server.server_close()
    return 0 if result['passed'] else 1
