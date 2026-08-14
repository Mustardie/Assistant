"""
Lightweight markdown → block renderer for assistant messages.

Parses the markdown Nova's backend produces into a list of block dicts:

    {"type": "paragraph", "html": "<b>…</b>", "text": "plain…"}
    {"type": "heading", "level": 1..3, "html": "…"}
    {"type": "list", "ordered": bool, "items": [html, …]}
    {"type": "code", "lang": "python", "code": "…"}
    {"type": "table", "header": [...], "rows": [[...], …]}
    {"type": "quote", "html": "…"}

Inline formatting covers **bold**, *italic*, `code`, ~~strike~~ and links.
Code fences and tables are handled by dedicated widgets (CodeBlock) or
rendered as rich HTML (tables) in the message row.
"""
from __future__ import annotations

import html as _html
import re

from PySide6.QtGui import QColor

from .. import theme

_INLINE_RE = re.compile(
    r"(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*]+\*|_[^_]+_|~~[^~]+~~|\[[^\]]+\]\([^)]+\))"
)
_FENCE_RE = re.compile(r"^```([\w+-]*)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


def _inline(text: str) -> str:
    """Escape + apply inline formatting. Returns HTML string."""

    def repl(match):
        tok = match.group(1)
        if tok.startswith("`"):
            inner = _html.escape(tok[1:-1])
            return f'<span style="background:{theme.rgba(theme.ACCENT, 10)};' \
                   f'border-radius:5px;padding:0 5px;' \
                   f'font-family:\'Cascadia Code\',Consolas,monospace;' \
                   f'font-size:0.92em;color:{theme.ACCENT_2};">{inner}</span>'
        if tok.startswith("**") or tok.startswith("__"):
            return f"<b>{_inline(tok[2:-2])}</b>"
        if tok.startswith("*") or tok.startswith("_"):
            return f"<i>{_inline(tok[1:-1])}</i>"
        if tok.startswith("~~"):
            return f"<s>{_inline(tok[2:-2])}</s>"
        if tok.startswith("["):
            title, _, url = tok[1:-1].partition("](")
            return f'<a href="{_html.escape(url[:-1])}" style="color:{theme.ACCENT};">' \
                   f"{_inline(title)}</a>"
        return _html.escape(tok)

    return _INLINE_RE.sub(repl, _html.escape(text))


def parse_markdown(markdown: str) -> list[dict]:
    blocks: list[dict] = []
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].rstrip()

        # blank
        if not line.strip():
            i += 1
            continue

        # fenced code
        fm = _FENCE_RE.match(line.strip())
        if fm:
            lang = fm.group(1) or ""
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            blocks.append({"type": "code", "lang": lang,
                           "code": "\n".join(code_lines).rstrip()})
            continue

        # heading
        if line.startswith("### "):
            blocks.append({"type": "heading", "level": 3, "html": _inline(line[4:])})
            i += 1
            continue
        if line.startswith("## "):
            blocks.append({"type": "heading", "level": 2, "html": _inline(line[3:])})
            i += 1
            continue
        if line.startswith("# "):
            blocks.append({"type": "heading", "level": 1, "html": _inline(line[2:])})
            i += 1
            continue

        # horizontal rule
        if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", line.strip()):
            blocks.append({"type": "hr"})
            i += 1
            continue

        # blockquote
        if line.strip().startswith(">"):
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"type": "quote", "html": _inline(" ".join(quote))})
            continue

        # table: header line + separator + rows
        if "|" in line and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            blocks.append({"type": "table", "header": header, "rows": rows})
            continue

        # list
        if re.match(r"^\s*[-*+]\s+", line):
            items = []
            while i < n:
                m = re.match(r"^\s*[-*+]\s+(.*)", lines[i])
                if not m:
                    break
                items.append(_inline(m.group(1).strip()))
                i += 1
            blocks.append({"type": "list", "ordered": False, "items": items})
            continue
        if re.match(r"^\s*\d+[.)]\s+", line):
            items = []
            while i < n:
                m = re.match(r"^\s*\d+[.)]\s+(.*)", lines[i])
                if not m:
                    break
                items.append(_inline(m.group(1).strip()))
                i += 1
            blocks.append({"type": "list", "ordered": True, "items": items})
            continue

        # paragraph (gather consecutive lines)
        para = [line]
        i += 1
        while i < n:
            nxt = lines[i].rstrip()
            if not nxt.strip() or nxt.strip().startswith(("```", "#", ">", "-", "*", "+")):
                break
            if re.match(r"^\s*\d+[.)]\s+", nxt) or _FENCE_RE.match(nxt.strip()):
                break
            if re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", nxt.strip()):
                break
            para.append(nxt)
            i += 1
        blocks.append({"type": "paragraph",
                       "html": _inline(" ".join(x.strip() for x in para))})
        continue

    return blocks


def table_html(block: dict) -> str:
    """Render a table block as an HTML string for a rich-text label."""
    from .. import theme
    header = block.get("header") or []
    rows = block.get("rows") or []
    th_color = QColor(theme.BORDER_STRONG).name()
    td_color = QColor(theme.BORDER).name()
    parts = ['<table border="0" cellspacing="0" cellpadding="8" '
             'style="border-collapse:collapse;width:100%;">']
    parts.append("<thead>")
    parts.append("<tr>")
    for h in header:
        parts.append(f'<th align="left" style="border-bottom:1px solid {th_color};'
                     f'font-weight:600;font-size:12.5px;color:{theme.TEXT};">{_inline(h)}</th>')
    parts.append("</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>")
        for cell in row:
            parts.append(f'<td style="border-bottom:1px solid {td_color};'
                         f'font-size:12.5px;color:{theme.TEXT_SOFT};padding-top:7px;">{_inline(cell)}</td>')
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)
