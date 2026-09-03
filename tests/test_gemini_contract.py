from __future__ import annotations

import unittest

from browser_chat_bridge.gemini import (
    GeminiDriver,
    durable_urls_from_target_rows,
    fixed_model_selected,
    normalize_text,
    parse_conversation_id,
    prompt_matches,
)


class GeminiContractTests(unittest.TestCase):
    def test_conversation_id_comes_from_durable_app_route(self):
        self.assertEqual(
            parse_conversation_id("https://gemini.google.com/app/491c5405bb57437c"),
            "491c5405bb57437c",
        )
        self.assertIsNone(parse_conversation_id("https://gemini.google.com/app"))
        self.assertIsNone(parse_conversation_id("https://example.com/app/abc"))

    def test_fixed_model_requires_flash_expansion(self):
        self.assertTrue(fixed_model_selected("Flash\n拡張"))
        self.assertFalse(fixed_model_selected("Flash"))
        self.assertFalse(fixed_model_selected("3.5 Flash-Lite"))

    def test_prompt_confirmation_normalizes_only_line_endings_and_outer_space(self):
        self.assertTrue(prompt_matches("hello\r\nworld", "hello\nworld"))
        self.assertFalse(prompt_matches("hello  world", "hello world"))
        self.assertEqual(normalize_text("  a\r\nb  "), "a\nb")

    def test_new_chat_marker_selects_only_the_exact_background_target(self):
        class Page:
            def __init__(self, url):
                self.url = url

        class Context:
            pages = [
                Page("https://gemini.google.com/app"),
                Page("https://gemini.google.com/app#bcb-old"),
                Page("https://gemini.google.com/app#bcb-new"),
            ]

        selected = GeminiDriver._find_page(Context(), "https://gemini.google.com/app#bcb-new")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.url, "https://gemini.google.com/app#bcb-new")

    def test_durable_target_rows_extract_only_gemini_conversations(self):
        rows = [
            {"type": "page", "url": "https://gemini.google.com/app/abc123"},
            {"type": "page", "url": "https://gemini.google.com/app"},
            {"type": "page", "url": "https://example.com/app/ignored"},
            {"type": "service_worker", "url": "https://gemini.google.com/app/worker"},
        ]
        self.assertEqual(
            durable_urls_from_target_rows(rows),
            {"https://gemini.google.com/app/abc123"},
        )


if __name__ == "__main__":
    unittest.main()

