# v2 - a from-scratch redesign

Built after comparing this repo's original agent (v1, in the parent directory)
against [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).
Combines the best idea from each rather than picking one wholesale.

## What v1 had that jev-ultrafast didn't

- Glyph-safe accessible-name extraction. Icon-font buttons (Material Icons
  etc.) inject a Private-Use-Area glyph character as their "text" - v1 fixed
  this; jev-ultrafast's `snapshot.js` doesn't, and it broke on yopmail's
  icon-only Send/Compose buttons in a live side-by-side test.
- Multi-frame (iframe) scanning - many real forms live inside an iframe.
- Disabled controls kept as annotated candidates instead of dropped, so a
  "found but disabled" state is distinguishable from "not found at all."
- A planner LLM that decomposes a multi-hop instruction into an ordered plan
  and can replan a stuck step, instead of re-asking the same single goal
  string forever.

## What jev-ultrafast had that v1 didn't

- One TypeSafe request per tick decides BOTH the operation (CLICK/TYPE_TEXT/
  SELECT/SCROLL/WAIT/DONE/BLOCKED) and its target, instead of the planner
  pre-deciding the action type and spending a second call just to ground it.
- Stable node identity across ticks (a JS-side id map), so execution re-fetches
  the exact DOM node a decision was made against instead of re-resolving a
  fresh candidate list by description on every click.
- A freshness guard compares a small state tuple (disabled/value/checked) for
  the *specific* target right before touching it - catches drift during the
  Jev/text-helper network round trip that a full new snapshot wouldn't
  necessarily need.
- Response-shape validation before trusting anything (`validate_choice`) - v1
  never checked whether TypeSafe's returned probabilities were even coherent.

v2 combines these: single combined operation+target call, stable node
identity + freshness guards, response validation, glyph-safe/iframe-safe/
disabled-annotated snapshotting, and a planner for decomposition + replanning
on BLOCKED. It still uses Playwright (launches its own browser) rather than
attaching to a live Chrome over CDP like jev-ultrafast/browser-harness -
deliberately, for portability: no remote-debugging dance, works the same on
any OS, and doesn't require (or risk touching) whatever browser the user
already has open.

## Bugs found and fixed while building this

- **Password fields excluded entirely** (copied from jev-ultrafast's own
  safety filter) broke every login flow, live, on SauceDemo. Since the
  literal password already flows through the goal string to the planner/
  text-helper anyway, hiding the *field* added no real privacy and just
  broke functionality. Fixed: password inputs are valid TYPE_TEXT targets,
  but their live typed value is masked (`"(filled)"` / `""`) rather than
  echoed back to Jev on later observations.
- **Unbounded stale-retry loop.** A reCAPTCHA-style widget can defensively
  regenerate itself on every attempted interaction - that's its job working
  as intended, not transient DOM drift worth chasing. The freshness guard
  correctly flagged it as stale every time, but nothing stopped it from
  re-asking Jev (a paid call) in a tight loop until the entire step budget
  was gone. Fixed with a 5-strike circuit breaker that fails fast with a
  clear reason instead.

## Known limitation: reCAPTCHA / bot-scoring sites

yopmail (and Google) can show a reCAPTCHA challenge to automated traffic.
This is true of every approach tried in this environment - Playwright's own
launched Chromium, and even attaching over CDP to this sandbox's Chrome,
since that Chrome is itself a fresh, headless, `--enable-automation`-flagged
instance with no real cookies/history, not a genuinely-used daily-driver
profile. CDP-attach only helps against bot scoring when there's an actual
organic browser session to attach to; there isn't one in this sandbox.
Verified end-to-end instead on sites without bot-detection (Wikipedia
article search, a SauceDemo login → add-to-cart → checkout flow) - both
completed cleanly with zero replans needed.

## Try it

```bash
cd browser-agent
uv run python3 run_v2_cli.py --url "https://www.saucedemo.com/" \
  "Log in with username 'standard_user' and password 'secret_sauce'. Then add the 'Sauce Labs Backpack' to the cart, go to the cart, and click Checkout."
```

`--headed` to watch it, `--no-planner` to run the instruction as one atomic
goal (skips decomposition), `--cdp http://127.0.0.1:9222` to attach to an
already-running Chrome instead of launching a fresh one.
