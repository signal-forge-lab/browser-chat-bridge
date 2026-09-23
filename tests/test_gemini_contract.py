from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from browser_chat_bridge.gemini import (
    BOUND_HISTORY_TIMEOUT_S,
    CANCEL_GENERATION_DIALOG_SELECTOR,
    CDP_CONNECT_TIMEOUT_MS,
    COMPOSER_SELECTOR,
    COMPLETE_FOOTER_SELECTOR,
    CONFIRM_CANCEL_SELECTOR,
    DISPATCH_CONFIRM_TIMEOUT_S,
    DispatchState,
    RECOVERY_RESPONSE_TIMEOUT_S,
    UI_ACTION_INTERVAL_MAX_S,
    UI_ACTION_INTERVAL_MIN_S,
    ORPHAN_UNBOUND_GRACE_S,
    DriverResult,
    GeminiDriver,
    GOAL_CARD_SELECTOR,
    MESSAGE_CONTENT_SELECTOR,
    MODEL_RESPONSE_SELECTOR,
    RESPONSE_UI_EXCLUDE_SELECTOR,
    SEND_BUTTON_SELECTOR,
    STOP_RESPONSE_SELECTOR,
    USER_QUERY_SELECTOR,
    bound_history_hydrated,
    composer_prompt_matches,
    durable_urls_from_target_rows,
    fresh_spark_page_eligible,
    is_spark_home_url,
    is_spark_task_url,
    is_task_reopen_surface,
    looks_like_tool_permission_surface,
    normalize_text,
    parse_conversation_id,
    prompt_matches,
    query_prompt_matches,
    recovery_observation,
    response_text,
    task_conversation_url_from_goal_id,
    completed_task_binding,
    target_url_from_rows,
    unique_new_goal_id,
    unbound_owned_page_releasable,
)


