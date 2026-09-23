from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.request
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from .nodriver_sync import connect_over_cdp

GEMINI_ORIGIN = "https://gemini.google.com"
NEW_CHAT_URL = f"{GEMINI_ORIGIN}/spark"

COMPOSER_SELECTOR = 'rich-textarea div[contenteditable="true"][role="textbox"]'
SEND_BUTTON_SELECTOR = (
    'div[data-test-id="send-button-container"] button[aria-label="プロンプトを送信"]'
)
USER_QUERY_SELECTOR = "user-query"
USER_QUERY_TEXT_SELECTOR = ".query-text p.query-text-line"
MODEL_RESPONSE_SELECTOR = "model-response"
MESSAGE_CONTENT_SELECTOR = "message-content .markdown"
COMPLETE_FOOTER_SELECTOR = ".response-footer.complete"
RESPONSE_UI_EXCLUDE_SELECTOR = ".attachment-container.action-card"
STOP_RESPONSE_SELECTOR = 'button[aria-label="回答を停止"], button[aria-label="Stop response"]'
CANCEL_GENERATION_DIALOG_SELECTOR = "mat-dialog-container response-generation-cancel-dialog"
CONFIRM_CANCEL_SELECTOR = 'button[data-test-id="confirm-cancel-button"]'
CONVERSATION_ACTIONS_SELECTOR = "conversation-actions-icon button"
DELETE_MENU_ITEM_SELECTOR = 'gem-menu-item[data-test-id="delete-button"]'
DELETE_DIALOG_SELECTOR = '[role="dialog"]'
DELETE_CONFIRM_SELECTOR = 'gem-button[cdkfocusinitial] button'
GOAL_CARD_SELECTOR = '[role="option"][id^="goal-c_"]'
AUTOMATION_PAGE_NAME_PREFIX = "browser-chat-bridge:"

CDP_CONNECT_TIMEOUT_MS = 30_000
CDP_CONNECT_ATTEMPTS = 3
PAGE_REATTACH_TIMEOUT_S = 30.0
UI_ACTION_INTERVAL_MIN_S = 3.0
UI_ACTION_INTERVAL_MAX_S = 6.0
BOUND_HISTORY_TIMEOUT_S = 30.0
DISPATCH_CONFIRM_TIMEOUT_S = 120.0
RECOVERY_RESPONSE_TIMEOUT_S = 45.0
ORPHAN_UNBOUND_GRACE_S = 30.0 * 60.0


def normalize_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def looks_like_tool_permission_surface(value: str) -> bool:
    """Detect Gemini connector/tool approval UI accidentally surfaced as answer text."""
    lines = [line.strip() for line in normalize_text(value).splitlines() if line.strip()]
    lowered = [line.casefold() for line in lines]
    if len(lines) < 4:
        return False
    has_request = any(line.startswith("let gemini use ") for line in lowered)
    has_tool = any(line.startswith("tool:") for line in lowered)
    has_deny = any(line in {"deny", "拒否"} for line in lowered)
    has_allow = any(line in {"allow", "許可"} for line in lowered)
    return has_request and has_tool and has_deny and has_allow


def prompt_matches(actual: str, expected: str) -> bool:
    return normalize_text(actual) == normalize_text(expected)


def composer_prompt_matches(paragraph_texts: list[str], expected: str) -> bool:
    """Compare Gemini rich-textarea paragraphs to the exact outbound prompt.

    Gemini represents each logical line as a ``<p>`` and an intentional blank
    line as an empty ``<p><br></p>``. Chromium ``innerText`` inserts extra
    layout linefeeds between those paragraphs, so it is not a faithful
    pre-dispatch read-back for multi-paragraph prompts. Reconstructing the
    logical text from the direct paragraph children preserves every requested
    blank line while keeping ordinary prompt confirmation strict.
    """
    logical_paragraphs: list[str] = []
    for text in paragraph_texts:
        paragraph = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        # Chromium exposes <p><br></p> as one layout newline through
        # all_inner_texts(). Semantically that node is the empty logical line
        # between the surrounding paragraphs.
        if paragraph == "\n":
            paragraph = ""
        logical_paragraphs.append(paragraph)
    actual = "\n".join(logical_paragraphs)
    return prompt_matches(actual, expected)


def query_prompt_matches(line_texts: list[str], expected: str) -> bool:
    """Compare persisted Gemini query-line nodes to the exact sent prompt.

    ``user-query`` uses the same paragraph semantics as the composer for a
    multi-line submission. Reuse the logical reconstruction so the
    post-click confirmation stays byte-meaningful across paragraph boundaries
    instead of weakening to substring/whitespace matching.
    """
    return composer_prompt_matches(line_texts, expected)


def recovery_observation(
    user_count: int,
    response_count: int,
    prompt_attributed: bool,
    response_content_count: int,
    response_complete: bool,
    response_busy: str | None,
    response_text: str,
) -> tuple[str, str | None]:
    """Classify one read-only recovery snapshot without dispatching anything.

    COMPLETE is returned only when the expected prompt is the latest user turn
    and the matching latest model response is structurally complete. WAIT means
    the expected turn is attributable but its response is not complete yet (or
    the page may still be hydrating). Impossible count shapes fail closed as
    AMBIGUOUS.
    """
    if user_count < 0 or response_count < 0:
        return "AMBIGUOUS", None
    if response_count > user_count or user_count - response_count > 1:
        return "AMBIGUOUS", None
    if not prompt_attributed:
        return "WAIT", None
    if response_count == user_count - 1:
        return "WAIT", None
    if response_count != user_count:
        return "AMBIGUOUS", None
    text = normalize_text(response_text)
    if (
        response_content_count == 1
        and response_complete
        and response_busy == "false"
        and text
    ):
        return "COMPLETE", text
    return "WAIT", None


def locator_text_contents(locator) -> list[str]:
    """Read persisted query lines without CSS whitespace collapsing.

    Gemini renders submitted prompts into normal HTML where ``innerText``
    collapses repeated spaces inside tool/schema prose. ``textContent`` keeps
    the original text-node spacing, allowing strict prompt confirmation without
    weakening comparison rules.
    """
    return [str(locator.nth(index).text_content() or "") for index in range(locator.count())]


def response_text(content) -> str:
    """Return model-authored response text without rendered action-card UI."""
    extractor = getattr(content, "inner_text_excluding", None)
    if callable(extractor):
        return str(extractor(RESPONSE_UI_EXCLUDE_SELECTOR) or "")
    return str(content.inner_text() or "")


def bound_history_hydrated(
    user_count: int,
    response_count: int,
    has_user_text: bool,
    has_response_content: bool,
    response_complete: bool,
) -> bool:
    """Whether an existing durable conversation is safe to continue.

    Gemini can expose the composer before its prior turns have hydrated. A
    bound run must never submit into that transient empty-looking state: wait
    until at least one complete historical user/model pair is structurally
    present. No message content is inspected or normalized here.
    """
    return (
        user_count > 0
        and user_count == response_count
        and has_user_text
        and has_response_content
        and response_complete
    )


