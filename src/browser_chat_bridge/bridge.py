from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from typing import Any

from .store import BridgeStore


DriverCall = Callable[[dict[str, Any]], dict[str, Any]]


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _turn_payload(row: dict[str, Any], *, cached: bool) -> dict[str, Any]:
    return {
        "request_id": row["request_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "content": row.get("content"),
        "conversation_id": row.get("conversation_id"),
        "conversation_url": row.get("conversation_url"),
        "error": row.get("error"),
        "cached": cached,
    }


class BridgeService:
    """Run-scoped router. Browser/DOM knowledge stays in the Driver process."""

    def __init__(self, store: BridgeStore):
        self.store = store
        self._guard = threading.Lock()
        self._run_locks: dict[str, threading.Lock] = {}
        # Gemini may promote a new /app send into a separate durable target.
        # Serialize only unbound first turns so that target promotion can be
        # correlated without cross-run ambiguity; bound runs still parallelize.
        self._new_conversation_lock = threading.Lock()

    def _lock_for(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._run_locks.setdefault(run_id, threading.Lock())

    def run_turn(
        self,
        run_id: str,
        request_id: str,
        prompt: str,
        driver_call: DriverCall,
    ) -> dict[str, Any]:
        if not run_id.strip() or not request_id.strip() or not prompt.strip():
            raise ValueError("run_id, request_id, and prompt are required")

        with self._lock_for(run_id):
            turn, created = self.store.begin_turn(run_id, request_id, _prompt_hash(prompt))
            if not created:
                return _turn_payload(turn, cached=True)

            run = self.store.get_run(run_id)
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
                if not conversation_id or not conversation_url:
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

