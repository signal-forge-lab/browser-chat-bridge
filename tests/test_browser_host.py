import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_chat_bridge.browser_host import NodriverManagedEdge, endpoint_alive, repair_nodriver_network_source


class FakeConfig:
    host = "127.0.0.1"
    port = 45678


class FakeBrowser:
    config = FakeConfig()

    def __init__(self):
        self.urls = []
        self.stopped = False
        self.closed = False
        self.tabs = []

    async def get(self, url, new_tab=False):
        self.urls.append((url, new_tab))
        return object()

    async def aclose(self):
        self.closed = True

    def stop(self):
        self.stopped = True


class BrowserHostTests(unittest.TestCase):
    def test_repair_nodriver_network_source_rewrites_only_the_known_cp1252_plusminus(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nodriver" / "cdp" / "network.py"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"# generated\n#: JSON (\xb1Inf).\n")
            self.assertTrue(repair_nodriver_network_source(path))
            self.assertEqual(path.read_bytes(), b"# generated\n#: JSON (\xc2\xb1Inf).\n")
            self.assertFalse(repair_nodriver_network_source(path))

    def test_endpoint_alive_reports_cdp_loss(self):
        response = type("Response", (), {"__enter__": lambda self: self, "__exit__": lambda self, *args: False, "status": 200})()
        with patch("browser_chat_bridge.browser_host.urlopen", return_value=response):
            self.assertTrue(endpoint_alive("http://127.0.0.1:45678"))
        with patch("browser_chat_bridge.browser_host.urlopen", side_effect=OSError("down")):
            self.assertFalse(endpoint_alive("http://127.0.0.1:45678"))

    def test_endpoint_alive_rejects_non_loopback_endpoint(self):
        with patch("browser_chat_bridge.browser_host.urlopen") as opener:
            self.assertFalse(endpoint_alive("http://192.0.2.1:9222"))
            opener.assert_not_called()


    def test_nodriver_edge_exposes_dynamic_cdp_endpoint_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = FakeBrowser()

            async def start_fn(**kwargs):
                self.assertFalse(kwargs["headless"])
                self.assertEqual(Path(kwargs["user_data_dir"]), (tmp_path / "profile").resolve())
                self.assertEqual(kwargs["browser_executable_path"], __file__)
                self.assertIn("https://gemini.google.com/spark", kwargs["browser_args"])
                return fake

            managed = NodriverManagedEdge(
                profile_dir=tmp_path / "profile",
                browser_executable=__file__,
                start_fn=start_fn,
            )
            self.assertEqual(managed.start(), "http://127.0.0.1:45678")
            self.assertEqual(fake.urls, [])
            self.assertTrue(fake.closed)
            managed.stop()
            self.assertTrue(fake.stopped)

    def test_nodriver_edge_can_reattach_existing_cdp_without_owning_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeBrowser()
            calls = []

            async def start_fn(**kwargs):
                calls.append(kwargs)
                return fake

            managed = NodriverManagedEdge(
                profile_dir=Path(tmp) / "unused-profile",
                browser_executable=__file__,
                attach_endpoint="http://127.0.0.1:9222",
                start_fn=start_fn,
            )
            self.assertEqual(managed.start(), "http://127.0.0.1:45678")
            self.assertEqual(calls, [{"host": "127.0.0.1", "port": 9222}])
            self.assertEqual(fake.urls, [])
            self.assertTrue(fake.closed)
            managed.stop()
            self.assertTrue(fake.closed)
            self.assertFalse(fake.stopped)

    def test_ensure_starts_edge_only_on_demand_and_reuses_healthy_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeBrowser()
            calls = []

            async def start_fn(**kwargs):
                calls.append(kwargs)
                return fake

            managed = NodriverManagedEdge(
                profile_dir=Path(tmp) / "profile",
                browser_executable=__file__,
                start_fn=start_fn,
            )
            self.assertEqual(managed.endpoint, "")
            self.assertEqual(calls, [])
            with patch("browser_chat_bridge.browser_host.endpoint_alive", return_value=True):
                self.assertEqual(managed.ensure(), "http://127.0.0.1:45678")
                self.assertEqual(managed.ensure(), "http://127.0.0.1:45678")
            self.assertEqual(len(calls), 1)
            managed.stop()

    def test_ensure_is_single_flight_for_concurrent_callers(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeBrowser()
            calls = []

            async def start_fn(**kwargs):
                calls.append(kwargs)
                return fake

            managed = NodriverManagedEdge(
                profile_dir=Path(tmp) / "profile",
                browser_executable=__file__,
                start_fn=start_fn,
            )
            results = []
            with patch("browser_chat_bridge.browser_host.endpoint_alive", return_value=True):
                threads = [threading.Thread(target=lambda: results.append(managed.ensure())) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=2)
            self.assertEqual(results, ["http://127.0.0.1:45678"] * 2)
            self.assertEqual(len(calls), 1)
            managed.stop()

    def test_ensure_restarts_after_cdp_loss_instead_of_returning_stale_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FakeBrowser()
            second = FakeBrowser()
            browsers = iter((first, second))
            calls = []

            async def start_fn(**kwargs):
                calls.append(kwargs)
                return next(browsers)

            managed = NodriverManagedEdge(
                profile_dir=Path(tmp) / "profile",
                browser_executable=__file__,
                start_fn=start_fn,
            )
            with patch("browser_chat_bridge.browser_host.endpoint_alive", return_value=False):
                self.assertEqual(managed.ensure(), "http://127.0.0.1:45678")
                self.assertEqual(managed.ensure(), "http://127.0.0.1:45678")
            self.assertEqual(len(calls), 2)
            self.assertTrue(first.stopped)
            managed.stop()
