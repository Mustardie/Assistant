"""A person's editing habits, in their own words.

Every other knob in this system is a number somebody chose: a caption ceiling,
a keep threshold, a zoom limit. None of them can express "I hold two beats
after deaths" or "I never open on walking", and adding a field for each such
rule would be a decade of fields.

So the style guide is prose. The model reads it and is asked to cite the line
it is following; the deterministic safety layer deliberately does **not** parse
it, because a rule this system cannot check is a rule it must not claim to
enforce. What the guide changes is which decisions get proposed -- and the
report says which rules were actually cited, which is how you find out whether
yours is being used at all.

Four places one can come from, in precedence order:

1. ``--style-guide <path>`` on the command line
2. ``EDITING_STYLE_GUIDE`` in the environment
3. ``docs/editing_style.md`` beside the project, if it exists
4. the built-in default below

The default is deliberately opinionated rather than neutral. A guide saying
"make good editing choices" tells a model nothing; one with real rules in it
produces real decisions, and disagreeing with a specific rule is how somebody
discovers they want to write their own.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from editing.errors import EditingError
from editing.director.schema import StyleGuide

logger = logging.getLogger("nova.editing.director.style_guide")

#: Where a project-level guide is looked for when nothing else is given.
DEFAULT_PATHS = (
    "docs/editing_style.md",
    "docs/my_editing_style.md",
    "editing_style.md",
)

#: Guides larger than this are truncated: past a few pages a style guide is a
#: document about a channel rather than a set of editing rules, and every
#: character of it displaces a segment the director could have judged.
MAX_CHARACTERS = 8000

#: The built-in guide. Written as an editor would write it, in the second
#: person, because that is what the model is being asked to be.
BUILTIN = """\
# Default editing style -- Minecraft commentary

Pacing
- Open on something happening. Never open on walking, sorting inventory, or
  reading a crafting recipe.
- The first fifteen seconds have to earn the next thirty. If the best moment in
  the episode is at minute nine, consider showing a piece of it first.
- Cut grind to under twenty seconds. Mining, tunnelling, walking between bases
  and sorting chests are grind, however long they actually took.
- Speed up grind rather than deleting it when it shows progress the viewer
  needs; delete it when it shows nothing.
- Never speed up anybody talking. Sped-up commentary is unusable.

Story
- Protect setups. A dull stretch that makes a later moment land is not dull,
  and cutting it makes the payoff arrive from nowhere.
- Protect payoffs completely: full speed, no effects, no trims.
- If the episode states an objective, keep the line where it is stated. If it
  never states one, say so rather than inventing one.
- Close open loops or cut what opened them. A question raised and never
  answered reads as a mistake.
- One callback per running joke is funny. Three is a different video.

Comedy and reaction
- Hold two beats after a death, a fall or a big failure. The reaction is the
  joke; cutting on the impact throws it away.
- Keep the moment *before* a reaction as well as the reaction. The setup is
  what makes it read.
- Cut away from a joke that did not land rather than sitting in it.

Clarity
- Keep the Minecraft HUD readable. Do not cover the hotbar or the health bar.
- If something on screen would confuse a viewer who has not been watching for
  ten minutes, either keep the line that explains it or cut the moment.
- Prefer story over captions. A caption that repeats what was just said is
  noise.

Endings
- End on the payoff or on a clean statement of what happens next. Do not end on
  walking away, and do not end mid-sentence.
"""


def load(
    path: Optional[str] = None,
    *,
    text: Optional[str] = None,
    search_root: Optional[str] = None,
) -> StyleGuide:
    """The style guide to use, and where it came from.

    ``text`` short-circuits everything and is how a test or a caller supplies
    a guide directly. An explicit ``path`` that does not exist is an error
    rather than a silent fall back to the default -- somebody who typed a path
    wants *that* guide, and quietly editing to different rules than they asked
    for is the worst possible outcome here.
    """
    if text is not None:
        return StyleGuide(
            text=_trim(text), source="inline", name="inline")

    if path:
        return _from_path(Path(path).expanduser(), source="argument")

    from_env = os.getenv("EDITING_STYLE_GUIDE") or ""
    if from_env:
        return _from_path(Path(from_env).expanduser(), source="environment")

    found = _search(search_root)
    if found is not None:
        return _from_path(found, source="project")

    return StyleGuide(text=BUILTIN, source="builtin", name="default")


def _from_path(target: Path, *, source: str) -> StyleGuide:
    if not target.exists():
        raise EditingError(
            f"No style guide at '{target}'",
            hint="Point --style-guide at a markdown or text file, or omit it "
                 "to use the built-in guide. `director show-style` prints "
                 "whichever is in use.",
            detail={"path": str(target), "source": source},
        )
    if target.is_dir():
        raise EditingError(
            f"'{target}' is a folder, not a style guide",
            hint="Point --style-guide at the file itself.",
            detail={"path": str(target)},
        )
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EditingError(
            f"Could not read the style guide at '{target}'",
            hint="It should be a UTF-8 text or markdown file.",
            detail={"path": str(target), "reason": str(exc)},
        ) from exc

    if not raw.strip():
        raise EditingError(
            f"The style guide at '{target}' is empty",
            hint="Write a few rules in it, or omit --style-guide to use the "
                 "built-in guide.",
            detail={"path": str(target)},
        )
    return StyleGuide(
        text=_trim(raw), source=source, path=str(target), name=target.stem)


def _search(root: Optional[str]) -> Optional[Path]:
    """Look for a project guide in the usual places. Never raises."""
    base = Path(root).expanduser() if root else Path.cwd()
    for relative in DEFAULT_PATHS:
        candidate = base / relative
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - an unreadable drive
            continue
    return None


def _trim(text: str) -> str:
    """Cap the guide, saying so in the text rather than silently."""
    clean = str(text or "").strip()
    if len(clean) <= MAX_CHARACTERS:
        return clean
    logger.warning(
        "Style guide truncated from %d to %d characters",
        len(clean), MAX_CHARACTERS,
    )
    return (
        clean[:MAX_CHARACTERS].rstrip()
        + "\n\n[...style guide truncated: only the first "
        + f"{MAX_CHARACTERS} characters are used.]"
    )


def describe(guide: StyleGuide) -> str:
    """The guide, for the CLI."""
    lines = [
        "=" * 78,
        f"STYLE GUIDE -- {guide.name} ({guide.source})",
        "=" * 78,
        "",
    ]
    if guide.path:
        lines.append(f"  file  : {guide.path}")
    lines.append(f"  rules : {len(guide.rules)} line(s), "
                 f"{len(guide.text)} character(s)")
    if guide.is_default:
        lines.append("")
        lines.append("  This is the built-in guide. To use your own:")
        lines.append("    python -m editing.cli director plan "
                     "--style-guide docs/my_editing_style.md")
    lines.append("")
    lines.append("-" * 78)
    lines.append(guide.text)
    return "\n".join(lines)