def parse_conversation_id(url: str) -> str | None:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or parsed.netloc != "gemini.google.com":
        return None
    match = re.fullmatch(r"/spark/chat/([A-Za-z0-9_-]+)(?:/)?", parsed.path)
    return None if match is None else match.group(1)


def is_spark_task_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "gemini.google.com"
        and parsed.path.rstrip("/") == "/spark/tasks"
    )


def is_task_reopen_surface(url: str) -> bool:
    """Whether the current Spark surface can expose a completed task card."""
    return is_spark_home_url(url) or is_spark_task_url(url)


def is_spark_home_url(url: str) -> bool:
    parsed = urlsplit(str(url or ""))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "gemini.google.com"
        and parsed.path.rstrip("/") == "/spark"
    )


def fresh_spark_page_eligible(
    url: str,
    user_count: int,
    response_count: int,
    composer_ready: bool,
    stop_visible: bool,
) -> bool:
    """Whether one existing dedicated Spark home tab is safe for a fresh turn."""
    return (
        is_spark_home_url(url)
        and user_count == 0
        and response_count == 0
        and composer_ready
        and not stop_visible
    )


def unbound_owned_page_releasable(age_seconds: float | None) -> bool:
    """Whether an unresolved automation-owned Spark page is old enough to sweep."""
    return age_seconds is not None and age_seconds >= ORPHAN_UNBOUND_GRACE_S


def task_conversation_url_from_goal_id(goal_id: str) -> str | None:
    match = re.fullmatch(r"goal-c_([A-Za-z0-9_-]+)", str(goal_id or ""))
    if match is None:
        return None
    return f"{GEMINI_ORIGIN}/spark/chat/{match.group(1)}"


def unique_new_goal_id(baseline: set[str], current: set[str]) -> str | None:
    new_ids = current - baseline
    if not new_ids:
        return None
    if len(new_ids) != 1:
        raise ValueError("more than one new Gemini task appeared")
    return next(iter(new_ids))


def completed_task_binding(
    current_url: str,
    baseline_goal_ids: set[str],
    current_goal_ids: set[str],
) -> tuple[str | None, str | None, str | None]:
    """Resolve a completed Spark task into one durable chat binding.

    Returns conversation_id, conversation_url, and an optional goal id to
    reopen. A task that already promoted its page needs no reopen.
    """
    conversation_id = parse_conversation_id(current_url)
    if conversation_id is not None:
        return (
            conversation_id,
            f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}",
            None,
        )
    goal_id = unique_new_goal_id(baseline_goal_ids, current_goal_ids)
    if goal_id is None:
        return None, None, None
    conversation_url = task_conversation_url_from_goal_id(goal_id)
    if conversation_url is None:
        return None, None, None
    return parse_conversation_id(conversation_url), conversation_url, goal_id


def durable_urls_from_target_rows(rows: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in {None, "page"}:
            continue
        conversation_id = parse_conversation_id(str(row.get("url") or ""))
        if conversation_id is not None:
            urls.add(f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}")
    return urls


def target_url_from_rows(rows: list[dict[str, Any]], target_id: str) -> str | None:
    for row in rows:
        if not isinstance(row, dict) or str(row.get("id") or "") != target_id:
            continue
        if row.get("type") not in {None, "page"}:
            return None
        url = str(row.get("url") or "")
        return url or None
    return None


@dataclass(frozen=True)
class DriverResult:
    status: str
    conversation_id: str | None = None
    conversation_url: str | None = None
    content: str | None = None
    error: str | None = None
    execution_kind: str | None = None
    remote_stopped: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status,
            "conversation_id": self.conversation_id,
            "conversation_url": self.conversation_url,
            "content": self.content,
            "error": self.error,
        }
        if self.execution_kind is not None:
            result["execution_kind"] = self.execution_kind
        if self.remote_stopped is not None:
            result["remote_stopped"] = self.remote_stopped
        return result


@dataclass(frozen=True)
class DispatchState:
    baseline_responses: int
    baseline_goal_ids: frozenset[str]
    baseline_durable_urls: frozenset[str]


