"""
Resolves natural-language element descriptions ("the Search button", "email
field", "Add to cart") to Playwright locators, without ever falling back to
a guessed CSS selector. Shared by interaction.py and form_handler.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class ElementNotFoundError(Exception):
    def __init__(self, description: str, attempts: list[str]):
        self.description = description
        self.attempts = attempts
        super().__init__(
            f"Could not find an element matching {description!r}. "
            f"Tried: {', '.join(attempts) if attempts else '(no strategies attempted)'}"
        )


# Loose keyword -> ARIA role hints, used only to narrow the first attempt;
# every other strategy still runs if this doesn't find anything.
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
]


@dataclass
class ResolvedElement:
    locator: object  # playwright Locator, already narrowed to a single element
    strategy: str
    description: str
    match_count: int


def _infer_role(description: str) -> Optional[str]:
    lowered = description.lower()
    for keyword, role in ROLE_HINTS:
        if keyword in lowered:
            return role
    return None


def resolve(page, description: str, *, role: Optional[str] = None, nth: int = 0) -> ResolvedElement:
    description = (description or "").strip()
    if not description:
        raise ElementNotFoundError(description, [])

    inferred_role = role or _infer_role(description)
    attempts: list[str] = []

    strategies = []
    if inferred_role:
        strategies.append((f"role={inferred_role}", lambda: page.get_by_role(inferred_role, name=description)))
    strategies.extend([
        ("label", lambda: page.get_by_label(description)),
        ("input placeholder/text", lambda: page.locator(f'input[placeholder="{_escape(description)}"], input[aria-label="{_escape(description)}"]')),
        ("placeholder", lambda: page.get_by_placeholder(description)),
        ("role=button", lambda: page.get_by_role("button", name=description)),
        ("role=link", lambda: page.get_by_role("link", name=description)),
        ("role=textbox", lambda: page.get_by_role("textbox", name=description)),
        ("title/aria-label", lambda: page.locator(f'[title="{_escape(description)}"], [aria-label="{_escape(description)}"]')),
        # Covers what read_dom_summary shows as a fallback "label" when a
        # field has no real <label>/aria-label -- its name or id attribute.
        ("name/id attribute", lambda: page.locator(f'[name="{_escape(description)}"], [id="{_escape(description)}"]')),
        ("text", lambda: page.get_by_text(description)),
    ])

    for name, build in strategies:
        try:
            candidate = build()
            count = candidate.count()
        except Exception:
            attempts.append(f"{name}(error)")
            continue

        attempts.append(f"{name}({count})")
        if count == 0:
            continue

        if count == 1:
            return ResolvedElement(locator=candidate.first, strategy=name, description=description, match_count=1)

        try:
            visible = candidate.locator("visible=true")
            visible_count = visible.count()
        except Exception:
            visible_count = count

        if visible_count >= 1:
            return ResolvedElement(
                locator=(visible if visible_count else candidate).nth(nth),
                strategy=f"{name}+visible[{nth}]",
                description=description,
                match_count=visible_count or count,
            )

        return ResolvedElement(locator=candidate.nth(nth), strategy=f"{name}[{nth}]", description=description, match_count=count)

    raise ElementNotFoundError(description, attempts)


def _escape(value: str) -> str:
    return value.replace('"', '\\"')
