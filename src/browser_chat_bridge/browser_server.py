from __future__ import annotations

import ipaddress
import os
import threading
from http.server import ThreadingHTTPServer

from .browser_host import BrowserHostError, NodriverManagedEdge, default_profile_dir
from .http_json import JsonHandler


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class BrowserHandler(JsonHandler):
    managed: NodriverManagedEdge

    def do_GET(self) -> None:
        if self.path == "/health":
            ready = self.managed.healthy
            self.send_json(
                200,
                {
                    "ok": True,
                    "ready": ready,
                    "lazy_start": True,
                    "backend": "nodriver",
                    "browser": "edge",
                    "cdp_endpoint": self.managed.endpoint if ready else None,
                    "profile_dir": str(self.managed.profile_dir),
                    "attached_existing": self.managed.attached_existing,
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/ensure":
            try:
                self.read_json()
                endpoint = self.managed.ensure()
            except (ValueError, BrowserHostError) as exc:
                self.send_json(503, {"ok": False, "error": str(exc)})
                return
            self.send_json(
                200,
                {
                    "ok": True,
                    "ready": True,
                    "cdp_endpoint": endpoint,
                    "started_on_demand": True,
                },
            )
            return
        if self.path != "/shutdown":
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, {"ok": True})
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main() -> None:
    host = os.environ.get("CHAT_BROWSER_HOST", "127.0.0.1")
    if not _loopback(host):
        raise SystemExit("CHAT_BROWSER_HOST must be loopback")
    port = int(os.environ.get("CHAT_BROWSER_PORT", "8764"))
    managed = NodriverManagedEdge(
        profile_dir=default_profile_dir(),
        browser_executable=os.environ.get("CHAT_BROWSER_EDGE_EXECUTABLE"),
        attach_endpoint=os.environ.get("CHAT_BROWSER_ATTACH_ENDPOINT"),
    )
    BrowserHandler.managed = managed
    server = ThreadingHTTPServer((host, port), BrowserHandler)
    try:
        server.serve_forever()
    finally:
        managed.stop()


if __name__ == "__main__":
    main()
