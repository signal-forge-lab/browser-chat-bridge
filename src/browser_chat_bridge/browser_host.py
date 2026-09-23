from __future__ import annotations

import asyncio
import atexit
import importlib
import ipaddress
import inspect
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, cast
from urllib.parse import urlparse
from urllib.request import urlopen


class BrowserHostError(RuntimeError):
    pass


def repair_nodriver_network_source(path: Path) -> bool:
    """Repair nodriver 0.50.x's one malformed CP1252 byte in generated CDP source."""
    candidate = Path(path)
    if candidate.name.lower() != "network.py":
        return False
    parts = tuple(part.lower() for part in candidate.parts)
    if len(parts) < 3 or parts[-3:] != ("nodriver", "cdp", "network.py"):
        return False
    raw = candidate.read_bytes()
    broken = b"#: JSON (\xb1Inf)."
    if raw.count(broken) != 1:
        return False
    candidate.write_bytes(raw.replace(broken, b"#: JSON (\xc2\xb1Inf)."))
    return True


def _import_nodriver():
    try:
        return importlib.import_module("nodriver")
    except SyntaxError as exc:
        filename = getattr(exc, "filename", None)
        if not filename or not repair_nodriver_network_source(Path(filename)):
            raise
        # Failed package imports can leave successfully imported siblings in
        # sys.modules. Drop only nodriver's namespace before the one retry.
        for name in [name for name in sys.modules if name == "nodriver" or name.startswith("nodriver.")]:
            sys.modules.pop(name, None)
        return importlib.import_module("nodriver")


def endpoint_alive(endpoint: str, timeout_s: float = 1.0) -> bool:
    parsed = urlparse(str(endpoint or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
        return False
    try:
        if not ipaddress.ip_address(parsed.hostname).is_loopback:
            return False
    except ValueError:
        if parsed.hostname != "localhost":
            return False
    try:
        with urlopen(
            f"{endpoint.rstrip('/')}/json/version",
            timeout=max(0.1, float(timeout_s)),
        ) as response:
            return int(getattr(response, "status", 0)) == 200
    except Exception:
        return False


def resolve_edge_executable(explicit: str | None = None) -> str:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    configured = str(os.environ.get("CHAT_BROWSER_EDGE_EXECUTABLE") or "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    if os.name == "nt":
        candidates.extend(
            Path(value)
            for value in (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe"),
            )
        )
    else:
        for name in ("microsoft-edge", "microsoft-edge-stable"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise BrowserHostError(
        "Browser Chat nodriver host could not find Microsoft Edge; set CHAT_BROWSER_EDGE_EXECUTABLE"
    )


def default_profile_dir() -> Path:
    configured = str(os.environ.get("CHAT_BROWSER_PROFILE") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return (
            Path(os.environ["LOCALAPPDATA"])
            / "BrowserChatBridge"
            / "BrowserChatEdge"
            / "User Data"
        ).resolve()
    return (Path.home() / ".browser-chat-edge" / "User Data").resolve()


class NodriverManagedEdge:
    """Own or reattach one persistent Edge profile and expose its CDP endpoint."""

    def __init__(
        self,
        *,
        profile_dir: Path,
        browser_executable: str | None = None,
        start_url: str = "https://gemini.google.com/spark",
        attach_endpoint: str | None = None,
        start_fn: Callable[..., Any] | None = None,
    ):
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.browser_executable = resolve_edge_executable(browser_executable)
        self.start_url = str(start_url or "https://gemini.google.com/spark")
        self.attach_endpoint = str(attach_endpoint or "").strip()
        self._start_fn = start_fn
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._ensure_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._endpoint = ""
        self._error: BaseException | None = None

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def attached_existing(self) -> bool:
        return bool(self.attach_endpoint)

    @property
    def healthy(self) -> bool:
        return bool(self._endpoint) and self._error is None and endpoint_alive(self._endpoint)

    def ensure(self, timeout_s: float = 20.0) -> str:
        """Return a live CDP endpoint, starting/restarting Edge only on demand."""
        with self._ensure_lock:
            if self.healthy:
                return self._endpoint

            if self._thread is not None:
                self.stop()

            if self.attach_endpoint and not endpoint_alive(self.attach_endpoint):
                # A Safe-Restart attach target can disappear while the host
                # survives. Do not loop forever trying the stale endpoint.
                self.attach_endpoint = ""

            return self.start(timeout_s)

    def start(self, timeout_s: float = 20.0) -> str:
        if self._thread is not None:
            return self._endpoint
        self._ready.clear()
        self._stop.clear()
        self._error = None
        self._endpoint = ""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._thread_main,
            name="browser-chat-nodriver-edge",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(max(0.1, float(timeout_s))):
            self.stop()
            raise BrowserHostError("Browser Chat Edge did not become ready in time")
        if self._error is not None:
            self.stop()
            raise BrowserHostError(f"Browser Chat Edge startup failed: {self._error}") from self._error
        atexit.register(self.stop)
        return self._endpoint

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=10.0)
        self._thread = None
        self._endpoint = ""

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    async def _run(self) -> None:
        start_fn = self._start_fn
        attached_existing = self.attached_existing
        if start_fn is None:
            try:
                uc = _import_nodriver()
            except ModuleNotFoundError as exc:
                raise BrowserHostError(
                    "Browser Chat Edge requires nodriver; install the project dependencies"
                ) from exc
            start_fn = uc.start

        if attached_existing:
            parsed = urlparse(self.attach_endpoint)
            if not parsed.hostname or not parsed.port:
                raise BrowserHostError("CHAT_BROWSER_ATTACH_ENDPOINT must include host and port")
            start_kwargs: dict[str, Any] = {"host": parsed.hostname, "port": parsed.port}
        else:
            start_kwargs = {
                "headless": False,
                "user_data_dir": str(self.profile_dir),
                "browser_executable_path": self.browser_executable,
                "browser_args": [
                    "--no-first-run",
                    "--no-default-browser-check",
                    self.start_url,
                ],
            }

        browser = await start_fn(**start_kwargs)
        try:
            host = str(browser.config.host or "127.0.0.1")
            port = int(browser.config.port)
            self._endpoint = f"http://{host}:{port}"

            # Browser Host only needs this nodriver connection for launch or
            # reattach. Detach it while leaving Edge itself running so Driver
            # can attach its own short-lived nodriver CDP client per operation.
            for tab in list(getattr(browser, "tabs", ())):
                close = getattr(tab, "aclose", None)
                if callable(close):
                    try:
                        close_result = close()
                        if inspect.isawaitable(close_result):
                            await cast(Awaitable[Any], close_result)
                    except Exception:
                        pass
            close = getattr(browser, "aclose", None)
            if callable(close):
                close_result = close()
                if inspect.isawaitable(close_result):
                    await cast(Awaitable[Any], close_result)

            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            if attached_existing:
                # The attached browser is externally owned. Its nodriver CDP
                # connection was already detached above; never stop the process.
                pass
            else:
                # Browser Host owns Edge in this branch. nodriver's socket is
                # already detached, but stop() still terminates the owned
                # browser process without affecting unrelated Edge instances.
                browser.stop()
                await asyncio.sleep(0.25)
