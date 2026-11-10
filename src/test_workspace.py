import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, AsyncMock
from unittest.mock import MagicMock
from types import SimpleNamespace

import app
import resources
from bot.config import Config, DeepSeekConfig, ImageGenConfig
from bot.llm import DeepSeekClient
from bot.imagegen import generate_prepared, ImageGenError


class WorkspaceTests(unittest.TestCase):
    def test_model_uses_system_proxy_and_keeps_loopback_direct(self):
        import aiohttp
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                paths.append(self.path)
                payload = b'{"choices":[{"message":{"content":"ok"}}]}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        local = f'http://127.0.0.1:{server.server_port}'
        async def run():
            async with aiohttp.ClientSession() as session:
                cfg = DeepSeekConfig(api_key='offline-fixture', base_url='http://model.example.invalid/v1', timeout=3)
                with patch('bot.llm.getproxies', return_value={'http': local}), patch('bot.llm.proxy_bypass', return_value=False):
                    response = await DeepSeekClient(cfg, session)._post({'messages': []})
                    self.assertEqual(response['choices'][0]['message']['content'], 'ok')
                    cfg.base_url = local
                    with patch('bot.llm.getproxies', return_value={'http': 'http://127.0.0.1:1'}):
                        await DeepSeekClient(cfg, session)._post({'messages': []})
        try:
            asyncio.run(run())
            self.assertEqual(paths, ['http://model.example.invalid/v1/chat/completions', '/chat/completions'])
        finally:
            server.shutdown(); server.server_close()

    def test_enabled_plugin_is_prepared_before_engine_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = app.Controller(Path(temp))
            controller.settings['plugins']['comfyui']['enabled'] = True
            completed = MagicMock()
            completed.is_alive.return_value = False
            controller.resources.thread = completed
            with patch('app.validate_model_key', return_value='ok'), patch.object(controller.resources, 'install') as install, patch.object(controller.resources, 'snapshot', return_value={'job': {'phase': 'done'}}), patch.object(controller, 'launch_engine', side_effect=InterruptedError) as launch:
                controller.start('offline-fixture-key')
                controller.worker.join(2)
                install.assert_called_once_with('comfyui', 'http://127.0.0.1:8188')
                launch.assert_called_once()
            self.assertEqual(controller.snapshot()['stage'], 'idle')

    def test_remove_all_bots_preserves_files_and_stays_empty_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            controller = app.Controller(root)
            history = root / 'data/history.json'
            history.parent.mkdir(); history.write_text('preserve fixture')
            controller.save_bot({'name': '临时', 'persona': '测试'})
            temporary = controller.selected_bot()['id']
            controller.remove_bot(temporary)
            self.assertEqual(controller.selected_bot()['id'], 'taotao')
            controller.remove_bot('taotao')
            self.assertIsNone(controller.snapshot()['bot'])
            loaded = app.Controller(root)
            self.assertEqual(loaded.snapshot()['bots'], [])
            self.assertEqual(history.read_text(), 'preserve fixture')
            with self.assertRaises(ValueError): loaded.start('fixture-key')
            loaded.save_bot({'name': '新的', 'persona': ''})
            self.assertEqual(loaded.selected_bot()['name'], '新的')

    def test_plugin_removal_is_persistent_and_disables_engine(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = app.Controller(Path(temp))
            controller.settings['plugins']['comfyui']['enabled'] = True
            controller.change_plugin('comfyui', True)
            loaded = app.Controller(Path(temp))
            self.assertTrue(loaded.snapshot()['plugins']['comfyui']['removed'])
            self.assertFalse(loaded.snapshot()['plugins']['comfyui']['enabled'])
            loaded.change_plugin('comfyui', False)
            self.assertFalse(loaded.snapshot()['plugins']['comfyui']['removed'])
            loaded.settings['onebot']['ws_url'] = ''
            with self.assertRaisesRegex(ValueError, 'WebSocket'): loaded.start('fixture-key')

    def test_bots_keep_separate_profiles_and_persist_personas(self):
        with tempfile.TemporaryDirectory() as temp, patch('resources.installed_voice', return_value=''):
            root = Path(temp)
            controller = app.Controller(root)
            controller.save_bot({'name': '小火车', 'persona': '说话简短，喜欢旅行。'})
            first = controller.selected_bot()['id']
            self.assertTrue(controller.directory.is_relative_to(root.resolve() / 'bots'))
            controller.setup()
            cfg = Config.load(controller.directory / 'config.json')
            self.assertEqual(cfg.bot.self_names, ['小火车'])
            self.assertIn('喜欢旅行', cfg.bot.system_prompt)
            controller.save_bot({'name': '第二个', 'persona': '安静。'})
            self.assertNotEqual(first, controller.selected_bot()['id'])
            controller.select_bot(first)
            loaded = app.Controller(root)
            self.assertEqual(loaded.selected_bot()['name'], '小火车')
            self.assertEqual(loaded.selected_bot()['persona'], '说话简短，喜欢旅行。')
            self.assertNotEqual(loaded.directory, root)

    def test_custom_provider_preserves_secret_and_does_not_send_deepseek_fields(self):
        with tempfile.TemporaryDirectory() as temp, patch('resources.installed_voice', return_value=''):
            controller = app.Controller(Path(temp))
            key = 'fake-custom-key-for-local-tests'
            custom = {'provider': '自定义', 'base_url': 'https://api.example.com/v1', 'model': 'custom-model', 'key': key}
            with patch('app.validate_model_key', return_value='ok'):
                controller.save_model(custom)
            self.assertNotIn(key, json.dumps(controller.snapshot()))
            self.assertNotIn(key, controller.settings_path.read_text('utf-8'))
            self.assertEqual(controller.snapshot()['model']['model'], 'custom-model')
            cfg = DeepSeekConfig(api_key=key, base_url=custom['base_url'], model=custom['model'])
            payload = DeepSeekClient(cfg, None)._build_payload([{'role': 'user', 'content': 'hi'}], None)
            self.assertNotIn('thinking', payload)
            self.assertNotIn('reasoning_effort', payload)
            changed = {**custom, 'base_url': 'https://another.example.com/v1', 'key': ''}
            with patch('app.validate_model_key') as validate:
                with self.assertRaises(ValueError):
                    controller.save_model(changed)
                validate.assert_not_called()
            self.assertEqual(controller.snapshot()['model']['base_url'], custom['base_url'])

    def test_plugin_configuration_reaches_engine(self):
        plugins = {'comfyui': {'enabled': True, 'url': 'http://127.0.0.1:8189', 'checkpoint': 'demo.safetensors'},
                   'voicestudio': {'enabled': True, 'url': 'http://localhost:3900', 'voice': 'voice-a', 'model': 'engine-a'}}
        with tempfile.TemporaryDirectory() as temp, patch('resources.installed_voice', return_value=''):
            controller = app.Controller(Path(temp))
            controller.save_plugins(plugins)
            controller.setup()
            cfg = Config.load(Path(temp) / 'config.json')
            self.assertTrue(cfg.imagegen.local_only)
            self.assertEqual(cfg.imagegen.comfyui_checkpoint, 'demo.safetensors')
            self.assertEqual(cfg.tts.voice, 'voice-a')
            self.assertEqual(cfg.tts.base_url, 'http://localhost:3900')
            self.assertTrue(cfg.tts.release_comfyui)

    def test_endpoint_validation(self):
        for value in ['http://api.example.com', 'https://user:key@example.com', 'https://example.com?key=x', 'file:///tmp/a']:
            with self.assertRaises(ValueError): app.endpoint(value)
        self.assertEqual(app.endpoint('https://example.com/v1/'), 'https://example.com/v1')
        with self.assertRaises(ValueError): app.endpoint('https://example.com', local_only=True)

    def test_local_only_drawing_never_falls_back_online(self):
        cfg = ImageGenConfig(local_only=True, comfyui_enable=True)
        with patch('bot.imagegen.probe_comfyui', new=AsyncMock(return_value=False)), patch('bot.imagegen.generate', new=AsyncMock()) as online:
            with self.assertRaises(ImageGenError): asyncio.run(generate_prepared(None, cfg, '一只猫'))
            online.assert_not_called()

    def test_key_is_not_forwarded_to_redirected_host(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header('Location', 'https://another.example.com/models')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(ValueError): app.validate_model_key({'base_url': f'http://127.0.0.1:{server.server_port}'}, 'fake-key')
            self.assertEqual(calls, ['/models'])
        finally:
            server.shutdown(); server.server_close()


class ResourceTests(unittest.TestCase):
    def test_incomplete_managed_install_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            incomplete = folder / 'comfyui/0.44.0.staging/ComfyUI_windows_portable'
            incomplete.mkdir(parents=True)
            scanner = self.local_scanner
            with patch('resources.comfy_record', side_effect=lambda path: {'entry': 'partial'} if path == incomplete else None):
                self.assertIsNone(scanner(folder))
                complete = folder / 'comfyui/0.44.0/ComfyUI_windows_portable'
                incomplete.parent.rename(complete.parent)
                with patch('resources.comfy_record', side_effect=lambda path: {'entry': 'ready'} if path == complete else None):
                    self.assertEqual(scanner(folder)['entry'], 'ready')

    def test_external_apps_do_not_inherit_frozen_dll_directory(self):
        with patch.object(resources.sys, 'frozen', True, create=True), patch.object(resources.sys, '_MEIPASS', 'frozen-runtime', create=True), patch('resources.ctypes.windll.kernel32.SetDllDirectoryW') as dll, patch('resources.subprocess.Popen', side_effect=OSError):
            with self.assertRaises(OSError): resources.external_process(['fixture.exe'])
            self.assertEqual([call.args[0] for call in dll.call_args_list], [None, 'frozen-runtime'])

    def setUp(self):
        self.local_scanner = resources.installed_comfy
        scanner = patch('resources.installed_comfy', return_value=None)
        scanner.start()
        self.addCleanup(scanner.stop)

    def test_local_source_and_portable_are_found_without_downloading(self):
        with tempfile.TemporaryDirectory() as temp, patch('resources.installed_voice', return_value=''):
            root = Path(temp)
            for kind in ('source', 'portable'):
                base = root / kind
                source = base if kind == 'source' else base / 'ComfyUI'
                source.mkdir(parents=True)
                (source / 'main.py').write_text('# fixture')
                (source / 'nodes.py').write_text('# fixture')
                (source / 'start_comfyui.bat').write_text('python main.py --fp8_e4m3fn-unet --reserve-vram 1.5')
                python = base / ('venv/Scripts/python.exe' if kind == 'source' else 'python_embeded/python.exe')
                python.parent.mkdir(parents=True); python.write_bytes(b'fixture')
                record = resources.comfy_record(base)
                self.assertEqual(record['portable'], kind == 'portable')
                self.assertEqual(record['args'], ['--fp8_e4m3fn-unet', '--reserve-vram', '1.5'])
                manager = resources.Resources(root / ('app-' + kind))
                with patch('resources.installed_comfy', return_value=record), patch('resources.download') as download:
                    manager._install('comfyui')
                    download.assert_not_called()
                    self.assertEqual(manager.snapshot()['job']['phase'], 'done')
                manager.remove('comfyui')
                loaded = resources.Resources(root / ('app-' + kind))
                self.assertFalse(loaded.snapshot()['installed']['comfyui']['installed'])
                self.assertTrue(python.is_file())
                loaded.scan('comfyui')
                self.assertTrue(loaded.snapshot()['installed']['comfyui']['installed'])

    def test_running_service_is_reused_and_missing_resource_installs_then_starts(self):
        with tempfile.TemporaryDirectory() as temp, patch('resources.installed_voice', return_value=''):
            manager = resources.Resources(Path(temp))
            with patch.object(manager, 'probe', return_value={'connected': True}), patch('resources.download') as download:
                manager._install('comfyui', 'http://127.0.0.1:8188')
                download.assert_not_called()
                self.assertEqual(manager.snapshot()['job']['phase'], 'done')
            entry = Path(temp) / 'existing/VoiceStudio.exe'
            entry.parent.mkdir(); entry.write_bytes(b'existing')
            with patch('resources.installed_voice', return_value=str(entry)), patch.object(manager, 'probe', side_effect=OSError), patch.object(manager, 'start', return_value={'message': 'started'}) as start, patch('resources.download') as download:
                manager._install('voicestudio', 'http://127.0.0.1:3900')
                download.assert_not_called()
                start.assert_called_once()

    def test_download_resume_and_integrity(self):
        class Response(io.BytesIO):
            status = 206
            headers = {'Content-Range': 'bytes 3-5/6'}
        content = b'abcdef'
        package = {'url': 'https://example.com/test', 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'archive.7z'
            target.with_suffix('.7z.part').write_bytes(content[:3])
            with patch('resources.urllib.request.urlopen', return_value=Response(content[3:])):
                resources.download(package, target, threading.Event(), lambda _: None)
            self.assertEqual(target.read_bytes(), content)
            with patch('resources.urllib.request.urlopen') as request:
                resources.download(package, target, threading.Event(), lambda _: None)
                request.assert_not_called()

    def test_comfy_archive_install_and_path_guard(self):
        import py7zr
        with tempfile.TemporaryDirectory() as temp, patch('resources.installed_voice', return_value=''):
            root = Path(temp)
            archive = root / 'fixture.7z'
            with py7zr.SevenZipFile(archive, 'w') as bundle:
                bundle.writestr(b'not-an-executable', 'ComfyUI_windows_portable/python_embeded/python.exe')
                bundle.writestr(b'# fixture', 'ComfyUI_windows_portable/ComfyUI/main.py')
            manager = resources.Resources(root / 'app')
            def copy_fixture(package, destination, *args):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read_bytes())
            with patch('resources.download', side_effect=copy_fixture), patch.object(manager, 'probe', side_effect=OSError), patch.object(manager, 'start', return_value={'message': 'started'}) as start:
                manager._install('comfyui', 'http://127.0.0.1:8188')
                start.assert_called_once_with('comfyui', 'http://127.0.0.1:8188')
            record = manager.snapshot()['installed']['comfyui']
            self.assertTrue(record['installed'])
            self.assertTrue(Path(record['entry']).is_relative_to(manager.folder))
            self.assertEqual(manager.snapshot()['job']['phase'], 'done')
            bundle = MagicMock()
            bundle.list.return_value = [SimpleNamespace(filename='../outside.txt', is_symlink=False, uncompressed=1)]
            context = MagicMock(); context.__enter__.return_value = bundle
            with patch('py7zr.SevenZipFile', return_value=context):
                with self.assertRaises(ValueError): resources.extract_7z(root / 'bad.7z', root / 'extract')
            bundle.extractall.assert_not_called()
            self.assertFalse((root / 'outside.txt').exists())

    def test_existing_voice_is_never_reinstalled_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = root / 'existing/VoiceStudio.exe'
            installed.parent.mkdir(); installed.write_bytes(b'existing-custom-build')
            with patch('resources.installed_voice', return_value=str(installed)), patch('resources.download') as download, patch('resources.subprocess.Popen') as execute:
                manager = resources.Resources(root / 'app')
                manager._install('voicestudio')
                download.assert_not_called(); execute.assert_not_called()
                self.assertTrue(manager.snapshot()['installed']['voicestudio']['installed'])
            self.assertEqual(installed.read_bytes(), b'existing-custom-build')


if __name__ == '__main__': unittest.main()
