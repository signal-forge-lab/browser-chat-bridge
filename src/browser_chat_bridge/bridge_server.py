from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from .bridge import DEFAULT_MAX_IN_FLIGHT, BridgeService
from .http_json import JsonHandler
from .store import BridgeStore

DEFAULT_BRIDGE_DRIVER_TIMEOUT_S = 960.0


RUN_TURN_RE = re.compile(r"^/v1/runs/([^/]+)/turn$")
RUN_RELEASE_RE = re.compile(r"^/v1/runs/([^/]+)/release-target$")
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


def _driver_release_call(driver_url: str, timeout_s: float, request: dict) -> dict:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        driver_url.rstrip("/") + "/v1/release-target",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict) or not value.get("status"):
        raise RuntimeError("driver returned an invalid release result")
    return value


def _driver_sweep_call(driver_url: str, timeout_s: float, request: dict) -> dict:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        driver_url.rstrip("/") + "/v1/release-orphan-targets",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict) or not value.get("status"):
        raise RuntimeError("driver returned an invalid sweep result")
    return value


def _driver_recovery_call(driver_url: str, timeout_s: float, request: dict) -> dict:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        driver_url.rstrip("/") + "/v1/recover-turn",
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
            raise RuntimeError(f"driver recovery HTTP {exc.code}") from exc
    if not isinstance(value, dict) or not value.get("status"):
        raise RuntimeError("driver returned an invalid recovery result")
    return value


def _recover_after_runtime_refresh(
    browser_url: str,
    driver_url: str,
    runtime_timeout_s: float,
    driver_timeout_s: float,
    request: dict,
) -> dict:
    """Refresh the Driver binding before read-only post-dispatch recovery."""
    _ensure_runtime(browser_url, driver_url, runtime_timeout_s)
    return _driver_recovery_call(driver_url, driver_timeout_s, request)


def _post_json(url: str, timeout_s: float, request: dict) -> dict:
    body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"runtime HTTP {exc.code}") from exc
    if not isinstance(value, dict) or value.get("ok") is not True:
        raise RuntimeError("runtime returned an invalid result")
    return value


def _ensure_runtime(browser_url: str, driver_url: str, timeout_s: float) -> str:
    browser = _post_json(browser_url.rstrip("/") + "/ensure", timeout_s, {})
    endpoint = str(browser.get("cdp_endpoint") or "").strip()
    if not endpoint:
        raise RuntimeError("browser host did not return a CDP endpoint")
    rebound = _post_json(
        driver_url.rstrip("/") + "/v1/rebind",
        timeout_s,
        {"cdp_endpoint": endpoint},
    )
    if str(rebound.get("cdp_endpoint") or "").rstrip("/") != endpoint.rstrip("/"):
        raise RuntimeError("driver did not bind the ensured CDP endpoint")
    return endpoint


class BridgeHandler(JsonHandler):
    service: BridgeService
    browser_url: str
    driver_url: str
    driver_timeout_s: float
    runtime_timeout_s: float

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json(
                200,
                {
                    "ok": True,
                    "driver_url": self.driver_url,
                    "browser_url": self.browser_url,
                    "lazy_browser": True,
                    "max_in_flight": self.service.max_in_flight,
                },
            )
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        release_match = RUN_RELEASE_RE.fullmatch(self.path)
        if release_match is not None:
            run_id = release_match.group(1)
            try:
                _ensure_runtime(self.browser_url, self.driver_url, self.runtime_timeout_s)
                result = self.service.release_run_target(
                    run_id,
                    lambda request: _driver_release_call(self.driver_url, self.runtime_timeout_s, request),
                )
            except ValueError as exc:
                self.send_json(400, {"status": "RELEASE_FAILED", "error": str(exc)})
                return
            self.send_json(200, result)
            return
        if self.path == "/v1/release-orphan-targets":
            try:
                _ensure_runtime(self.browser_url, self.driver_url, self.runtime_timeout_s)
                result = self.service.sweep_orphan_targets(
                    lambda request: _driver_sweep_call(self.driver_url, self.runtime_timeout_s, request)
                )
            except ValueError as exc:
                self.send_json(400, {"status": "RELEASE_FAILED", "error": str(exc)})
                return
            self.send_json(200, result)
            return
        match = RUN_TURN_RE.fullmatch(self.path)
        if match is None:
            self.send_json(404, {"error": "not found"})
            return
        try:
            body = self.read_json()
            run_id = match.group(1)
            request_id = str(body.get("request_id") or "")
            prompt = str(body.get("prompt") or "")
            response_timeout_raw = body.get("response_timeout_seconds")
            response_timeout_seconds = (
                None if response_timeout_raw is None else float(response_timeout_raw)
            )

            def ensure_runtime() -> None:
                _ensure_runtime(
                    self.browser_url,
                    self.driver_url,
                    self.runtime_timeout_s,
                )

            result = self.service.run_turn(
                run_id,
                request_id,
                prompt,
                lambda request: _driver_call(self.driver_url, self.driver_timeout_s, request),
                before_dispatch=ensure_runtime,
                recovery_call=lambda request: _recover_after_runtime_refresh(
                    self.browser_url,
                    self.driver_url,
                    self.runtime_timeout_s,
                    self.driver_timeout_s,
                    request,
                ),
                response_timeout_seconds=response_timeout_seconds,
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
            def cleanup_driver(request: dict) -> dict:
                _ensure_runtime(self.browser_url, self.driver_url, self.runtime_timeout_s)
                return _driver_cleanup_call(self.driver_url, self.driver_timeout_s, request)

            result = self.service.cleanup_run(
                run_id,
                cleanup_driver,
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
    browser_url = os.environ.get("CHAT_BRIDGE_BROWSER_URL", "http://127.0.0.1:8764")
    driver_url = os.environ.get("CHAT_BRIDGE_DRIVER_URL", "http://127.0.0.1:8766")
    driver_timeout = float(
        os.environ.get(
            "CHAT_BRIDGE_DRIVER_TIMEOUT_S",
            str(DEFAULT_BRIDGE_DRIVER_TIMEOUT_S),
        )
    )
    runtime_timeout = float(os.environ.get("CHAT_BRIDGE_RUNTIME_TIMEOUT_S", "30"))
    max_in_flight = int(os.environ.get("CHAT_BRIDGE_MAX_IN_FLIGHT", str(DEFAULT_MAX_IN_FLIGHT)))

    BridgeHandler.service = BridgeService(BridgeStore(db_path), max_in_flight=max_in_flight)
    BridgeHandler.browser_url = browser_url
    BridgeHandler.driver_url = driver_url
    BridgeHandler.driver_timeout_s = driver_timeout
    BridgeHandler.runtime_timeout_s = runtime_timeout
    ThreadingHTTPServer((host, port), BridgeHandler).serve_forever()


if __name__ == "__main__":
    main()

