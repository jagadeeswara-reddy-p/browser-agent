"""Async Playwright wrapper: page lifecycle, text-only candidate extraction for
Jev grounding, and action execution. No screenshots are sent anywhere - Jev is
text-only, so everything it sees comes from this candidate list."""
from playwright.async_api import async_playwright

CLICK_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "option", "switch"}
TYPE_TAGS = {"input", "textarea", "select"}

CANDIDATE_SELECTOR = (
    "a, button, input, select, textarea, "
    "[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], "
    "[role=menuitem], [role=switch], [onclick], "
    "[contenteditable=true], [role=textbox]"
)


def _is_pua_char(c: str) -> bool:
    cp = ord(c)
    return 0xE000 <= cp <= 0xF8FF or 0xF0000 <= cp <= 0xFFFFD or 0x100000 <= cp <= 0x10FFFD


def _clean_label_text(text: str) -> str:
    """Strips Private-Use-Area icon-font glyphs out of element text and
    normalizes whitespace. Icon fonts (Material Icons etc.) render their
    glyph as a PUA character injected right alongside real text - seen live
    on yopmail's Send button, whose actual innerText is '\\ue0be\\n\\xa0Send'.
    Left as-is, that garbled prefix confused grounding (0.93 confidence the
    real, visible Send button wasn't on the page). Stripping the glyph and
    collapsing whitespace turns it into a clean "Send"."""
    cleaned = "".join(c for c in text if not _is_pua_char(c))
    return " ".join(cleaned.split())


async def _best_label(page, handle) -> str:
    val = await handle.get_attribute("aria-label")
    if val and val.strip():
        return val.strip()
    # associated <label for=id> - the real human-visible label
    el_id = await handle.get_attribute("id")
    if el_id:
        label = await page.query_selector(f"label[for='{el_id}']")
        if label:
            text = _clean_label_text((await label.inner_text()).strip())
            if text:
                return text
    # a <label> wrapping this element (no `for`, implicit association)
    wrapping_label = await handle.evaluate_handle("el => el.closest('label')")
    if wrapping_label:
        el = wrapping_label.as_element()
        if el:
            text = _clean_label_text((await el.inner_text()).strip())
            if text:
                return text
    # `placeholder` is purpose-built to describe an input's expected content
    # (e.g. "Enter your inbox here") - rank it above `title`, which on form
    # fields is often just a generic word (seen live: title="Login" on the
    # exact same field, far less descriptive than its placeholder).
    val = await handle.get_attribute("placeholder")
    if val and val.strip():
        return val.strip()
    # `title` is a curated tooltip - most useful on buttons/icons that have
    # no placeholder, and MUST be checked before inner_text because
    # icon-only buttons often have real text (an icon-font glyph) that would
    # otherwise wrongly win.
    val = await handle.get_attribute("title")
    if val and val.strip():
        return val.strip()
    text = _clean_label_text((await handle.inner_text()).strip())
    if text:
        return text[:80]
    for attr in ("alt", "value", "name"):
        val = await handle.get_attribute(attr)
        if val and val.strip():
            return val.strip()
    # Last resort: a semantic `id` (e.g. id="newmail" on yopmail's compose
    # button) beats nothing at all, even with no other accessible name.
    if el_id and not el_id.isdigit():
        return el_id
    return ""


class BrowserSession:
    def __init__(self):
        self._pw = None
        self.browser = None
        self.page = None
        self._candidate_map: dict[str, object] = {}

    async def start(self, headless: bool = True, slow_mo: int = 0):
        self._pw = await async_playwright().start()
        self.browser = await self._pw.chromium.launch(headless=headless, slow_mo=slow_mo)
        self.page = await self.browser.new_page()
        if not headless:
            await self.page.set_viewport_size({"width": 1280, "height": 900})

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def navigate(self, url: str):
        await self.page.goto(url, wait_until="load")

    async def snapshot(self) -> dict:
        """Return {"click": {id: desc}, "type_text": {id: desc}} plus store the
        live ElementHandles for this step in self._candidate_map.

        Scans every frame on the page, not just the top-level document - many
        real sites (yopmail's compose form, payment widgets, etc.) load their
        actual interactive content inside an iframe, and Playwright's own
        element queries never descend into them automatically. An
        ElementHandle carries its own frame internally, so click/fill/extract
        need no changes regardless of which frame a candidate came from."""
        self._candidate_map = {}
        click, type_text = {}, {}
        idx = 0
        for frame in self.page.frames:
            if frame.url == "about:blank":
                continue
            try:
                handles = await frame.query_selector_all(CANDIDATE_SELECTOR)
            except Exception:
                continue  # a detached/navigating frame can throw transiently
            for h in handles:
                try:
                    if not await h.is_visible():
                        continue
                    enabled = await h.is_enabled()
                    tag = await h.evaluate("el => el.tagName.toLowerCase()")
                    input_type = (await h.get_attribute("type") or "").lower()
                    is_editable = (await h.get_attribute("contenteditable") or "").lower() == "true" \
                        or (await h.get_attribute("role") or "").lower() == "textbox"
                    label = await _best_label(frame, h)
                except Exception:
                    continue
                if not label:
                    continue
                cand_id = str(idx)
                idx += 1
                self._candidate_map[cand_id] = h
                role_desc = f"{tag}" + (f"[type={input_type}]" if input_type else "")
                desc = f"{label} [{role_desc}]" + ("" if enabled else " (disabled)")
                if is_editable or tag in ("textarea", "select") \
                        or (tag == "input" and input_type not in ("submit", "button", "checkbox", "radio")):
                    type_text[cand_id] = desc
                    click[cand_id] = desc  # focusing a field is also a valid click target
                else:
                    # Include disabled buttons too (annotated), instead of
                    # hiding them entirely. Seen live: yopmail's Send button
                    # stays `disabled` until more of the compose form is
                    # filled in - excluding it meant Jev could never even
                    # report "it's there but disabled," just "not found,"
                    # which gives the replanner nothing useful to act on.
                    click[cand_id] = desc
        result = {}
        if click:
            result["CLICK"] = click
        if type_text:
            result["TYPE_TEXT"] = type_text
        return result

    async def is_enabled(self, cand_id: str) -> bool:
        return await self._candidate_map[cand_id].is_enabled()

    async def click(self, cand_id: str):
        """Click with fallbacks: a normal click can hang for the full default
        timeout when something else (an autocomplete dropdown, a consent
        overlay) visually covers the target - seen live on Google's search
        button after typing a query. Fail fast, then force through."""
        handle = self._candidate_map[cand_id]
        try:
            await handle.click(timeout=5000)
            return
        except Exception:
            pass
        try:
            # Bypasses Playwright's actionability/interception checks - still
            # a real mouse-event dispatch, just without waiting for the
            # overlay to move out of the way.
            await handle.click(timeout=5000, force=True)
            return
        except Exception:
            pass
        # Last resort: a JS-dispatched click always lands on the element
        # itself regardless of what's visually on top of it.
        await handle.evaluate("el => el.click()")

    async def fill(self, cand_id: str, value: str):
        handle = self._candidate_map[cand_id]
        await handle.fill(value)

    async def extract_text(self, cand_id: str) -> str:
        handle = self._candidate_map[cand_id]
        value = await handle.input_value() if await handle.evaluate("el => 'value' in el") else None
        if value:
            return value
        return (await handle.inner_text()).strip()

    async def current_url(self) -> str:
        return self.page.url
