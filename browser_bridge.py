"""Async Playwright wrapper: page lifecycle, text-only candidate extraction for
Jev grounding, and action execution. No screenshots are sent anywhere - Jev is
text-only, so everything it sees comes from this candidate list."""
from playwright.async_api import async_playwright

CLICK_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "option", "switch"}
TYPE_TAGS = {"input", "textarea", "select"}

CANDIDATE_SELECTOR = (
    "a, button, input, select, textarea, "
    "[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], "
    "[role=menuitem], [role=switch], [onclick]"
)


async def _best_label(page, handle) -> str:
    val = await handle.get_attribute("aria-label")
    if val and val.strip():
        return val.strip()
    # associated <label for=id> - the real human-visible label, checked before
    # any attribute fallback (name/placeholder are developer identifiers, not
    # what a user reads on screen)
    el_id = await handle.get_attribute("id")
    if el_id:
        label = await page.query_selector(f"label[for='{el_id}']")
        if label:
            text = (await label.inner_text()).strip()
            if text:
                return text
    # a <label> wrapping this element (no `for`, implicit association)
    wrapping_label = await handle.evaluate_handle("el => el.closest('label')")
    if wrapping_label:
        el = wrapping_label.as_element()
        if el:
            text = (await el.inner_text()).strip()
            if text:
                return text
    text = (await handle.inner_text()).strip()
    if text:
        return text[:80]
    for attr in ("placeholder", "title", "alt", "value", "name"):
        val = await handle.get_attribute(attr)
        if val and val.strip():
            return val.strip()
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
        live ElementHandles for this step in self._candidate_map."""
        self._candidate_map = {}
        handles = await self.page.query_selector_all(CANDIDATE_SELECTOR)
        click, type_text = {}, {}
        idx = 0
        for h in handles:
            if not await h.is_visible():
                continue
            enabled = await h.is_enabled()
            tag = await h.evaluate("el => el.tagName.toLowerCase()")
            input_type = (await h.get_attribute("type") or "").lower()
            label = await _best_label(self.page, h)
            if not label:
                continue
            cand_id = str(idx)
            idx += 1
            self._candidate_map[cand_id] = h
            role_desc = f"{tag}" + (f"[type={input_type}]" if input_type else "")
            desc = f"{label} [{role_desc}]" + ("" if enabled else " (disabled)")
            if tag in ("textarea", "select") or (tag == "input" and input_type not in ("submit", "button", "checkbox", "radio")):
                type_text[cand_id] = desc
                if enabled:
                    click[cand_id] = desc  # focusing a field is also a valid click target
            elif enabled:
                click[cand_id] = desc
        result = {}
        if click:
            result["CLICK"] = click
        if type_text:
            result["TYPE_TEXT"] = type_text
        return result

    async def click(self, cand_id: str):
        handle = self._candidate_map[cand_id]
        await handle.click()

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
