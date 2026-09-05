# Browser Chat Bridge

Local run-scoped bridge for driving a browser-hosted Gemini-compatible chat UI.
The caller sends one new prompt; the cloud conversation retains history.

## Processes

Run the Driver and Bridge independently:

```powershell
$env:CHAT_DRIVER_CDP_ENDPOINT = "http://127.0.0.1:51881"
$env:CHAT_DRIVER_BACKEND = "chromium"
python -m browser_chat_bridge.driver_server

python -m browser_chat_bridge.bridge_server
```

Then send a turn:

```powershell
$body = @{ request_id = "req-1"; prompt = "Reply with OK" } | ConvertTo-Json
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8765/v1/runs/run-1/turn" `
  -ContentType "application/json" -Body $body
```

The first turn creates a new Gemini conversation; later turns with a new
`request_id` under the same `run-1` reuse exactly that conversation. A different
run starts a different conversation.

The Bridge admits at most two newly created turns at once. A third concurrent
turn returns `BUSY` before Driver dispatch; the `request_id` caches that result,
so callers can fall back without risking a late duplicate browser send.

The fixed UI mode is Gemini 3.8 Flash with `強化版思考モード` enabled. The
Driver identifies the current middle Flash option by UI family (`Flash`
present, `Lite` absent) and requires the compact button summary to also carry
`拡張`; this avoids depending on the version number rendered by a particular
Gemini Web rollout while still rejecting Flash-Lite and Pro.

`CHAT_DRIVER_BACKEND=obscura` uses the same Driver contract. Current shadow
verification shows that Obscura can hydrate the Gemini SPA/composer, but an
unauthenticated Obscura profile does not expose the required non-Lite Flash +
enhanced mode, so production remains on Chromium. See
`docs/OBSCURA_SHADOW_20260904.md`.

See `docs/DESIGN.md` and `docs/GEMINI_DOM_CONTRACT_20260904.md`.

## Operations

On Windows, the production Bridge and Driver can be managed independently of
the user-owned authenticated Chromium process:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
powershell -ExecutionPolicy Bypass -File scripts/status.ps1
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

`start.ps1` never launches, focuses, refreshes, or closes Chromium. An explicit
`-CdpEndpoint` or `CHAT_DRIVER_CDP_ENDPOINT` remains authoritative; otherwise
the script detects the current AegisChrome `--remote-debugging-port` so an
AegisChrome restart that changes the port does not leave the Driver bound to a
stale endpoint. If no AegisChrome endpoint can be detected, the historical
`127.0.0.1:51881` fallback is used.

## DSH integration handoff

For a fresh AI or DSH orchestrator that should implement the remaining DSH
integration end-to-end, including live verification and independent review, use:

- `docs/DSH_ORCHESTRATOR_HANDOFF_20260904.md` — current contracts, design
  constraints, acceptance matrix, and review/completion gates.
- `docs/DSH_ORCHESTRATOR_PROMPT_20260904.md` — copyable top-level execution
  prompt with the recommended parallel lanes and model-depth assignments.
- `docs/DSH_BROWSER_CHAT_PROVIDER_PLAN_20260904.md` — frozen implementation
  contract and plan for the DSH `browser-chat` subagent provider, with the
  staged change set and apply procedure.

