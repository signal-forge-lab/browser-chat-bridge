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
NEW_CHAT_URL = f"{GEMINI_ORIGIN}/app"

COMPOSER_SELECTOR = 'rich-textarea div[contenteditable="true"][role="textbox"]'
MODEL_BUTTON_SELECTOR = 'button[data-test-id="bard-mode-menu-button"]'
MODEL_ITEM_SELECTOR = 'gem-menu-item[role="menuitem"]'
SEND_BUTTON_SELECTOR = (
    'div[data-test-id="send-button-container"] button[aria-label="プロンプトを送信"]'
)
USER_QUERY_SELECTOR = "user-query"
USER_QUERY_TEXT_SELECTOR = ".query-text p.query-text-line"
MODEL_RESPONSE_SELECTOR = "model-response"
MESSAGE_CONTENT_SELECTOR = "message-content .markdown"
COMPLETE_FOOTER_SELECTOR = ".response-footer.complete"

BASE_MODEL_LABEL = "3.8 Flash"
ENHANCED_MODE_LABEL = "強化版思考モード"
FIXED_MODEL_PRIMARY = "Flash"
FIXED_MODEL_SECONDARY = "拡張"


def normalize_text(value: str) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def prompt_matches(actual: str, expected: str) -> bool:
    return normalize_text(actual) == normalize_text(expected)


def fixed_model_selected(button_text: str) -> bool:
    lines = {line.strip() for line in normalize_text(button_text).split("\n") if line.strip()}
    return FIXED_MODEL_PRIMARY in lines and FIXED_MODEL_SECONDARY in lines


def parse_conversation_id(url: str) -> str | None:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https" or parsed.netloc != "gemini.google.com":
        return None
    match = re.fullmatch(r"/app/([A-Za-z0-9_-]+)(?:/)?", parsed.path)
    return None if match is None else match.group(1)


def durable_urls_from_target_rows(rows: list[dict[str, Any]]) -> set[str]:
    urls: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in {None, "page"}:
            continue
        conversation_id = parse_conversation_id(str(row.get("url") or ""))
        if conversation_id is not None:
            urls.add(f"{GEMINI_ORIGIN}/app/{conversation_id}")
    return urls


