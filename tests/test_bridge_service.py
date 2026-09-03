from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from browser_chat_bridge.bridge import BridgeService
from browser_chat_bridge.store import BridgeStore


class BridgeServiceTests(unittest.TestCase):
    def make_service(self):
        temp = tempfile.TemporaryDirectory()
        store = BridgeStore(Path(temp.name) / "bridge.sqlite3")
        service = BridgeService(store)
        self.addCleanup(temp.cleanup)
        return service, store

    def test_completed_request_is_idempotent_and_binds_run(self):
        service, store = self.make_service()
        calls = []

        def driver(request):
            calls.append(request)
            return {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/app/abc123",
                "content": "answer",
            }

        first = service.run_turn("run-1", "req-1", "hello", driver)
        second = service.run_turn("run-1", "req-1", "hello", driver)

        self.assertEqual(first["status"], "COMPLETED")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(second["content"], "answer")
        self.assertEqual(len(calls), 1)
        run = store.get_run("run-1")
        self.assertEqual(run["conversation_id"], "abc123")

    def test_driver_transport_failure_becomes_ambiguous_and_never_blind_resends(self):
        service, _store = self.make_service()
        calls = []

        def driver(_request):
            calls.append(1)
            raise TimeoutError("lost reply after dispatch")

        first = service.run_turn("run-1", "req-1", "hello", driver)
        second = service.run_turn("run-1", "req-1", "hello", driver)

        self.assertEqual(first["status"], "AMBIGUOUS")
        self.assertEqual(second["status"], "AMBIGUOUS")
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)

    def test_same_request_id_with_different_prompt_is_rejected(self):
        service, _store = self.make_service()

        def driver(_request):
            return {
                "status": "NOT_DISPATCHED",
                "conversation_id": None,
                "conversation_url": None,
                "content": None,
            }

        service.run_turn("run-1", "req-1", "one", driver)
        with self.assertRaisesRegex(ValueError, "request_id"):
            service.run_turn("run-1", "req-1", "two", driver)

    def test_new_run_does_not_reuse_another_runs_conversation(self):
        service, _store = self.make_service()
        requests = []

        def driver(request):
            requests.append(request)
            run_number = len(requests)
            return {
                "status": "COMPLETED",
                "conversation_id": f"c{run_number}",
                "conversation_url": f"https://gemini.google.com/app/c{run_number}",
                "content": f"a{run_number}",
            }

        service.run_turn("run-a", "req-a", "a", driver)
        service.run_turn("run-b", "req-b", "b", driver)

        self.assertIsNone(requests[0]["conversation_url"])
        self.assertIsNone(requests[1]["conversation_url"])


if __name__ == "__main__":
    unittest.main()

