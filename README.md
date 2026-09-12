# Browser Chat Bridge

[English](README.md) | [日本語](README.ja.md)

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

A safe environment template is available in `.env.example`. Keep machine-specific paths and credentials outside the repository.

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

Browser Chat opens `https://gemini.google.com/spark`. Spark implicitly uses
the model exposed by that UI and has no model selector, so the Driver performs
no model-selection UI interaction. Durable conversations are bound by
`https://gemini.google.com/spark/chat/<id>`.

`CHAT_DRIVER_BACKEND=obscura` uses the same Driver contract. Production remains
on Chromium; see `docs/OBSCURA_SHADOW_20260904.md`.

See `docs/DESIGN.md` and `docs/GEMINI_DOM_CONTRACT_20260904.md`.

## Operations

On Windows, Browser Chat owns a dedicated persistent Microsoft Edge profile via
`nodriver`. Browser Host, Driver, and Bridge stay lightweight and resident, but
Edge itself is lazy-started only after the Bridge admits a real browser-chat
turn. The Driver is then rebound to nodriver's dynamic local CDP endpoint. The
profile is isolated from normal Edge and defaults to:

```text
%LOCALAPPDATA%\BrowserChatBridge\BrowserChatEdge\User Data
```

Override it with `CHAT_BROWSER_PROFILE` when a different local location is required.

Manage the nodriver Edge host, Driver, and Bridge together with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
powershell -ExecutionPolicy Bypass -File scripts/status.ps1
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

`start.ps1` starts the Browser Host / Driver / Bridge control plane but does not
open Edge. On the first admitted browser-chat turn, Bridge calls Browser Host
`POST /ensure`; Browser Host single-flights concurrent callers and starts (or
reattaches) the dedicated Edge, then Bridge asks Driver `POST /v1/rebind` to use
the returned CDP endpoint before any prompt is sent. If the user later closes
Edge, it stays closed until the next admitted browser-chat turn, which launches
it again. A third concurrent turn still receives `BUSY` before this lazy-start
preflight. `stop.ps1` closes only Browser Chat's recorded services and dedicated
BrowserChatEdge processes; it does not touch unrelated Edge processes.

## DSH integration handoff

For a fresh AI or DSH orchestrator implementing DSH integration end-to-end,
including live verification and independent review, use:

- `docs/DSH_ORCHESTRATOR_HANDOFF_20260904.md` — current contracts, design
  constraints, acceptance matrix, and review/completion gates.
- `docs/DSH_ORCHESTRATOR_PROMPT_20260904.md` — copyable top-level execution
  prompt with the recommended parallel lanes and model-depth assignments.
- `docs/DSH_BROWSER_CHAT_PROVIDER_PLAN_20260904.md` — frozen implementation
  contract and plan for the DSH `browser-chat` subagent provider.

## Public repository boundary

All public branches must remain safe to disclose. Do not commit credentials,
real workstation paths, browser profile data, runtime logs, local worktrees, or
private integration state.

## License

This project is licensed under the MIT License. See `LICENSE` for details.
