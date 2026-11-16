"""Tiny localhost API used by the browser extension (and by a second app instance).

Only accepts requests from 127.0.0.1 that carry the ``X-OpenDM-Client`` header.
Normal web pages cannot send a custom header to another origin without a CORS
preflight, and we never answer preflights, so a random website can't queue downloads.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import APP_NAME, __version__
from .util import is_url

HEADER = "X-OpenDM-Client"
ALLOWED_ORIGIN_PREFIXES = ("chrome-extension://", "moz-extension://", "extension://")


class _Handler(BaseHTTPRequestHandler):
    on_add = None  # callable(dict) -> None
    on_probe = None  # callable(url) -> dict  (video qualities via yt-dlp)

    def log_message(self, *a):
        pass

    def _reply(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trusted(self) -> bool:
        if not self.headers.get(HEADER):
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.startswith(ALLOWED_ORIGIN_PREFIXES)

    def do_OPTIONS(self):
        self._reply(403, {"error": "forbidden"})

    def do_GET(self):
        if not self._trusted():
            return self._reply(403, {"error": "forbidden"})
        if self.path == "/ping":
            return self._reply(200, {"app": APP_NAME, "version": __version__})
        if self.path == "/show":
            type(self).on_add({"action": "show"})
            return self._reply(200, {"ok": True})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._trusted():
            return self._reply(403, {"error": "forbidden"})
        if self.path == "/probe":
            return self._probe()
        if self.path != "/add":
            return self._reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError
            payload = json.loads(self.rfile.read(length) or b"{}")
            urls = payload.get("urls") or [payload.get("url")]
            urls = [u for u in urls if isinstance(u, str) and is_url(u)]
            if not urls:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            return self._reply(400, {"error": "bad request"})
        item = dict(payload)
        item["urls"] = urls
        item["url"] = urls[0]
        if len(urls) > 1:
            item.pop("filename", None)
        type(self).on_add(item)
        self._reply(200, {"ok": True, "count": len(urls)})


def _probe(self):
    try:
        length = int(self.headers.get("Content-Length") or 0)
        url = json.loads(self.rfile.read(min(length, 100_000)) or b"{}").get("url", "")
        if not (isinstance(url, str) and is_url(url)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        return self._reply(400, {"error": "bad request"})
    probe = type(self).on_probe
    if probe is None:
        return self._reply(404, {"error": "not found"})
    try:
        info = probe(url)
    except Exception as e:  # noqa: BLE001 - message goes to the extension's menu
        missing = e.__class__.__name__ == "YtdlpMissing"
        return self._reply(200, {"ok": False, "error": str(e), "missing": missing})
    return self._reply(200, {"ok": True, **info})


_Handler._probe = _probe


class ApiServer:
    def __init__(self, port: int, on_add, on_probe=None):
        handler = type("Handler", (_Handler,), {"on_add": staticmethod(on_add),
                                                "on_probe": staticmethod(on_probe) if on_probe else None})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="api")

    def start(self):
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()


def _port_in_use(port: int) -> bool:
    """Instant check. Connecting to a closed port on Windows takes ~2 s to fail (it retries),
    which used to delay every launch; binding to a free port succeeds immediately."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def send_to_running_instance(port: int, urls: list[str]) -> bool:
    """If OpenDM is already running, hand it the URLs and return True."""
    if not _port_in_use(port):
        return False
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/add",
            data=json.dumps({"urls": urls}).encode() if urls else None,
            headers={HEADER: "cli", "Content-Type": "application/json"},
            method="POST" if urls else "GET",
        )
        if not urls:
            req.full_url = f"http://127.0.0.1:{port}/show"  # just bring the window forward
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def instance_running(port: int) -> bool:
    """True if an OpenDM is already answering on this port."""
    if not _port_in_use(port):
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/ping", headers={HEADER: "cli"})
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False
