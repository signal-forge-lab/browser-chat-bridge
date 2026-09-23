from unittest.mock import patch

from browser_chat_bridge.bridge_server import (
    DEFAULT_BRIDGE_DRIVER_TIMEOUT_S,
    _recover_after_runtime_refresh,
)
from browser_chat_bridge.driver_server import DEFAULT_DRIVER_RESPONSE_TIMEOUT_S
from browser_chat_bridge.gemini import RECOVERY_RESPONSE_TIMEOUT_S


def test_recovery_refreshes_runtime_before_driver_recovery():
    request = {
        "conversation_url": "https://gemini.google.com/spark/chat/abc123",
        "prompt": "recover me",
    }
    calls = []

    with (
        patch(
            "browser_chat_bridge.bridge_server._ensure_runtime",
            side_effect=lambda browser_url, driver_url, timeout_s: calls.append(
                ("ensure", browser_url, driver_url, timeout_s)
            )
            or "http://127.0.0.1:59857",
        ),
        patch(
            "browser_chat_bridge.bridge_server._driver_recovery_call",
            side_effect=lambda driver_url, timeout_s, payload: calls.append(
                ("recover", driver_url, timeout_s, payload)
            )
            or {"status": "COMPLETED", "content": "done"},
        ),
    ):
        result = _recover_after_runtime_refresh(
            "http://127.0.0.1:8764",
            "http://127.0.0.1:8876",
            30.0,
            390.0,
            request,
        )

    assert result == {"status": "COMPLETED", "content": "done"}
    assert calls == [
        ("ensure", "http://127.0.0.1:8764", "http://127.0.0.1:8876", 30.0),
        ("recover", "http://127.0.0.1:8876", 390.0, request),
    ]


def test_default_timeout_budget_leaves_room_for_read_only_recovery():
    assert DEFAULT_DRIVER_RESPONSE_TIMEOUT_S == 900.0
    assert DEFAULT_BRIDGE_DRIVER_TIMEOUT_S == 960.0
    assert DEFAULT_BRIDGE_DRIVER_TIMEOUT_S > DEFAULT_DRIVER_RESPONSE_TIMEOUT_S
    assert DEFAULT_DRIVER_RESPONSE_TIMEOUT_S + RECOVERY_RESPONSE_TIMEOUT_S < DEFAULT_BRIDGE_DRIVER_TIMEOUT_S
