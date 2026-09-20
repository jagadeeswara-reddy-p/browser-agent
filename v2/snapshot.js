// Atomic per-frame DOM snapshot: stable node identity, glyph-safe accessible
// names, and disabled controls kept (annotated) instead of dropped.
//
// Runs once per frame per tick (browser.py evaluates it in every frame,
// including nested iframes - many real sites, e.g. yopmail's compose form,
// load their actual interactive content inside one). Injected by jev-ultrafast
// for node identity; PUA-glyph stripping, the label-priority order, and
// keeping disabled controls are learned from browser-agent v1's real-site bugs.
(() => {
  if (!document.body) return null;
  const cache = window.__jevV2 ||= { ids: new WeakMap(), nodes: new Map(), next: 1 };
  const identity = (e) => {
    if (!cache.ids.has(e)) cache.ids.set(e, cache.next++);
    const id = cache.ids.get(e);
    cache.nodes.set(id, e);
    return id;
  };
  for (const [id, e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);

  // `file`/`hidden` inputs are truly out of scope. `password` fields must
  // stay selectable (typing a login password is a normal, common goal, and
  // that password is already in the goal string sent to the model anyway -
  // excluding the FIELD adds no real privacy and just breaks every login
  // flow, seen live on SauceDemo). Its current typed value is still never
  // reported (see `value` below) so an already-entered password is never
  // echoed back to Jev on a later observation.
  const safe = (e) => !["file", "hidden"].includes(e.type);
  const visible = (e) =>
    !e.closest('[aria-hidden="true"],[inert]') &&
    e.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });

  // Icon fonts (Material Icons etc.) inject their glyph as a Private-Use-Area
  // character right alongside real text - e.g. yopmail's Send button's real
  // innerText is "\n\xa0Send". Left in, it can dominate or replace the
  // human-readable label entirely (seen live: jev-ultrafast's own snapshot.js
  // has no PUA filter and its icon-only buttons come through as bare "").
  const isPUA = (c) => {
    const cp = c.codePointAt(0);
    return (cp >= 0xe000 && cp <= 0xf8ff) || (cp >= 0xf0000 && cp <= 0xffffd) || (cp >= 0x100000 && cp <= 0x10fffd);
  };
  const cleanText = (text) => [...(text || "")].filter((c) => !isPUA(c)).join("").replace(/\s+/g, " ").trim();

  const labelFor = (e) => {
    const id = e.id;
    if (id) {
      const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (label) {
        const t = cleanText(label.innerText);
        if (t) return t;
      }
    }
    const wrapping = e.closest("label");
    if (wrapping) {
      const t = cleanText(wrapping.innerText);
      if (t) return t;
    }
    return "";
  };

  // Priority: aria-label/aria-labelledby -> <label> -> placeholder (purpose-
  // built to describe expected content) -> title (curated tooltip, checked
  // BEFORE innerText so an icon-only button's glyph text can't win) ->
  // cleaned innerText -> alt/value/name -> id as a last resort.
  const name = (e, seen = new Set()) => {
    if (!e || seen.has(e)) return "";
    seen.add(e);
    const referenced = (e.getAttribute("aria-labelledby") || "")
      .split(/\s+/)
      .map((id) => name(document.getElementById(id), seen))
      .filter(Boolean)
      .join(" ");
    if (referenced) return referenced;
    const ariaLabel = e.getAttribute("aria-label");
    if (ariaLabel && ariaLabel.trim()) return ariaLabel.trim();
    const labelled = labelFor(e);
    if (labelled) return labelled;
    const placeholder = e.getAttribute("placeholder");
    if (placeholder && placeholder.trim()) return placeholder.trim();
    const title = e.getAttribute("title");
    if (title && title.trim()) return title.trim();
    if (["button", "submit", "reset"].includes(e.type) && e.value) return e.value;
    if (e.tagName !== "INPUT") {
      const inner = cleanText(
        [...e.childNodes]
          .map((n) => (n.nodeType === 3 ? n.textContent : n.nodeType === 1 && n.getAttribute("aria-hidden") !== "true" ? name(n, seen) : ""))
          .join(" ")
      );
      if (inner) return inner.slice(0, 80);
    }
    const alt = e.getAttribute("alt");
    if (alt && alt.trim()) return alt.trim();
    if ("value" in e && typeof e.value === "string" && e.value.trim() && e.tagName !== "SELECT") return e.value.trim();
    const nm = e.getAttribute("name");
    if (nm && nm.trim()) return nm.trim();
    if (e.id && !/^\d+$/.test(e.id)) return e.id;
    return "";
  };

  const roles = [
    "button", "link", "checkbox", "radio", "switch", "tab", "menuitem", "menuitemradio",
    "option", "gridcell", "combobox", "textbox", "searchbox", "spinbutton",
  ];
  const selector =
    'a[href],button,input,textarea,select,summary,[contenteditable="true"],[onclick],' +
    roles.map((r) => `[role="${r}"]`).join(",");

  const role = (e) => {
    const explicit = e.getAttribute("role");
    if (roles.includes(explicit)) return explicit;
    if (e.tagName === "BUTTON" || e.tagName === "SUMMARY") return "button";
    if (e.tagName === "A") return "link";
    if (e.tagName === "SELECT") return "combobox";
    if (e.tagName === "TEXTAREA" || e.isContentEditable) return "textbox";
    if (e.hasAttribute("onclick")) return "button";
    if (e.tagName === "INPUT") {
      if (["checkbox", "radio"].includes(e.type)) return e.type;
      if (["button", "submit", "reset", "image"].includes(e.type)) return "button";
      if (e.type === "search") return "searchbox";
      if (e.type === "number") return "spinbutton";
      if (["text", "email", "url", "tel", "password", ""].includes(e.type)) return "textbox";
    }
    return null;
  };

  const actions = [];
  for (const e of document.querySelectorAll(selector)) {
    if (!safe(e) || !visible(e)) continue;
    const r = e.getBoundingClientRect();
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    const rname = role(e);
    if (!rname || r.width <= 0 || r.height <= 0 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    if (rname === "gridcell" && e.querySelector('button,[role="button"]')) continue;

    const disabled = e.matches(":disabled") || e.closest('[aria-disabled="true"],[inert]') !== null;
    const label = name(e) || rname;
    const base = { node: identity(e), role: rname, label, disabled };

    if (e.tagName === "SELECT") {
      if (disabled) {
        actions.push({ ...base, kind: "click", value: "", current_value: [...e.selectedOptions].map((o) => o.label).join(", ") });
        continue;
      }
      for (const o of e.options) {
        if (o.disabled || o.closest("optgroup[disabled]")) continue;
        actions.push({
          ...base,
          kind: "select",
          value: o.value,
          current_value: [...e.selectedOptions].map((o) => o.label).join(", "),
          label: base.label + " → " + o.label,
        });
      }
    } else {
      const editable =
        !disabled &&
        !e.readOnly &&
        e.getAttribute("aria-readonly") !== "true" &&
        (["textbox", "searchbox", "spinbutton"].includes(rname) ||
          (rname === "combobox" && ["INPUT", "TEXTAREA"].includes(e.tagName)) ||
          e.isContentEditable);
      const value =
        e.type === "password"
          ? e.value
            ? "(filled)"
            : ""
          : "value" in e
          ? String(e.value)
          : e.isContentEditable
          ? e.innerText.trim()
          : "";
      actions.push({ ...base, kind: editable ? "fill" : "click", value });
    }
  }

  const words = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let node, length = 0;
  while ((node = walker.nextNode()) && length < 6000) {
    const value = node.textContent.trim();
    const parent = node.parentElement;
    if (!value || !parent || parent.closest("script,style,noscript,template") || !visible(parent)) continue;
    range.selectNodeContents(node);
    const r = range.getBoundingClientRect();
    if (r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth) {
      words.push(value);
      length += value.length;
    }
  }
  const text = words.join("\n").slice(0, 6000);
  const height = document.documentElement.scrollHeight;

  const omitted = Math.max(0, actions.length - 250);
  actions.splice(250);
  actions.forEach((a, i) => (a.id = "e" + (i + 1)));

  const fingerprint = JSON.stringify([
    location.href, scrollX, scrollY, innerWidth, innerHeight, text,
    actions.map(({ node, kind, role, label, value, disabled }) => [node, kind, role, label, value, disabled]),
  ]);

  return { url: location.href, title: document.title, text, scroll: { y: scrollY, height }, actions, fingerprint, omitted };
})();