class GeminiDriver:
    """One Gemini UI turn over a caller-supplied nodriver CDP browser."""

    def __init__(
        self,
        cdp_endpoint: str,
        *,
        backend_kind: str = "chromium",
        promotion_timeout_s: float = 120.0,
        response_timeout_s: float = 900.0,
    ):
        self.cdp_endpoint = cdp_endpoint.rstrip("/")
        self.backend_kind = backend_kind
        self.promotion_timeout_s = max(15.0, float(promotion_timeout_s))
        self.response_timeout_s = max(10.0, float(response_timeout_s))
        # Edge/CDP target creation and immediate re-attach are a short shared
        # critical section. Keep that section serialized across concurrent
        # runs, but release the lock before dispatch/response waiting so
        # independent Gemini conversations can proceed in parallel.
        self._target_lock = threading.Lock()
        # Fresh Spark turns share one Task-list surface until Gemini promotes
        # them to durable /spark/chat/<id> conversations. If more than one
        # unbound first turn is in flight, historical/task hydration can make
        # ownership impossible to prove fail-closed. Serialize only that first
        # turn; bound continuations keep their normal parallelism.
        self._unbound_first_turn_lock = threading.Lock()
        # Only targets created explicitly by this Driver are eligible for
        # automatic tab release. Human/pre-existing Gemini tabs are never
        # entered here, even if they later show the same durable URL.
        self._owned_target_guard = threading.Lock()
        self._owned_targets: dict[str, str] = {}
        self._active_page_guard = threading.Lock()
        self._active_page_names: set[str] = set()

    def _turn_guard(self, requested_url: str | None):
        return self._unbound_first_turn_lock if requested_url is None else nullcontext()

    def _remember_owned_target(self, conversation_url: str, target_id: str | None) -> None:
        if target_id is None or parse_conversation_id(conversation_url) is None:
            return
        with self._owned_target_guard:
            self._owned_targets[conversation_url] = target_id

    def _forget_owned_target(self, conversation_url: str) -> str | None:
        with self._owned_target_guard:
            return self._owned_targets.pop(conversation_url, None)

    def _owned_target_snapshot(self) -> dict[str, str]:
        with self._owned_target_guard:
            return dict(self._owned_targets)

    def _mark_page_active(self, page_name: str | None) -> None:
        if not page_name:
            return
        with self._active_page_guard:
            self._active_page_names.add(page_name)

    def _mark_page_inactive(self, page_name: str | None) -> None:
        if not page_name:
            return
        with self._active_page_guard:
            self._active_page_names.discard(page_name)

    def _active_page_name_snapshot(self) -> set[str]:
        with self._active_page_guard:
            return set(self._active_page_names)

    def _connect_browser(self, _driver_client=None):
        """Attach to local CDP with bounded pre-action retries."""
        last_error = None
        for attempt in range(CDP_CONNECT_ATTEMPTS):
            try:
                return connect_over_cdp(self.cdp_endpoint)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < CDP_CONNECT_ATTEMPTS:
                    time.sleep(0.5)
        assert last_error is not None
        raise last_error

    def run_turn(self, request: dict[str, Any]) -> dict[str, Any]:
        requested_response_timeout = request.get("response_timeout_seconds")
        response_timeout_s = self.response_timeout_s
        if requested_response_timeout is not None:
            response_timeout_s = min(
                self.response_timeout_s,
                max(10.0, float(requested_response_timeout)),
            )
        turn_deadline = time.monotonic() + response_timeout_s
        prompt = str(request.get("prompt") or "")
        requested_url = request.get("conversation_url")
        if not prompt.strip():
            return DriverResult("NOT_DISPATCHED", error="prompt is blank").as_dict()
        if requested_url is not None and parse_conversation_id(str(requested_url)) is None:
            return DriverResult("CONVERSATION_LOST", error="invalid durable Gemini conversation URL").as_dict()

        with self._turn_guard(None if requested_url is None else str(requested_url)), nullcontext(None) as driver_client:
            browser = None
            active_page_name: str | None = None
            try:
                with self._target_lock:
                    try:
                        browser = self._connect_browser(driver_client)
                    except Exception as exc:
                        return DriverResult(
                            "TARGET_LOST",
                            error=f"CDP connect failed: {type(exc).__name__}",
                        ).as_dict()
                    if not browser.contexts:
                        return DriverResult("TARGET_LOST", error="CDP browser exposed no context").as_dict()
                    context = browser.contexts[0]
                    target_url = str(requested_url or f"{NEW_CHAT_URL}#bcb-{uuid.uuid4().hex}")
                    if requested_url is not None:
                        page = self._find_page(context, target_url)
                    else:
                        page = self._find_reusable_fresh_spark_page(context)
                    page_created_here = page is None
                    dispatch_target_id = (
                        str(getattr(page, "target_id", "") or "")
                        if page is not None
                        else ""
                    ) or None
                    if page is None:
                        if self.backend_kind == "obscura":
                            # Obscura is an automation-only headless process, so a
                            # normal CDP newPage has no Human tab/focus to protect.
                            try:
                                page = context.new_page()
                                page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                                dispatch_target_id = (
                                    str(getattr(page, "target_id", "") or "") or None
                                )
                            except Exception:
                                page = None
                        else:
                            dispatch_target_id = self._create_automation_target(browser, target_url)
                            if dispatch_target_id is None:
                                page = None
                            else:
                                # This Edge profile can omit a newly-created target
                                # from one nodriver attach even though /json/list
                                # already exposes it. Retry whole attaches; polling
                                # context.pages on one connection never discovers
                                # a target that was absent from that attach.
                                browser, page = self._reattach_find_page(
                                    driver_client,
                                    browser,
                                    target_url,
                                    target_id=dispatch_target_id,
                                )
                if page is None:
                    return DriverResult("TARGET_LOST", error="could not create or resolve Gemini page").as_dict()
                if page_created_here:
                    active_page_name = self._tag_automation_page(
                        page,
                        dispatch_target_id or uuid.uuid4().hex,
                    )
                else:
                    active_page_name = self._automation_page_name(page)
                self._mark_page_active(active_page_name)
                if requested_url is not None and not self._wait_for_bound_history(page):
                    conversation_id = parse_conversation_id(str(requested_url))
                    return DriverResult(
                        "CONVERSATION_LOST",
                        conversation_id=conversation_id,
                        conversation_url=str(requested_url),
                        error="bound Gemini conversation history did not hydrate before dispatch",
                    ).as_dict()
                dispatch = self._dispatch_page_turn(
                    page,
                    prompt,
                    allow_count_only_confirmation=requested_url is not None,
                    paced=True,
                )
                if isinstance(dispatch, DriverResult):
                    # A fresh marker tab is automation-owned and never a
                    # durable conversation. Close it on every terminal result,
                    # including AMBIGUOUS: the Bridge never blind-retries that
                    # request, and leaving marker targets behind can wedge a
                    # later nodriver CDP attach. Pre-existing Human tabs are
                    # never closed here.
                    if page_created_here:
                        self._close_page_quietly(page)
                    return dispatch.as_dict()

                if requested_url is None:
                    destination, promoted_url = self._wait_first_turn_destination(
                        page,
                        dispatch_target_id,
                        baseline_durable_urls=dispatch.baseline_durable_urls,
                        baseline_goal_ids=dispatch.baseline_goal_ids,
                        deadline=turn_deadline,
                    )
                    if destination == "task":
                        response = self._wait_response(
                            page,
                            dispatch.baseline_responses,
                            None,
                            None,
                            prompt,
                            execution_kind="task",
                            baseline_goal_ids=dispatch.baseline_goal_ids,
                            deadline=turn_deadline,
                        )
                        promoted_id = None
                        promoted_url = None
                        if response.status == "COMPLETED":
                            promoted_id, promoted_url = self._wait_owned_task_promotion(page)
                            goal_id_to_reopen = None
                            if promoted_id is None:
                                baseline_goals = set(dispatch.baseline_goal_ids)
                                promoted_id, promoted_url, goal_id_to_reopen = self._wait_correlated_task_binding(
                                    page,
                                    baseline_goals,
                                )
                            if goal_id_to_reopen is not None and promoted_url is not None:
                                card = page.locator(f"#{goal_id_to_reopen}")
                                if card.count() != 1:
                                    return DriverResult(
                                        "AMBIGUOUS",
                                        error="new Gemini task could not be uniquely reopened",
                                        execution_kind="task",
                                    ).as_dict()
                                try:
                                    card.click(timeout=5_000)
                                    page.wait_for_url(promoted_url, timeout=5_000)
                                except Exception as exc:
                                    return DriverResult(
                                        "AMBIGUOUS",
                                        error=(
                                            "completed Gemini task could not be reopened as durable chat: "
                                            f"{type(exc).__name__}"
                                        ),
                                        execution_kind="task",
                                    ).as_dict()
                        if response.status == "TIMEOUT":
                            stopped = self._stop_owned_task(page)
                            response = replace(response, remote_stopped=stopped)
                            if stopped and page_created_here:
                                self._close_page_quietly(page)
                        if (
                            response.status == "COMPLETED"
                            and promoted_id is not None
                            and promoted_url is not None
                        ):
                            response = DriverResult(
                                "COMPLETED",
                                promoted_id,
                                promoted_url,
                                content=response.content,
                                execution_kind="chat",
                            )
                        if page_created_here and response.status in {"COMPLETED", "TASK_FAILED"}:
                            self._close_page_quietly(page)
                        return response.as_dict()
                    if destination != "chat" or promoted_url is None:
                        return DriverResult(
                            "AMBIGUOUS",
                            error="user turn persisted but durable conversation target was not correlated",
                        ).as_dict()
                    # Do not close the automation-owned page before resolving
                    # the durable route. Spark promotes the same browser
                    # target from /spark#bcb-* to /spark/chat/<conversation-id> on this
                    # Edge profile. Closing the marker page here therefore
                    # closes the promoted conversation itself and makes the
                    # subsequent attach unable to enumerate it. Disconnecting
                    # the nodriver client is sufficient; it does not own the
                    # browser target.
                    try:
                        browser, page = self._reattach_find_page(
                            driver_client,
                            browser,
                            promoted_url,
                            target_id=dispatch_target_id,
                        )
                    except Exception as exc:
                        return DriverResult(
                            "AMBIGUOUS",
                            error=f"user turn persisted but CDP reattach failed: {type(exc).__name__}",
                        ).as_dict()
                    if page is None:
                        return DriverResult(
                            "AMBIGUOUS",
                            error="user turn persisted but promoted conversation page did not hydrate after reattach",
                        ).as_dict()
                    conversation_id = parse_conversation_id(page.url)
                else:
                    conversation_id = parse_conversation_id(str(requested_url))

                if conversation_id is None:
                    return DriverResult(
                        "AMBIGUOUS",
                        error="user turn persisted but durable conversation id was not resolved",
                    ).as_dict()
                conversation_url = f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}"
                if requested_url is None and page_created_here:
                    self._remember_owned_target(conversation_url, dispatch_target_id)
                response = self._wait_response(
                    page,
                    dispatch.baseline_responses,
                    conversation_id,
                    conversation_url,
                    prompt,
                    deadline=turn_deadline,
                )
                # Keep an automation-created durable chat target alive across
                # continuation turns. Reopening the same Gemini conversation by
                # URL can trigger Google's anti-automation "sorry" surface on
                # this Edge profile, while reusing the already-promoted target
                # is both cheaper and unambiguous. Pre-existing Human tabs are
                # still never closed here; terminal cleanup owns removal of
                # automation conversations when requested.
                return response.as_dict()
            finally:
                self._mark_page_inactive(active_page_name)
                # nodriver disconnects this client only; it does not own the
                # attached Chrome/Obscura process.
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    @staticmethod
    def _close_cdp_target(browser, target_id: str) -> bool:
        session = browser.new_browser_cdp_session()
        try:
            result = session.send("Target.closeTarget", {"targetId": target_id})
        finally:
            session.detach()
        return bool((result or {}).get("success", False))

    @staticmethod
    def _tag_automation_page(page, ownership_id: str) -> str | None:
        name = AUTOMATION_PAGE_NAME_PREFIX + str(ownership_id)
        try:
            page.evaluate(
                "name => { window.name = name; }",
                name,
            )
        except Exception:
            return None
        return name

    @staticmethod
    def _automation_page_name(page) -> str | None:
        try:
            value = str(page.evaluate("() => window.name") or "")
        except Exception:
            return None
        return value if value.startswith(AUTOMATION_PAGE_NAME_PREFIX) else None

    @staticmethod
    def _page_is_automation_owned(page) -> bool:
        return GeminiDriver._automation_page_name(page) is not None

    @staticmethod
    def _page_age_seconds(page) -> float | None:
        try:
            value = float(
                page.evaluate("() => Math.max(0, (Date.now() - performance.timeOrigin) / 1000)")
            )
        except Exception:
            return None
        return max(0.0, value)

    def release_target(self, request: dict[str, Any]) -> dict[str, Any]:
        """Close one Driver-owned browser target without deleting cloud state."""
        conversation_url = str(request.get("conversation_url") or "")
        conversation_id = parse_conversation_id(conversation_url)
        if conversation_id is None:
            return {"status": "RELEASE_FAILED", "error": "invalid durable Gemini conversation URL"}
        target_id = self._owned_target_snapshot().get(conversation_url)

        with nullcontext(None) as driver_client:
            browser = None
            try:
                browser = self._connect_browser(driver_client)
                if target_id is not None:
                    current_url = self._cdp_target_url(target_id)
                    if current_url is None:
                        self._forget_owned_target(conversation_url)
                        return {"status": "RELEASED", "conversation_url": conversation_url, "already_closed": True}
                    if current_url == conversation_url and self._close_cdp_target(browser, target_id):
                        self._forget_owned_target(conversation_url)
                        return {"status": "RELEASED", "conversation_url": conversation_url}
                    self._forget_owned_target(conversation_url)

                if not browser.contexts:
                    return {"status": "RELEASE_FAILED", "conversation_url": conversation_url, "error": "no context"}
                for page in list(browser.contexts[0].pages):
                    try:
                        if str(page.url or "") != conversation_url:
                            continue
                        if not self._page_is_automation_owned(page):
                            continue
                        self._close_page_quietly(page)
                        return {"status": "RELEASED", "conversation_url": conversation_url}
                    except Exception:
                        continue
                return {"status": "NOT_OWNED", "conversation_url": conversation_url}
            except Exception as exc:
                return {
                    "status": "RELEASE_FAILED",
                    "conversation_url": conversation_url,
                    "error": type(exc).__name__,
                }
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def release_orphan_targets(self, request: dict[str, Any]) -> dict[str, Any]:
        """Release owned targets except conversations explicitly marked active."""
        active_raw = request.get("active_conversation_urls") or []
        if not isinstance(active_raw, list):
            return {"status": "RELEASE_FAILED", "error": "active_conversation_urls must be a list"}
        active = {str(value) for value in active_raw if parse_conversation_id(str(value)) is not None}
        released: list[str] = []
        retained: list[str] = []
        failed: list[str] = []
        active_page_names = self._active_page_name_snapshot()
        with nullcontext(None) as driver_client:
            browser = None
            try:
                browser = self._connect_browser(driver_client)
                if not browser.contexts:
                    return {"status": "RELEASE_FAILED", "error": "CDP browser exposed no context"}
                for page in list(browser.contexts[0].pages):
                    try:
                        page_name = self._automation_page_name(page)
                        if page_name is None:
                            continue
                        page_url = str(page.url or "")
                        conversation_url = page_url if parse_conversation_id(page_url) is not None else ""
                        identifier = conversation_url or page_name
                        if page_name in active_page_names:
                            retained.append(identifier)
                            continue
                        if conversation_url in active:
                            retained.append(identifier)
                            continue
                        if not conversation_url and not unbound_owned_page_releasable(
                            self._page_age_seconds(page)
                        ):
                            retained.append(identifier)
                            continue
                        self._close_page_quietly(page)
                        if conversation_url:
                            self._forget_owned_target(conversation_url)
                        released.append(identifier)
                    except Exception:
                        failed.append(str(getattr(page, "url", "") or "unknown"))
            except Exception as exc:
                return {"status": "RELEASE_FAILED", "error": type(exc).__name__}
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
        return {
            "status": "RELEASED" if not failed else "PARTIAL",
            "released": released,
            "retained": retained,
            "failed": failed,
        }

    def recover_turn(self, request: dict[str, Any]) -> dict[str, Any]:
        """Recover an already-dispatched bound turn without resending it."""
        prompt = str(request.get("prompt") or "")
        conversation_url = str(request.get("conversation_url") or "")
        conversation_id = parse_conversation_id(conversation_url)
        if not prompt.strip():
            return DriverResult("AMBIGUOUS", error="recovery prompt is blank").as_dict()
        if conversation_id is None:
            return DriverResult(
                "CONVERSATION_LOST",
                error="recovery requires a valid durable Gemini conversation URL",
            ).as_dict()

        with nullcontext(None) as driver_client:
            browser = None
            try:
                try:
                    browser = self._connect_browser(driver_client)
                except Exception as exc:
                    return DriverResult(
                        "TARGET_LOST",
                        conversation_id,
                        conversation_url,
                        error=f"recovery CDP connect failed: {type(exc).__name__}",
                    ).as_dict()
                if not browser.contexts:
                    return DriverResult(
                        "TARGET_LOST",
                        conversation_id,
                        conversation_url,
                        error="recovery CDP browser exposed no context",
                    ).as_dict()
                page = self._find_page(browser.contexts[0], conversation_url)
                if page is None:
                    browser, page = self._reattach_find_page(
                        driver_client,
                        browser,
                        conversation_url,
                        timeout_s=5.0,
                    )
                if page is None:
                    return DriverResult(
                        "TARGET_LOST",
                        conversation_id,
                        conversation_url,
                        error="recovery could not resolve existing Gemini conversation target",
                    ).as_dict()

                deadline = time.monotonic() + RECOVERY_RESPONSE_TIMEOUT_S
                attributed = False
                stable_text: str | None = None
                stable_samples = 0
                while time.monotonic() < deadline:
                    try:
                        users = page.locator(USER_QUERY_SELECTOR)
                        responses = page.locator(MODEL_RESPONSE_SELECTOR)
                        user_count = users.count()
                        response_count = responses.count()
                        prompt_attributed = False
                        if user_count > 0:
                            latest_query = users.last.locator(USER_QUERY_TEXT_SELECTOR)
                            prompt_attributed = (
                                latest_query.count() > 0
                                and query_prompt_matches(
                                    locator_text_contents(latest_query),
                                    prompt,
                                )
                            )
                        attributed = attributed or prompt_attributed

                        content_count = 0
                        response_complete = False
                        response_busy = None
                        response_text = ""
                        if response_count > 0:
                            response = responses.last
                            content = response.locator(MESSAGE_CONTENT_SELECTOR)
                            content_count = content.count()
                            response_complete = (
                                response.locator(COMPLETE_FOOTER_SELECTOR).count() > 0
                            )
                            if content_count == 1:
                                response_busy = content.get_attribute("aria-busy")
                                response_text = response_text(content)
                                if looks_like_tool_permission_surface(response_text):
                                    return DriverResult(
                                        "AMBIGUOUS",
                                        conversation_id,
                                        conversation_url,
                                        error="Gemini tool-permission surface appeared inside model response",
                                        execution_kind="chat",
                                    ).as_dict()

                        state, text = recovery_observation(
                            user_count,
                            response_count,
                            prompt_attributed,
                            content_count,
                            response_complete,
                            response_busy,
                            response_text,
                        )
                        if state == "AMBIGUOUS":
                            return DriverResult(
                                "AMBIGUOUS",
                                conversation_id,
                                conversation_url,
                                error="recovery observed an impossible user/response count shape",
                            ).as_dict()
                        if state == "COMPLETE" and text is not None:
                            if text == stable_text:
                                stable_samples += 1
                            else:
                                stable_text = text
                                stable_samples = 1
                            if stable_samples >= 2:
                                return DriverResult(
                                    "COMPLETED",
                                    conversation_id,
                                    conversation_url,
                                    content=text,
                                    execution_kind="chat",
                                ).as_dict()
                    except Exception:
                        pass
                    time.sleep(0.35)

                if attributed:
                    return DriverResult(
                        "TIMEOUT",
                        conversation_id,
                        conversation_url,
                        error="recovery attributed the user turn but response is still incomplete",
                        execution_kind="chat",
                    ).as_dict()
                return DriverResult(
                    "AMBIGUOUS",
                    conversation_id,
                    conversation_url,
                    error="recovery could not attribute the expected prompt to the latest user turn",
                    execution_kind="chat",
                ).as_dict()
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def delete_conversation(self, request: dict[str, Any]) -> dict[str, Any]:
        """Delete exactly one durable Gemini conversation through its UI."""
        conversation_url = str(request.get("conversation_url") or "")
        conversation_id = parse_conversation_id(conversation_url)
        if conversation_id is None:
            return DriverResult("DELETE_FAILED", error="invalid durable Gemini conversation URL").as_dict()

        with nullcontext(None) as driver_client:
            browser = None
            page = None
            page_created_here = False
            try:
                try:
                    browser = self._connect_browser(driver_client)
                except Exception as exc:
                    return DriverResult("TARGET_LOST", error=f"CDP connect failed: {type(exc).__name__}").as_dict()
                if not browser.contexts:
                    return DriverResult("TARGET_LOST", error="CDP browser exposed no context").as_dict()
                context = browser.contexts[0]
                page = self._find_page(context, conversation_url)
                page_created_here = page is None
                if page is None:
                    if self.backend_kind == "obscura":
                        try:
                            page = context.new_page()
                            page.goto(conversation_url, wait_until="domcontentloaded", timeout=30_000)
                        except Exception:
                            page = None
                    elif self._create_automation_target(browser, conversation_url):
                        browser, page = self._reattach_find_page(
                            driver_client,
                            browser,
                            conversation_url,
                        )
                if page is None:
                    return DriverResult("DELETE_FAILED", error="conversation page could not be resolved").as_dict()
                if "accounts.google.com" in page.url:
                    return DriverResult("AUTH_REQUIRED", error="Gemini authentication is required").as_dict()

                actions = page.locator(CONVERSATION_ACTIONS_SELECTOR)
                try:
                    actions.wait_for(state="visible", timeout=30_000)
                except Exception:
                    if parse_conversation_id(page.url) != conversation_id:
                        return DriverResult("NOT_FOUND").as_dict()
                    if self._deleted_conversation_surface(page):
                        return DriverResult("NOT_FOUND").as_dict()
                    return DriverResult("DELETE_FAILED", error="conversation actions did not become ready").as_dict()
                try:
                    actions.click(timeout=5_000)
                    delete_item = page.locator(DELETE_MENU_ITEM_SELECTOR)
                    delete_item.wait_for(state="visible", timeout=5_000)
                    delete_item.click(timeout=5_000)
                    dialog = page.locator(DELETE_DIALOG_SELECTOR)
                    dialog.wait_for(state="visible", timeout=5_000)
                    confirm = dialog.locator(DELETE_CONFIRM_SELECTOR)
                    if confirm.count() != 1:
                        return DriverResult("DELETE_FAILED", error="delete confirmation was not uniquely ready").as_dict()
                    confirm.click(timeout=5_000)
                except Exception as exc:
                    return DriverResult("DELETE_FAILED", error=f"delete UI action failed: {type(exc).__name__}").as_dict()

                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    try:
                        if page.is_closed() or parse_conversation_id(page.url) != conversation_id:
                            return DriverResult("DELETED").as_dict()
                        if self._deleted_conversation_surface(page):
                            return DriverResult("DELETED").as_dict()
                    except Exception:
                        return DriverResult("DELETED").as_dict()
                    time.sleep(0.2)
                # Gemini can keep the deleted conversation URL in the address
                # bar while clearing its server-side history. Reload the exact
                # durable URL once and verify it resolves to the empty composer
                # surface rather than requiring a route change as proof.
                try:
                    page.goto(conversation_url, wait_until="domcontentloaded", timeout=30_000)
                    ready_deadline = time.monotonic() + 10.0
                    while time.monotonic() < ready_deadline:
                        if self._deleted_conversation_surface(page):
                            return DriverResult("DELETED").as_dict()
                        time.sleep(0.2)
                except Exception:
                    pass
                return DriverResult("DELETE_FAILED", error="Gemini did not confirm conversation removal").as_dict()
            finally:
                if page_created_here and page is not None:
                    self._close_page_quietly(page)
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    @staticmethod
    def _deleted_conversation_surface(page) -> bool:
        """Whether a durable URL now resolves to Gemini's empty-chat surface."""
        try:
            composer = page.locator(COMPOSER_SELECTOR)
            return (
                composer.count() == 1
                and composer.is_visible()
                and page.locator(USER_QUERY_SELECTOR).count() == 0
                and page.locator(CONVERSATION_ACTIONS_SELECTOR).count() == 0
            )
        except Exception:
            return False

    @staticmethod
    def _close_page_quietly(page) -> None:
        try:
            page.close()
        except Exception:
            pass

    @staticmethod
    def _find_page(context, url: str):
        expected = urlsplit(url)
        exact_path = expected.path.rstrip("/")
        for page in context.pages:
            parsed = urlsplit(page.url)
            if parsed.netloc != "gemini.google.com" or parsed.path.rstrip("/") != exact_path:
                continue
            if expected.fragment and parsed.fragment != expected.fragment:
                continue
            if not expected.fragment and parsed.fragment:
                continue
            return page
        return None

    def _find_reusable_fresh_spark_page(self, context):
        """Reuse one exact empty Spark home tab from the dedicated profile.

        Fresh turns are serialized by _unbound_first_turn_lock. Reuse is
        allowed only when exactly one existing page is structurally empty and
        ready. Ambiguity falls back to creating a new automation-owned target.
        The reused page is deliberately not tagged or registered as owned, so
        normal release/orphan cleanup never closes a pre-existing tab.
        """
        candidates = []
        for page in list(context.pages):
            try:
                users = page.locator(USER_QUERY_SELECTOR)
                responses = page.locator(MODEL_RESPONSE_SELECTOR)
                stops = page.locator(STOP_RESPONSE_SELECTOR)
                stop_visible = any(
                    stops.nth(index).is_visible()
                    for index in range(stops.count())
                )
                if fresh_spark_page_eligible(
                    str(page.url or ""),
                    users.count(),
                    responses.count(),
                    self._ready_composer(page) is not None,
                    stop_visible,
                ):
                    candidates.append(page)
            except Exception:
                continue
        return candidates[0] if len(candidates) == 1 else None

    def _reattach_find_page(
        self,
        driver_client,
        browser,
        url: str,
        *,
        target_id: str | None = None,
        timeout_s: float = PAGE_REATTACH_TIMEOUT_S,
    ):
        """Reconnect until one attach initially enumerates the exact target.

        In the shared Edge profile, a target created through browser-level CDP
        can be visible in ``/json/list`` before nodriver includes it in
        ``context.pages``. The page list on that connection then stays stale,
        so only a fresh attach can make progress.
        """
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        current = browser
        while time.monotonic() < deadline:
            try:
                current.close()
            except Exception:
                pass
            time.sleep(0.25)
            current = self._connect_browser(driver_client)
            if current.contexts:
                lookup_url = url
                if target_id is not None:
                    redirected = self._cdp_target_url(target_id)
                    if redirected:
                        lookup_url = redirected
                page = self._find_page(current.contexts[0], lookup_url)
                if page is not None:
                    return current, page
        return current, None

    @staticmethod
    def _create_automation_target(browser, url: str) -> str | None:
        try:
            session = browser.new_browser_cdp_session()
            try:
                result = session.send("Target.createTarget", {"url": url, "background": False})
            finally:
                session.detach()
        except Exception:
            return None
        target_id = str((result or {}).get("targetId") or "")
        return target_id or None

    def _wait_for_ready(self, page) -> bool:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if "accounts.google.com" in page.url:
                return False
            try:
                if self._ready_composer(page) is not None:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    @staticmethod
    def _ready_composer(page):
        composers = page.locator(COMPOSER_SELECTOR)
        if composers.count() < 1:
            return None
        composer = composers.last
        return composer if composer.is_visible() else None

    @staticmethod
    def _wait_unique_visible(locator, timeout_s: float = 5.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                count = locator.count()
                if count == 1:
                    if locator.is_visible():
                        return locator
                    visible = []
                else:
                    visible = [
                        locator.nth(index)
                        for index in range(count)
                        if locator.nth(index).is_visible()
                    ]
                if len(visible) == 1:
                    return visible[0]
            except Exception:
                pass
            time.sleep(0.1)
        return None

    def _wait_for_bound_history(self, page) -> bool:
        deadline = time.monotonic() + BOUND_HISTORY_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                users = page.locator(USER_QUERY_SELECTOR)
                responses = page.locator(MODEL_RESPONSE_SELECTOR)
                user_count = users.count()
                response_count = responses.count()
                has_user_text = (
                    user_count > 0
                    and users.last.locator(USER_QUERY_TEXT_SELECTOR).count() > 0
                )
                has_response_content = (
                    response_count > 0
                    and responses.last.locator(MESSAGE_CONTENT_SELECTOR).count() == 1
                )
                response_complete = (
                    response_count > 0
                    and responses.last.locator(COMPLETE_FOOTER_SELECTOR).count() > 0
                )
                if bound_history_hydrated(
                    user_count,
                    response_count,
                    has_user_text,
                    has_response_content,
                    response_complete,
                ):
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    @staticmethod
    def _counts(page) -> tuple[int, int]:
        return page.locator(USER_QUERY_SELECTOR).count(), page.locator(MODEL_RESPONSE_SELECTOR).count()

    @staticmethod
    def _goal_ids(page) -> set[str]:
        try:
            cards = page.locator(GOAL_CARD_SELECTOR)
            values = {
                str(cards.nth(index).get_attribute("id") or "")
                for index in range(cards.count())
            }
        except Exception:
            return set()
        return {
            value
            for value in values
            if task_conversation_url_from_goal_id(value) is not None
        }

    @staticmethod
    def _selected_new_goal_id(page, baseline_goal_ids: set[str]) -> str | None:
        try:
            cards = page.locator(GOAL_CARD_SELECTOR)
            selected: list[str] = []
            for index in range(cards.count()):
                card = cards.nth(index)
                goal_id = str(card.get_attribute("id") or "")
                current = (
                    card.get_attribute("aria-selected") == "true"
                    or card.get_attribute("tabindex") == "0"
                )
                if (
                    current
                    and goal_id not in baseline_goal_ids
                    and task_conversation_url_from_goal_id(goal_id) is not None
                ):
                    selected.append(goal_id)
        except Exception:
            return None
        return selected[0] if len(selected) == 1 else None

    def _wait_correlated_task_binding(
        self,
        page,
        baseline_goal_ids: set[str],
        *,
        timeout_s: float = 5.0,
    ) -> tuple[str | None, str | None, str | None]:
        """Wait until one automation-owned Task is positively correlatable."""
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while time.monotonic() < deadline:
            conversation_id = parse_conversation_id(str(page.url or ""))
            if conversation_id is not None:
                return (
                    conversation_id,
                    f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}",
                    None,
                )

            selected_goal_id = self._selected_new_goal_id(page, baseline_goal_ids)
            if selected_goal_id is not None:
                conversation_url = task_conversation_url_from_goal_id(selected_goal_id)
                return (
                    parse_conversation_id(conversation_url or ""),
                    conversation_url,
                    selected_goal_id,
                )

            try:
                new_goal_id = unique_new_goal_id(baseline_goal_ids, self._goal_ids(page))
            except ValueError:
                # Spark can hydrate the full historical Task list in one burst.
                # That is not evidence that this automation dispatched multiple
                # tasks, so keep waiting for the owned card to become selected.
                new_goal_id = None
            if new_goal_id is not None:
                conversation_url = task_conversation_url_from_goal_id(new_goal_id)
                return (
                    parse_conversation_id(conversation_url or ""),
                    conversation_url,
                    new_goal_id,
                )
            time.sleep(0.1)
        return None, None, None

    @staticmethod
    def _wait_owned_task_promotion(
        page,
        timeout_s: float = 5.0,
    ) -> tuple[str | None, str | None]:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                conversation_id = parse_conversation_id(str(page.url or ""))
                if conversation_id is not None:
                    return (
                        conversation_id,
                        f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}",
                    )
            except Exception:
                pass
            time.sleep(0.1)
        return None, None

    def _cdp_target_rows(self) -> list[dict[str, Any]]:
        try:
            with urllib.request.urlopen(
                self.cdp_endpoint + "/json/list",
                timeout=2.0,
            ) as response:
                rows = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return []
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _cdp_target_url(self, target_id: str | None) -> str | None:
        if not target_id:
            return None
        return target_url_from_rows(self._cdp_target_rows(), target_id)

    def _wait_first_turn_destination(
        self,
        page,
        target_id: str | None,
        *,
        baseline_durable_urls: frozenset[str] | None = None,
        baseline_goal_ids: frozenset[str] | None = None,
        deadline: float | None = None,
    ) -> tuple[str | None, str | None]:
        promotion_deadline = time.monotonic() + self.promotion_timeout_s
        if deadline is not None:
            promotion_deadline = min(promotion_deadline, deadline)
        while time.monotonic() < promotion_deadline:
            target_url = self._cdp_target_url(target_id) or str(page.url or "")
            conversation_id = parse_conversation_id(target_url)
            if conversation_id is not None:
                return "chat", f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}"
            if is_spark_task_url(target_url):
                return "task", None
            if baseline_goal_ids is not None:
                baseline_goals = set(baseline_goal_ids)
                if self._selected_new_goal_id(page, baseline_goals) is not None:
                    return "task", None
                try:
                    new_goal_id = unique_new_goal_id(
                        baseline_goals,
                        self._goal_ids(page),
                    )
                except ValueError:
                    # Spark can hydrate multiple historical cards at once.
                    # That is not enough to attribute a Task to this send;
                    # keep waiting for a single owned delta or selection.
                    new_goal_id = None
                if new_goal_id is not None:
                    return "task", None
            if baseline_durable_urls is not None:
                current_durable_urls = durable_urls_from_target_rows(self._cdp_target_rows())
                new_durable_urls = current_durable_urls - set(baseline_durable_urls)
                if len(new_durable_urls) == 1:
                    return "chat", next(iter(new_durable_urls))
                if len(new_durable_urls) > 1:
                    return "ambiguous", None
            time.sleep(0.15)
        return None, None

    @staticmethod
    def _stop_owned_task(page, *, timeout_s: float = 10.0) -> bool:
        def terminal() -> bool:
            responses = page.locator(MODEL_RESPONSE_SELECTOR)
            if responses.count() < 1:
                return False
            response = responses.last
            content = response.locator(MESSAGE_CONTENT_SELECTOR)
            if response.locator(COMPLETE_FOOTER_SELECTOR).count() > 0:
                return True
            return content.count() == 1 and content.get_attribute("aria-busy") == "false"

        try:
            stop = page.locator(STOP_RESPONSE_SELECTOR)
            if stop.count() != 1 or not stop.is_visible():
                return terminal()
            stop.click(timeout=5_000)
        except Exception:
            return False

        deadline = time.monotonic() + max(0.1, float(timeout_s))
        while time.monotonic() < deadline:
            try:
                dialog = page.locator(CANCEL_GENERATION_DIALOG_SELECTOR)
                if dialog.count() == 1 and dialog.is_visible():
                    confirm = dialog.locator(CONFIRM_CANCEL_SELECTOR)
                    if confirm.count() != 1 or not confirm.is_visible():
                        return False
                    confirm.click(timeout=5_000)
                    time.sleep(0.1)
                    continue
                stop = page.locator(STOP_RESPONSE_SELECTOR)
                if stop.count() == 0 or not stop.is_visible():
                    return True
                if terminal():
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def _settled_counts(self, page) -> tuple[int, int]:
        previous = self._counts(page)
        stable = 0
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            current = self._counts(page)
            if current == previous:
                stable += 1
                if stable >= 2:
                    return current
            else:
                previous = current
                stable = 0
        return previous

    def _dispatch_page_turn(
        self,
        page,
        prompt: str,
        *,
        allow_count_only_confirmation: bool = False,
        paced: bool = False,
    ) -> DriverResult | DispatchState:
        if not self._wait_for_ready(page):
            status = "AUTH_REQUIRED" if "accounts.google.com" in page.url else "TARGET_LOST"
            return DriverResult(status, error="Gemini composer did not become ready")

        baseline_users, baseline_responses = self._settled_counts(page)
        baseline_goal_ids = frozenset(self._goal_ids(page))
        baseline_durable_urls = frozenset(durable_urls_from_target_rows(self._cdp_target_rows()))
        if baseline_users != baseline_responses:
            return DriverResult("BUSY", error="conversation has an unmatched in-flight turn")

        composer = self._ready_composer(page)
        if composer is None:
            return DriverResult("NOT_DISPATCHED", error="Gemini composer disappeared before dispatch")
        try:
            if paced:
                time.sleep(random.uniform(UI_ACTION_INTERVAL_MIN_S, UI_ACTION_INTERVAL_MAX_S))
            composer.fill(prompt)
            paragraphs = composer.locator(":scope > p")
            composer_matches = (
                composer_prompt_matches(paragraphs.all_inner_texts(), prompt)
                if paragraphs.count() > 0
                else prompt_matches(composer.inner_text(), prompt)
            )
            if not composer_matches:
                return DriverResult("NOT_DISPATCHED", error="composer read-back mismatch")
            send = self._wait_unique_visible(page.locator(SEND_BUTTON_SELECTOR))
            if send is None:
                return DriverResult("NOT_DISPATCHED", error="send button is not uniquely ready")
            if paced:
                time.sleep(random.uniform(UI_ACTION_INTERVAL_MIN_S, UI_ACTION_INTERVAL_MAX_S))
            # Match the successful manual Browser Chat path: keep the Send
            # button only as a readiness proof, then submit from the focused
            # composer with a trusted Enter key. Direct button clicks on Spark
            # can promote otherwise-chat prompts into the Task surface, where
            # repeated live canaries have stalled at "Initializing task".
            composer.press("Enter", timeout=5_000)
        except Exception as exc:
            detail = normalize_text(str(exc))
            suffix = f": {detail[:240]}" if detail else ""
            return DriverResult(
                "NOT_DISPATCHED",
                error=f"pre-dispatch browser action failed: {type(exc).__name__}{suffix}",
            )

        # Once click() returned, failure to observe the exact new User turn is
        # ambiguous. Never convert this branch to NOT_DISPATCHED; the live probe
        # demonstrated delayed persistence can otherwise duplicate a prompt.
        dispatch_deadline = time.monotonic() + DISPATCH_CONFIRM_TIMEOUT_S
        confirmed = False
        while time.monotonic() < dispatch_deadline:
            try:
                # Current Spark can promote a fresh prompt directly into a
                # Task route before a durable user-query node appears. The
                # route transition on this automation-owned target is itself
                # positive proof that the click dispatched; accepting it
                # avoids falsely classifying a real Task launch as AMBIGUOUS.
                if is_spark_task_url(str(page.url or "")):
                    confirmed = True
                    break
                if self._selected_new_goal_id(page, set(baseline_goal_ids)) is not None:
                    confirmed = True
                    break
                try:
                    new_goal_id = unique_new_goal_id(
                        set(baseline_goal_ids),
                        self._goal_ids(page),
                    )
                except ValueError:
                    # Multiple newly visible cards can be a delayed history
                    # hydration burst. Do not attribute that burst to this send.
                    new_goal_id = None
                if new_goal_id is not None:
                    confirmed = True
                    break
                user_count = page.locator(USER_QUERY_SELECTOR).count()
                if user_count > baseline_users + 1:
                    return DriverResult("AMBIGUOUS", error="more than one new user turn appeared")
                if user_count == baseline_users + 1:
                    latest = page.locator(USER_QUERY_SELECTOR).last.locator(USER_QUERY_TEXT_SELECTOR)
                    if latest.count() > 0 and query_prompt_matches(locator_text_contents(latest), prompt):
                        confirmed = True
                        break
                    if allow_count_only_confirmation:
                        # A continuation can reopen the exact durable
                        # conversation in an automation-created target. Spark
                        # sometimes persists the new user-query with empty
                        # query-line text after hydration even though the turn
                        # itself is present and later produces a response. In
                        # that narrow case, the baseline + 1 transition after
                        # our successful click is sufficient dispatch proof:
                        # no Human tab is reused and >1 remains ambiguous.
                        confirmed = True
                        break
            except Exception:
                pass
            time.sleep(0.15)
        if not confirmed:
            return DriverResult("AMBIGUOUS", error="send clicked but exact persisted user turn was not proven")

        return DispatchState(
            baseline_responses=baseline_responses,
            baseline_goal_ids=baseline_goal_ids,
            baseline_durable_urls=baseline_durable_urls,
        )

    def _wait_response(
        self,
        page,
        baseline_responses: int,
        conversation_id: str | None,
        conversation_url: str | None,
        expected_prompt: str,
        *,
        execution_kind: str = "chat",
        baseline_goal_ids: frozenset[str] | None = None,
        deadline: float | None = None,
    ) -> DriverResult:
        response_deadline = deadline if deadline is not None else time.monotonic() + self.response_timeout_s
        expected_responses = baseline_responses + 1
        stable_text = None
        stable_samples = 0
        reopened_goal_id: str | None = None
        while time.monotonic() < response_deadline:
            try:
                if (
                    execution_kind == "task"
                    and baseline_goal_ids is not None
                    and reopened_goal_id is None
                    and (
                    is_task_reopen_surface(str(page.url or ""))
                        or is_spark_task_url(str(page.url or ""))
                    )
                ):
                    baseline_goals = set(baseline_goal_ids)
                    new_goal_id = self._selected_new_goal_id(page, baseline_goals)
                    if new_goal_id is None:
                        try:
                            new_goal_id = unique_new_goal_id(
                                baseline_goals,
                                self._goal_ids(page),
                            )
                        except ValueError:
                            # A fresh Task surface can hydrate historical cards
                            # before the owned card is marked selected. Keep
                            # waiting for positive correlation instead of
                            # treating that hydration burst as multiple sends.
                            new_goal_id = None
                    if new_goal_id is not None:
                        expected_url = task_conversation_url_from_goal_id(new_goal_id)
                        card = page.locator(f"#{new_goal_id}")
                        if expected_url is None or card.count() != 1:
                            return DriverResult(
                                "AMBIGUOUS",
                                error="new Gemini task could not be uniquely reopened",
                                execution_kind="task",
                            )
                        card.click(timeout=5_000)
                        reopened_goal_id = new_goal_id
                        time.sleep(0.25)
                        continue
                responses = page.locator(MODEL_RESPONSE_SELECTOR)
                count = responses.count()
                if count > expected_responses:
                    return DriverResult(
                        "AMBIGUOUS",
                        conversation_id,
                        conversation_url,
                        error="more than one new model response appeared",
                        execution_kind=execution_kind,
                    )
                if count == expected_responses:
                    response = responses.last
                    content = response.locator(MESSAGE_CONTENT_SELECTOR)
                    footer_complete = response.locator(COMPLETE_FOOTER_SELECTOR).count() > 0
                    if content.count() == 1:
                        busy = content.get_attribute("aria-busy")
                        text = normalize_text(response_text(content))
                        if footer_complete and busy == "false" and text:
                            if looks_like_tool_permission_surface(text):
                                return DriverResult(
                                    "AMBIGUOUS",
                                    conversation_id,
                                    conversation_url,
                                    error="Gemini tool-permission surface appeared inside model response",
                                    execution_kind=execution_kind,
                                )
                            if execution_kind == "task" and text.startswith("Something went wrong"):
                                return DriverResult(
                                    "TASK_FAILED",
                                    error=text,
                                    execution_kind="task",
                                )
                            # `_dispatch_page_turn()` already proves the exact
                            # persisted user prompt immediately after click.
                            # Re-checking the prompt here is both redundant and
                            # brittle: Spark can rehydrate/promote the owned
                            # target into a different DOM representation while
                            # the same response is completing. Attribution is
                            # still fail-closed through the owned target,
                            # baseline response count, and >expected ambiguity
                            # check above.
                            if text == stable_text:
                                stable_samples += 1
                            else:
                                stable_text = text
                                stable_samples = 1
                            if stable_samples >= 2:
                                return DriverResult(
                                    "COMPLETED",
                                    conversation_id,
                                    conversation_url,
                                    content=text,
                                    execution_kind=execution_kind,
                                )
            except Exception:
                pass
            time.sleep(0.35)

        return DriverResult(
            "TIMEOUT",
            conversation_id,
            conversation_url,
            error="confirmed user turn did not reach structural Gemini completion before timeout",
            execution_kind=execution_kind,
        )

