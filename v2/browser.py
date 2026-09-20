"""Playwright wrapper: multi-frame snapshotting with stable node identity,
freshness guards, and action execution. Deliberately keeps Playwright (not a
raw CDP-attach to the user's live Chrome, as jev-ultrafast does) - it launches
its own throwaway browser, so it needs no remote-debugging dance against
whatever browser the user happens to have open, and works the same on any OS."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

SNAPSHOT_JS = Path(__file__).with_name("snapshot.js").read_text()

GUARD_JS = """(node => {
  const e = window.__jevV2?.nodes.get(node);
  if (!e || !e.isConnected) return null;
  return [e.matches(':disabled'), e.getAttribute('aria-disabled'),
          'value' in e ? String(e.value) : null, e.checked ?? null];
})"""


class StalePage(Exception):
    """The observed page/element no longer matches what a decision was made against."""


class Frame:
    """One frame's live handle cache + last snapshot, so execution can fetch
    the exact DOM node a decision was made against instead of re-resolving it
    by description."""

    def __init__(self, frame, index):
        self.frame = frame
        self.index = index

    async def snapshot(self):
        try:
            return await self.frame.evaluate(SNAPSHOT_JS)
        except Exception:
            return None  # detached/navigating frame - skip this tick

    async def handle_for(self, node_id):
        js_handle = await self.frame.evaluate_handle(f"window.__jevV2?.nodes.get({node_id})")
        element = js_handle.as_element()
        if element is None:
            raise StalePage(f"Node {node_id} is no longer attached to the page")
        return element

    async def guard(self, node_id):
        return await self.frame.evaluate(GUARD_JS, node_id)


class Browser:
    def __init__(self):
        self._pw = None
        self.browser = None
        self.page = None
        self._owns_browser = False  # False when attached to the user's real Chrome over CDP

    async def start(self, headless: bool = True, slow_mo: int = 0, cdp_url: str | None = None):
        self._pw = await async_playwright().start()
        if cdp_url:
            # Attach to the user's actual, already-running Chrome (started with
            # --remote-debugging-port) instead of launching a fresh throwaway
            # profile. A launched Chromium has no cookies/history and sets
            # navigator.webdriver - exactly what trips Google's bot-risk
            # scoring on sites like yopmail/Google. A real, logged-in-looking
            # profile is far less likely to hit a reCAPTCHA wall at all.
            self.browser = await self._pw.chromium.connect_over_cdp(cdp_url)
            context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
            self.page = await context.new_page()
            self._owns_browser = False
        else:
            self.browser = await self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
            self.page = await self.browser.new_page()
            self._owns_browser = True
            if not headless:
                await self.page.set_viewport_size({"width": 1280, "height": 900})

    async def close(self):
        if self._owns_browser:
            # We launched this browser ourselves - safe to close entirely.
            if self.browser:
                await self.browser.close()
        elif self.page:
            # Attached to the user's real Chrome: close only the tab we
            # opened, never the browser itself or their other tabs.
            await self.page.close()
        if self._pw:
            await self._pw.stop()

    async def navigate(self, url: str):
        await self.page.goto(url, wait_until="load")

    def _frames(self):
        return [Frame(f, i) for i, f in enumerate(self.page.frames) if f.url != "about:blank"]

    async def current_url(self) -> str:
        return self.page.url

    async def snapshot(self) -> dict:
        """One call per frame; merges into a single indexed action table with
        each action tagged by which frame it came from (needed at execution
        time to re-fetch the right node)."""
        frames = self._frames()
        results = await asyncio.gather(*(f.snapshot() for f in frames))
        actions, texts = [], []
        idx = 1
        for frame, result in zip(frames, results):
            if not result:
                continue
            for a in result["actions"]:
                a = dict(a)
                a["frame_index"] = frame.index
                a["id"] = f"e{idx}"
                idx += 1
                actions.append(a)
            texts.append(result["text"])
        top = results[0] if results else {"url": self.page.url, "title": "", "scroll": {"y": 0, "height": 0}}
        fingerprint = "|".join(r["fingerprint"] for r in results if r)
        return {
            "url": top.get("url", self.page.url),
            "title": top.get("title", ""),
            "text": "\n".join(t for t in texts if t)[:6000],
            "scroll": top.get("scroll", {"y": 0, "height": 0}),
            "actions": actions,
            "fingerprint": fingerprint,
        }

    def frame_for(self, action) -> "Frame | None":
        frames = self._frames()
        return frames[action["frame_index"]] if action["frame_index"] < len(frames) else None

    async def guard_ok(self, action, recorded_guard) -> bool:
        """Cheap pre-execution freshness check: does the specific target
        element still look like it did when the decision was made? Catches
        the case where the page mutated between observe and act without
        forcing a full re-snapshot for every single click."""
        frame = self.frame_for(action)
        if frame is None:
            return False
        try:
            current = await frame.guard(action["node"])
        except Exception:
            return False
        return current == recorded_guard

    async def click(self, action):
        frame = self.frame_for(action)
        handle = await frame.handle_for(action["node"])
        # Three-tier fallback: a normal click can hang the full default
        # timeout when something else visually covers the target (seen live:
        # an autocomplete dropdown over Google's search button). Fail fast,
        # then force through, then dispatch a JS click as a last resort.
        try:
            await handle.click(timeout=5000)
            return
        except Exception:
            pass
        try:
            await handle.click(timeout=5000, force=True)
            return
        except Exception:
            pass
        await handle.evaluate("el => el.click()")

    async def fill(self, action, value: str):
        frame = self.frame_for(action)
        handle = await frame.handle_for(action["node"])
        if action["role"] == "textbox" and await handle.evaluate("el => el.isContentEditable"):
            await handle.click()
            await handle.evaluate("el => { el.textContent = ''; }")
            await handle.type(value)
        else:
            await handle.fill(value)

    async def select(self, action):
        frame = self.frame_for(action)
        handle = await frame.handle_for(action["node"])
        await handle.select_option(value=action["value"])

    async def scroll(self, delta: int):
        await self.page.mouse.wheel(0, delta)

    async def wait(self):
        await asyncio.sleep(0.3)
