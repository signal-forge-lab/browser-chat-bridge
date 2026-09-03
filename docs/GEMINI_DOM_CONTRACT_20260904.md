# Gemini DOM contract - live probe 2026-09-04

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

The model picker is:

```text
button[data-test-id="bard-mode-menu-button"]
```

The live menu exposed:

```text
3.5 Flash-Lite
3.8 Flash
3.1 Pro
強化版思考モード
```

`3.8 Flash` had `data-mode-id="56fdd199312815e2"` during the probe. The Driver
does not hard-code that server value. It selects by the visible menu contract,
then selects `強化版思考モード`, and finally verifies the picker renders:

```text
Flash
拡張
```

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

The durable conversation URL promoted from `/app` to:

```text
https://gemini.google.com/app/491c5405bb57437c
```

The Driver treats the `/app/<id>` segment as the run's durable conversation
identity.

## Duplicate-send finding

The first low-level synthetic send appeared not to progress. A later Playwright
probe then showed that it had in fact persisted, resulting in two identical
prompts. This is direct live evidence for the Converlay-style rule:

```text
pre-submit failure proven -> NOT_DISPATCHED
click may have happened but persistence unproven -> AMBIGUOUS
AMBIGUOUS -> never blind resend
```

