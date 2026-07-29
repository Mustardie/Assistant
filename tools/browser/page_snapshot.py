"""
Single-shot page snapshot: everything the agent needs to understand a page
in one structured call. Replaces the need for 5 separate DOM reads.

Returns a dict with:
  - metadata: url, title, page_type
  - interactables: buttons, links, inputs, selects, checkboxes, radios
  - structure: headings, lists, tables, forms, regions
  - content: visible_text (truncated), text_length
  - viewport: scroll_position, viewport_size, total_height
  - page_type: detected classification of the page
"""
import logging

logger = logging.getLogger(__name__)

# ── JS that runs once inside the page to extract everything ──────────

_SNAPSHOT_JS = """
() => {
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim().substring(0, 300);
  const cleanLines = (s) => (s || "").replace(/\\s+/g, " ").trim().substring(0, 5000);

  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0
      && style.visibility !== "hidden"
      && style.display !== "none"
      && style.opacity !== "0";
  };

  // ── Page type detection ──
  const url = location.href;
  const title = document.title;
  const metaDesc = (document.querySelector('meta[name=description]') || {}).content || '';
  const bodyText = (document.body || {}).innerText || '';
  const hasForm = document.querySelector('form');
  const hasLoginField = document.querySelector('input[type=password]');
  const hasSearchField = document.querySelector('input[type=search], input[name*=search], input[placeholder*=search i]');
  const hasResults = !!document.querySelector('[class*="result"], [id*="result"], [role="listbox"], [role="feed"], .search-results, #search, #results');
  const isError = !!(document.querySelector('[class*="error"], [id*="error"], [class*="not-found"], [class*="404"]') || bodyText.match(/404|not found|page not found|error|something went wrong/i));

  let pageType = 'unknown';
  if (isError) pageType = 'error';
  else if (hasLoginField) pageType = 'login';
  else if (hasSearchField && document.querySelector('form')) pageType = 'search';
  else if (hasResults) pageType = 'search_results';
  else if (hasForm && document.querySelectorAll('input').length >= 3) pageType = 'form';
  else if (document.querySelector('article')) pageType = 'article';
  else if (document.querySelectorAll('table').length > 0) pageType = 'data_table';
  else if (document.querySelector('nav')) pageType = 'navigation';
  else if (bodyText.length > 2000) pageType = 'content';

  // ── SINGLE PASS: walk all elements once instead of 12 querySelectorAll loops ──
  const allElements = document.querySelectorAll('*');
  const headings = [];
  const buttons = []; const seenButtons = new Set();
  const links = []; const seenLinks = new Set();
  const inputs = [];
  const selects = [];
  const checkboxes = [];
  const radios = [];
  const lists = [];
  const tables = [];
  const forms = [];
  const regions = [];

  for (const el of allElements) {
    if (!isVisible(el)) continue;
    const tag = el.tagName.toLowerCase();

    // Headings
    if (tag === 'h1' || tag === 'h2' || tag === 'h3' || tag === 'h4') {
      const text = clean(el.innerText);
      if (text) headings.push({ level: tag, text });
      continue;
    }

    // Regions (sectioning elements)
    if (tag === 'header' || tag === 'nav' || tag === 'main' || tag === 'aside' || tag === 'footer' || tag === 'article') {
      const text = clean(el.innerText).substring(0, 100);
      if (text && regions.length < 12) regions.push({ tag, text: text.substring(0, 80) });
    }

    // Buttons
    if (tag === 'button' || el.getAttribute('role') === 'button' || tag === 'a' && (el.getAttribute('role') === 'button' || (el.className || '').includes('btn'))) {
      const text = clean(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title'));
      if (text && !seenButtons.has(text) && buttons.length < 30) { seenButtons.add(text); buttons.push(text); }
      continue;
    }

    // Links (only direct <a> not already caught as button)
    if (tag === 'a' && el.href) {
      const text = clean(el.innerText || el.getAttribute('aria-label'));
      if (text && !seenLinks.has(text) && links.length < 50) { seenLinks.add(text); links.push({ text, href: el.href }); }
      continue;
    }

    // Select dropdowns
    if (tag === 'select') {
      const label = clean(el.getAttribute('aria-label')) || clean(el.name) || clean(el.id);
      const options = Array.from(el.options).map((o) => clean(o.text)).filter(Boolean);
      if (selects.length < 15) selects.push({ label: label || el.name, options: options.slice(0, 30), selected: clean(el.options[el.selectedIndex]?.text) });
      continue;
    }

    // Lists
    if (tag === 'ul' || tag === 'ol') {
      if (lists.length < 10) {
        const items = Array.from(el.querySelectorAll(':scope > li')).map((li) => clean(li.innerText)).filter(Boolean);
        if (items.length) lists.push({ type: tag, items: items.slice(0, 15), count: items.length });
      }
      continue;
    }

    // Tables
    if (tag === 'table') {
      if (tables.length < 5) {
        const rows = [];
        el.querySelectorAll('tr').forEach((tr) => {
          const cells = Array.from(tr.querySelectorAll('th,td')).map((c) => clean(c.innerText));
          if (cells.length) rows.push(cells);
        });
        if (rows.length) tables.push({ rows: rows.slice(0, 20), row_count: rows.length });
      }
      continue;
    }

    // Forms
    if (tag === 'form') {
      if (forms.length < 5) {
        const fields = [];
        el.querySelectorAll(':scope > input:not([type=hidden]), :scope > textarea, :scope > select').forEach((fieldEl) => {
          if (['submit', 'button'].includes(fieldEl.type)) return;
          const lbl = clean(document.querySelector(`label[for="${fieldEl.id}"]`)) || clean(fieldEl.getAttribute('aria-label')) || clean(fieldEl.placeholder) || clean(fieldEl.name) || clean(fieldEl.id);
          fields.push({ label: lbl || fieldEl.name, type: fieldEl.type || fieldEl.tagName.toLowerCase(), required: fieldEl.required || false });
        });
        forms.push({ action: el.action || '', method: el.method || 'get', fields: fields.slice(0, 15) });
      }
      continue;
    }

    // Inputs, checkboxes, radios
    if (tag === 'input' || tag === 'textarea') {
      const type = (el.type || 'text').toLowerCase();
      if (type === 'hidden' || type === 'submit' || type === 'button') continue;

      if (type === 'checkbox') {
        const label = clean(document.querySelector(`label[for="${el.id}"]`)) || clean(el.getAttribute('aria-label')) || clean(el.name) || '';
        if (checkboxes.length < 15) checkboxes.push({ label: label || el.name || el.id, checked: el.checked, name: el.name || '' });
      } else if (type === 'radio') {
        const label = clean(document.querySelector(`label[for="${el.id}"]`)) || clean(el.getAttribute('aria-label')) || clean(el.value) || clean(el.name) || '';
        if (radios.length < 10) radios.push({ label: label || el.name, checked: el.checked, name: el.name || '' });
        const label = clean(document.querySelector(`label[for="${el.id}"]`)) || clean(el.getAttribute('aria-label')) || clean(el.placeholder) || clean(el.name) || clean(el.id);
        if (inputs.length < 30) inputs.push({ label: label || el.name || el.id, type: el.type || 'text', name: el.name || '', required: el.required || false });
      }
    }
  }

  // ── Visible text (single TreeWalker pass) ──
  const visibleText = (() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
    const parts = [];
    let node;
    while ((node = walker.nextNode())) {
      const text = node.textContent.trim();
      if (!text) continue;
      const parent = node.parentElement;
      if (parent && isVisible(parent)) {
        parts.push(text);
      }
      if (parts.join(' ').length > 5000) break;
    }
    return parts.join(' ').substring(0, 5000);
  })();

  // ── Radio groups (multiple-choice grouped by name) ──
  const radioGroups = {};
  radios.forEach((r) => {
    const key = r.name || '_ungrouped';
    if (!radioGroups[key]) radioGroups[key] = { name: key, options: [], selected: null };
    radioGroups[key].options.push(r.label);
    if (r.checked) radioGroups[key].selected = r.label;
  });
  const radioGroupList = Object.values(radioGroups).slice(0, 5);

  // ── Viewport / scroll ──
  const scrollY = window.scrollY || window.pageYOffset;
  const viewportHeight = window.innerHeight;
  const totalHeight = document.documentElement.scrollHeight;
  const moreContentBelow = (scrollY + viewportHeight) < totalHeight - 100;

  return {
    metadata: {
      url,
      title: clean(title),
      description: clean(metaDesc),
    },
    page_type: pageType,
    structure: {
      headings: headings.slice(0, 25),
      regions: regions.slice(0, 12),
    },
    interactables: {
      buttons: buttons.slice(0, 30),
      links: links.slice(0, 50),
      inputs: inputs.slice(0, 30),
      selects: selects.slice(0, 15),
      checkboxes: checkboxes.slice(0, 15),
      radios: radios.slice(0, 10),
      radio_groups: radioGroupList,
    },
    content: {
      visible_text: visibleText.substring(0, 4000),
      text_length: bodyText.length,
      lists: lists.slice(0, 10),
      tables: tables.slice(0, 5),
    },
    forms: forms.slice(0, 5),
    viewport: {
      scroll_y: scrollY,
      viewport_height: viewportHeight,
      total_height: totalHeight,
      more_content_below: moreContentBelow,
    },
  };
}
"""


def snapshot(page) -> dict:
    """Return a comprehensive structured snapshot of the current page.
    This is the single entry point for page understanding."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    try:
        data = page.evaluate(_SNAPSHOT_JS)
    except Exception as exc:
        return {"success": False, "error": f"Page snapshot failed: {exc}"}

    if not isinstance(data, dict):
        return {"success": False, "error": "Snapshot returned non-dict data"}

    return {"success": True, **data}


def snapshot_light(page) -> dict:
    """Quick page check: URL, title, page type, buttons, links, inputs.
    Lighter than full snapshot for fast verification loops."""
    try:
        data = page.evaluate(_SNAPSHOT_JS)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    return {
        "success": True,
        "url": (data.get("metadata") or {}).get("url", ""),
        "title": (data.get("metadata") or {}).get("title", ""),
        "page_type": data.get("page_type", "unknown"),
        "buttons": (data.get("interactables") or {}).get("buttons", [])[:10],
        "links": (data.get("interactables") or {}).get("links", [])[:15],
        "inputs": (data.get("interactables") or {}).get("inputs", [])[:10],
        "has_forms": bool(data.get("forms")),
        "more_content_below": (data.get("viewport") or {}).get("more_content_below", False),
    }
