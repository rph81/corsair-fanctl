"""Minimal HTTP/JSON API and static file server.

Deliberately built on `http.server` so the daemon has no third-party
dependencies: on a Proxmox host the whole thing runs against the stock
`python3` package with nothing installed via pip.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOG = logging.getLogger("fanctl.http")

WEB_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "corsair-fanctl"
    protocol_version = "HTTP/1.1"

    # injected by make_server()
    controller = None
    auth_token = None
    web_root = WEB_ROOT

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
        LOG.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed

    def _authorized(self, query: dict) -> bool:
        if not self.auth_token:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:] == self.auth_token:
            return True
        if self.headers.get("X-Auth-Token") == self.auth_token:
            return True
        return query.get("token", [None])[0] == self.auth_token

    # -- routing ----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib signature
        self._dispatch("GET")

    def do_HEAD(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self):  # noqa: N802
        self._dispatch("PUT")

    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/healthz":
            self._json({"ok": True})
            return

        if not self._authorized(query):
            self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid token")
            return

        try:
            if path.startswith("/api/"):
                self._api(method, path, query)
            elif method == "GET":
                self._static(path)
            else:
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, f"{method} not allowed on {path}")
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            LOG.exception("error handling %s %s", method, path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _api(self, method: str, path: str, query: dict):
        controller = self.controller
        parts = path.strip("/").split("/")[1:]  # drop "api"

        if parts == ["state"] and method == "GET":
            self._json(controller.snapshot())
            return

        if parts == ["history"] and method == "GET":
            since = float(query.get("since", ["0"])[0] or 0)
            points = int(query.get("points", ["600"])[0] or 600)
            self._json({"samples": controller.history(since, max(10, min(2000, points)))})
            return

        if parts == ["config"]:
            if method == "GET":
                self._json(controller.config)
                return
            if method == "PUT":
                self._json(controller.update_config(self._body()))
                return

        if len(parts) == 2 and parts[0] == "fan" and method == "POST":
            index = self._fan_index(parts[1])
            self._json(controller.patch_fan(index, self._body()))
            return

        if len(parts) == 2 and parts[0] == "identify" and method == "POST":
            index = self._fan_index(parts[1])
            body = self._body()
            controller.identify(
                index,
                duty=float(body.get("duty", 100)),
                seconds=float(body.get("seconds", 5)),
            )
            self._json({"ok": True})
            return

        if parts == ["storage", "refresh"] and method == "POST":
            controller.refresh_storage()
            self._json({"ok": True})
            return

        if parts == ["reconnect"] and method == "POST":
            controller.reconnect()
            self._json({"ok": True})
            return

        self._error(HTTPStatus.NOT_FOUND, f"no route for {method} {path}")

    @staticmethod
    def _fan_index(raw: str) -> int:
        try:
            index = int(raw)
        except ValueError as exc:
            raise ValueError(f"invalid fan index: {raw}") from exc
        if not 1 <= index <= 6:
            raise ValueError(f"fan index out of range: {index}")
        return index

    def _static(self, path: str):
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = os.path.normpath(os.path.join(self.web_root, relative))
        if not target.startswith(os.path.abspath(self.web_root) + os.sep):
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if not os.path.isfile(target):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        with open(target, "rb") as handle:
            body = handle.read()
        extension = os.path.splitext(target)[1]
        self._send(200, body, CONTENT_TYPES.get(extension, "application/octet-stream"))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, ipv6: bool):
        if ipv6:
            self.address_family = socket.AF_INET6
        super().__init__(address, handler)


def make_server(controller, bind: str, port: int, auth_token: str | None, web_root: str = WEB_ROOT):
    handler = type("BoundHandler", (Handler,), {
        "controller": controller,
        "auth_token": auth_token,
        "web_root": os.path.abspath(web_root),
    })
    return Server((bind, port), handler, ipv6=":" in bind)


def serve_forever(server) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    thread.start()
    return thread
