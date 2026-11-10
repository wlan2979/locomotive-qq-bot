"""Isolated UI fixture; never logs in to QQ or calls a real model."""
import json
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import app

root = Path(__file__).resolve().parent.parent
controller = app.Controller(root / 'locomotive-preview-profile')
class FakeModel(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        content = b'{"data":[{"id":"offline-demo"}]}'
        self.send_response(200); self.send_header('Content-Length', str(len(content)))
        self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(content)
model = ThreadingHTTPServer(('127.0.0.1',0), FakeModel)
threading.Thread(target=model.serve_forever,daemon=True).start()
server,url = app.serve(controller, open_browser=False)
app.write_json(root / 'locomotive-preview.json', {'url':url,'model_url':f'http://127.0.0.1:{model.server_port}/v1'})
print('Preview ready',flush=True)
try: server.serve_forever()
finally: model.shutdown(); server.server_close()
