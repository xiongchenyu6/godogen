"""Authenticated HTTPS transport for the self-hosted ComfyUI API."""
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://comfy.panda.qzz.io"
DEFAULT_KEY_FILE = Path.home() / ".config/godogen/comfyui-api-key"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a program credential to an SSO or another host.
        return None


class Backend:
    def __init__(self):
        self.url = os.environ.get("COMFYUI_BASE_URL", DEFAULT_URL).rstrip("/")
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("COMFYUI_BASE_URL must be an HTTPS URL without credentials, query or fragment")
        self.key = os.environ.get("COMFYUI_API_KEY", "").strip()
        if not self.key:
            path = Path(os.environ.get("COMFYUI_API_KEY_FILE", str(DEFAULT_KEY_FILE))).expanduser()
            try:
                if path.stat().st_mode & 0o077:
                    raise ValueError("ComfyUI API key file must be private (chmod 600)")
                self.key = path.read_text().strip()
            except FileNotFoundError:
                raise ValueError("Set COMFYUI_API_KEY or COMFYUI_API_KEY_FILE to a program API credential") from None
        if not self.key or any(c.isspace() for c in self.key):
            raise ValueError("ComfyUI API key must be a non-empty single token")
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, path, *, data=None, headers=None, timeout=30):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("ComfyUI request paths must stay on the configured server")
        request_headers = dict(headers or {})
        request_headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.url + path, data=data, headers=request_headers)
        try:
            return self.opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                error.close()
                raise RuntimeError("ComfyUI API redirected; use its HTTPS API endpoint and a program key, not browser SSO") from None
            if error.code in (401, 403):
                error.close()
                raise RuntimeError(f"ComfyUI API authorization denied (HTTP {error.code}); check the program key") from None
            raise

    def __enter__(self):
        with self.open("/system_stats", headers={"Accept": "application/json"}) as response:
            json.load(response)
        return self

    def __exit__(self, *_):
        pass
