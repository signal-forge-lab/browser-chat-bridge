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

    def test_cleanup_deletes_bound_remote_conversation_then_purges_local_run(self):
        service, store = self.make_service()

        def turn_driver(_request):
            return {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/app/abc123",
                "content": "answer",
            }

        service.run_turn("run-1", "req-1", "hello", turn_driver)
        cleanup_calls = []

        def cleanup_driver(request):
            cleanup_calls.append(request)
            return {"status": "DELETED"}

        result = service.cleanup_run("run-1", cleanup_driver)

        self.assertEqual(result, {"status": "DELETED", "run_id": "run-1"})
        self.assertEqual(
            cleanup_calls,
            [{"conversation_url": "https://gemini.google.com/app/abc123"}],
        )
        self.assertIsNone(store.get_run("run-1"))
        self.assertIsNone(store.get_turn("req-1"))

    def test_cleanup_keeps_mapping_when_driver_cannot_confirm_delete(self):
        service, store = self.make_service()

        def turn_driver(_request):
            return {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/app/abc123",
                "content": "answer",
            }

        service.run_turn("run-1", "req-1", "hello", turn_driver)
        result = service.cleanup_run("run-1", lambda _request: {"status": "DELETE_FAILED"})

        self.assertEqual(result["status"], "DELETE_FAILED")
        self.assertIsNotNone(store.get_run("run-1"))
        self.assertIsNotNone(store.get_turn("req-1"))

    def test_cleanup_is_idempotent_for_unknown_or_already_purged_run(self):
        service, _store = self.make_service()
        calls = []

        result = service.cleanup_run("missing", lambda request: calls.append(request))

        self.assertEqual(result, {"status": "NOT_FOUND", "run_id": "missing"})
        self.assertEqual(calls, [])

    def test_cleanup_uses_turn_binding_when_noncompleted_result_did_not_bind_run(self):
        service, store = self.make_service()

        def turn_driver(_request):
            return {
                "status": "TIMEOUT",
                "conversation_id": "slow123",
                "conversation_url": "https://gemini.google.com/app/slow123",
                "content": None,
                "error": "response timed out",
            }

        service.run_turn("run-1", "req-1", "hello", turn_driver)
        self.assertIsNone(store.get_run("run-1")["conversation_url"])
        calls = []

        result = service.cleanup_run(
            "run-1",
            lambda request: calls.append(request) or {"status": "DELETED"},
        )

        self.assertEqual(result["status"], "DELETED")
        self.assertEqual(
            calls,
            [{"conversation_url": "https://gemini.google.com/app/slow123"}],
        )
        self.assertIsNone(store.get_run("run-1"))


if __name__ == "__main__":
    unittest.main()

