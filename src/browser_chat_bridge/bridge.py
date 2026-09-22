from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from typing import Any

from .store import BridgeStore


DriverCall = Callable[[dict[str, Any]], dict[str, Any]]
PreDispatchCall = Callable[[], None]
DEFAULT_MAX_IN_FLIGHT = 2
LOCAL_ONLY_CLEANUP_STATUSES = frozenset(
    {"NOT_DISPATCHED", "TARGET_LOST", "AUTH_REQUIRED", "MODEL_MISMATCH", "BUSY", "COMPLETED"}
)


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _turn_payload(row: dict[str, Any], *, cached: bool) -> dict[str, Any]:
    payload = {
        "request_id": row["request_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "content": row.get("content"),
        "conversation_id": row.get("conversation_id"),
        "conversation_url": row.get("conversation_url"),
        "error": row.get("error"),
        "cached": cached,
    }
    raw = row.get("raw_result")
    if isinstance(raw, str) and raw:
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            result = None
        if isinstance(result, dict):
            if "execution_kind" in result:
                payload["execution_kind"] = result["execution_kind"]
            if "remote_stopped" in result:
                payload["remote_stopped"] = result["remote_stopped"]
    return payload


class BridgeService:
    """Run-scoped router. Browser/DOM knowledge stays in the Driver process."""

    def __init__(self, store: BridgeStore, *, max_in_flight: int = DEFAULT_MAX_IN_FLIGHT):
        if not isinstance(max_in_flight, int) or isinstance(max_in_flight, bool) or max_in_flight <= 0:
            raise ValueError("max_in_flight must be a positive integer")
        self.store = store
        self.max_in_flight = max_in_flight
        self._guard = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}
        self._capacity_guard = threading.Lock()
        self._in_flight = 0
        # Spark may promote a new /spark send into a separate durable target.
        # Serialize only unbound first turns so that target promotion can be
        # correlated without cross-run ambiguity; bound runs still parallelize.
        self._new_conversation_lock = threading.Lock()

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._run_locks.setdefault(run_id, threading.Lock())

    def _try_acquire_capacity(self) -> bool:
        with self._capacity_guard:
            if self._in_flight >= self.max_in_flight:
                return False
            self._in_flight += 1
            return True

    def _release_capacity(self) -> None:
        with self._capacity_guard:
            self._in_flight -= 1

    def run_turn(
        self,
        run_id: str,
        request_id: str,
        prompt: str,
        driver_call: DriverCall,
        *,
        before_dispatch: PreDispatchCall | None = None,
    ) -> dict[str, Any]:
        if not run_id.strip() or not request_id.strip() or not prompt.strip():
            raise ValueError("run_id, request_id, and prompt are required")

        with self._lock_for(run_id):
            turn, created = self.store.begin_turn(run_id, request_id, _prompt_hash(prompt))
            if not created:
                return _turn_payload(turn, cached=True)

            run = self.store.get_run(run_id)
            if not self._try_acquire_capacity():
                result = {
                    "status": "BUSY",
                    "conversation_id": None if run is None else run.get("conversation_id"),
                    "conversation_url": None if run is None else run.get("conversation_url"),
                    "content": None,
                    "error": f"browser-chat capacity unavailable (max in flight {self.max_in_flight}); not dispatched",
                }
                self.store.finish_turn(request_id, result)
                row = self.store.get_turn(request_id)
                assert row is not None
                return _turn_payload(row, cached=False)

            try:
                if before_dispatch is not None:
                    try:
                        before_dispatch()
                    except Exception as exc:
                        result = {
                            "status": "NOT_DISPATCHED",
                            "conversation_id": None if run is None else run.get("conversation_id"),
                            "conversation_url": None if run is None else run.get("conversation_url"),
                            "content": None,
                            "error": f"browser runtime unavailable before dispatch: {type(exc).__name__}",
                        }
                        self.store.finish_turn(request_id, result)
                        row = self.store.get_turn(request_id)
                        assert row is not None
                        return _turn_payload(row, cached=False)

                request = {
                    "request_id": request_id,
                    "conversation_url": None if run is None else run.get("conversation_url"),
                    "prompt": prompt,
                }
                try:
                    if request["conversation_url"] is None:
                        with self._new_conversation_lock:
                            result = dict(driver_call(request))
                    else:
                        result = dict(driver_call(request))
                except Exception as exc:
                    # The Driver call crossed a side-effecting boundary. A missing
                    # reply cannot prove that the browser did not submit, so never
                    # turn a transport failure into an automatic resend.
                    result = {
                        "status": "AMBIGUOUS",
                        "conversation_id": None if run is None else run.get("conversation_id"),
                        "conversation_url": None if run is None else run.get("conversation_url"),
                        "content": None,
                        "error": f"driver result unavailable after dispatch attempt: {type(exc).__name__}",
                    }

                if result.get("status") == "COMPLETED":
                    conversation_id = str(result.get("conversation_id") or "")
                    conversation_url = str(result.get("conversation_url") or "")
                    execution_kind = str(result.get("execution_kind") or "")
                    if not conversation_id and not conversation_url and execution_kind == "task":
                        pass
                    elif not conversation_id or not conversation_url:
                        result = {
                            **result,
                            "status": "AMBIGUOUS",
                            "error": "completed driver result did not carry a durable conversation binding",
                        }
                    elif run and run.get("conversation_id") and (
                        run["conversation_id"] != conversation_id
                        or run["conversation_url"] != conversation_url
                    ):
                        result = {
                            **result,
                            "status": "CONVERSATION_MISMATCH",
                            "content": None,
                            "error": "driver returned a different conversation for an existing run",
                        }
                    else:
                        self.store.bind_run(run_id, conversation_id, conversation_url)

                self.store.finish_turn(request_id, result)
                row = self.store.get_turn(request_id)
                assert row is not None
                return _turn_payload(row, cached=False)
            finally:
                self._release_capacity()

    def cleanup_run(self, run_id: str, driver_call: DriverCall) -> dict[str, Any]:
        """Delete a bound cloud conversation, or purge a proven local-only run."""
        if not run_id.strip():
            raise ValueError("run_id is required")

        # The same per-run lock makes cleanup wait for any in-flight turn to
        # settle before reading its durable conversation binding. This matters
        # when a caller cancels locally while the independent Driver continues.
        with self._lock_for(run_id):
            run = self.store.get_run(run_id)
            if run is None:
                return {"status": "NOT_FOUND", "run_id": run_id}
            conversation_url = str(run.get("conversation_url") or "")
            latest_turn = None
            if not conversation_url:
                latest_turn = self.store.get_latest_turn_for_run(run_id)
                conversation_url = str((latest_turn or {}).get("conversation_url") or "")
            if not conversation_url:
                if str((latest_turn or {}).get("status") or "") in LOCAL_ONLY_CLEANUP_STATUSES:
                    self.store.delete_run(run_id)
                    return {"status": "DELETED", "run_id": run_id}
                return {
                    "status": "UNRESOLVED",
                    "run_id": run_id,
                    "error": "run has no confirmed durable conversation binding",
                }
            try:
                result = dict(driver_call({"conversation_url": conversation_url}))
            except Exception as exc:
                return {
                    "status": "DELETE_FAILED",
                    "run_id": run_id,
                    "error": f"driver cleanup unavailable: {type(exc).__name__}",
                }
            status = str(result.get("status") or "DELETE_FAILED")
            if status in {"DELETED", "NOT_FOUND"}:
                self.store.delete_run(run_id)
                return {"status": status, "run_id": run_id}
            return {
                "status": status,
                "run_id": run_id,
                "error": result.get("error"),
            }

