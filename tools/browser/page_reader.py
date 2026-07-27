import logging

logger = logging.getLogger(__name__)

_DOM_SUMMARY_JS = """
() => {
    const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
    const isVisible = (el) => {
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    };

    const results = { headings: [], buttons: [], links: [], inputs: [], selects: [] };

    document.querySelectorAll("h1, h2, h3").forEach((el) => {
        if (!isVisible(el)) return;
        const text = clean(el.innerText);
        if (text) results.headings.push(text);
    });

    document.querySelectorAll("button, [role=button], input[type=submit], input[type=button]").forEach((el) => {
        if (!isVisible(el)) return;
        const text = clean(el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("title"));
        if (text) results.buttons.push(text);
    });

    document.querySelectorAll("a[href]").forEach((el) => {
        if (!isVisible(el)) return;
        const text = clean(el.innerText || el.getAttribute("aria-label"));
        if (text) results.links.push({ text: text, href: el.href });
    });

    document.querySelectorAll("input, textarea").forEach((el) => {
        if (!isVisible(el)) return;
        if (["submit", "button", "hidden"].includes(el.type)) return;
        const label =
            clean(el.getAttribute("aria-label")) ||
            clean(el.placeholder) ||
            clean(el.name) ||
            clean(el.id);
        results.inputs.push({ label: label, type: el.type || "text", name: el.name || "" });
    });

    document.querySelectorAll("select").forEach((el) => {
        if (!isVisible(el)) return;
        const label = clean(el.getAttribute("aria-label")) || clean(el.name) || clean(el.id);
        const options = Array.from(el.options).map((o) => clean(o.text)).filter(Boolean);
        results.selects.push({ label: label, options: options.slice(0, 20) });
    });

    return results;
}
"""

_TABLES_JS = """
() => {
    const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
    const tables = [];
    document.querySelectorAll("table").forEach((table) => {
        const rows = [];
        table.querySelectorAll("tr").forEach((tr) => {
            const cells = Array.from(tr.querySelectorAll("th,td")).map((c) => clean(c.innerText));
            if (cells.length) rows.push(cells);
        });
        if (rows.length) tables.push(rows);
    });
    return tables;
}
"""

_FORMS_JS = """
() => {
    const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
    const forms = [];
    document.querySelectorAll("form").forEach((form, i) => {
        const fields = [];
        form.querySelectorAll("input, textarea, select").forEach((el) => {
            if (["submit", "button", "hidden"].includes(el.type)) return;
            const label =
                clean(el.getAttribute("aria-label")) ||
                clean(el.placeholder) ||
                clean(el.name) ||
                clean(el.id);
            fields.push({ label: label, type: el.type || el.tagName.toLowerCase(), name: el.name || "" });
        });
        forms.push({ index: i, action: form.action || "", fields: fields });
    });
    return forms;
}
"""


def read_text(page, max_chars: int = 4000) -> dict:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass

    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        text = ""

    text = " ".join(str(text or "").split())
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    return {"title": page.title(), "url": page.url, "text": text, "truncated": truncated}


def read_dom_summary(page, max_items: int = 40) -> dict:
    """Structured list of what's actually interactable on the page --
    headings, buttons, links, inputs, dropdowns with their visible
    text/labels. This is what the agent should read BEFORE clicking, so it
    clicks by real visible text instead of guessing a description."""
    try:
        raw = page.evaluate(_DOM_SUMMARY_JS)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    def dedupe_cap(items, cap):
        seen = []
        for item in items:
            if item not in seen:
                seen.append(item)
            if len(seen) >= cap:
                break
        return seen

    return {
        "success": True,
        "title": page.title(),
        "url": page.url,
        "headings": dedupe_cap(raw.get("headings", []), max_items),
        "buttons": dedupe_cap(raw.get("buttons", []), max_items),
        "links": raw.get("links", [])[:max_items],
        "inputs": raw.get("inputs", [])[:max_items],
        "selects": raw.get("selects", [])[:max_items],
    }


def extract_tables(page) -> dict:
    try:
        tables = page.evaluate(_TABLES_JS)
        return {"success": True, "count": len(tables), "tables": tables}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def extract_links(page, limit: int = 50) -> dict:
    try:
        summary = page.evaluate(_DOM_SUMMARY_JS)
        links = summary.get("links", [])[:limit]
        return {"success": True, "count": len(links), "links": links}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def extract_forms(page) -> dict:
    try:
        forms = page.evaluate(_FORMS_JS)
        return {"success": True, "count": len(forms), "forms": forms}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
