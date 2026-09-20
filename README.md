# Jev Browser Agent

<video src="https://github.com/jagadeeswara-reddy-p/browser-agent/releases/download/demo-media/screencast.webm" controls autoplay loop muted width="100%">
  Your browser doesn't support inline video - <a href="https://github.com/jagadeeswara-reddy-p/browser-agent/releases/download/demo-media/screencast.webm">download the screencast</a> directly.
</video>

> Notes:
> - GitHub only renders `<video>` tags inline when `src` points to their own
>   attachment CDN or a Release asset URL (like above) - a relative path to a
>   file committed in the repo gets stripped by their README sanitizer and
>   won't play. The video is hosted as a [GitHub Release asset](https://github.com/jagadeeswara-reddy-p/browser-agent/releases/tag/demo-media)
>   for that reason.
> - This repo is private, so the video will only load for accounts with
>   access, viewed while logged in on github.com.
> - GitHub's sanitizer also generally strips `autoplay`, so this will most
>   likely render as a normal playable video (click to play) rather than
>   actually autoplaying.

A proof of concept for an AI-driven browser automation agent: give it a
sequence of steps in plain English, and it understands, plans, and executes
them in a real browser — navigating, clicking, typing, extracting data, and
running small computations on the results.

It splits the work across three purpose-fit pieces instead of asking one
model to do everything:

- **A planner LLM** (`dev-model-alpha`, via a gateway) reads your instruction
  and decomposes it into an ordered list of atomic steps. It's the only piece
  that authors free text (e.g. deciding literal values to type), and it's
  called again to replan if a step fails.
