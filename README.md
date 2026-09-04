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

The fixed UI mode is Gemini 3.8 Flash with `強化版思考モード` enabled.

`CHAT_DRIVER_BACKEND=obscura` uses the same Driver contract. Current shadow
verification shows that Obscura can hydrate the Gemini SPA/composer, but an
unauthenticated Obscura profile does not expose the required 3.8 Flash enhanced
mode, so production remains on Chromium. See `docs/OBSCURA_SHADOW_20260904.md`.

See `docs/DESIGN.md` and `docs/GEMINI_DOM_CONTRACT_20260904.md`.

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

