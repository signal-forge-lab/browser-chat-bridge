# Gemini DOM contract - live probe 2026-09-04, Spark update 2026-09-09

The probe used a dedicated background Gemini tab in the existing CDP browser.
No existing ChatGPT/Converlay tab was navigated or focused.

## Composer

```text
rich-textarea
  div.ql-editor[contenteditable=true][role=textbox]
```

Stable selector used by the Driver:

```text
rich-textarea div[contenteditable="true"][role="textbox"]
```

The container exposes `data-test-id="textarea-inner"` and
`data-test-id="textarea-wrapper"`.

## Send

After a real Playwright fill the unique button is under:

```text
div[data-test-id="send-button-container"]
  button[aria-label="プロンプトを送信"]
```

Direct DOM text assignment is insufficient for this Quill/Angular input. The
first synthetic probe demonstrated that visual state and framework state can
diverge.

## Model mode

The current Spark route is:

```text
https://gemini.google.com/spark
```

Spark exposes no model picker. The Browser Chat route implicitly uses Gemini
3.8 Flash, so the Driver no longer selects or verifies a model menu before
dispatch.

## Turn structure

Each persisted prompt creates one `user-query`; each answer creates one
`model-response`.

The visible User prompt is:

```text
user-query .query-text p.query-text-line
```

The Assistant body is:

```text
model-response message-content .markdown
```

The live response carried a response id in the `message-content` id and a
conversation id in `response-container` metadata, but these are not needed for
the primary turn binding.

## Completion

A completed live response had:

```text
message-content .markdown aria-busy="false"
.response-footer.complete
```

Normal chat turns promote from `/spark` to:

```text
https://gemini.google.com/spark/chat/491c5405bb57437c
```

The Driver treats the `/spark/chat/<id>` segment as the run's durable
conversation identity. File-tool executions can instead move to
`https://gemini.google.com/spark/tasks`; those are single unbound tasks and do
not create a durable run binding.

The 2026-09-09 live probes also established that Spark can read files from the
connected root when addressed as `Gドライブのルート\<folder>\<file>`. Direct
plain-text Drive writes are currently unreliable, so DSH's workspace bridge
uses read-only staging plus a locally validated unified-diff fallback for
mutation tasks.

## Duplicate-send finding

The first low-level synthetic send appeared not to progress. A later Playwright
probe then showed that it had in fact persisted, resulting in two identical
prompts. This is direct live evidence for the Converlay-style rule:

```text
pre-submit failure proven -> NOT_DISPATCHED
click may have happened but persistence unproven -> AMBIGUOUS
AMBIGUOUS -> never blind resend
```

