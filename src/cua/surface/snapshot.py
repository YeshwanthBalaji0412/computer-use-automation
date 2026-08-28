"""Turning a hostile DOM into semantic ElementNodes.

Playwright's ``aria_snapshot()`` gives a beautifully compact role/name tree - it is what
we hand the model - but it is *text*, with no way back to the elements it describes. The
locator ladder needs four things that text cannot carry: frame path, table row/column
context, a section anchor, and a bounding box.

So we do both. ``aria_snapshot()`` for the model's eyes; this DOM walk for the ladder's
raw material. The DOM is used here and **nowhere else** - an import-linter contract in
setup.cfg keeps Playwright, and therefore anything DOM-shaped, confined to this package.

On the ``data-cua-ref`` stamp
----------------------------
The walk writes a temporary ``data-cua-ref`` attribute so a ref can be resolved back to
an element cheaply. At first glance that looks like cheating - the whole premise is that
this app has no test ids. It is not, for three reasons: we add it at runtime rather than
finding it, it is wiped and reassigned on every observation, and it is never written into
a capability artifact. Replay resolves elements through the locator ladder, not through
these stamps. They exist purely so the model can say "click e14" in one turn and have it
mean something in the next.
"""

from __future__ import annotations

# Language: JavaScript, evaluated inside each frame.
#
# Accessible-name computation here is a pragmatic subset of the accname spec, in the
# order that actually matters for enterprise forms: aria-label, aria-labelledby, an
# associated <label>, a button's value attribute, alt, title, text content, placeholder.
# Full accname is not required - Playwright's get_by_role does the authoritative match at
# resolution time. This pass only needs to be good enough to rank candidate locators.
COLLECT_JS = r"""
() => {
  const MAX_NAME = 160;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, MAX_NAME);
  const textOf = (el) => norm(el ? el.textContent : "");

  const INPUT_ROLES = {
    button: "button", submit: "button", reset: "button", image: "button",
    checkbox: "checkbox", radio: "radio", range: "slider",
    text: "textbox", search: "searchbox", email: "textbox", tel: "textbox",
    url: "textbox", password: "textbox", number: "spinbutton",
  };
  const TAG_ROLES = {
    A: "link", BUTTON: "button", SELECT: "combobox", TEXTAREA: "textbox",
    TABLE: "table", TR: "row", TD: "cell", TH: "columnheader",
    IMG: "img", FORM: "form", DIALOG: "dialog", LABEL: "label",
    H1: "heading", H2: "heading", H3: "heading",
    H4: "heading", H5: "heading", H6: "heading",
    DT: "term", DD: "definition", LI: "listitem",
    // Plain body text. Legacy applications announce almost everything this way - "No
    // member records found", "You are not authorized" - in a <p> or a <span> with a red
    // class and no ARIA role at all. Without these the system is blind to exactly the
    // messages the error taxonomy has to detect.
    P: "paragraph", SPAN: "text", DIV: "text", STRONG: "text", B: "text", EM: "text",
  };

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.trim().toLowerCase();
    const tag = el.tagName;
    if (tag === "INPUT") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "hidden") return null;
      return INPUT_ROLES[t] || "textbox";
    }
    if (tag === "A") return el.hasAttribute("href") ? "link" : null;
    if (tag === "TH") {
      const scope = (el.getAttribute("scope") || "col").toLowerCase();
      return scope === "row" ? "rowheader" : "columnheader";
    }
    return TAG_ROLES[tag] || null;
  }

  function labelFor(el) {
    if (el.id) {
      const lab = el.ownerDocument.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) return textOf(lab);
    }
    const wrapping = el.closest("label");
    if (wrapping) return textOf(wrapping);
    return "";
  }

  function accName(el, role) {
    const aria = el.getAttribute("aria-label");
    if (aria) return norm(aria);

    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map((id) => el.ownerDocument.getElementById(id))
        .filter(Boolean).map(textOf);
      if (parts.length) return norm(parts.join(" "));
    }

    const tag = el.tagName;
    if (tag === "INPUT") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      // Submit/button inputs are named by their value - "Search", "Find Member".
      if (t === "submit" || t === "button" || t === "reset") {
        return norm(el.getAttribute("value") || el.value || "");
      }
      const lab = labelFor(el);
      if (lab) return lab;
      return norm(el.getAttribute("placeholder") || el.getAttribute("title") || "");
    }
    if (tag === "SELECT" || tag === "TEXTAREA") {
      const lab = labelFor(el);
      if (lab) return lab;
      return norm(el.getAttribute("title") || "");
    }
    if (tag === "IMG") return norm(el.getAttribute("alt") || el.getAttribute("title") || "");

    const own = textOf(el);
    if (own) return own;
    return norm(el.getAttribute("title") || "");
  }

  function statesOf(el) {
    const s = [];
    // Marked here, at perception, so every layer above can treat the value as a secret
    // without having to re-derive that fact from markup it is not allowed to see.
    if (el.tagName === "INPUT" && (el.getAttribute("type") || "").toLowerCase() === "password") {
      s.push("secret");
    }
    if (el.disabled) s.push("disabled");
    if (el.required || el.getAttribute("aria-required") === "true") s.push("required");
    if (el.checked) s.push("checked");
    if (el.readOnly) s.push("readonly");
    const exp = el.getAttribute("aria-expanded");
    if (exp) s.push(exp === "true" ? "expanded" : "collapsed");
    if (el === el.ownerDocument.activeElement) s.push("focused");
    return s;
  }

  function valueOf(el) {
    const tag = el.tagName;
    if (tag === "SELECT") {
      const opt = el.options[el.selectedIndex];
      return opt ? norm(opt.textContent || opt.value) : "";
    }
    if (tag === "TEXTAREA") return norm(el.value);
    if (tag === "INPUT") {
      const t = (el.getAttribute("type") || "text").toLowerCase();
      if (t === "password") return "";           // never read secret values back
      if (t === "submit" || t === "button" || t === "reset") return "";
      return norm(el.value);
    }
    return "";
  }

  // Row/column context, but ONLY for real data grids. A table with no header cells is a
  // layout table - legacy apps are full of them - and inventing column names from one
  // would produce locators that look precise and are not.
  function rowContext(el) {
    const tr = el.closest("tr");
    if (!tr) return null;
    const table = tr.closest("table");
    if (!table) return null;

    // `r.cells` is the row's OWN cells. Using r.querySelector("th") here would search
    // descendants, so a layout row that merely *contains* a data grid would be mistaken
    // for a header row - and every piece of page chrome would come back with a row
    // context built from the entire page's text. Legacy apps nest tables constantly,
    // so this distinction is not hypothetical.
    const rows = Array.from(table.rows || []);
    const headerRow = rows.find((r) =>
      Array.from(r.cells).some((c) => c.tagName === "TH")
    );
    if (!headerRow || headerRow === tr) return null;

    const headers = Array.from(headerRow.cells).map(textOf);
    const cells = Array.from(tr.cells);
    const rowKey = {};
    cells.forEach((c, i) => {
      const h = headers[i];
      if (h) rowKey[h] = textOf(c);
    });
    if (!Object.keys(rowKey).length) return null;

    const ownCell = el.closest("td, th");
    const idx = ownCell ? cells.indexOf(ownCell) : -1;
    return { row_key: rowKey, column_header: idx >= 0 ? (headers[idx] || "") : "" };
  }

  // The text immediately before this element. On header-less layout tables - which
  // legacy apps use for every detail screen - this is the only thing that identifies a
  // value: <td class="lbl">Member ID</td><td>100042</td>. Capped short so a wrapper
  // element's entire subtree does not become an "anchor".
  function anchorText(el) {
    const cell = el.closest("td, th, dd");
    const prev = (cell || el).previousElementSibling;
    if (!prev) return null;
    const txt = textOf(prev);
    if (!txt || txt.length > 60) return null;
    return txt;
  }

  function visible(el) {
    const rects = el.getClientRects();
    if (!rects.length) return false;
    const st = el.ownerDocument.defaultView.getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none") return false;
    if (parseFloat(st.opacity || "1") === 0) return false;
    return true;
  }

  // Cells and headings are emitted for their text (the savings balance IS a <td>), but
  // only when they are leaves - otherwise every layout wrapper shows up as content.
  const ALWAYS = new Set(["button", "link", "textbox", "searchbox", "checkbox", "radio",
                          "combobox", "spinbutton", "slider", "dialog", "heading"]);
  const TEXT_ROLES = new Set([
    "cell", "columnheader", "rowheader", "term", "definition",
    "paragraph", "text", "listitem",
  ]);

  const root = document.body || document.documentElement;
  if (!root) return [];

  // Refs are per-observation: wipe any stamps left over from the previous pass.
  root.querySelectorAll("[data-cua-ref]").forEach((e) => e.removeAttribute("data-cua-ref"));

  const out = [];
  let heading = null;
  let seq = 0;

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  let el = walker.currentNode.nodeType === 1 ? walker.currentNode : walker.nextNode();
  for (; el; el = walker.nextNode()) {
    const role = roleOf(el);
    if (!role) continue;

    if (role === "heading" && visible(el)) heading = textOf(el);

    const interesting =
      ALWAYS.has(role) ||
      (TEXT_ROLES.has(role) && el.children.length === 0 && textOf(el));
    if (!interesting) continue;
    if (!visible(el)) continue;

    const name = accName(el, role);
    if (!name && role !== "textbox" && role !== "combobox") continue;

    const r = el.getBoundingClientRect();
    const ref = "e" + seq++;
    el.setAttribute("data-cua-ref", ref);

    out.push({
      ref,
      role,
      name,
      value: valueOf(el) || null,
      states: statesOf(el),
      section: role === "heading" ? null : heading,
      row_context: rowContext(el),
      anchor_text: anchorText(el),
      bbox: { x: r.x, y: r.y, w: r.width, h: r.height },
      tag: el.tagName.toLowerCase(),
    });
  }
  return out;
}
"""

#: Interactive roles rank above content when the element budget is tight: an agent that
#: cannot see a button cannot act, whereas missing one row of a long table is survivable.
PRIORITY: dict[str, int] = {
    "button": 0,
    "link": 0,
    "textbox": 0,
    "searchbox": 0,
    "combobox": 0,
    "checkbox": 0,
    "radio": 0,
    "spinbutton": 0,
    "dialog": 0,
    "heading": 1,
    "columnheader": 2,
    "rowheader": 2,
    "cell": 3,
    "term": 3,
    "definition": 3,
}

DEFAULT_PRIORITY = 4


def priority(role: str) -> int:
    return PRIORITY.get(role, DEFAULT_PRIORITY)
