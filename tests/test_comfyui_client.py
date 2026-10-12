import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'asset-gen/tools'))
from comfyui_client import Backend
import comfyui_gen as gen


class TransportTests(unittest.TestCase):
    def test_every_business_request_is_authenticated(self):
        seen = []
        class API:
            def open(self, req, timeout):
                seen.append(req)
                path = req.full_url.split('example.test', 1)[1]
                data = {'name':'uploaded.png'} if path == '/upload/image' else {'ok': True}
                return io.BytesIO(b'output bytes' if path.startswith('/view?') else json.dumps(data).encode())
        with patch.dict(os.environ, {'COMFYUI_BASE_URL':'https://example.test','COMFYUI_API_KEY':'test-key'}, clear=True):
            client = Backend();client.opener = API()
            gen._get_json(client, '/queue')
            gen._post_json(client, '/prompt', {'prompt':{}})
            with tempfile.TemporaryDirectory() as folder:
                image=Path(folder)/'input.png';image.write_bytes(b'input bytes')
                self.assertEqual(gen._upload_image(client,image),'uploaded.png')
            self.assertEqual(gen._fetch(client,{'filename':'out.png','type':'output'}),b'output bytes')
        self.assertEqual(len(seen),4)
        self.assertTrue(all(req.get_header('Authorization')=='Bearer test-key' for req in seen))
        self.assertEqual(json.loads(seen[1].data),{'prompt':{}})
        self.assertIn(b'input bytes',seen[2].data)

    def test_real_http_redirect_does_not_forward_the_key(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location',f'http://127.0.0.1:{self.server.server_port}/must-not-be-followed')
                self.end_headers()
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with patch.dict(os.environ,{'COMFYUI_API_KEY':'test-key'},clear=True):
                client=Backend()
                # Local HTTP fixture only; production configuration requires HTTPS.
                client.url=f'http://127.0.0.1:{server.server_port}'
                with self.assertRaisesRegex(RuntimeError,'redirected'):
                    client.open('/start')
            self.assertEqual(requests,['/start'])
        finally:
            server.shutdown();server.server_close();worker.join()

    def test_reject_insecure_urls_and_exposed_key_files(self):
        with patch.dict(os.environ,{'COMFYUI_BASE_URL':'http://example.test','COMFYUI_API_KEY':'test-key'},clear=True):
            with self.assertRaisesRegex(ValueError,'HTTPS'):Backend()
        with tempfile.TemporaryDirectory() as folder:
            key=Path(folder)/'key';key.write_text('test-key');key.chmod(0o644)
            with patch.dict(os.environ,{'COMFYUI_API_KEY_FILE':str(key)},clear=True):
                with self.assertRaisesRegex(ValueError,'private'):Backend()
                key.chmod(0o600)
                self.assertEqual(Backend().key,'test-key')

if __name__=='__main__':unittest.main()
