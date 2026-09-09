from __future__ import annotations

import json
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit


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
CONVERSATION_ACTIONS_SELECTOR = "conversation-actions-icon button"
DELETE_MENU_ITEM_SELECTOR = 'gem-menu-item[data-test-id="delete-button"]'
DELETE_DIALOG_SELECTOR = '[role="dialog"]'
DELETE_CONFIRM_SELECTOR = 'gem-button[cdkfocusinitial] button'

CDP_CONNECT_TIMEOUT_MS = 30_000
CDP_CONNECT_ATTEMPTS = 3
PAGE_REATTACH_TIMEOUT_S = 30.0
BOUND_HISTORY_TIMEOUT_S = 30.0


def normalize_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def prompt_matches(actual: str, expected: str) -> bool:
    return normalize_text(actual) == normalize_text(expected)


def composer_prompt_matches(paragraph_texts: list[str], expected: str) -> bool:
    """Compare Gemini rich-textarea paragraphs to the exact outbound prompt.

    Gemini represents each logical line as a ``<p>`` and an intentional blank
    line as an empty ``<p><br></p>``. Playwright ``inner_text()`` inserts extra
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


def durable_urls_from_target_rows(rows: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in {None, "page"}:
            continue
        conversation_id = parse_conversation_id(str(row.get("url") or ""))
        if conversation_id is not None:
            urls.add(f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}")
    return urls


@dataclass(frozen=True)
class DriverResult:
    status: str
    conversation_id: str | None = None
    conversation_url: str | None = None
    content: str | None = None
    error: str | None = None
    execution_kind: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "status": self.status,
            "conversation_id": self.conversation_id,
            "conversation_url": self.conversation_url,
            "content": self.content,
            "error": self.error,
        }
        if self.execution_kind is not None:
            result["execution_kind"] = self.execution_kind
        return result


@dataclass(frozen=True)
class DispatchState:
    baseline_responses: int
    baseline_durable_urls: frozenset[str]


class GeminiDriver:
    """One Gemini UI turn over a caller-supplied Playwright CDP browser."""

    def __init__(
        self,
        cdp_endpoint: str,
        *,
        backend_kind: str = "chromium",
        promotion_timeout_s: float = 120.0,
        response_timeout_s: float = 360.0,
    ):
        self.cdp_endpoint = cdp_endpoint.rstrip("/")
        self.backend_kind = backend_kind
        self.promotion_timeout_s = max(15.0, float(promotion_timeout_s))
        self.response_timeout_s = max(10.0, float(response_timeout_s))

    def _connect_browser(self, playwright):
        """Attach to local CDP with bounded pre-action retries."""
        last_error = None
        for attempt in range(CDP_CONNECT_ATTEMPTS):
            try:
                return playwright.chromium.connect_over_cdp(
                    self.cdp_endpoint,
                    timeout=CDP_CONNECT_TIMEOUT_MS,
                )
            except Exception as exc:
                last_error = exc
                if attempt + 1 < CDP_CONNECT_ATTEMPTS:
                    time.sleep(0.5)
        assert last_error is not None
        raise last_error

    def run_turn(self, request: dict[str, Any]) -> dict[str, Any]:
        from playwright.sync_api import sync_playwright

        prompt = str(request.get("prompt") or "")
        requested_url = request.get("conversation_url")
        if not prompt.strip():
            return DriverResult("NOT_DISPATCHED", error="prompt is blank").as_dict()
        if requested_url is not None and parse_conversation_id(str(requested_url)) is None:
            return DriverResult("CONVERSATION_LOST", error="invalid durable Gemini conversation URL").as_dict()

        with sync_playwright() as playwright:
            browser = None
            try:
                browser = self._connect_browser(playwright)
            except Exception as exc:
                return DriverResult("TARGET_LOST", error=f"CDP connect failed: {type(exc).__name__}").as_dict()
            try:
                if not browser.contexts:
                    return DriverResult("TARGET_LOST", error="CDP browser exposed no context").as_dict()
                context = browser.contexts[0]
                target_url = str(requested_url or f"{NEW_CHAT_URL}#bcb-{uuid.uuid4().hex}")
                page = self._find_page(context, target_url) if requested_url is not None else None
                page_created_here = page is None
                if page is None:
                    if self.backend_kind == "obscura":
                        # Obscura is an automation-only headless process, so a
                        # normal CDP newPage has no Human tab/focus to protect.
                        try:
                            page = context.new_page()
                            page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
                        except Exception:
                            page = None
                    else:
                        if not self._create_background_target(browser, target_url):
                            page = None
                        else:
                            # This Edge profile can omit a newly-created target
                            # from one Playwright attach even though /json/list
                            # already exposes it. Retry whole attaches; polling
                            # context.pages on one connection never discovers
                            # a target that was absent from that attach.
                            browser, page = self._reattach_find_page(
                                playwright,
                                browser,
                                target_url,
                            )
                if page is None:
                    return DriverResult("TARGET_LOST", error="could not create or resolve Gemini page").as_dict()
                if requested_url is not None and not self._wait_for_bound_history(page):
                    conversation_id = parse_conversation_id(str(requested_url))
                    return DriverResult(
                        "CONVERSATION_LOST",
                        conversation_id=conversation_id,
                        conversation_url=str(requested_url),
                        error="bound Gemini conversation history did not hydrate before dispatch",
                    ).as_dict()
                dispatch = self._dispatch_page_turn(page, prompt)
                if isinstance(dispatch, DriverResult):
                    # A fresh marker tab is automation-owned and never a
                    # durable conversation. Close it on every terminal result,
                    # including AMBIGUOUS: the Bridge never blind-retries that
                    # request, and leaving marker targets behind can wedge a
                    # later Playwright CDP attach. Pre-existing Human tabs are
                    # never closed here.
                    if page_created_here:
                        self._close_page_quietly(page)
                    return dispatch.as_dict()

                if requested_url is None:
                    destination, promoted_url = self._wait_first_turn_destination(
                        page,
                        set(dispatch.baseline_durable_urls),
                    )
                    if destination == "task":
                        response = self._wait_response(
                            page,
                            dispatch.baseline_responses,
                            None,
                            None,
                            prompt,
                            execution_kind="task",
                        )
                        promoted_id = parse_conversation_id(page.url)
                        if response.status == "COMPLETED" and promoted_id is not None:
                            response = DriverResult(
                                "COMPLETED",
                                promoted_id,
                                f"{GEMINI_ORIGIN}/spark/chat/{promoted_id}",
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
                    # the Playwright client is sufficient; it does not own the
                    # browser target.
                    try:
                        browser, page = self._reattach_find_page(
                            playwright,
                            browser,
                            promoted_url,
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
                response = self._wait_response(
                    page,
                    dispatch.baseline_responses,
                    conversation_id,
                    conversation_url,
                    prompt,
                )
                # A successfully completed durable conversation can always be
                # reopened by its URL on the next turn. Close only pages this
                # invocation created; never close a pre-existing Human tab.
                # Ambiguous/timeout paths remain open for reconciliation.
                if page_created_here and response.status == "COMPLETED":
                    self._close_page_quietly(page)
                return response.as_dict()
            finally:
                # connect_over_cdp disconnects this client only; it does not own
                # the attached Chrome/Obscura process.
                try:
                    browser.close()
                except Exception:
                    pass

    def delete_conversation(self, request: dict[str, Any]) -> dict[str, Any]:
        """Delete exactly one durable Gemini conversation through its UI."""
        from playwright.sync_api import sync_playwright

        conversation_url = str(request.get("conversation_url") or "")
        conversation_id = parse_conversation_id(conversation_url)
        if conversation_id is None:
            return DriverResult("DELETE_FAILED", error="invalid durable Gemini conversation URL").as_dict()

        with sync_playwright() as playwright:
            browser = None
            page = None
            page_created_here = False
            try:
                try:
                    browser = self._connect_browser(playwright)
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
                    elif self._create_background_target(browser, conversation_url):
                        browser, page = self._reattach_find_page(
                            playwright,
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

    def _reattach_find_page(self, playwright, browser, url: str, *, timeout_s: float = PAGE_REATTACH_TIMEOUT_S):
        """Reconnect until one attach initially enumerates the exact target.

        In the shared Edge profile, a target created through browser-level CDP
        can be visible in ``/json/list`` before Playwright includes it in
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
            current = self._connect_browser(playwright)
            if current.contexts:
                page = self._find_page(current.contexts[0], url)
                if page is not None:
                    return current, page
        return current, None

    @staticmethod
    def _create_background_target(browser, url: str) -> bool:
        try:
            session = browser.new_browser_cdp_session()
            try:
                session.send("Target.createTarget", {"url": url, "background": True})
            finally:
                session.detach()
        except Exception:
            return False
        return True

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
    def _durable_urls(context) -> set[str]:
        return {
            f"{GEMINI_ORIGIN}/spark/chat/{conversation_id}"
            for candidate in context.pages
            if (conversation_id := parse_conversation_id(candidate.url)) is not None
        }

    def _cdp_durable_urls(self) -> set[str]:
        try:
            with urllib.request.urlopen(
                self.cdp_endpoint + "/json/list",
                timeout=2.0,
            ) as response:
                rows = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return set()
        if not isinstance(rows, list):
            return set()
        return durable_urls_from_target_rows(rows)

    def _wait_first_turn_destination(self, page, baseline_urls: set[str]) -> tuple[str | None, str | None]:
        deadline = time.monotonic() + self.promotion_timeout_s
        while time.monotonic() < deadline:
            candidates = self._cdp_durable_urls() - baseline_urls
            if len(candidates) == 1:
                return "chat", next(iter(candidates))
            if len(candidates) > 1:
                return None, None
            parsed = urlsplit(str(page.url or ""))
            if (
                parsed.scheme == "https"
                and parsed.netloc == "gemini.google.com"
                and parsed.path.rstrip("/") == "/spark/tasks"
            ):
                return "task", None
            time.sleep(0.15)
        return None, None

    @staticmethod
    def _page_last_prompt_matches(page, prompt: str) -> bool:
        try:
            users = page.locator(USER_QUERY_SELECTOR)
            if users.count() < 1:
                return False
            visible = users.last.locator(USER_QUERY_TEXT_SELECTOR)
            return visible.count() > 0 and query_prompt_matches(visible.all_inner_texts(), prompt)
        except Exception:
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

    def _dispatch_page_turn(self, page, prompt: str) -> DriverResult | DispatchState:
        if not self._wait_for_ready(page):
            status = "AUTH_REQUIRED" if "accounts.google.com" in page.url else "TARGET_LOST"
            return DriverResult(status, error="Gemini composer did not become ready")

        baseline_durable_urls = self._cdp_durable_urls()
        if not baseline_durable_urls:
            # Keep a local fallback for CDP-compatible engines that do not
            # expose Chrome's HTTP target-list endpoint.
            baseline_durable_urls = self._durable_urls(page.context)
        baseline_users, baseline_responses = self._settled_counts(page)
        if baseline_users != baseline_responses:
            return DriverResult("BUSY", error="conversation has an unmatched in-flight turn")

        composer = self._ready_composer(page)
        if composer is None:
            return DriverResult("NOT_DISPATCHED", error="Gemini composer disappeared before dispatch")
        try:
            composer.fill(prompt)
            paragraphs = composer.locator(":scope > p")
            composer_matches = (
                composer_prompt_matches(paragraphs.all_inner_texts(), prompt)
                if paragraphs.count() > 0
                else prompt_matches(composer.inner_text(), prompt)
            )
            if not composer_matches:
                return DriverResult("NOT_DISPATCHED", error="composer read-back mismatch")
            send = page.locator(SEND_BUTTON_SELECTOR)
            if send.count() != 1 or not send.is_visible():
                return DriverResult("NOT_DISPATCHED", error="send button is not uniquely ready")
            send.click(timeout=5_000)
        except Exception as exc:
            return DriverResult("NOT_DISPATCHED", error=f"pre-dispatch browser action failed: {type(exc).__name__}")

        # Once click() returned, failure to observe the exact new User turn is
        # ambiguous. Never convert this branch to NOT_DISPATCHED; the live probe
        # demonstrated delayed persistence can otherwise duplicate a prompt.
        dispatch_deadline = time.monotonic() + 15.0
        confirmed = False
        while time.monotonic() < dispatch_deadline:
            try:
                user_count = page.locator(USER_QUERY_SELECTOR).count()
                if user_count > baseline_users + 1:
                    return DriverResult("AMBIGUOUS", error="more than one new user turn appeared")
                if user_count == baseline_users + 1:
                    latest = page.locator(USER_QUERY_SELECTOR).last.locator(USER_QUERY_TEXT_SELECTOR)
                    if latest.count() > 0 and query_prompt_matches(latest.all_inner_texts(), prompt):
                        confirmed = True
                        break
            except Exception:
                pass
            time.sleep(0.15)
        if not confirmed:
            return DriverResult("AMBIGUOUS", error="send clicked but exact persisted user turn was not proven")

        return DispatchState(
            baseline_responses=baseline_responses,
            baseline_durable_urls=frozenset(baseline_durable_urls),
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
    ) -> DriverResult:
        response_deadline = time.monotonic() + self.response_timeout_s
        expected_responses = baseline_responses + 1
        stable_text = None
        stable_samples = 0
        while time.monotonic() < response_deadline:
            try:
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
                        text = normalize_text(content.inner_text())
                        if footer_complete and busy == "false" and text:
                            if execution_kind == "task" and text.startswith("Something went wrong"):
                                return DriverResult(
                                    "TASK_FAILED",
                                    error=text,
                                    execution_kind="task",
                                )
                            if not self._page_last_prompt_matches(page, expected_prompt):
                                time.sleep(0.2)
                                continue
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

