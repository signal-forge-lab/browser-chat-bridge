import unittest
from unittest.mock import patch

from browser_chat_bridge.driver_server import DriverRuntime


class DriverRuntimeTests(unittest.TestCase):
    def test_runtime_can_start_unbound_and_rebind_to_loopback_cdp(self):
        fake_driver = object()
        with patch("browser_chat_bridge.driver_server.GeminiDriver", return_value=fake_driver) as factory:
            runtime = DriverRuntime(
                backend_kind="chromium",
                promotion_timeout_s=120,
                response_timeout_s=360,
            )
            self.assertIsNone(runtime.driver)
            changed = runtime.rebind("http://127.0.0.1:45678")
            self.assertTrue(changed)
            self.assertIs(runtime.driver, fake_driver)
            self.assertEqual(runtime.cdp_endpoint, "http://127.0.0.1:45678")
            self.assertFalse(runtime.rebind("http://127.0.0.1:45678"))
            factory.assert_called_once()

    def test_runtime_rejects_non_loopback_rebind(self):
        runtime = DriverRuntime(
            backend_kind="chromium",
            promotion_timeout_s=120,
            response_timeout_s=360,
        )
        with self.assertRaisesRegex(ValueError, "loopback"):
            runtime.rebind("http://192.0.2.1:9222")


if __name__ == "__main__":
    unittest.main()
