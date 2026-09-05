from __future__ import annotations

import ipaddress
import os
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

from .browser_host import endpoint_alive
from .gemini import GeminiDriver
from .http_json import JsonHandler


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _validate_cdp_endpoint(endpoint: str) -> str:
    candidate = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        raise ValueError("cdp_endpoint must be an HTTP(S) loopback endpoint with an explicit port")
    if not _loopback(parsed.hostname):
        raise ValueError("cdp_endpoint must be loopback")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("cdp_endpoint must be a bare loopback origin")
    return candidate


class DriverRuntime:
    """Hot-swappable GeminiDriver binding for lazy browser startup."""

    def __init__(
        self,
        *,
        backend_kind: str,
        promotion_timeout_s: float,
        response_timeout_s: float,
        initial_endpoint: str | None = None,
    ):
        self.backend_kind = backend_kind
        self.promotion_timeout_s = promotion_timeout_s
        self.response_timeout_s = response_timeout_s
        self._lock = threading.Lock()
        self._driver: GeminiDriver | None = None
        self._cdp_endpoint = ""
        if initial_endpoint:
            self.rebind(initial_endpoint)

    @property
    def driver(self) -> GeminiDriver | None:
        with self._lock:
            return self._driver

    @property
    def cdp_endpoint(self) -> str:
        with self._lock:
            return self._cdp_endpoint

    @property
    def ready(self) -> bool:
        endpoint = self.cdp_endpoint
        return bool(endpoint) and endpoint_alive(endpoint)

    def rebind(self, endpoint: str) -> bool:
        candidate = _validate_cdp_endpoint(endpoint)
        with self._lock:
            if self._driver is not None and self._cdp_endpoint == candidate:
                return False
            driver = GeminiDriver(
                candidate,
                backend_kind=self.backend_kind,
                promotion_timeout_s=self.promotion_timeout_s,
                response_timeout_s=self.response_timeout_s,
            )
            self._driver = driver
            self._cdp_endpoint = candidate
            return True


class DriverHandler(JsonHandler):
    runtime: DriverRuntime
    backend_kind: str

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "ready": self.runtime.ready,
                    "runtime_rebind": True,
                    "backend": self.backend_kind,
                    "cdp_endpoint": self.runtime.cdp_endpoint or None,
                    "fixed_model": "Gemini 3.8 Flash / 強化版思考モード",
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/v1/rebind":
            try:
                request = self.read_json()
                endpoint = str(request.get("cdp_endpoint") or "")
                changed = self.runtime.rebind(endpoint)
            except ValueError as exc:
                self.send_json(400, {"ok": False, "error": str(exc)})
                return
            self.send_json(
                200,
                {
                    "ok": True,
                    "ready": True,
                    "changed": changed,
                    "cdp_endpoint": self.runtime.cdp_endpoint,
                },
            )
            return
        if self.path == "/v1/delete-conversation":
            try:
                request = self.read_json()
                driver = self.runtime.driver
                if driver is None:
                    self.send_json(503, {"status": "DELETE_FAILED", "error": "driver is not bound to a browser"})
                    return
                result = driver.delete_conversation(request)
            except ValueError as exc:
                self.send_json(400, {"status": "DELETE_FAILED", "error": str(exc)})
                return
            except Exception as exc:
                self.send_json(500, {"status": "DELETE_FAILED", "error": type(exc).__name__})
                return
            self.send_json(200, result)
            return
        if self.path != "/v1/turn":
            self.send_json(404, {"error": "not found"})
            return
        try:
            request = self.read_json()
            driver = self.runtime.driver
            if driver is None:
                self.send_json(503, {"status": "NOT_DISPATCHED", "error": "driver is not bound to a browser"})
                return
            result = driver.run_turn(request)
        except ValueError as exc:
            self.send_json(400, {"status": "NOT_DISPATCHED", "error": str(exc)})
            return
        except Exception as exc:
            # An uncaught failure at this layer may have crossed click(), so do
            # not tell the Bridge it is safe to resend.
            self.send_json(500, {"status": "AMBIGUOUS", "error": type(exc).__name__})
            return
        self.send_json(200, result)


def main() -> None:
    host = os.environ.get("CHAT_DRIVER_HOST", "127.0.0.1")
    if not _loopback(host):
        raise SystemExit("CHAT_DRIVER_HOST must be loopback")
    port = int(os.environ.get("CHAT_DRIVER_PORT", "8766"))
    endpoint = os.environ.get("CHAT_DRIVER_CDP_ENDPOINT", "").strip()
    backend = os.environ.get("CHAT_DRIVER_BACKEND", "chromium").strip().lower()
    if backend not in {"chromium", "obscura"}:
        raise SystemExit("CHAT_DRIVER_BACKEND must be chromium or obscura")
    promotion_timeout = float(os.environ.get("CHAT_DRIVER_PROMOTION_TIMEOUT_S", "120"))
    timeout = float(os.environ.get("CHAT_DRIVER_RESPONSE_TIMEOUT_S", "360"))

    DriverHandler.runtime = DriverRuntime(
        backend_kind=backend,
        promotion_timeout_s=promotion_timeout,
        response_timeout_s=timeout,
        initial_endpoint=endpoint or None,
    )
    DriverHandler.backend_kind = backend
    ThreadingHTTPServer((host, port), DriverHandler).serve_forever()


if __name__ == "__main__":
    main()

