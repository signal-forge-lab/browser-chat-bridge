from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from .bridge import BridgeService
from .http_json import JsonHandler
from .store import BridgeStore


RUN_TURN_RE = re.compile(r"^/v1/runs/([^/]+)/turn$")
RUN_RE = re.compile(r"^/v1/runs/([^/]+)$")


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def _driver_call(driver_url: str, timeout_s: float, request: dict) -> dict:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        driver_url.rstrip("/") + "/v1/turn",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise RuntimeError(f"driver HTTP {exc.code}") from exc
    if not isinstance(value, dict) or not value.get("status"):
        raise RuntimeError("driver returned an invalid result")
    return value


def _driver_cleanup_call(driver_url: str, timeout_s: float, request: dict) -> dict:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        driver_url.rstrip("/") + "/v1/delete-conversation",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            value = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise RuntimeError(f"driver cleanup HTTP {exc.code}") from exc
    if not isinstance(value, dict) or not value.get("status"):
        raise RuntimeError("driver returned an invalid cleanup result")
    return value


class BridgeHandler(JsonHandler):
    service: BridgeService
    driver_url: str
    driver_timeout_s: float

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(200, {"ok": True, "driver_url": self.driver_url})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        match = RUN_TURN_RE.fullmatch(self.path)
        if match is None:
            self.send_json(404, {"error": "not found"})
            return
        try:
            body = self.read_json()
            run_id = match.group(1)
            request_id = str(body.get("request_id") or "")
            prompt = str(body.get("prompt") or "")
            result = self.service.run_turn(
                run_id,
                request_id,
                prompt,
                lambda request: _driver_call(self.driver_url, self.driver_timeout_s, request),
            )
        except ValueError as exc:
            self.send_json(409, {"status": "REQUEST_CONFLICT", "error": str(exc)})
            return
        self.send_json(200, result)

    def do_DELETE(self) -> None:
        match = RUN_RE.fullmatch(self.path)
        if match is None:
            self.send_json(404, {"error": "not found"})
            return
        run_id = match.group(1)
        try:
            result = self.service.cleanup_run(
                run_id,
                lambda request: _driver_cleanup_call(
                    self.driver_url,
                    self.driver_timeout_s,
                    request,
                ),
            )
        except ValueError as exc:
            self.send_json(400, {"status": "DELETE_FAILED", "error": str(exc)})
            return
        self.send_json(200, result)


def main() -> None:
    host = os.environ.get("CHAT_BRIDGE_HOST", "127.0.0.1")
    if not _loopback(host):
        raise SystemExit("CHAT_BRIDGE_HOST must be loopback")
    port = int(os.environ.get("CHAT_BRIDGE_PORT", "8765"))
    db_path = Path(os.environ.get("CHAT_BRIDGE_DB", ".runtime/bridge.sqlite3"))
    driver_url = os.environ.get("CHAT_BRIDGE_DRIVER_URL", "http://127.0.0.1:8766")
    driver_timeout = float(os.environ.get("CHAT_BRIDGE_DRIVER_TIMEOUT_S", "420"))

    BridgeHandler.service = BridgeService(BridgeStore(db_path))
    BridgeHandler.driver_url = driver_url
    BridgeHandler.driver_timeout_s = driver_timeout
    ThreadingHTTPServer((host, port), BridgeHandler).serve_forever()


if __name__ == "__main__":
    main()

