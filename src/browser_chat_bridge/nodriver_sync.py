from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from websockets.sync.client import connect as websocket_connect

from .browser_host import _import_nodriver


class NodriverAdapterError(RuntimeError):
    pass


NODRIVER_INPUT_TIMEOUT_FLOOR_S = 10.0


class _LoopRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="browser-chat-nodriver-driver",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def submit(self, awaitable, timeout_s: float = 30.0):
        future: Future[Any] = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result(timeout=max(0.1, float(timeout_s)))

    def close(self) -> None:
        if not self._thread.is_alive():
            return
        async def drain() -> None:
            current = asyncio.current_task()
            pending = [
                task
                for task in asyncio.all_tasks()
                if task is not current and not task.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            self.submit(drain(), timeout_s=2.0)
        except Exception:  # noqa: BLE001,S110 - final loop drain is best-effort.
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        if not self._loop.is_running():
            self._loop.close()


@dataclass(frozen=True)
class _LocatorStep:
    selector: str
    index: int | None = None


def _resolve_nodes_script(steps: tuple[_LocatorStep, ...], expression: str) -> str:
    encoded = json.dumps(
        [{"selector": step.selector, "index": step.index} for step in steps],
        ensure_ascii=False,
    )
    return f"""
(() => {{
  const steps = {encoded};
  let nodes = [document];
  for (const step of steps) {{
    const next = [];
    for (const parent of nodes) {{
      const matches = Array.from(parent.querySelectorAll(step.selector));
      if (step.index === null) {{
        next.push(...matches);
      }} else {{
        const index = step.index < 0 ? matches.length + step.index : step.index;
        if (index >= 0 && index < matches.length) next.push(matches[index]);
      }}
    }}
    nodes = next;
  }}
  {expression}
}})()
"""


class NodriverLocator:
    def __init__(self, page: NodriverPage, steps: tuple[_LocatorStep, ...]):
        self._page = page
        self._steps = steps

    @property
    def last(self) -> NodriverLocator:
        if not self._steps:
            return self
        return NodriverLocator(
            self._page,
            (*self._steps[:-1], _LocatorStep(self._steps[-1].selector, -1)),
        )

    def nth(self, index: int) -> NodriverLocator:
        if not self._steps:
            return self
        return NodriverLocator(
            self._page,
            (*self._steps[:-1], _LocatorStep(self._steps[-1].selector, int(index))),
        )

    def locator(self, selector: str) -> NodriverLocator:
        return NodriverLocator(self._page, (*self._steps, _LocatorStep(str(selector))))

    def _value(self, expression: str):
        return self._page._eval(_resolve_nodes_script(self._steps, expression))

    def count(self) -> int:
        return int(self._value("return nodes.length;") or 0)

    def inner_text(self) -> str:
        return str(self._value("return nodes.length ? (nodes[0].innerText || '') : '';") or "")

    def inner_text_excluding(self, selector: str) -> str:
        encoded = json.dumps(str(selector), ensure_ascii=False)
        return str(
            self._value(
                f"""
if (!nodes.length) return '';
const clone = nodes[0].cloneNode(true);
for (const item of clone.querySelectorAll({encoded})) item.remove();
return clone.innerText || '';
"""
            )
            or ""
        )

    def text_content(self) -> str:
        return str(self._value("return nodes.length ? (nodes[0].textContent || '') : '';") or "")

    def all_inner_texts(self) -> list[str]:
        raw = self._value(
            "return JSON.stringify(nodes.map(node => node.innerText || ''));"
        )
        values = json.loads(str(raw or "[]"))
        return [str(value or "") for value in values]

    def all_text_contents(self) -> list[str]:
        raw = self._value(
            "return JSON.stringify(nodes.map(node => node.textContent || ''));"
        )
        values = json.loads(str(raw or "[]"))
        return [str(value or "") for value in values]

    def get_attribute(self, name: str) -> str | None:
        encoded = json.dumps(str(name), ensure_ascii=False)
        value = self._value(f"return nodes.length ? nodes[0].getAttribute({encoded}) : null;")
        return None if value is None else str(value)

    def is_visible(self) -> bool:
        return bool(
            self._value(
                """
if (!nodes.length) return false;
const node = nodes[0];
const style = getComputedStyle(node);
const rect = node.getBoundingClientRect();
return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
"""
            )
        )

    def fill(self, text: str) -> None:
        self._page._fill(self._steps, str(text))

    def click(self, timeout: int = 5_000) -> None:
        self._page._click(self._steps, timeout_s=max(0.1, float(timeout) / 1000.0))

    def press(self, key: str, timeout: int = 5_000) -> None:
        self._page._press(
            self._steps,
            str(key),
            timeout_s=max(0.1, float(timeout) / 1000.0),
        )


class NodriverPage:
    def __init__(self, browser: NodriverBrowser, tab: Any):
        self._browser = browser
        self._tab = tab

    @property
    def target_id(self) -> str:
        return str(getattr(getattr(self._tab, "target", None), "target_id", "") or "")

    @property
    def url(self) -> str:
        try:
            return str(self._eval("window.location.href") or "")
        except Exception:  # noqa: BLE001 - stale tab metadata is a safe fallback here.
            return str(getattr(self._tab, "url", "") or "")

    def locator(self, selector: str) -> NodriverLocator:
        return NodriverLocator(self, (_LocatorStep(str(selector)),))

    def _eval(self, expression: str):
        value = self._browser._runner.submit(
            self._tab.evaluate(expression, await_promise=True, return_by_value=True),
            timeout_s=30.0,
        )
        return getattr(value, "value", value)

    def evaluate(self, expression: str, *args):
        source = str(expression)
        if args:
            encoded = ",".join(json.dumps(arg, ensure_ascii=False) for arg in args)
            source = f"({source})({encoded})"
        elif source.lstrip().startswith(("() =>", "async () =>", "function")):
            source = f"({source})()"
        return self._eval(source)

    def goto(self, url: str, wait_until: str | None = None, timeout: int = 30_000) -> None:
        del wait_until
        self._browser._runner.submit(
            self._tab.get(str(url)),
            timeout_s=max(0.1, timeout / 1000.0),
        )

    def wait_for_url(self, url: str, timeout: int = 5_000) -> None:
        deadline = time.monotonic() + max(0.1, timeout / 1000.0)
        while time.monotonic() < deadline:
            if self.url == str(url):
                return
            time.sleep(0.1)
        raise TimeoutError(f"URL did not become {url!r}")

    def close(self) -> None:
        self._browser._runner.submit(self._tab.close(), timeout_s=5.0)

    def _click(self, steps: tuple[_LocatorStep, ...], *, timeout_s: float) -> None:
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while time.monotonic() < deadline:
            raw = self._eval(
                _resolve_nodes_script(
                    steps,
                    """
if (nodes.length !== 1) {
  return JSON.stringify({ready: false, count: nodes.length});
}
const node = nodes[0];
node.scrollIntoView({block: 'center', inline: 'center'});
const rect = node.getBoundingClientRect();
const style = getComputedStyle(node);
const visible = style.visibility !== 'hidden'
  && style.display !== 'none'
  && rect.width > 0
  && rect.height > 0;
return JSON.stringify({
  ready: visible,
  count: 1,
  x: rect.left + rect.width / 2,
  y: rect.top + rect.height / 2
});
""",
                )
            )
            geometry = json.loads(str(raw or "{}"))
            if bool(geometry.get("ready")):
                self._browser._dispatch_trusted_mouse_click(
                    target_id=self.target_id,
                    x=float(geometry["x"]),
                    y=float(geometry["y"]),
                    timeout_s=max(
                        NODRIVER_INPUT_TIMEOUT_FLOOR_S,
                        float(timeout_s),
                    ),
                )
                return
            time.sleep(0.05)
        raise NodriverAdapterError("click target was not uniquely resolvable")

    def _fill(self, steps: tuple[_LocatorStep, ...], text: str) -> None:
        prepared = self._eval(
            _resolve_nodes_script(
                steps,
                """
if (nodes.length !== 1) return false;
const node = nodes[0];
node.focus();
if (node.isContentEditable) {
  node.replaceChildren();
} else if ('value' in node) {
  node.value = '';
}
node.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
return true;
""",
            )
        )
        if not prepared:
            raise NodriverAdapterError("fill target was not unique")
        self._browser._dispatch_trusted_text(
            target_id=self.target_id,
            text=text,
            timeout_s=5.0,
        )

    def _press(
        self,
        steps: tuple[_LocatorStep, ...],
        key: str,
        *,
        timeout_s: float,
    ) -> None:
        focused = self._eval(
            _resolve_nodes_script(
                steps,
                """
if (nodes.length !== 1) return false;
const node = nodes[0];
const style = getComputedStyle(node);
const rect = node.getBoundingClientRect();
const visible = style.visibility !== 'hidden'
  && style.display !== 'none'
  && rect.width > 0
  && rect.height > 0;
if (!visible) return false;
node.focus();
return document.activeElement === node || node.contains(document.activeElement);
""",
            )
        )
        if not focused:
            raise NodriverAdapterError("key target was not uniquely focusable")
        self._browser._dispatch_trusted_key(
            target_id=self.target_id,
            key=key,
            timeout_s=max(NODRIVER_INPUT_TIMEOUT_FLOOR_S, float(timeout_s)),
        )


class NodriverContext:
    def __init__(self, browser: NodriverBrowser):
        self._browser = browser

    @property
    def pages(self) -> list[NodriverPage]:
        self._browser._refresh_targets()
        return [NodriverPage(self._browser, tab) for tab in list(self._browser._browser.tabs)]

    def new_page(self) -> NodriverPage:
        tab = self._browser._runner.submit(self._browser._browser.get("about:blank", new_tab=True))
        return NodriverPage(self._browser, tab)


class NodriverCdpSession:
    def __init__(self, browser: NodriverBrowser):
        self._browser = browser

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        uc = self._browser._uc
        transport = getattr(self._browser._browser, "main_tab", None)
        if transport is None:
            raise NodriverAdapterError("nodriver attach exposed no tab for Target-domain transport")
        if method == "Target.createTarget":
            target_id = self._browser._runner.submit(
                transport.send(
                    uc.cdp.target.create_target(
                        url=str(params.get("url") or "about:blank"),
                        background=bool(params.get("background", False)),
                    )
                )
            )
            return {"targetId": str(target_id)}
        if method == "Target.closeTarget":
            target_id = uc.cdp.target.TargetID(str(params.get("targetId") or ""))
            success = self._browser._runner.submit(
                transport.send(
                    uc.cdp.target.close_target(target_id)
                )
            )
            return {"success": bool(success)}
        raise NodriverAdapterError(f"unsupported browser-level CDP method: {method}")

    def detach(self) -> None:
        return None


class NodriverBrowser:
    def __init__(self, endpoint: str, *, start_fn: Callable[..., Any] | None = None):
        parsed = urlsplit(str(endpoint or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or not parsed.port:
            raise NodriverAdapterError("nodriver CDP endpoint must include host and port")
        self._uc = _import_nodriver()
        self._endpoint = str(endpoint).rstrip("/")
        self._runner = _LoopRunner()
        self._closed = False
        starter = start_fn or self._uc.start
        try:
            self._browser = self._runner.submit(
                starter(host=parsed.hostname, port=parsed.port),
                timeout_s=30.0,
            )
        except Exception:
            self._runner.close()
            raise
        self.contexts = [NodriverContext(self)]

    def _target_websocket_url(self, target_id: str) -> str:
        try:
            with urlopen(f"{self._endpoint}/json/list", timeout=3.0) as response:
                rows = json.load(response)
        except Exception as exc:
            raise NodriverAdapterError("failed to enumerate local CDP targets") from exc
        if not isinstance(rows, list):
            raise NodriverAdapterError("local CDP target list was not an array")
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or "") != str(target_id):
                continue
            websocket_url = str(row.get("webSocketDebuggerUrl") or "")
            if websocket_url:
                return websocket_url
        raise NodriverAdapterError("exact CDP target websocket was not found")

    def _dispatch_trusted_text(
        self,
        *,
        target_id: str,
        text: str,
        timeout_s: float,
    ) -> None:
        websocket_url = self._target_websocket_url(target_id)
        commands = (
            {
                "id": 1,
                "method": "Input.insertText",
                "params": {"text": str(text)},
            },
            # Gemini's rich-textarea can render Input.insertText content while
            # leaving its internal "composer has text" state stale. A real
            # keystroke makes the framework publish that state and reveal the
            # Send button. Type one harmless space and remove it immediately;
            # the caller still verifies the exact composer text afterwards.
            {
                "id": 2,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyDown",
                    "key": " ",
                    "code": "Space",
                    "text": " ",
                    "windowsVirtualKeyCode": 32,
                    "nativeVirtualKeyCode": 32,
                },
            },
            {
                "id": 3,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyUp",
                    "key": " ",
                    "code": "Space",
                    "windowsVirtualKeyCode": 32,
                    "nativeVirtualKeyCode": 32,
                },
            },
            {
                "id": 4,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyDown",
                    "key": "Backspace",
                    "code": "Backspace",
                    "windowsVirtualKeyCode": 8,
                    "nativeVirtualKeyCode": 8,
                },
            },
            {
                "id": 5,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyUp",
                    "key": "Backspace",
                    "code": "Backspace",
                    "windowsVirtualKeyCode": 8,
                    "nativeVirtualKeyCode": 8,
                },
            },
        )
        try:
            with websocket_connect(
                websocket_url,
                open_timeout=max(0.1, float(timeout_s)),
                close_timeout=1.0,
            ) as socket:
                deadline = time.monotonic() + max(0.1, float(timeout_s))
                for command in commands:
                    socket.send(json.dumps(command, ensure_ascii=False))
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("trusted text CDP response timed out")
                        response = json.loads(socket.recv(timeout=remaining))
                        if response.get("id") != command["id"]:
                            continue
                        if "error" in response:
                            raise NodriverAdapterError(
                                f"trusted text CDP error: {response['error']}"
                            )
                        break
        except NodriverAdapterError:
            raise
        except Exception as exc:
            raise NodriverAdapterError("trusted text CDP transport failed") from exc

    def _dispatch_trusted_mouse_click(
        self,
        *,
        target_id: str,
        x: float,
        y: float,
        timeout_s: float,
    ) -> None:
        websocket_url = self._target_websocket_url(target_id)
        commands = (
            {
                "id": 1,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": "mouseMoved",
                    "x": float(x),
                    "y": float(y),
                },
            },
            {
                "id": 2,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": "mousePressed",
                    "x": float(x),
                    "y": float(y),
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 1,
                },
            },
            {
                "id": 3,
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": "mouseReleased",
                    "x": float(x),
                    "y": float(y),
                    "button": "left",
                    "buttons": 0,
                    "clickCount": 1,
                },
            },
        )
        try:
            with websocket_connect(
                websocket_url,
                open_timeout=max(0.1, float(timeout_s)),
                close_timeout=1.0,
            ) as socket:
                deadline = time.monotonic() + max(0.1, float(timeout_s))
                for command in commands:
                    socket.send(json.dumps(command))
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("trusted mouse CDP response timed out")
                        response = json.loads(socket.recv(timeout=remaining))
                        if response.get("id") != command["id"]:
                            continue
                        if "error" in response:
                            raise NodriverAdapterError(
                                f"trusted mouse CDP error: {response['error']}"
                            )
                        break
        except NodriverAdapterError:
            raise
        except Exception as exc:
            detail = str(exc).strip()
            suffix = f": {type(exc).__name__}: {detail[:240]}" if detail else f": {type(exc).__name__}"
            raise NodriverAdapterError(f"trusted mouse CDP transport failed{suffix}") from exc

    def _dispatch_trusted_key(
        self,
        *,
        target_id: str,
        key: str,
        timeout_s: float,
    ) -> None:
        if key != "Enter":
            raise NodriverAdapterError(f"unsupported trusted key: {key}")
        websocket_url = self._target_websocket_url(target_id)
        commands = (
            {
                "id": 1,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyDown",
                    "key": "Enter",
                    "code": "Enter",
                    "text": "\r",
                    "unmodifiedText": "\r",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            },
            {
                "id": 2,
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": "keyUp",
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            },
        )
        try:
            with websocket_connect(
                websocket_url,
                open_timeout=max(0.1, float(timeout_s)),
                close_timeout=1.0,
            ) as socket:
                deadline = time.monotonic() + max(0.1, float(timeout_s))
                for command in commands:
                    socket.send(json.dumps(command, ensure_ascii=False))
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("trusted key CDP response timed out")
                        response = json.loads(socket.recv(timeout=remaining))
                        if response.get("id") != command["id"]:
                            continue
                        if "error" in response:
                            raise NodriverAdapterError(
                                f"trusted key CDP error: {response['error']}"
                            )
                        break
        except NodriverAdapterError:
            raise
        except Exception as exc:
            raise NodriverAdapterError("trusted key CDP transport failed") from exc

    def _refresh_targets(self) -> None:
        self._runner.submit(self._browser.update_targets(), timeout_s=10.0)

    def new_browser_cdp_session(self) -> NodriverCdpSession:
        return NodriverCdpSession(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        async def detach() -> None:
            for tab in list(getattr(self._browser, "tabs", ())):
                close = getattr(tab, "aclose", None)
                if callable(close):
                    try:
                        await close()
                    except Exception:  # noqa: BLE001,S110 - detach is best-effort per tab.
                        pass
            close = getattr(self._browser, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001,S110 - browser detach must not own Edge.
                    pass
            await asyncio.sleep(0)

        try:
            self._runner.submit(detach(), timeout_s=5.0)
        finally:
            self._runner.close()


def connect_over_cdp(endpoint: str) -> NodriverBrowser:
    return NodriverBrowser(endpoint)
