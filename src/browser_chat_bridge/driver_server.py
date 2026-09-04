from __future__ import annotations

import ipaddress
import os
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

from .gemini import GeminiDriver
from .http_json import JsonHandler


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


class DriverHandler(JsonHandler):
    driver: GeminiDriver
    backend_kind: str

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "backend": self.backend_kind,
                    "cdp_endpoint": self.driver.cdp_endpoint,
                    "fixed_model": "Gemini 3.8 Flash / 強化版思考モード",
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/v1/delete-conversation":
            try:
                request = self.read_json()
                result = self.driver.delete_conversation(request)
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
            result = self.driver.run_turn(request)
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
    endpoint = os.environ.get("CHAT_DRIVER_CDP_ENDPOINT", "http://127.0.0.1:51881")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SystemExit("CHAT_DRIVER_CDP_ENDPOINT must be an HTTP(S) CDP endpoint")
    backend = os.environ.get("CHAT_DRIVER_BACKEND", "chromium").strip().lower()
    if backend not in {"chromium", "obscura"}:
        raise SystemExit("CHAT_DRIVER_BACKEND must be chromium or obscura")
    promotion_timeout = float(os.environ.get("CHAT_DRIVER_PROMOTION_TIMEOUT_S", "120"))
    timeout = float(os.environ.get("CHAT_DRIVER_RESPONSE_TIMEOUT_S", "360"))

    DriverHandler.driver = GeminiDriver(
        endpoint,
        backend_kind=backend,
        promotion_timeout_s=promotion_timeout,
        response_timeout_s=timeout,
    )
    DriverHandler.backend_kind = backend
    ThreadingHTTPServer((host, port), DriverHandler).serve_forever()


if __name__ == "__main__":
    main()

