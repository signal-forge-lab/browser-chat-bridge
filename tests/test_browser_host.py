import tempfile
import unittest
from pathlib import Path

from browser_chat_bridge.browser_host import NodriverManagedEdge


class FakeConfig:
    host = "127.0.0.1"
    port = 45678


class FakeBrowser:
    config = FakeConfig()

    def __init__(self):
        self.urls = []
        self.stopped = False
        self.closed = False

    async def get(self, url, new_tab=False):
        self.urls.append((url, new_tab))
        return object()

    async def aclose(self):
        self.closed = True

    def stop(self):
        self.stopped = True


class BrowserHostTests(unittest.TestCase):
    def test_nodriver_edge_exposes_dynamic_cdp_endpoint_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = FakeBrowser()

            async def start_fn(**kwargs):
                self.assertFalse(kwargs["headless"])
                self.assertEqual(Path(kwargs["user_data_dir"]), (tmp_path / "profile").resolve())
                self.assertEqual(kwargs["browser_executable_path"], __file__)
                return fake

            managed = NodriverManagedEdge(
                profile_dir=tmp_path / "profile",
                browser_executable=__file__,
                start_fn=start_fn,
            )
            self.assertEqual(managed.start(), "http://127.0.0.1:45678")
            self.assertEqual(fake.urls, [("https://gemini.google.com/app", True)])
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
            managed.stop()
            self.assertTrue(fake.closed)
            self.assertFalse(fake.stopped)
