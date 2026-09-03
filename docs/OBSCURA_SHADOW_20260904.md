# Obscura Gemini Shadow — 2026-09-04

## Result

The same Gemini Driver interface was exercised against a dedicated Obscura CDP
server on loopback. Obscura successfully loaded `https://gemini.google.com/app`
and exposed the expected Gemini title, composer, and model picker DOM.

The unauthenticated Obscura session exposed these model choices:

- 3.5 Flash-Lite
- 3.6 Flash
- 3.1 Pro
- Sign in for all models

The required production mode, Gemini 3.8 Flash with enhanced thinking, was not
available. The Driver therefore failed closed with `MODEL_MISMATCH` before any
prompt dispatch.

## Disposition

`chromium` remains the production backend because its two-turn live E2E passed
with exact run-to-conversation binding and the fixed `Flash / 拡張` mode.

`obscura` remains implemented behind the same Driver interface as a shadow or
future backend. Promotion requires an independently authenticated Obscura
profile that exposes the required model and then passes the same two-turn live
E2E. Browser Chat Bridge does not transfer browser session credentials between
engines automatically.