class GeminiContractTests(unittest.TestCase):
    def test_response_text_excludes_rendered_action_card_when_supported(self):
        class Content:
            def __init__(self):
                self.selector = None

            def inner_text_excluding(self, selector):
                self.selector = selector
                return "FINAL_ONLY"

            def inner_text(self):
                raise AssertionError("fallback should not be used")

        content = Content()
        self.assertEqual(response_text(content), "FINAL_ONLY")
        self.assertEqual(content.selector, RESPONSE_UI_EXCLUDE_SELECTOR)

    def test_response_text_falls_back_for_contract_test_locators(self):
        class Content:
            def inner_text(self):
                return "LEGACY"

        self.assertEqual(response_text(Content()), "LEGACY")
    def test_cdp_attach_budget_tolerates_slow_local_browser_handshake(self):
        # The authenticated desktop browser can take materially longer than a
        # loopback HTTP probe to finish nodriver's full CDP attach. Keep the
        # attach budget above the observed ~15 s edge rather than treating a
        # healthy local browser as TARGET_LOST at the old boundary.
        self.assertGreaterEqual(CDP_CONNECT_TIMEOUT_MS, 30_000)

    def test_only_unbound_first_turns_share_the_first_turn_guard(self):
        driver = GeminiDriver("http://127.0.0.1:1")

        unbound_guard = driver._turn_guard(None)
        self.assertIs(unbound_guard, driver._unbound_first_turn_lock)
        with unbound_guard:
            self.assertFalse(driver._unbound_first_turn_lock.acquire(blocking=False))
            # Bound continuations must bypass the shared first-turn lock so
            # independent durable conversations can keep running in parallel.
            with driver._turn_guard("https://gemini.google.com/spark/chat/abc123"):
                pass

    def test_paced_dispatch_waits_random_three_to_six_seconds_before_input_and_send(self):
        class Paragraphs:
            def count(self):
                return 0

        class Composer:
            def __init__(self, page):
                self.page = page
                self.value = ""

            def fill(self, value):
                self.value = value

            def locator(self, _selector):
                return Paragraphs()

            def inner_text(self):
                return self.value

            def press(self, key, timeout):
                self.key = key
                self.timeout = timeout
                self.page.url = "https://gemini.google.com/spark/tasks"

        class Send:
            def __init__(self, page):
                self.page = page

            def click(self, timeout):
                self.timeout = timeout
                self.page.url = "https://gemini.google.com/spark/tasks"

        class Empty:
            def count(self):
                return 0

        class Page:
            url = "https://gemini.google.com/spark/chat/owned"

            def __init__(self):
                self.composer = Composer(self)
                self.send = Send(self)

            def locator(self, selector):
                if selector == COMPOSER_SELECTOR:
                    return self.composer
                if selector == SEND_BUTTON_SELECTOR:
                    return self.send
                if selector == USER_QUERY_SELECTOR:
                    return Empty()
                raise AssertionError(selector)

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._wait_for_ready = lambda _page: True  # type: ignore[method-assign]
        driver._settled_counts = lambda _page: (0, 0)  # type: ignore[method-assign]
        driver._goal_ids = lambda _page: set()  # type: ignore[method-assign]
        driver._cdp_target_rows = lambda: []  # type: ignore[method-assign]
        driver._ready_composer = lambda page: page.composer  # type: ignore[method-assign]
        driver._wait_unique_visible = lambda locator: locator  # type: ignore[method-assign]

        sleeps = []
        with (
            patch("browser_chat_bridge.gemini.random.uniform", side_effect=[3.25, 5.75]) as uniform,
            patch("browser_chat_bridge.gemini.time.sleep", side_effect=lambda value: sleeps.append(value)),
        ):
            result = driver._dispatch_page_turn(Page(), "hello", paced=True)

        self.assertIsInstance(result, DispatchState)
        self.assertEqual(sleeps[:2], [3.25, 5.75])
        self.assertEqual(uniform.call_count, 2)
        for call in uniform.call_args_list:
            self.assertEqual(call.args, (UI_ACTION_INTERVAL_MIN_S, UI_ACTION_INTERVAL_MAX_S))

    def test_bound_history_requires_one_complete_hydrated_pair(self):
        self.assertGreaterEqual(BOUND_HISTORY_TIMEOUT_S, 30.0)
        self.assertTrue(bound_history_hydrated(1, 1, True, True, True))
        self.assertTrue(bound_history_hydrated(2, 2, True, True, True))
        self.assertFalse(bound_history_hydrated(0, 0, False, False, False))
        self.assertFalse(bound_history_hydrated(1, 0, True, False, False))
        self.assertFalse(bound_history_hydrated(1, 1, False, True, True))
        self.assertFalse(bound_history_hydrated(1, 1, True, False, True))
        self.assertFalse(bound_history_hydrated(1, 1, True, True, False))

    def test_dispatch_confirmation_budget_covers_slow_workspace_persistence(self):
        # Live Spark Drive-backed prompts have persisted their exact user-query
        # after the old 15 s window. Keep exact matching, but allow enough time
        # for the durable user turn to appear before declaring ambiguity.
        self.assertGreaterEqual(DISPATCH_CONFIRM_TIMEOUT_S, 45.0)

    def test_recovery_budget_is_bounded_and_long_enough_for_late_dom_completion(self):
        self.assertGreaterEqual(RECOVERY_RESPONSE_TIMEOUT_S, 30.0)
        self.assertLessEqual(RECOVERY_RESPONSE_TIMEOUT_S, 90.0)

    def test_recovery_observation_requires_attributed_complete_matching_pair(self):
        self.assertEqual(
            recovery_observation(2, 1, True, 0, False, None, ""),
            ("WAIT", None),
        )
        self.assertEqual(
            recovery_observation(2, 2, True, 1, True, "false", " answer "),
            ("COMPLETE", "answer"),
        )
        self.assertEqual(
            recovery_observation(2, 2, False, 1, True, "false", "other"),
            ("WAIT", None),
        )
        self.assertEqual(
            recovery_observation(2, 3, True, 1, True, "false", "impossible"),
            ("AMBIGUOUS", None),
        )

    def test_conversation_id_comes_from_durable_spark_chat_route(self):
        self.assertEqual(
            parse_conversation_id("https://gemini.google.com/spark/chat/491c5405bb57437c"),
            "491c5405bb57437c",
        )
        self.assertIsNone(parse_conversation_id("https://gemini.google.com/spark"))
        self.assertIsNone(parse_conversation_id("https://gemini.google.com/app/abc"))
        self.assertIsNone(parse_conversation_id("https://example.com/spark/chat/abc"))

    def test_spark_task_route_is_positive_first_turn_destination(self):
        self.assertTrue(is_spark_task_url("https://gemini.google.com/spark/tasks"))
        self.assertTrue(is_spark_task_url("https://gemini.google.com/spark/tasks/"))
        self.assertFalse(is_spark_task_url("https://gemini.google.com/spark"))
        self.assertFalse(is_spark_task_url("https://example.com/spark/tasks"))
        self.assertTrue(is_task_reopen_surface("https://gemini.google.com/spark"))
        self.assertTrue(is_task_reopen_surface("https://gemini.google.com/spark/tasks"))
        self.assertFalse(is_task_reopen_surface("https://gemini.google.com/spark/chat/abc"))

    def test_spark_home_and_task_goal_identity_support_task_reopen(self):
        self.assertTrue(is_spark_home_url("https://gemini.google.com/spark"))
        self.assertTrue(is_spark_home_url("https://gemini.google.com/spark/"))
        self.assertFalse(is_spark_home_url("https://gemini.google.com/spark/tasks"))
        self.assertEqual(
            task_conversation_url_from_goal_id("goal-c_9fd077db8797f674"),
            "https://gemini.google.com/spark/chat/9fd077db8797f674",
        )
        self.assertEqual(
            completed_task_binding(
                "https://gemini.google.com/spark/tasks",
                {"goal-c_old"},
                {"goal-c_old", "goal-c_new"},
            ),
            (
                "new",
                "https://gemini.google.com/spark/chat/new",
                "goal-c_new",
            ),
        )
        self.assertEqual(
            completed_task_binding(
                "https://gemini.google.com/spark/chat/already",
                {"goal-c_old"},
                {"goal-c_old"},
            ),
            (
                "already",
                "https://gemini.google.com/spark/chat/already",
                None,
            ),
        )

    def test_goal_id_helpers_are_strict(self):
        self.assertIsNone(task_conversation_url_from_goal_id("not-a-goal"))
        self.assertEqual(
            unique_new_goal_id(
                {"goal-c_old"},
                {"goal-c_old", "goal-c_new"},
            ),
            "goal-c_new",
        )
        self.assertIsNone(unique_new_goal_id({"goal-c_old"}, {"goal-c_old"}))
        with self.assertRaises(ValueError):
            unique_new_goal_id(
                {"goal-c_old"},
                {"goal-c_old", "goal-c_new-1", "goal-c_new-2"},
            )

    def test_first_turn_destination_accepts_spark_tasks_without_waiting_for_chat_promotion(self):
        class Page:
            url = "https://gemini.google.com/spark/tasks"

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._cdp_target_url = lambda _target_id: "https://gemini.google.com/spark/tasks"  # type: ignore[method-assign]

        self.assertEqual(
            driver._wait_first_turn_destination(Page(), "target-1"),
            ("task", None),
        )

    def test_owned_task_promotion_prefers_page_url(self):
        class Page:
            url = "https://gemini.google.com/spark/chat/owned123"

        driver = GeminiDriver("http://127.0.0.1:1")
        self.assertEqual(
            driver._wait_owned_task_promotion(Page(), timeout_s=0.01),
            ("owned123", "https://gemini.google.com/spark/chat/owned123"),
        )

    def test_selected_new_goal_id_resolves_one_owned_task_among_multiple_new_goals(self):
        class Card:
            def __init__(self, goal_id, selected):
                self.goal_id = goal_id
                self.selected = selected

            def get_attribute(self, name):
                if name == "id":
                    return self.goal_id
                if name == "aria-selected":
                    return self.selected
                return None

        class Cards:
            def __init__(self):
                self.values = [
                    Card("goal-c_old", "false"),
                    Card("goal-c_other-new", "false"),
                    Card("goal-c_owned-new", "true"),
                ]

            def count(self):
                return len(self.values)

            def nth(self, index):
                return self.values[index]

        class Page:
            def locator(self, _selector):
                return Cards()

        self.assertEqual(
            GeminiDriver._selected_new_goal_id(Page(), {"goal-c_old"}),
            "goal-c_owned-new",
        )

    def test_first_turn_destination_correlates_only_the_created_target_id(self):
        class Page:
            url = "https://gemini.google.com/spark#bcb-owned"

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._cdp_target_url = lambda target_id: (  # type: ignore[method-assign]
            "https://gemini.google.com/spark/chat/owned123"
            if target_id == "owned-target"
            else "https://gemini.google.com/spark/chat/other456"
        )

        self.assertEqual(
            driver._wait_first_turn_destination(Page(), "owned-target"),
            ("chat", "https://gemini.google.com/spark/chat/owned123"),
        )

    def test_first_turn_destination_accepts_one_new_durable_target_when_owned_target_stays_on_home(self):
        class Page:
            url = "https://gemini.google.com/spark#bcb-owned"

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._cdp_target_url = lambda _target_id: "https://gemini.google.com/spark"  # type: ignore[method-assign]
        driver._cdp_target_rows = lambda: [  # type: ignore[method-assign]
            {"type": "page", "url": "https://gemini.google.com/spark/chat/existing"},
            {"type": "page", "url": "https://gemini.google.com/spark/chat/new123"},
        ]

        self.assertEqual(
            driver._wait_first_turn_destination(
                Page(),
                "owned-target",
                baseline_durable_urls=frozenset({"https://gemini.google.com/spark/chat/existing"}),
            ),
            ("chat", "https://gemini.google.com/spark/chat/new123"),
        )

    def test_first_turn_destination_fails_closed_when_multiple_new_durable_targets_appear(self):
        class Page:
            url = "https://gemini.google.com/spark#bcb-owned"

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._cdp_target_url = lambda _target_id: "https://gemini.google.com/spark"  # type: ignore[method-assign]
        driver._cdp_target_rows = lambda: [  # type: ignore[method-assign]
            {"type": "page", "url": "https://gemini.google.com/spark/chat/new1"},
            {"type": "page", "url": "https://gemini.google.com/spark/chat/new2"},
        ]

        self.assertEqual(
            driver._wait_first_turn_destination(
                Page(),
                "owned-target",
                baseline_durable_urls=frozenset(),
            ),
            ("ambiguous", None),
        )

    def test_first_turn_promotion_cannot_extend_the_shared_response_deadline(self):
        class Page:
            url = "https://gemini.google.com/spark#bcb-owned"

        driver = GeminiDriver("http://127.0.0.1:1", promotion_timeout_s=120)
        driver._cdp_target_url = lambda _target_id: Page.url  # type: ignore[method-assign]
        deadline = time.monotonic() + 0.05
        started = time.monotonic()

        self.assertEqual(
            driver._wait_first_turn_destination(Page(), "owned-target", deadline=deadline),
            (None, None),
        )
        self.assertLess(time.monotonic() - started, 0.5)

    def test_first_turn_destination_accepts_one_new_goal_on_same_marker_target(self):
        class Page:
            url = "https://gemini.google.com/spark#bcb-owned"

        driver = GeminiDriver("http://127.0.0.1:1", promotion_timeout_s=1)
        driver._cdp_target_url = lambda _target_id: Page.url  # type: ignore[method-assign]
        driver._selected_new_goal_id = lambda _page, _baseline: None  # type: ignore[method-assign]
        driver._goal_ids = lambda _page: {"goal-c_old", "goal-c_new"}  # type: ignore[method-assign]

        self.assertEqual(
            driver._wait_first_turn_destination(
                Page(),
                "owned-target",
                baseline_goal_ids=frozenset({"goal-c_old"}),
                deadline=time.monotonic() + 0.2,
            ),
            ("task", None),
        )

    def test_prompt_confirmation_normalizes_only_line_endings_and_outer_space(self):
        self.assertTrue(prompt_matches("hello\r\nworld", "hello\nworld"))
        self.assertFalse(prompt_matches("hello  world", "hello world"))
        self.assertEqual(normalize_text("  a\r\nb  "), "a\nb")

    def test_tool_permission_surface_is_not_treated_as_model_answer(self):
        self.assertTrue(
            looks_like_tool_permission_surface(
                "Connector\nLet Gemini use \"memory search\" from Connector\n"
                "Tool: memory_search\nquery: project folder\nDeny\nAllow"
            )
        )
        self.assertFalse(
            looks_like_tool_permission_surface(
                "A normal answer mentioning Tool: in prose without an approval choice"
            )
        )

    def test_composer_confirmation_reconstructs_contenteditable_paragraphs(self):
        # Gemini's rich-textarea renders one intentional blank line as
        # <p>line 1</p><p><br></p><p>line 2</p>. Chromium innerText
        # expands that DOM to five linefeeds, so composer admission must
        # reconstruct the paragraph semantics instead of comparing innerText.
        self.assertTrue(
            composer_prompt_matches(
                ["line 1", "\n", "line 2"],
                "line 1\n\nline 2",
            )
        )
        self.assertTrue(composer_prompt_matches(["line 1", "", "line 2"], "line 1\n\nline 2"))
        self.assertTrue(composer_prompt_matches(["line 1", "line 2"], "line 1\nline 2"))
        self.assertFalse(composer_prompt_matches(["line 1", "\n", "line 2"], "line 1\nline 2"))
        self.assertFalse(composer_prompt_matches(["line 1", "line X"], "line 1\nline 2"))

    def test_user_turn_confirmation_reconstructs_all_query_lines_strictly(self):
        # Gemini persists the same multi-paragraph prompt as multiple
        # p.query-text-line nodes; the blank logical line is exposed as one
        # layout newline. All lines must be reconstructed in order rather than
        # requiring exactly one p node.
        actual = ["Reply with exactly BCB_EW_OK and nothing else.", "\n", "Keep the final answer within approximately 8192 tokens."]
        expected = "Reply with exactly BCB_EW_OK and nothing else.\n\nKeep the final answer within approximately 8192 tokens."
        self.assertTrue(query_prompt_matches(actual, expected))
        self.assertFalse(query_prompt_matches(actual, expected.replace("8192", "4096")))
        self.assertFalse(query_prompt_matches([actual[0], actual[2]], expected))

    def test_bound_automation_target_can_confirm_dispatch_by_single_user_count_increment(self):
        class EmptyQueryText:
            def count(self):
                return 1

            def all_inner_texts(self):
                return [""]

            def nth(self, _index):
                return self

            def text_content(self):
                return ""

        class UserQueries:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 2 if self.page.dispatched else 1

            @property
            def last(self):
                return self

            def locator(self, selector):
                self.assertion = selector
                return EmptyQueryText()

        class Responses:
            def count(self):
                return 1

        class ComposerParagraphs:
            def count(self):
                return 1

            def all_inner_texts(self):
                return ["continue"]

        class Composer:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 1

            @property
            def last(self):
                return self

            def fill(self, prompt):
                self.prompt = prompt

            def locator(self, selector):
                self.assertion = selector
                return ComposerParagraphs()

            def is_visible(self):
                return True

            def press(self, key, timeout):
                self.key = key
                self.timeout = timeout
                self.page.dispatched = True

        class Send:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 1

            def is_visible(self):
                return True

            def click(self, timeout):
                self.timeout = timeout
                self.page.dispatched = True

        class Page:
            url = "https://gemini.google.com/spark/chat/owned"

            def __init__(self):
                self.dispatched = False
                self.users = UserQueries(self)
                self.responses = Responses()
                self.composer = Composer(self)
                self.send = Send(self)

            def locator(self, selector):
                if selector == USER_QUERY_SELECTOR:
                    return self.users
                if selector == MODEL_RESPONSE_SELECTOR:
                    return self.responses
                if selector == COMPOSER_SELECTOR:
                    return self.composer
                if selector == SEND_BUTTON_SELECTOR:
                    return self.send
                raise AssertionError(selector)

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._wait_for_ready = lambda _page: True  # type: ignore[method-assign]
        driver._goal_ids = lambda _page: set()  # type: ignore[method-assign]
        driver._cdp_target_rows = lambda: []  # type: ignore[method-assign]
        result = driver._dispatch_page_turn(
            Page(),
            "continue",
            allow_count_only_confirmation=True,
        )

        self.assertFalse(isinstance(result, DriverResult))

    def test_fresh_task_dispatch_confirms_one_new_goal_without_selected_state(self):
        class EmptyQueries:
            def count(self):
                return 0

        class EmptyResponses:
            def count(self):
                return 0

        class ComposerParagraphs:
            def count(self):
                return 1

            def all_inner_texts(self):
                return ["create the test file"]

        class Composer:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 1

            @property
            def last(self):
                return self

            def fill(self, _prompt):
                return None

            def locator(self, _selector):
                return ComposerParagraphs()

            def is_visible(self):
                return True

            def press(self, key, timeout):
                self.key = key
                self.timeout = timeout
                self.page.dispatched = True

        class Send:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 1

            def is_visible(self):
                return True

            def click(self, timeout):
                self.timeout = timeout
                self.page.dispatched = True

        class Page:
            url = "https://gemini.google.com/spark#bcb-owned"

            def __init__(self):
                self.dispatched = False
                self.composer = Composer(self)
                self.send = Send(self)

            def locator(self, selector):
                if selector == USER_QUERY_SELECTOR:
                    return EmptyQueries()
                if selector == MODEL_RESPONSE_SELECTOR:
                    return EmptyResponses()
                if selector == COMPOSER_SELECTOR:
                    return self.composer
                if selector == SEND_BUTTON_SELECTOR:
                    return self.send
                raise AssertionError(selector)

        page = Page()
        driver = GeminiDriver("http://127.0.0.1:1")
        driver._wait_for_ready = lambda _page: True  # type: ignore[method-assign]
        driver._settled_counts = lambda _page: (0, 0)  # type: ignore[method-assign]
        driver._selected_new_goal_id = lambda _page, _baseline: None  # type: ignore[method-assign]
        driver._goal_ids = lambda _page: (  # type: ignore[method-assign]
            {"goal-c_old", "goal-c_new"} if page.dispatched else {"goal-c_old"}
        )
        driver._cdp_target_rows = list  # type: ignore[method-assign]

        result = driver._dispatch_page_turn(page, "create the test file")

        self.assertFalse(isinstance(result, DriverResult))
        self.assertEqual(result.baseline_goal_ids, frozenset({"goal-c_old"}))

    def test_new_chat_marker_selects_only_the_exact_background_target(self):
        class Page:
            def __init__(self, url):
                self.url = url

        class Context:
            pages = [
                Page("https://gemini.google.com/spark"),
                Page("https://gemini.google.com/spark#bcb-old"),
                Page("https://gemini.google.com/spark#bcb-new"),
            ]

        selected = GeminiDriver._find_page(Context(), "https://gemini.google.com/spark#bcb-new")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.url, "https://gemini.google.com/spark#bcb-new")

    def test_reattach_retries_when_first_connection_does_not_enumerate_target(self):
        class Page:
            def __init__(self, url):
                self.url = url

        class Context:
            def __init__(self, pages):
                self.pages = pages

        class Browser:
            def __init__(self, pages):
                self.contexts = [Context(pages)]
                self.closed = 0

            def close(self):
                self.closed += 1

        target_url = "https://gemini.google.com/spark#bcb-new"
        initial = Browser([])
        first_reattach = Browser([Page("https://gemini.google.com/spark")])
        second_reattach = Browser([Page(target_url)])
        driver = GeminiDriver("http://127.0.0.1:1")
        candidates = iter([first_reattach, second_reattach])
        driver._connect_browser = lambda _client: next(candidates)  # type: ignore[method-assign]

        browser, page = driver._reattach_find_page(object(), initial, target_url, timeout_s=1.0)

        self.assertIs(browser, second_reattach)
        self.assertIsNotNone(page)
        self.assertEqual(page.url, target_url)
        self.assertEqual(initial.closed, 1)
        self.assertEqual(first_reattach.closed, 1)
        self.assertEqual(second_reattach.closed, 0)

    def test_reattach_tracks_created_target_after_spark_redirect(self):
        class Page:
            def __init__(self, url):
                self.url = url

        class Context:
            def __init__(self, pages):
                self.pages = pages

        class Browser:
            def __init__(self, pages):
                self.contexts = [Context(pages)]
                self.closed = 0

            def close(self):
                self.closed += 1

        marker_url = "https://gemini.google.com/spark#bcb-new"
        redirected_url = "https://gemini.google.com/spark/tasks"
        initial = Browser([])
        reattached = Browser([Page(redirected_url)])
        driver = GeminiDriver("http://127.0.0.1:1")
        driver._connect_browser = lambda _client: reattached  # type: ignore[method-assign]
        driver._cdp_target_url = lambda target_id: (  # type: ignore[method-assign]
            redirected_url if target_id == "owned-target" else None
        )

        browser, page = driver._reattach_find_page(
            object(),
            initial,
            marker_url,
            target_id="owned-target",
            timeout_s=1.0,
        )

        self.assertIs(browser, reattached)
        self.assertIsNotNone(page)
        self.assertEqual(page.url, redirected_url)
        self.assertEqual(initial.closed, 1)

    def test_durable_target_rows_extract_only_gemini_conversations(self):
        rows = [
            {"type": "page", "url": "https://gemini.google.com/spark/chat/abc123"},
            {"type": "page", "url": "https://gemini.google.com/spark"},
            {"type": "page", "url": "https://example.com/spark/chat/ignored"},
            {"type": "service_worker", "url": "https://gemini.google.com/spark/chat/worker"},
        ]
        self.assertEqual(
            durable_urls_from_target_rows(rows),
            {"https://gemini.google.com/spark/chat/abc123"},
        )

    def test_target_url_from_rows_uses_exact_cdp_target_identity(self):
        rows = [
            {"id": "other", "type": "page", "url": "https://gemini.google.com/spark/chat/other"},
            {"id": "owned", "type": "page", "url": "https://gemini.google.com/spark/chat/owned"},
        ]
        self.assertEqual(
            target_url_from_rows(rows, "owned"),
            "https://gemini.google.com/spark/chat/owned",
        )
        self.assertIsNone(target_url_from_rows(rows, "missing"))

    def test_automation_target_creation_activates_dedicated_edge_tab(self):
        class Session:
            def __init__(self):
                self.detached = 0

            def send(self, method, params):
                self.assertion = (method, params)
                return {"targetId": "target-123"}

            def detach(self):
                self.detached += 1

        class Browser:
            def __init__(self):
                self.session = Session()

            def new_browser_cdp_session(self):
                return self.session

        browser = Browser()
        url = "https://gemini.google.com/spark#bcb-owned"
        self.assertEqual(
            GeminiDriver._create_automation_target(browser, url),
            "target-123",
        )
        self.assertEqual(
            browser.session.assertion,
            ("Target.createTarget", {"url": url, "background": False}),
        )
        self.assertEqual(browser.session.detached, 1)

    def test_stop_owned_task_confirms_stop_button_disappears(self):
        class StopButton:
            def __init__(self):
                self.visible = True

            def count(self):
                return 1

            def is_visible(self):
                return self.visible

            def click(self, timeout):
                self.visible = False

        class EmptyLocator:
            def count(self):
                return 0

            def is_visible(self):
                return False

        class Page:
            def __init__(self):
                self.stop = StopButton()

            def locator(self, selector):
                if "回答を停止" in selector or "Stop response" in selector:
                    return self.stop
                return EmptyLocator()

        driver = GeminiDriver("http://127.0.0.1:1")
        self.assertTrue(driver._stop_owned_task(Page(), timeout_s=0.2))

    def test_stop_owned_task_confirms_current_spark_cancel_dialog(self):
        class Button:
            def __init__(self, on_click=None):
                self.visible = True
                self.clicked = 0
                self._on_click = on_click

            def count(self):
                return 1

            def is_visible(self):
                return self.visible

            def click(self, timeout):
                self.clicked += 1
                self.timeout = timeout
                if self._on_click is not None:
                    self._on_click()

        class EmptyLocator:
            def count(self):
                return 0

            def is_visible(self):
                return False

        class Dialog:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 1 if self.page.dialog_visible else 0

            def is_visible(self):
                return self.page.dialog_visible

            def locator(self, selector):
                self.page.confirm_selector = selector
                return self.page.confirm

        class Page:
            def __init__(self):
                self.dialog_visible = False
                self.stop = Button(self._show_dialog)
                self.confirm = Button(self._confirm)
                self.confirm_selector = None

            def _show_dialog(self):
                self.dialog_visible = True

            def _confirm(self):
                self.dialog_visible = False
                self.stop.visible = False

            def locator(self, selector):
                if selector == STOP_RESPONSE_SELECTOR:
                    return self.stop
                if selector == CANCEL_GENERATION_DIALOG_SELECTOR:
                    return Dialog(self)
                if selector == MODEL_RESPONSE_SELECTOR:
                    return EmptyLocator()
                return EmptyLocator()

        page = Page()
        driver = GeminiDriver("http://127.0.0.1:1")
        self.assertTrue(driver._stop_owned_task(page, timeout_s=0.5))
        self.assertEqual(page.stop.clicked, 1)
        self.assertEqual(page.confirm.clicked, 1)
        self.assertEqual(page.confirm_selector, CONFIRM_CANCEL_SELECTOR)

    def test_wait_response_does_not_reconfirm_prompt_after_dispatch(self):
        class Content:
            def count(self):
                return 1

            def get_attribute(self, name):
                self.assertion = name
                return "false"

            def inner_text(self):
                return '{"version":1,"type":"tool_call","call_id":"c1","name":"read_file","arguments":{}}'

        class Footer:
            def count(self):
                return 1

        class Response:
            def locator(self, selector):
                if selector == MESSAGE_CONTENT_SELECTOR:
                    return Content()
                if selector == COMPLETE_FOOTER_SELECTOR:
                    return Footer()
                raise AssertionError(selector)

        class Responses:
            def count(self):
                return 1

            @property
            def last(self):
                return Response()

        class Page:
            url = "https://gemini.google.com/spark/chat/owned"

            def locator(self, selector):
                if selector == MODEL_RESPONSE_SELECTOR:
                    return Responses()
                raise AssertionError(selector)

        driver = GeminiDriver("http://127.0.0.1:1")
        with patch("browser_chat_bridge.gemini.time.sleep", return_value=None):
            result = driver._wait_response(
                Page(),
                baseline_responses=0,
                conversation_id="owned",
                conversation_url="https://gemini.google.com/spark/chat/owned",
                expected_prompt="the exact prompt was already confirmed during dispatch",
                deadline=time.monotonic() + 0.2,
            )

        self.assertEqual(result.status, "COMPLETED")
        self.assertIn('"name":"read_file"', result.content or "")

    def test_wait_response_rejects_tool_permission_surface_as_ambiguous(self):
        class Content:
            def count(self):
                return 1

            def get_attribute(self, name):
                self.assertion = name
                return "false"

            def inner_text(self):
                return (
                    "Connector\nLet Gemini use \"memory search\" from Connector\n"
                    "Tool: memory_search\nquery: project folder\nDeny\nAllow"
                )

        class Footer:
            def count(self):
                return 1

        class Response:
            def locator(self, selector):
                if selector == MESSAGE_CONTENT_SELECTOR:
                    return Content()
                if selector == COMPLETE_FOOTER_SELECTOR:
                    return Footer()
                raise AssertionError(selector)

        class Responses:
            def count(self):
                return 1

            @property
            def last(self):
                return Response()

        class Page:
            url = "https://gemini.google.com/spark/chat/owned"

            def locator(self, selector):
                if selector == MODEL_RESPONSE_SELECTOR:
                    return Responses()
                raise AssertionError(selector)

        driver = GeminiDriver("http://127.0.0.1:1")
        result = driver._wait_response(
            Page(),
            baseline_responses=0,
            conversation_id="owned",
            conversation_url="https://gemini.google.com/spark/chat/owned",
            expected_prompt="already confirmed",
            deadline=time.monotonic() + 0.2,
        )

        self.assertEqual(result.status, "AMBIGUOUS")
        self.assertIn("tool-permission surface", result.error or "")

    def test_task_wait_prefers_selected_goal_when_history_hydrates_multiple_cards(self):
        class GoalCard:
            def __init__(self, goal_id, selected=False):
                self.goal_id = goal_id
                self.selected = selected
                self.clicked = 0

            def get_attribute(self, name):
                if name == "id":
                    return self.goal_id
                if name == "aria-selected":
                    return "true" if self.selected else "false"
                return None

            def count(self):
                return 1

            def click(self, timeout):
                self.clicked += 1
                self.timeout = timeout

        class GoalCards:
            def __init__(self):
                self.items = [
                    GoalCard("goal-c_old1"),
                    GoalCard("goal-c_old2"),
                    GoalCard("goal-c_current", selected=True),
                ]

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Content:
            def count(self):
                return 1

            def get_attribute(self, name):
                self.assertion = name
                return "false"

            def inner_text(self):
                return '{"version":1,"type":"final","content":"PASS"}'

        class Footer:
            def count(self):
                return 1

        class Response:
            def locator(self, selector):
                if selector == MESSAGE_CONTENT_SELECTOR:
                    return Content()
                if selector == COMPLETE_FOOTER_SELECTOR:
                    return Footer()
                raise AssertionError(selector)

        class Responses:
            def count(self):
                return 1

            @property
            def last(self):
                return Response()

        class Page:
            url = "https://gemini.google.com/spark/tasks"

            def __init__(self):
                self.goals = GoalCards()

            def locator(self, selector):
                if selector == GOAL_CARD_SELECTOR:
                    return self.goals
                if selector == MODEL_RESPONSE_SELECTOR:
                    return Responses()
                if selector.startswith("#goal-c_"):
                    goal_id = selector[1:]
                    return next(item for item in self.goals.items if item.goal_id == goal_id)
                raise AssertionError(selector)

        page = Page()
        driver = GeminiDriver("http://127.0.0.1:1")
        with patch("browser_chat_bridge.gemini.time.sleep", return_value=None):
            result = driver._wait_response(
                page,
                baseline_responses=0,
                conversation_id=None,
                conversation_url=None,
                expected_prompt="already confirmed",
                execution_kind="task",
                baseline_goal_ids=frozenset(),
                deadline=time.monotonic() + 0.2,
            )

        self.assertEqual(result.status, "COMPLETED")
        current = next(item for item in page.goals.items if item.goal_id == "goal-c_current")
        self.assertEqual(current.clicked, 1)

    def test_correlated_task_binding_waits_for_selected_card_after_history_hydration(self):
        class GoalCard:
            def __init__(self, page, goal_id, selected_after=0):
                self.page = page
                self.goal_id = goal_id
                self.selected_after = selected_after

            def get_attribute(self, name):
                if name == "id":
                    return self.goal_id
                if name == "aria-selected":
                    if self.goal_id == "goal-c_current":
                        self.page.selected_checks += 1
                        return "true" if self.page.selected_checks > self.selected_after else "false"
                    return "false"
                return None

        class GoalCards:
            def __init__(self, page):
                self.items = [
                    GoalCard(page, "goal-c_old1"),
                    GoalCard(page, "goal-c_old2"),
                    GoalCard(page, "goal-c_current", selected_after=2),
                ]

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            url = "https://gemini.google.com/spark/tasks"

            def __init__(self):
                self.selected_checks = 0
                self.goals = GoalCards(self)

            def locator(self, selector):
                if selector == GOAL_CARD_SELECTOR:
                    return self.goals
                raise AssertionError(selector)

        page = Page()
        driver = GeminiDriver("http://127.0.0.1:1")
        with patch("browser_chat_bridge.gemini.time.sleep", return_value=None):
            conversation_id, conversation_url, goal_id = driver._wait_correlated_task_binding(
                page,
                set(),
                timeout_s=0.2,
            )

        self.assertEqual(goal_id, "goal-c_current")
        self.assertEqual(conversation_id, "current")
        self.assertEqual(conversation_url, "https://gemini.google.com/spark/chat/current")

    def test_correlated_task_binding_accepts_current_tabindex_when_aria_selected_is_false(self):
        class GoalCard:
            def __init__(self, goal_id, tabindex):
                self.goal_id = goal_id
                self.tabindex = tabindex

            def get_attribute(self, name):
                if name == "id":
                    return self.goal_id
                if name == "aria-selected":
                    return "false"
                if name == "tabindex":
                    return self.tabindex
                return None

        class GoalCards:
            def __init__(self):
                self.items = [
                    GoalCard("goal-c_current", "0"),
                    GoalCard("goal-c_old1", "-1"),
                    GoalCard("goal-c_old2", "-1"),
                ]

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Page:
            url = "https://gemini.google.com/spark/tasks"

            def __init__(self):
                self.goals = GoalCards()

            def locator(self, selector):
                if selector == GOAL_CARD_SELECTOR:
                    return self.goals
                raise AssertionError(selector)

        page = Page()
        driver = GeminiDriver("http://127.0.0.1:1")
        conversation_id, conversation_url, goal_id = driver._wait_correlated_task_binding(
            page,
            {"goal-c_old1", "goal-c_old2"},
            timeout_s=0.2,
        )

        self.assertEqual(goal_id, "goal-c_current")
        self.assertEqual(conversation_id, "current")
        self.assertEqual(conversation_url, "https://gemini.google.com/spark/chat/current")

    def test_automation_page_close_is_best_effort(self):
        class Page:
            def __init__(self, fail=False):
                self.closed = 0
                self.fail = fail

            def close(self):
                self.closed += 1
                if self.fail:
                    raise RuntimeError("simulated close failure")

        page = Page()
        GeminiDriver._close_page_quietly(page)
        self.assertEqual(page.closed, 1)
        failing = Page(fail=True)
        GeminiDriver._close_page_quietly(failing)
        self.assertEqual(failing.closed, 1)

    def test_owned_target_registry_tracks_only_valid_durable_conversations(self):
        driver = GeminiDriver("http://127.0.0.1:1")
        driver._remember_owned_target(
            "https://gemini.google.com/spark/chat/owned123",
            "target-123",
        )
        driver._remember_owned_target("https://example.com/not-gemini", "target-other")
        driver._remember_owned_target("https://gemini.google.com/spark/chat/no-target", None)

        self.assertEqual(
            driver._owned_target_snapshot(),
            {"https://gemini.google.com/spark/chat/owned123": "target-123"},
        )
        self.assertEqual(
            driver._forget_owned_target("https://gemini.google.com/spark/chat/owned123"),
            "target-123",
        )
        self.assertEqual(driver._owned_target_snapshot(), {})

    def test_close_cdp_target_uses_exact_owned_target_id(self):
        class Session:
            def __init__(self):
                self.calls = []
                self.detached = 0

            def send(self, method, params):
                self.calls.append((method, params))
                return {"success": True}

            def detach(self):
                self.detached += 1

        class Browser:
            def __init__(self):
                self.session = Session()

            def new_browser_cdp_session(self):
                return self.session

        browser = Browser()
        self.assertTrue(GeminiDriver._close_cdp_target(browser, "target-owned"))
        self.assertEqual(
            browser.session.calls,
            [("Target.closeTarget", {"targetId": "target-owned"})],
        )
        self.assertEqual(browser.session.detached, 1)

    def test_automation_page_tag_distinguishes_owned_from_human_tabs(self):
        class Page:
            def __init__(self):
                self.name = ""

            def evaluate(self, script, value=None):
                if value is not None:
                    self.name = value
                    return None
                return self.name

        owned = Page()
        human = Page()
        tag = GeminiDriver._tag_automation_page(owned, "target-123")

        self.assertEqual(tag, "browser-chat-bridge:target-123")
        self.assertEqual(GeminiDriver._automation_page_name(owned), tag)
        self.assertTrue(GeminiDriver._page_is_automation_owned(owned))
        self.assertFalse(GeminiDriver._page_is_automation_owned(human))

    def test_unbound_orphan_sweep_uses_thirty_minute_grace(self):
        self.assertFalse(unbound_owned_page_releasable(None))
        self.assertFalse(unbound_owned_page_releasable(ORPHAN_UNBOUND_GRACE_S - 0.1))
        self.assertTrue(unbound_owned_page_releasable(ORPHAN_UNBOUND_GRACE_S))

    def test_active_page_tags_are_isolated_from_orphan_candidates(self):
        driver = GeminiDriver("http://127.0.0.1:1")
        driver._mark_page_active("browser-chat-bridge:active")
        self.assertEqual(driver._active_page_name_snapshot(), {"browser-chat-bridge:active"})
        driver._mark_page_inactive("browser-chat-bridge:active")
        self.assertEqual(driver._active_page_name_snapshot(), set())

    def test_ready_composer_prefers_last_visible_spark_chat_input(self):
        class Composer:
            def __init__(self, name):
                self.name = name

            def is_visible(self):
                return True

        class Locator:
            def __init__(self):
                self.items = [Composer("task"), Composer("chat")]

            def count(self):
                return len(self.items)

            @property
            def last(self):
                return self.items[-1]

        class Page:
            def locator(self, _selector):
                return Locator()

        composer = GeminiDriver._ready_composer(Page())
        self.assertIsNotNone(composer)
        self.assertEqual(composer.name, "chat")

    def test_fresh_spark_page_eligibility_requires_empty_ready_home(self):
        self.assertTrue(
            fresh_spark_page_eligible(
                "https://gemini.google.com/spark",
                0,
                0,
                True,
                False,
            )
        )
        self.assertFalse(
            fresh_spark_page_eligible(
                "https://gemini.google.com/spark/tasks",
                0,
                0,
                True,
                False,
            )
        )
        self.assertFalse(
            fresh_spark_page_eligible(
                "https://gemini.google.com/spark",
                1,
                1,
                True,
                False,
            )
        )
        self.assertFalse(
            fresh_spark_page_eligible(
                "https://gemini.google.com/spark",
                0,
                0,
                True,
                True,
            )
        )

    def test_reusable_fresh_spark_page_requires_exactly_one_safe_candidate(self):
        class Locator:
            def __init__(self, count=0, visible=False):
                self._count = count
                self._visible = visible

            def count(self):
                return self._count

            def nth(self, _index):
                return self

            def is_visible(self):
                return self._visible

            @property
            def last(self):
                return self

        class Page:
            def __init__(
                self,
                url,
                *,
                users=0,
                responses=0,
                stop=False,
                composer=True,
                target_id="t",
            ):
                self.url = url
                self.users = users
                self.responses = responses
                self.stop = stop
                self.composer = composer
                self.target_id = target_id

            def locator(self, selector):
                if selector == USER_QUERY_SELECTOR:
                    return Locator(self.users)
                if selector == MODEL_RESPONSE_SELECTOR:
                    return Locator(self.responses)
                if selector == STOP_RESPONSE_SELECTOR:
                    return Locator(1 if self.stop else 0, self.stop)
                if selector == COMPOSER_SELECTOR:
                    return Locator(1 if self.composer else 0, self.composer)
                raise AssertionError(selector)

        class Context:
            def __init__(self, pages):
                self.pages = pages

        driver = GeminiDriver("http://127.0.0.1:1")
        reusable = Page("https://gemini.google.com/spark", target_id="reused-target")
        busy = Page(
            "https://gemini.google.com/spark",
            users=1,
            responses=0,
            target_id="busy",
        )
        task = Page("https://gemini.google.com/spark/tasks", target_id="task")
        self.assertIs(
            driver._find_reusable_fresh_spark_page(Context([busy, reusable, task])),
            reusable,
        )
        self.assertIsNone(
            driver._find_reusable_fresh_spark_page(
                Context(
                    [
                        reusable,
                        Page("https://gemini.google.com/spark", target_id="second"),
                    ]
                )
            )
        )


if __name__ == "__main__":
    unittest.main()

