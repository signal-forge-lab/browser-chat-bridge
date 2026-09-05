from __future__ import annotations

import asyncio
import atexit
import importlib
import ipaddress
import inspect
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, cast
from urllib.parse import urlparse
from urllib.request import urlopen


class BrowserHostError(RuntimeError):
    pass


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
            / "Intelligence Works"
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
        start_url: str = "https://gemini.google.com/app",
        attach_endpoint: str | None = None,
        start_fn: Callable[..., Any] | None = None,
    ):
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.browser_executable = resolve_edge_executable(browser_executable)
        self.start_url = str(start_url or "https://gemini.google.com/app")
        self.attach_endpoint = str(attach_endpoint or "").strip()
        self._start_fn = start_fn
        self._ready = threading.Event()
        self._stop = threading.Event()
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

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:
            self._error = exc
            self._ready.set()

    async def _run(self) -> None:
        start_fn = self._start_fn
        cdp_close = None
        attached_existing = self.attached_existing
        if start_fn is None:
            try:
                uc = importlib.import_module("nodriver")
            except ModuleNotFoundError as exc:
                raise BrowserHostError(
                    "Browser Chat Edge requires nodriver; install the project dependencies"
                ) from exc
            start_fn = uc.start
            cdp_close = uc.cdp.browser.close

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
                "browser_args": ["--no-first-run", "--no-default-browser-check"],
            }

        browser = await start_fn(**start_kwargs)
        try:
            host = str(browser.config.host or "127.0.0.1")
            port = int(browser.config.port)
            self._endpoint = f"http://{host}:{port}"
            if self.start_url and not attached_existing:
                await browser.get(self.start_url, new_tab=True)
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.1)
        finally:
            if attached_existing:
                close = getattr(browser, "aclose", None)
                if callable(close):
                    try:
                        close_result = close()
                        if inspect.isawaitable(close_result):
                            await cast(Awaitable[Any], close_result)
                    except Exception:
                        pass
            else:
                clean_exit = False
                if cdp_close is not None:
                    try:
                        close_result = browser.send(cdp_close())
                        if inspect.isawaitable(close_result):
                            await cast(Awaitable[Any], close_result)
                    except Exception:
                        pass
                    process = getattr(browser, "_process", None)
                    if process is not None:
                        try:
                            await asyncio.wait_for(process.wait(), timeout=3.0)
                            clean_exit = True
                        except Exception:
                            pass
                if not clean_exit:
                    browser.stop()
                await asyncio.sleep(0.25)
