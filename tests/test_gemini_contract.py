from __future__ import annotations

import unittest

from browser_chat_bridge.gemini import (
    BOUND_HISTORY_TIMEOUT_S,
    CDP_CONNECT_TIMEOUT_MS,
    GeminiDriver,
    bound_history_hydrated,
    composer_prompt_matches,
    durable_urls_from_target_rows,
    normalize_text,
    parse_conversation_id,
    prompt_matches,
    query_prompt_matches,
)


class GeminiContractTests(unittest.TestCase):
    def test_cdp_attach_budget_tolerates_slow_local_browser_handshake(self):
        # The authenticated desktop browser can take materially longer than a
        # loopback HTTP probe to finish Playwright's full CDP attach. Keep the
        # attach budget above the observed ~15 s edge rather than treating a
        # healthy local browser as TARGET_LOST at the old boundary.
        self.assertGreaterEqual(CDP_CONNECT_TIMEOUT_MS, 30_000)

    def test_bound_history_requires_one_complete_hydrated_pair(self):
        self.assertGreaterEqual(BOUND_HISTORY_TIMEOUT_S, 30.0)
        self.assertTrue(bound_history_hydrated(1, 1, True, True, True))
        self.assertTrue(bound_history_hydrated(2, 2, True, True, True))
        self.assertFalse(bound_history_hydrated(0, 0, False, False, False))
        self.assertFalse(bound_history_hydrated(1, 0, True, False, False))
        self.assertFalse(bound_history_hydrated(1, 1, False, True, True))
        self.assertFalse(bound_history_hydrated(1, 1, True, False, True))
        self.assertFalse(bound_history_hydrated(1, 1, True, True, False))

    def test_conversation_id_comes_from_durable_spark_chat_route(self):
        self.assertEqual(
            parse_conversation_id("https://gemini.google.com/spark/chat/491c5405bb57437c"),
            "491c5405bb57437c",
        )
        self.assertIsNone(parse_conversation_id("https://gemini.google.com/spark"))
        self.assertIsNone(parse_conversation_id("https://gemini.google.com/app/abc"))
        self.assertIsNone(parse_conversation_id("https://example.com/spark/chat/abc"))

    def test_first_turn_destination_accepts_spark_tasks_without_waiting_for_chat_promotion(self):
        class Page:
            url = "https://gemini.google.com/spark/tasks"

        driver = GeminiDriver("http://127.0.0.1:1")
        driver._cdp_durable_urls = lambda: set()  # type: ignore[method-assign]

        self.assertEqual(
            driver._wait_first_turn_destination(Page(), set()),
            ("task", None),
        )

    def test_prompt_confirmation_normalizes_only_line_endings_and_outer_space(self):
        self.assertTrue(prompt_matches("hello\r\nworld", "hello\nworld"))
        self.assertFalse(prompt_matches("hello  world", "hello world"))
        self.assertEqual(normalize_text("  a\r\nb  "), "a\nb")

    def test_composer_confirmation_reconstructs_contenteditable_paragraphs(self):
        # Gemini's rich-textarea renders one intentional blank line as
        # <p>line 1</p><p><br></p><p>line 2</p>. Playwright inner_text()
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
        driver._connect_browser = lambda _playwright: next(candidates)  # type: ignore[method-assign]

        browser, page = driver._reattach_find_page(object(), initial, target_url, timeout_s=1.0)

        self.assertIs(browser, second_reattach)
        self.assertIsNotNone(page)
        self.assertEqual(page.url, target_url)
        self.assertEqual(initial.closed, 1)
        self.assertEqual(first_reattach.closed, 1)
        self.assertEqual(second_reattach.closed, 0)

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


if __name__ == "__main__":
    unittest.main()

