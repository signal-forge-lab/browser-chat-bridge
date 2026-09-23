from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from browser_chat_bridge.nodriver_sync import (
    NODRIVER_INPUT_TIMEOUT_FLOOR_S,
    NodriverBrowser,
    NodriverLocator,
    NodriverPage,
    _LocatorStep,
)


class _FakePage:
    def __init__(self, value: str):
        self.value = value
        self.expressions: list[str] = []

    def _eval(self, expression: str):
        self.expressions.append(expression)
        return self.value


class _Runner:
    def submit(self, awaitable, timeout_s: float = 30.0):
        del timeout_s
        return asyncio.run(awaitable)

    def close(self) -> None:
        return None


class _Tab:
    def __init__(self) -> None:
        self.target = type("Target", (), {"target_id": "target-1"})()


class _Browser:
    def __init__(self) -> None:
        self._runner = _Runner()
        self.texts: list[tuple[str, str, float]] = []
        self.clicks: list[tuple[str, float, float, float]] = []
        self.keys: list[tuple[str, str, float]] = []

    def _dispatch_trusted_text(
        self,
        *,
        target_id: str,
        text: str,
        timeout_s: float,
    ) -> None:
        self.texts.append((target_id, text, timeout_s))

    def _dispatch_trusted_mouse_click(
        self,
        *,
        target_id: str,
        x: float,
        y: float,
        timeout_s: float,
    ) -> None:
        self.clicks.append((target_id, x, y, timeout_s))

    def _dispatch_trusted_key(
        self,
        *,
        target_id: str,
        key: str,
        timeout_s: float,
    ) -> None:
        self.keys.append((target_id, key, timeout_s))

class NodriverSyncContractTests(unittest.TestCase):
    def test_close_detaches_tabs_then_browser_without_owning_edge(self):
        events: list[str] = []

        class Tab:
            async def aclose(self):
                events.append("tab")

        class Browser:
            def __init__(self):
                self.tabs = [Tab()]

            async def aclose(self):
                events.append("browser")

        class Runner(_Runner):
            def close(self) -> None:
                events.append("runner")

        browser = object.__new__(NodriverBrowser)
        browser._closed = False
        browser._browser = Browser()
        browser._runner = Runner()

        browser.close()

        self.assertEqual(events, ["tab", "browser", "runner"])
        self.assertTrue(browser._closed)

    def test_locator_text_arrays_cross_cdp_as_json_strings(self):
        page = _FakePage(json.dumps(["one", "", "two"]))
        locator = NodriverLocator(page, (_LocatorStep("p"),))  # type: ignore[arg-type]

        self.assertEqual(locator.all_inner_texts(), ["one", "", "two"])
        self.assertIn("JSON.stringify", page.expressions[-1])

    def test_locator_inner_text_excluding_removes_ui_selector_from_clone(self):
        page = _FakePage("answer only")
        locator = NodriverLocator(page, (_LocatorStep("message-content .markdown"),))  # type: ignore[arg-type]

        self.assertEqual(
            locator.inner_text_excluding(".attachment-container.action-card"),
            "answer only",
        )
        self.assertIn("cloneNode(true)", page.expressions[-1])
        self.assertIn(".attachment-container.action-card", page.expressions[-1])

        page.value = json.dumps(["alpha", "beta"])
        self.assertEqual(locator.all_text_contents(), ["alpha", "beta"])
        self.assertIn("JSON.stringify", page.expressions[-1])

    def test_nested_locator_click_uses_exact_target_trusted_mouse_transport(self):
        page = object.__new__(NodriverPage)
        page._browser = _Browser()
        page._tab = _Tab()
        page._eval = lambda _expression: json.dumps(  # type: ignore[method-assign]
            {"ready": True, "count": 1, "x": 123.5, "y": 456.25}
        )

        page._click(
            (_LocatorStep("dialog"), _LocatorStep("button.confirm")),
            timeout_s=1.0,
        )

        self.assertEqual(len(page._browser.clicks), 1)
        target_id, x, y, timeout_s = page._browser.clicks[0]
        self.assertEqual(target_id, "target-1")
        self.assertEqual(x, 123.5)
        self.assertEqual(y, 456.25)
        self.assertEqual(timeout_s, NODRIVER_INPUT_TIMEOUT_FLOOR_S)

    def test_fill_uses_exact_target_trusted_cdp_text_transport(self):
        page = object.__new__(NodriverPage)
        page._browser = _Browser()
        page._tab = _Tab()
        page._tab.target = type("Target", (), {"target_id": "target-1"})()
        page._eval = lambda _expression: True  # type: ignore[method-assign]

        page._fill((_LocatorStep("textarea"),), "hello")

        self.assertEqual(page._browser.texts, [("target-1", "hello", 5.0)])

    def test_press_enter_uses_exact_target_trusted_cdp_key_transport(self):
        page = object.__new__(NodriverPage)
        page._browser = _Browser()
        page._tab = _Tab()
        page._eval = lambda _expression: True  # type: ignore[method-assign]

        page._press((_LocatorStep("textarea"),), "Enter", timeout_s=1.0)

        self.assertEqual(
            page._browser.keys,
            [("target-1", "Enter", NODRIVER_INPUT_TIMEOUT_FLOOR_S)],
        )

    def test_exact_target_mouse_release_clears_pressed_buttons(self):
        class Socket:
            def __init__(self):
                self.sent = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def send(self, payload):
                self.sent.append(json.loads(payload))

            def recv(self, timeout=None):
                del timeout
                return json.dumps({"id": self.sent[-1]["id"], "result": {}})

        socket = Socket()
        browser = object.__new__(NodriverBrowser)
        browser._target_websocket_url = lambda _target_id: "ws://example.invalid/devtools/page/1"  # type: ignore[method-assign]

        with patch("browser_chat_bridge.nodriver_sync.websocket_connect", return_value=socket):
            browser._dispatch_trusted_mouse_click(
                target_id="target-1",
                x=12.5,
                y=34.5,
                timeout_s=1.0,
            )

        self.assertEqual(
            [event["params"]["type"] for event in socket.sent],
            ["mouseMoved", "mousePressed", "mouseReleased"],
        )
        self.assertEqual(socket.sent[1]["params"]["buttons"], 1)
        self.assertEqual(socket.sent[2]["params"]["buttons"], 0)



if __name__ == "__main__":
    unittest.main()
