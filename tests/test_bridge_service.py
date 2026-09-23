from __future__ import annotations

import tempfile
import threading
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
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
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

    def test_completed_spark_task_is_idempotent_without_durable_chat_binding(self):
        service, store = self.make_service()
        calls = []

        def driver(request):
            calls.append(request)
            return {
                "status": "COMPLETED",
                "execution_kind": "task",
                "conversation_id": None,
                "conversation_url": None,
                "content": "READ_SENTINEL_7391",
            }

        first = service.run_turn("run-task", "req-task", "read file", driver)
        second = service.run_turn("run-task", "req-task", "read file", driver)

        self.assertEqual(first["status"], "COMPLETED")
        self.assertEqual(first["content"], "READ_SENTINEL_7391")
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)
        self.assertIsNone(store.get_run("run-task")["conversation_url"])

        cleanup_calls = []
        self.assertEqual(
            service.cleanup_run("run-task", lambda request: cleanup_calls.append(request)),
            {"status": "DELETED", "run_id": "run-task"},
        )
        self.assertEqual(cleanup_calls, [])

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

    def test_bound_transport_failure_recovers_existing_response_without_redispatch(self):
        service, _store = self.make_service()
        service.run_turn(
            "run-1",
            "req-bind",
            "bind",
            lambda _request: {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": "bound",
            },
        )
        driver_calls = []
        recovery_calls = []

        def driver(_request):
            driver_calls.append(1)
            raise ConnectionResetError("reply lost")

        def recover(request):
            recovery_calls.append(request)
            return {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": "recovered answer",
            }

        result = service.run_turn(
            "run-1",
            "req-2",
            "planner prompt",
            driver,
            recovery_call=recover,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["content"], "recovered answer")
        self.assertTrue(result["recovery_attempted"])
        self.assertTrue(result["recovered"])
        self.assertEqual(driver_calls, [1])
        self.assertEqual(
            recovery_calls,
            [{
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "prompt": "planner prompt",
            }],
        )

    def test_timeout_with_returned_binding_recovers_and_binds_unbound_run(self):
        service, store = self.make_service()
        recovery_calls = []

        result = service.run_turn(
            "run-1",
            "req-1",
            "planner prompt",
            lambda _request: {
                "status": "TIMEOUT",
                "conversation_id": "slow123",
                "conversation_url": "https://gemini.google.com/spark/chat/slow123",
                "content": None,
                "error": "response timed out",
            },
            recovery_call=lambda request: recovery_calls.append(request) or {
                "status": "COMPLETED",
                "conversation_id": "slow123",
                "conversation_url": "https://gemini.google.com/spark/chat/slow123",
                "content": "late answer",
            },
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertTrue(result["recovered"])
        self.assertEqual(store.get_run("run-1")["conversation_id"], "slow123")
        self.assertEqual(len(recovery_calls), 1)

    def test_recovery_timeout_preserves_no_resend_semantics(self):
        service, _store = self.make_service()
        service.run_turn(
            "run-1",
            "req-bind",
            "bind",
            lambda _request: {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": "bound",
            },
        )
        driver_calls = []

        def driver(_request):
            driver_calls.append(1)
            raise TimeoutError("lost reply")

        result = service.run_turn(
            "run-1",
            "req-2",
            "planner prompt",
            driver,
            recovery_call=lambda _request: {
                "status": "TIMEOUT",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": None,
                "error": "attributed but still incomplete",
            },
        )

        self.assertEqual(result["status"], "TIMEOUT")
        self.assertTrue(result["recovery_attempted"])
        self.assertFalse(result["recovered"])
        self.assertEqual(driver_calls, [1])

    def test_turn_forwards_optional_response_timeout_to_driver(self):
        service, _store = self.make_service()
        seen = []

        result = service.run_turn(
            "run-timeout",
            "req-timeout",
            "hello",
            lambda request: seen.append(dict(request)) or {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": "ok",
            },
            response_timeout_seconds=70.0,
        )

        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(seen[0]["response_timeout_seconds"], 70.0)

    def test_turn_rejects_nonpositive_response_timeout(self):
        service, _store = self.make_service()
        with self.assertRaisesRegex(ValueError, "response_timeout_seconds"):
            service.run_turn(
                "run-timeout",
                "req-timeout",
                "hello",
                lambda _request: {"status": "NOT_DISPATCHED"},
                response_timeout_seconds=0.0,
            )

    def test_predispatch_failure_is_not_dispatched_and_is_cached(self):
        service, _store = self.make_service()
        preflight_calls = []
        driver_calls = []

        def preflight():
            preflight_calls.append(1)
            raise RuntimeError("browser unavailable")

        first = service.run_turn(
            "run-1",
            "req-1",
            "hello",
            lambda request: driver_calls.append(request),
            before_dispatch=preflight,
        )
        second = service.run_turn(
            "run-1",
            "req-1",
            "hello",
            lambda request: driver_calls.append(request),
            before_dispatch=preflight,
        )

        self.assertEqual(first["status"], "NOT_DISPATCHED")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(preflight_calls, [1])
        self.assertEqual(driver_calls, [])

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
                "conversation_url": f"https://gemini.google.com/spark/chat/c{run_number}",
                "content": f"a{run_number}",
            }

        service.run_turn("run-a", "req-a", "a", driver)
        service.run_turn("run-b", "req-b", "b", driver)

        self.assertIsNone(requests[0]["conversation_url"])
        self.assertIsNone(requests[1]["conversation_url"])

    def test_two_unbound_first_turns_are_serialized_for_safe_correlation(self):
        service, _store = self.make_service()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()
        call_order = []
        results = {}

        def driver(request):
            suffix = request["prompt"]
            call_order.append(suffix)
            if suffix == "run-a":
                first_entered.set()
                release_first.wait(timeout=2)
            else:
                second_entered.set()
            return {
                "status": "COMPLETED",
                "conversation_id": suffix,
                "conversation_url": f"https://gemini.google.com/spark/chat/{suffix}",
                "content": "done",
            }

        def run(run_id):
            results[run_id] = service.run_turn(run_id, f"req-{run_id}", run_id, driver)

        first = threading.Thread(target=run, args=("run-a",))
        second = threading.Thread(target=run, args=("run-b",))
        first.start()
        self.assertTrue(first_entered.wait(timeout=2))
        second.start()
        try:
            self.assertFalse(second_entered.wait(timeout=0.2))
        finally:
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())
        self.assertEqual(call_order, ["run-a", "run-b"])
        self.assertEqual(results["run-a"]["status"], "COMPLETED")
        self.assertEqual(results["run-b"]["status"], "COMPLETED")

    def test_global_capacity_admits_two_turns_and_returns_busy_for_third(self):
        service, _store = self.make_service()

        def bind(run_id):
            service.run_turn(
                run_id,
                f"{run_id}-bind",
                "bind",
                lambda _request: {
                    "status": "COMPLETED",
                    "conversation_id": run_id,
                    "conversation_url": f"https://gemini.google.com/spark/chat/{run_id}",
                    "content": "bound",
                },
            )

        run_ids = tuple(f"run-{index}" for index in range(3))
        for run_id in run_ids:
            bind(run_id)

        entered = threading.Barrier(3)
        release = threading.Event()

        def blocking_driver(_request):
            entered.wait(timeout=2)
            release.wait(timeout=2)
            return {
                "status": "COMPLETED",
                "conversation_id": "unused",
                "conversation_url": "https://gemini.google.com/spark/chat/unused",
                "content": "done",
            }

        results = {}

        def run_blocked(run_id):
            results[run_id] = service.run_turn(
                run_id,
                f"{run_id}-busy",
                "hold",
                blocking_driver,
            )

        active = [threading.Thread(target=run_blocked, args=(run_id,)) for run_id in run_ids[:2]]
        for thread in active:
            thread.start()
        try:
            entered.wait(timeout=2)

            third_calls = []
            third_preflight = []
            third = service.run_turn(
                run_ids[2],
                f"{run_ids[2]}-busy",
                "third",
                lambda request: third_calls.append(request),
                before_dispatch=lambda: third_preflight.append(1),
            )
            replay = service.run_turn(
                run_ids[2],
                f"{run_ids[2]}-busy",
                "third",
                lambda request: third_calls.append(request),
            )

            self.assertEqual(third["status"], "BUSY")
            self.assertFalse(third["cached"])
            self.assertEqual(replay["status"], "BUSY")
            self.assertTrue(replay["cached"])
            self.assertEqual(third_calls, [])
            self.assertEqual(third_preflight, [])
        finally:
            release.set()
            for thread in active:
                thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in active))

    def test_cleanup_deletes_bound_remote_conversation_then_purges_local_run(self):
        service, store = self.make_service()

        def turn_driver(_request):
            return {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
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
            [{"conversation_url": "https://gemini.google.com/spark/chat/abc123"}],
        )
        self.assertIsNone(store.get_run("run-1"))
        self.assertIsNone(store.get_turn("req-1"))

    def test_cleanup_keeps_mapping_when_driver_cannot_confirm_delete(self):
        service, store = self.make_service()

        def turn_driver(_request):
            return {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": "answer",
            }

        service.run_turn("run-1", "req-1", "hello", turn_driver)
        result = service.cleanup_run("run-1", lambda _request: {"status": "DELETE_FAILED"})

        self.assertEqual(result["status"], "DELETE_FAILED")
        self.assertIsNotNone(store.get_run("run-1"))
        self.assertIsNotNone(store.get_turn("req-1"))

    def test_release_run_target_preserves_conversation_and_cached_turns(self):
        service, store = self.make_service()
        service.run_turn(
            "run-1",
            "req-1",
            "hello",
            lambda _request: {
                "status": "COMPLETED",
                "conversation_id": "abc123",
                "conversation_url": "https://gemini.google.com/spark/chat/abc123",
                "content": "answer",
            },
        )
        calls = []

        result = service.release_run_target(
            "run-1",
            lambda request: calls.append(request) or {"status": "RELEASED"},
        )

        self.assertEqual(result, {"status": "RELEASED", "run_id": "run-1"})
        self.assertEqual(
            calls,
            [{"conversation_url": "https://gemini.google.com/spark/chat/abc123"}],
        )
        self.assertEqual(store.get_run("run-1")["conversation_id"], "abc123")
        self.assertEqual(store.get_turn("req-1")["content"], "answer")

    def test_orphan_sweep_retains_only_currently_active_conversation_urls(self):
        service, store = self.make_service()
        service.run_turn(
            "run-active",
            "req-active",
            "hello",
            lambda _request: {
                "status": "COMPLETED",
                "conversation_id": "active123",
                "conversation_url": "https://gemini.google.com/spark/chat/active123",
                "content": "answer",
            },
        )
        service._mark_active("run-active")
        calls = []
        try:
            result = service.sweep_orphan_targets(
                lambda request: calls.append(request) or {"status": "RELEASED"}
            )
        finally:
            service._mark_inactive("run-active")

        self.assertEqual(result, {"status": "RELEASED"})
        self.assertEqual(
            calls,
            [{"active_conversation_urls": ["https://gemini.google.com/spark/chat/active123"]}],
        )

    def test_orphan_sweep_protects_bound_timeout_for_post_dispatch_recovery(self):
        service, store = self.make_service()
        service.run_turn(
            "run-timeout",
            "req-timeout",
            "hello",
            lambda _request: {
                "status": "TIMEOUT",
                "conversation_id": "slow123",
                "conversation_url": "https://gemini.google.com/spark/chat/slow123",
                "content": None,
                "error": "response timed out",
            },
        )
        self.assertEqual(
            store.get_recovery_protected_conversation_urls(),
            ["https://gemini.google.com/spark/chat/slow123"],
        )
        calls = []

        result = service.sweep_orphan_targets(
            lambda request: calls.append(request) or {"status": "RELEASED"}
        )

        self.assertEqual(result, {"status": "RELEASED"})
        self.assertEqual(
            calls,
            [{"active_conversation_urls": ["https://gemini.google.com/spark/chat/slow123"]}],
        )

    def test_recovery_protection_expires_for_old_timeout(self):
        service, store = self.make_service()
        service.run_turn(
            "run-timeout",
            "req-timeout",
            "hello",
            lambda _request: {
                "status": "TIMEOUT",
                "conversation_id": "slow123",
                "conversation_url": "https://gemini.google.com/spark/chat/slow123",
                "content": None,
            },
        )

        self.assertEqual(store.get_recovery_protected_conversation_urls(max_age_seconds=0), [])

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
                "conversation_url": "https://gemini.google.com/spark/chat/slow123",
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
            [{"conversation_url": "https://gemini.google.com/spark/chat/slow123"}],
        )
        self.assertIsNone(store.get_run("run-1"))

    def test_cleanup_purges_stopped_unbound_task_timeout_without_remote_delete(self):
        service, store = self.make_service()
        turn = service.run_turn(
            "run-task-timeout",
            "req-task-timeout",
            "hello",
            lambda _request: {
                "status": "TIMEOUT",
                "execution_kind": "task",
                "remote_stopped": True,
                "conversation_id": None,
                "conversation_url": None,
                "content": None,
                "error": "task exceeded deadline",
            },
        )
        self.assertEqual(turn["execution_kind"], "task")
        self.assertIs(turn["remote_stopped"], True)
        replay = service.run_turn(
            "run-task-timeout",
            "req-task-timeout",
            "hello",
            lambda _request: self.fail("cached timeout must not redispatch"),
        )
        self.assertEqual(replay["execution_kind"], "task")
        self.assertIs(replay["remote_stopped"], True)
        self.assertTrue(replay["cached"])
        cleanup_calls = []

        result = service.cleanup_run(
            "run-task-timeout",
            lambda request: cleanup_calls.append(request),
        )

        self.assertEqual(result, {"status": "DELETED", "run_id": "run-task-timeout"})
        self.assertEqual(cleanup_calls, [])
        self.assertIsNone(store.get_run("run-task-timeout"))

    def test_cleanup_purges_local_only_run_after_proven_undispatched_status(self):
        for status in ("NOT_DISPATCHED", "TARGET_LOST", "AUTH_REQUIRED", "MODEL_MISMATCH", "BUSY"):
            with self.subTest(status=status):
                service, store = self.make_service()
                request_id = f"req-{status.lower()}"
                service.run_turn(
                    "run-1",
                    request_id,
                    "hello",
                    lambda _request, current=status: {
                        "status": current,
                        "conversation_id": None,
                        "conversation_url": None,
                        "content": None,
                    },
                )
                cleanup_calls = []

                result = service.cleanup_run(
                    "run-1",
                    lambda request: cleanup_calls.append(request) or {"status": "DELETED"},
                )

                self.assertEqual(result, {"status": "DELETED", "run_id": "run-1"})
                self.assertEqual(cleanup_calls, [])
                self.assertIsNone(store.get_run("run-1"))
                self.assertIsNone(store.get_turn(request_id))

    def test_cleanup_preserves_unbound_ambiguous_run_for_reconciliation(self):
        service, store = self.make_service()
        service.run_turn(
            "run-1",
            "req-1",
            "hello",
            lambda _request: {
                "status": "AMBIGUOUS",
                "conversation_id": None,
                "conversation_url": None,
                "content": None,
            },
        )
        cleanup_calls = []

        result = service.cleanup_run(
            "run-1",
            lambda request: cleanup_calls.append(request) or {"status": "DELETED"},
        )

        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(cleanup_calls, [])
        self.assertIsNotNone(store.get_run("run-1"))
        self.assertIsNotNone(store.get_turn("req-1"))


if __name__ == "__main__":
    unittest.main()

