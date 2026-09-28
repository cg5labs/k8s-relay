"""JSON logging and the /healthz and /readyz HTTP endpoints."""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .metrics import SchedulerMetrics

_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


StatusFn = Callable[[], tuple[bool, bool, dict]]


def make_handler(
    status: StatusFn, metrics: SchedulerMetrics | None = None
) -> type[BaseHTTPRequestHandler]:
    """``status`` returns ``(live, ready, details)``."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/metrics":
                live, ready, details = status()
                if metrics:
                    metrics.update_health(
                        live,
                        ready,
                        bool(details.get("leader", False)),
                        bool(details.get("audit_database_ready", True)),
                    )
                body = metrics.render() if metrics else b""
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/plain; version=0.0.4; charset=utf-8",
                )
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            live, ready, details = status()
            if self.path == "/healthz":
                ok = live
            elif self.path == "/readyz":
                ok = ready
            else:
                self.send_error(404)
                return
            body = json.dumps({"ok": ok, **details}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def start_health_server(
    port: int,
    status: StatusFn,
    host: str = "0.0.0.0",
    metrics: SchedulerMetrics | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(status, metrics))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    return server