- **[Jev](https://typesafe.ai)** (TypeSafe's "System One" model) does one
  narrow, repeated job: given a text list of every visible clickable/typeable
  element on the current page, decide *which one* matches a step's target
  description (`"the Submit button"`, `"the Password field"`) — or honestly
  say none of them do. It never sees a screenshot (it's text-only) and never
  decides what to do next; it just grounds a description to a specific
  element, fast and cheap.
- **Playwright** does the actual browser work: reading the page's interactive
  elements, and executing the click/fill/extract that Jev and the planner
  decided on.

## Why split it this way

Jev is a "System One" model: it returns typed, calibrated decisions (a
`choice` from an option set, a `score`, or a yes/no `noul`) instead of
generating text, and it's built to be fast/cheap for exactly this kind of
narrow, repeated, structured decision. Grounding "which element matches this
description" is a bounded choice problem — a good fit. Deciding what the
overall task means, breaking it into steps, and writing the literal content
to type needs actual language generation, which is the planner LLM's job.
Playwright's own accessibility snapshot API is fast enough (single-digit
milliseconds) that it's never the bottleneck; the Jev API call dominates
per-step latency (a few hundred ms).

## How a run works

1. **Plan** — the planner LLM turns your instruction into a JSON step list:
   `navigate`, `type`, `click`, `extract`, `code`, or `done`.
2. **Observe** — before each `click`/`type`/`extract` step, Playwright scans
   the live page for visible interactive elements and builds a plain-text
   candidate list (human-readable label + tag/type for each one).
3. **Ground** — that candidate list, plus the step's target description, goes
   to Jev as a single `choice` question, with an explicit "none of these"
   option. Jev returns which candidate matches (or none), with a confidence
   score.
4. **Act** — if Jev found a confident match, Playwright clicks/fills/reads
   that element. `type` steps use the literal text the planner already wrote
   into the step; Jev never generates the text.
5. **Replan on failure** — if Jev says "none of these" (or confidence is
   below threshold), the current page's full candidate list and the failure
   reason go back to the planner LLM, which returns a corrected step list to
   continue from.
6. **Code steps** — for pure computation on previously-extracted values (e.g.
   "double the number you just read"), the planner emits literal Python,
   executed in a restricted scope with no filesystem/network access.

## Project layout

| File | Role |
|---|---|
| `planner_client.py` | Planner LLM client (decompose + replan) |
| `browser_bridge.py` | Playwright session: candidate extraction, click/fill/extract |
| `jev_grounding.py` | The single Jev `choice` call per grounding decision, with confidence gating |
| `agent_loop.py` | Orchestrator tying planner → grounding → browser together, with the replan loop |
| `run_cli.py` | Text-mode runner (also supports a visible, slowed-down browser for watching it work) |
| `jev_client.py` | Thin async client for Jev's `/v1/systemone` endpoint |
| `env_loader.py` | Loads credentials from a `.env` file — no manual `source` needed |

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync
```

This creates `.venv/` and installs Playwright + httpx. Playwright needs
Chromium available — if `uv run python3 -c "from playwright.sync_api import sync_playwright as s; s().start().chromium.launch().close()"`
fails with a missing-browser error, install it once with:

```bash
uv run playwright install chromium
```

### Credentials

Create a `.env` file in this directory (never committed — see `.gitignore`)
with:

```
TYPESAFE_API_KEY=your_typesafe_api_key
GATEWAY_API_KEY=your_planner_gateway_api_key
GATEWAY_BASE_URL=https://your-openai-compatible-gateway/v1
```

- `TYPESAFE_API_KEY` — from [typesafe.ai](https://typesafe.ai), used for Jev grounding calls.
- `GATEWAY_API_KEY` / `GATEWAY_BASE_URL` — any OpenAI-compatible chat-completions
  endpoint for the planner model. `planner_client.py` defaults to
  `dev-model-alpha` as the model name and authenticates with an `x-api-key`
  header (not `Authorization: Bearer`) — adjust `planner_client.py` if your
  gateway uses a different scheme or model name.

## Running it

```bash
# Headless, built-in demo (fills and submits a public test form)
uv run python3 run_cli.py

# Your own instruction
uv run python3 run_cli.py "Go to https://example.com/login, type \"me@example.com\" into the email field, type \"password123\" into the password field, then click Sign In."

# Watch it work in a real, visible browser window
uv run python3 run_cli.py --headed --slowmo 300 --pause 1.5

# Watch your own instruction, slowed down
uv run python3 run_cli.py --headed --slowmo 300 --pause 1.5 "your instruction here"
```

Flags:
- `--headed` — opens a visible Chromium window instead of running headless
- `--slowmo MS` — adds artificial delay (ms) to each low-level Playwright
  action, so clicks/typing are visible instead of instant
- `--pause SECS` — pauses after each agent step completes, so you have time
  to read the console log alongside the browser
- `--hold SECS` — keeps the browser open for this long after the run finishes
  (success or failure) before closing, so you can actually check the final
  page state (e.g. that a form really submitted) instead of the window
  vanishing the instant the last step completes. Defaults to 5s in `--headed`
  mode; ignored (treated as 0) when headless, since there's nothing to look at.

The CLI prints every event as it happens: the decomposed plan, each Jev
grounding decision (candidate chosen, confidence, latency), replans if any,
and the final extracted variables.

## What's validated

- Full multi-field form fill + submit, with the submitted values verified
  correct on the resulting page
- Jev correctly returning "not found" (not a wrong guess) for elements that
  don't exist on the page, with high confidence
- Automatic replanning when a step's target isn't found — the planner is
  handed the failure and the current page's real elements, and returns a
  corrected continuation
- `extract` steps (reading a field's value into a named variable) and `code`
  steps (computing on previously-extracted variables) end-to-end

## Known limitations

- No visual UI yet — CLI only, though `--headed` mode lets you watch the
  actual browser.
- Grounding is text-only, no OCR/vision fallback: an icon-only control with
  no `aria-label`, `title`, `placeholder`, meaningful text, or usable `id`
  genuinely can't be identified. (Icon-font glyph text, iframes, disabled
  buttons, and contenteditable rich-text fields are all handled - see
  `browser_bridge.py`'s label-priority logic and frame-scanning `snapshot()`.)
- Grounding is stochastic, not deterministic: a genuinely close call (weak
  wording match, single ambiguous candidate) can flip between found/not-found
  across identical calls. There's a cheap same-question retry for the
  single-candidate case, but this is inherent to using a probabilistic model
  for the decision, not something a code fix eliminates entirely.
- The planner's replan budget is capped (`max_replans`, default 6) to avoid
  infinite loops on a truly broken page/instruction.
- `code` steps run in a restricted `exec()` scope (no imports, no I/O) — this
  is a POC-level safeguard, not a hardened sandbox.