@dataclass(frozen=True)
class DriverResult:
    status: str
    conversation_id: str | None = None
    conversation_url: str | None = None
    content: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "conversation_id": self.conversation_id,
            "conversation_url": self.conversation_url,
            "content": self.content,
            "error": self.error,
        }


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
                browser = playwright.chromium.connect_over_cdp(self.cdp_endpoint, timeout=15_000)
            except Exception as exc:
                return DriverResult("TARGET_LOST", error=f"CDP connect failed: {type(exc).__name__}").as_dict()
            try:
                if not browser.contexts:
                    return DriverResult("TARGET_LOST", error="CDP browser exposed no context").as_dict()
                context = browser.contexts[0]
                target_url = str(requested_url or f"{NEW_CHAT_URL}#bcb-{uuid.uuid4().hex}")
                page = self._find_page(context, target_url) if requested_url is not None else None
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
                            # Playwright does not add targets created through a
                            # browser-level CDP session to context.pages on the
                            # same connection. Reattach read-only to discover it.
                            try:
                                browser.close()
                            except Exception:
                                pass
                            browser = playwright.chromium.connect_over_cdp(
                                self.cdp_endpoint, timeout=15_000
                            )
                            if browser.contexts:
                                page = self._wait_find_page(browser.contexts[0], target_url)
                if page is None:
                    return DriverResult("TARGET_LOST", error="could not create or resolve Gemini page").as_dict()
                dispatch = self._dispatch_page_turn(page, prompt)
                if isinstance(dispatch, DriverResult):
                    # A fresh marker tab is automation-owned. If dispatch never
                    # crossed click(), close it so a transient startup/model
                    # readiness failure cannot accumulate stale /app tabs. All
                    # post-click uncertainty is classified AMBIGUOUS and must
                    # remain available for reconciliation instead.
                    if requested_url is None and dispatch.status != "AMBIGUOUS":
                        try:
                            page.close()
                        except Exception:
                            pass
                    return dispatch.as_dict()

                if requested_url is None:
                    # Gemini promotes a fresh /app submission into a separate
                    # durable target. A Playwright CDP connection does not
                    # reliably learn about targets created after that attach,
                    # so observe Chrome's local /json/list first and reconnect
                    # exactly once after the new durable URL exists.
                    promoted_url = self._wait_promoted_url(
                        set(dispatch.baseline_durable_urls)
                    )
                    if promoted_url is None:
                        return DriverResult(
                            "AMBIGUOUS",
                            error="user turn persisted but durable conversation target was not correlated",
                        ).as_dict()
                    try:
                        browser.close()
                    except Exception:
                        pass
                    try:
                        browser = playwright.chromium.connect_over_cdp(
                            self.cdp_endpoint, timeout=15_000
                        )
                    except Exception as exc:
                        return DriverResult(
                            "AMBIGUOUS",
                            error=f"user turn persisted but CDP reattach failed: {type(exc).__name__}",
                        ).as_dict()
                    if not browser.contexts:
                        return DriverResult(
                            "AMBIGUOUS",
                            error="user turn persisted but reattached browser exposed no context",
                        ).as_dict()
                    context = browser.contexts[0]
                    page = self._wait_find_page(context, promoted_url)
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
                conversation_url = f"{GEMINI_ORIGIN}/app/{conversation_id}"
                return self._wait_response(
                    page,
                    dispatch.baseline_responses,
                    conversation_id,
                    conversation_url,
                    prompt,
                ).as_dict()
            finally:
                # connect_over_cdp disconnects this client only; it does not own
                # the attached Chrome/Obscura process.
                try:
                    browser.close()
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

    def _wait_find_page(self, context, url: str):
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            page = self._find_page(context, url)
            if page is not None:
                return page
            time.sleep(0.1)
        return None

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
                composer = page.locator(COMPOSER_SELECTOR)
                if composer.count() == 1 and composer.is_visible():
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def _ensure_fixed_model(self, page) -> bool:
        # On a newly created Gemini tab the composer can become usable before
        # Angular's mode menu finishes its first render/animation. A later
        # read of the same page succeeds without navigation, so retry only this
        # pre-dispatch UI setup for a short bounded window.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                if self._try_ensure_fixed_model(page):
                    return True
            except Exception:
                # A click/wait timeout here is still pre-dispatch. Treat it as
                # transient UI readiness, never as an ambiguous send.
                pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _try_ensure_fixed_model(self, page) -> bool:
        button = page.locator(MODEL_BUTTON_SELECTOR)
        if button.count() != 1:
            return False
        if fixed_model_selected(button.inner_text()):
            return True

        button.click()
        items = page.locator(MODEL_ITEM_SELECTOR)
        try:
            items.first.wait_for(state="visible", timeout=5_000)
        except Exception:
            return False
        base = items.filter(has_text=BASE_MODEL_LABEL)
        if base.count() != 1:
            return False
        base.click()
        try:
            items.first.wait_for(state="hidden", timeout=5_000)
        except Exception:
            return False

        button.click()
        try:
            items.first.wait_for(state="visible", timeout=5_000)
        except Exception:
            return False
        enhanced = items.filter(has_text=ENHANCED_MODE_LABEL)
        if enhanced.count() != 1:
            return False
        enhanced.click()
        try:
            items.first.wait_for(state="hidden", timeout=5_000)
        except Exception:
            return False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if fixed_model_selected(button.inner_text()):
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    @staticmethod
    def _counts(page) -> tuple[int, int]:
        return page.locator(USER_QUERY_SELECTOR).count(), page.locator(MODEL_RESPONSE_SELECTOR).count()

    @staticmethod
    def _durable_urls(context) -> set[str]:
        return {
            f"{GEMINI_ORIGIN}/app/{conversation_id}"
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

    def _wait_promoted_url(self, baseline_urls: set[str]) -> str | None:
        deadline = time.monotonic() + self.promotion_timeout_s
        while time.monotonic() < deadline:
            candidates = self._cdp_durable_urls() - baseline_urls
            if len(candidates) == 1:
                return next(iter(candidates))
            if len(candidates) > 1:
                return None
            time.sleep(0.15)
        return None

    @staticmethod
    def _page_last_prompt_matches(page, prompt: str) -> bool:
        try:
            users = page.locator(USER_QUERY_SELECTOR)
            if users.count() < 1:
                return False
            visible = users.last.locator(USER_QUERY_TEXT_SELECTOR)
            return visible.count() == 1 and prompt_matches(visible.inner_text(), prompt)
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
        if not self._ensure_fixed_model(page):
            return DriverResult("MODEL_MISMATCH", error="3.8 Flash enhanced mode could not be verified")

        baseline_durable_urls = self._cdp_durable_urls()
        if not baseline_durable_urls:
            # Keep a local fallback for CDP-compatible engines that do not
            # expose Chrome's HTTP target-list endpoint.
            baseline_durable_urls = self._durable_urls(page.context)
        baseline_users, baseline_responses = self._settled_counts(page)
        if baseline_users != baseline_responses:
            return DriverResult("BUSY", error="conversation has an unmatched in-flight turn")

        composer = page.locator(COMPOSER_SELECTOR)
        try:
            composer.fill(prompt)
            if not prompt_matches(composer.inner_text(), prompt):
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
                    if latest.count() == 1 and prompt_matches(latest.inner_text(), prompt):
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
        conversation_id: str,
        conversation_url: str,
        expected_prompt: str,
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
                    )
                if count == expected_responses:
                    response = responses.last
                    content = response.locator(MESSAGE_CONTENT_SELECTOR)
                    footer_complete = response.locator(COMPLETE_FOOTER_SELECTOR).count() > 0
                    if content.count() == 1:
                        busy = content.get_attribute("aria-busy")
                        text = normalize_text(content.inner_text())
                        if footer_complete and busy == "false" and text:
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
                                )
            except Exception:
                pass
            time.sleep(0.35)

        return DriverResult(
            "TIMEOUT",
            conversation_id,
            conversation_url,
            error="confirmed user turn did not reach structural Gemini completion before timeout",
        )

