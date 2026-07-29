from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional


class ElementNotFoundError(Exception):
    def __init__(self, description: str, attempts: list[str]):
        self.description = description
        self.attempts = attempts
        super().__init__(
            f"Could not find an element matching {description!r}. "
            f"Tried: {', '.join(attempts) if attempts else '(no strategies attempted)'}"
        )


ROLE_HINTS = [
    ("checkbox", "checkbox"),
    ("radio", "radio"),
    ("dropdown", "combobox"),
    ("combobox", "combobox"),
    ("select", "combobox"),
    ("menu", "menu"),
    ("tab", "tab"),
    ("link", "link"),
    ("button", "button"),
    ("textbox", "textbox"),
    ("search", "searchbox"),
    ("slider", "slider"),
]


@dataclass
class ResolvedElement:
    locator: object
    strategy: str
    description: str
    match_count: int
    confidence: float = 1.0


def _infer_role(description: str) -> Optional[str]:
    lowered = description.lower()
    for keyword, role in ROLE_HINTS:
        if keyword in lowered:
            return role
    return None


def _text_similarity(a: str, b: str) -> float:
    a = a.lower().strip()
    b = b.lower().strip()
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.85
    return SequenceMatcher(None, a, b).ratio()


def _word_overlap(a: str, b: str) -> float:
    words_a = set(re.findall(r'\w+', a.lower()))
    words_b = set(re.findall(r'\w+', b.lower()))
    if not words_a or not words_b:
        return 0.0
    common = words_a & words_b
    return len(common) / len(words_a)


_ELEMENT_SCORE_JS = """
(el) => {
  const rect = el.getBoundingClientRect();
  const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || '').replace(/\\s+/g, ' ').trim().substring(0, 300);
  const visible = rect.width > 0 && rect.height > 0;
  const inViewport = visible && rect.y + rect.height / 2 >= 0 && rect.y + rect.height / 2 <= (window.innerHeight || 900);
  const vcx = (window.innerWidth || 1280) / 2;
  const vcy = (window.innerHeight || 900) / 2;
  const cx = rect.x + rect.width / 2;
  const cy = rect.y + rect.height / 2;
  const maxDist = Math.sqrt(vcx * vcx + vcy * vcy);
  const dist = Math.sqrt((cx - vcx) ** 2 + (cy - vcy) ** 2);
  const centerScore = maxDist > 0 ? Math.max(0, 1 - dist / maxDist) : 0.5;
  return { text, visible, inViewport, centerScore };
}
"""


def _score_element(page, locator) -> dict:
    try:
        return locator.evaluate(_ELEMENT_SCORE_JS)
    except Exception:
        return {"text": "", "visible": False, "inViewport": False, "centerScore": 0.0}


@dataclass
class _Candidate:
    locator: object
    strategy: str
    text: str
    confidence: float


_CONFIDENCE_EARLY_EXIT = 0.95
_MAX_CANDIDATES_PER_STRATEGY = 3


def resolve(page, description: str, *, role: Optional[str] = None, nth: int = 0) -> ResolvedElement:
    description = (description or "").strip()
    if not description:
        raise ElementNotFoundError(description, [])

    inferred_role = role or _infer_role(description)
    attempts: list[str] = []
    candidates: list[_Candidate] = []

    def _strategies():
        d = description
        s = []
        if inferred_role:
            s.append(("role", lambda: page.get_by_role(inferred_role, name=d)))
        s.extend([
            ("label", lambda: page.get_by_label(d, exact=True)),
            ("placeholder", lambda: page.get_by_placeholder(d, exact=True)),
            ("role=button", lambda: page.get_by_role("button", name=d, exact=True)),
            ("role=link", lambda: page.get_by_role("link", name=d, exact=True)),
            ("role=textbox", lambda: page.get_by_role("textbox", name=d, exact=True)),
            ("title/aria-label", lambda: page.locator(f'[title="{_escape(d)}"], [aria-label="{_escape(d)}"]')),
            ("name/id", lambda: page.locator(f'[name="{_escape(d)}"], [id="{_escape(d)}"]')),
            ("text", lambda: page.get_by_text(d, exact=True)),
            ("fuzzy-label", lambda: page.get_by_label(d, exact=False)),
            ("fuzzy-placeholder", lambda: page.get_by_placeholder(d, exact=False)),
            ("fuzzy-text", lambda: page.get_by_text(d, exact=False)),
            ("fuzzy-title", lambda: page.locator(f'[title*="{_escape(d)} i], [aria-label*="{_escape(d)} i]')),
        ])
        return s

    for name, build in _strategies():
        try:
            locator = build()
            count = locator.count()
        except Exception:
            attempts.append(f"{name}(error)")
            continue

        attempts.append(f"{name}({count})")
        if count == 0:
            continue

        best_for_strategy = None

        for i in range(min(count, _MAX_CANDIDATES_PER_STRATEGY)):
            try:
                loc = locator.nth(i)
                info = _score_element(page, loc)
                text = info["text"] or description
            except Exception:
                text = description
                info = {"visible": False, "inViewport": False, "centerScore": 0.0}

            sim_score = _text_similarity(description, text) * 0.4
            word_score = _word_overlap(description, text) * 0.2
            vis_score = (1.0 if info["inViewport"] else 0.5 if info["visible"] else 0.0) * 0.25
            center_score = info["centerScore"] * 0.15
            total = sim_score + word_score + vis_score + center_score

            c = _Candidate(locator=loc, strategy=name, text=text, confidence=total)
            candidates.append(c)
            if best_for_strategy is None or total > best_for_strategy.confidence:
                best_for_strategy = c

        if best_for_strategy and best_for_strategy.confidence >= _CONFIDENCE_EARLY_EXIT and name not in ("fuzzy-label", "fuzzy-placeholder", "fuzzy-text", "fuzzy-title"):
            break

    if not candidates:
        raise ElementNotFoundError(description, attempts)

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    best = candidates[0]
    return ResolvedElement(
        locator=best.locator,
        strategy=f"{best.strategy}(conf={best.confidence:.2f})",
        description=best.text or description,
        match_count=len(candidates),
        confidence=best.confidence,
    )


def resolve_all(page, description: str, *, role: Optional[str] = None) -> list[ResolvedElement]:
    try:
        best = resolve(page, description, role=role)
    except ElementNotFoundError:
        return []
    return [best]


def _escape(value: str) -> str:
    return value.replace('"', '\\"')
