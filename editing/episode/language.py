"""Reading what was said, without letting one phrase vote twice.

Session 5 shipped a bug worth not repeating: "run" and "help" appeared in both
the reaction list and the danger list, so a line containing them scored both
families and outranked things that deserved to win. The fix there was to make
the lists disjoint by hand. That works until someone adds a word.

Here the guarantee is structural instead. Every cue phrase is matched against
the text **longest first**, and a match *claims its characters*. A shorter cue
from another family cannot then match inside them. So "got it back" scores
``recovery`` and cannot also score ``payoff`` for the "got it" inside it, and
adding a new phrase to a family can never silently double-count an existing one.

Nothing in this module decides anything. It reports what it found; the callers
weigh it against the picture and the sound, and the confidence cap in
``schema.capped`` makes sure a hit here alone can never drive an edit.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Cue families
# ---------------------------------------------------------------------------
#
# Phrases, not stems: "the plan is" is evidence, "plan" on its own is a noun
# that appears in every third sentence. Single words are here only where the
# word is genuinely rare outside its meaning.

CUES: dict[str, tuple] = {
    "objective": (
        "the plan is", "the plan for today", "the goal is", "the goal for",
        "the mission", "the objective", "we need to", "we have to",
        "we're going to", "we are going to", "were going to", "i want to",
        "i'm going to try", "im going to try", "today we're", "today we are",
        "today im", "today i'm", "our job is", "trying to get",
        "trying to find", "we're trying to", "set out to", "aiming for",
        "by the end of this", "this episode we",
        "the plan today", "the goal today", "our plan is", "our goal is",
        "we're gonna", "were gonna", "we are gonna", "i'm gonna get",
        "im gonna get", "let's go and find", "lets go and find",
        "the whole point of this", "what we're doing today",
        "what were doing today",
    ),
    "plan": (
        "here's how", "heres how", "here is how", "the way this works",
        "first we", "then we", "after that we", "step one", "step two",
        "what i'm going to do", "what im going to do", "the idea is",
        "the strategy", "so the way", "which means we", "so first",
    ),
    "explanation": (
        "basically", "essentially", "the reason is", "the reason why",
        "what happens is", "let me explain", "in other words", "to be clear",
        "the thing about", "if you don't know", "if you dont know",
        "for those who", "quick explanation", "long story short",
    ),
    "discovery": (
        "look at that", "there it is", "there we go look", "oh my god",
        "no way", "what is that", "whats that", "what's that",
        "wait is that", "is that a", "i found", "we found", "found it",
        "found some", "have a look at this", "check this out",
    ),
    "payoff": (
        "we did it", "there we go", "that worked", "it worked", "we got it",
        "finally", "that's the one", "thats the one", "beautiful",
        "perfect", "exactly what we wanted", "job done", "done and done",
    ),
    "failure": (
        "i died", "we died", "he died", "that's a death", "thats a death",
        "i lost", "we lost", "lost everything", "lost it all", "it broke",
        "didn't work", "didnt work", "that failed", "oh no no no",
        "well that's bad", "well thats bad", "back to spawn",
    ),
    "recovery": (
        "got it back", "we're fine", "were fine", "okay we're okay",
        "recovered", "back on track", "that's better", "thats better",
        "crisis averted", "we're good now", "were good now",
    ),
    "danger": (
        "watch out", "look out", "be careful", "get away from me",
        "behind me", "behind you", "low health", "half a heart",
        "one heart", "i'm gonna die", "im gonna die", "we're gonna die",
        "this is bad", "oh god no", "get out get out",
    ),
    "joke": (
        "haha", "hahaha", "lmao", "lol", "that's hilarious",
        "thats hilarious", "i'm crying", "im crying", "why did that happen",
        "that's so stupid", "thats so stupid", "what a legend",
    ),
    "callback": (
        "remember when", "remember that", "like i said", "like i mentioned",
        "as i said", "as mentioned", "i told you", "told you so",
        "same as before", "same thing again", "like earlier",
        "back at the start", "we saw this", "there it is again",
        "just like last time",
    ),
    "outro": (
        "thanks for watching", "thank you for watching", "see you next",
        "see you in the next", "next episode", "next video",
        "that's it for today", "thats it for today", "leave a like",
        "subscribe", "peace out", "catch you later",
    ),
    "preparation": (
        "gear up", "geared up", "let's get ready", "lets get ready",
        "before we go", "grab some food", "stock up", "we'll need",
        "well need", "make sure we have", "bring the",
    ),
    "escalation": (
        "it's getting worse", "its getting worse", "even more",
        "now there's two", "now theres two", "this just got",
        "way harder than", "worse and worse",
    ),
}

#: Words that open a question when the line has no question mark.
QUESTION_OPENERS = (
    "will", "can", "could", "should", "what", "where", "how", "why",
    "who", "is", "are", "do", "does", "did", "am", "have", "has",
)

#: Words a topic would be pointless to be "about". Not a full stopword list --
#: it only has to be good enough that two lines matching on "the" is impossible.
STOPWORDS = frozenset("""
a an the and or but if then so because of to in on at for with from by is are
was were be been being am do does did doing done have has had having i you he
she it we they me him her us them my your his its our their this that these
those there here what when where which who whom how why not no yes okay ok
just really very much more most some any all every each other another same
now then again still even also too as up down out off over under about into
onto than only got get gets getting go goes going went gone come comes coming
came like want wants wanted need needs needed know knows knew think thinks
thought see sees saw look looks looking make makes made take takes took put
puts let lets gonna wanna kinda sorta actually basically literally probably
maybe perhaps thing things stuff bit lot lots way ways time times right left
good bad big small little one two three four five oh uh um ah yeah yep nope
gon na ve ll re don doesn didn isn aren wasn weren won can t s
""".split())

#: Nouns that carry an episode. A topic word from here is worth more than a
#: generic content word, because "diamonds" identifies a thread and "place"
#: does not. Deliberately short: the list is a bonus, not a requirement, so a
#: game it does not know about still works, just with weaker topic links.
SALIENT = frozenset("""
diamond diamonds netherite iron gold emerald emeralds ancient debris obsidian
totem totems elytra shulker beacon enchant enchanted enchantment anvil
portal nether end stronghold fortress bastion village villager villagers
creeper creepers skeleton skeletons zombie zombies enderman endermen
warden piglin piglins ghast blaze blazes wither dragon ravager pillager
base house farm storage chest chests furnace cave caves mineshaft ravine
spawner spawn armor armour sword pickaxe axe shield bow crossbow trident
food potion potions bed boat horse dog wolf cat
build building bridge tunnel mine mining tower wall roof floor
seed world server world's biome ocean monument temple trial chamber
""".split())

_WORD = re.compile(r"[a-z0-9']+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")

#: Every cue phrase paired with its family, longest first. The ordering is the
#: whole trick: see the module docstring.
_PHRASES: tuple = tuple(sorted(
    ((phrase, family) for family, phrases in CUES.items() for phrase in phrases),
    key=lambda pair: (-len(pair[0]), pair[0]),
))


def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, and strip the punctuation cues trip on."""
    lowered = str(text or "").lower()
    lowered = lowered.replace("’", "'").replace("‘", "'")
    lowered = re.sub(r"[^a-z0-9'\s?!.]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def words(text: str) -> list[str]:
    return _WORD.findall(normalise(text))


def cue_hits(text: str) -> dict[str, list[str]]:
    """Which cue families the text hits, and on which phrases.

    Longest phrase wins and claims its characters, so no stretch of text can
    contribute to two families. A family with no hits is absent rather than
    present-and-empty, so ``"objective" in hits`` reads correctly.
    """
    haystack = " " + normalise(text) + " "
    claimed: list[tuple] = []
    found: dict[str, list[str]] = {}

    for phrase, family in _PHRASES:
        needle = " " + phrase + " " if len(phrase.split()) > 1 else " " + phrase + " "
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index < 0:
                break
            span = (index, index + len(needle))
            start = index + 1
            if any(span[0] < end and start_ < span[1] for start_, end in claimed):
                continue
            claimed.append(span)
            found.setdefault(family, []).append(phrase)
    return found


def cue_families(text: str) -> set:
    return set(cue_hits(text))


def is_question(text: str) -> bool:
    """Whether the line reads as a question.

    A question mark settles it. Without one, a line that *opens* with a
    question word is a question often enough to be worth flagging and rarely
    enough to be worth flagging quietly -- which is what the confidence cap on
    a transcript-only finding does for us.
    """
    cleaned = normalise(text)
    if not cleaned:
        return False
    if "?" in cleaned:
        return True
    first = cleaned.split(" ", 1)[0].strip("'")
    return first in QUESTION_OPENERS


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(str(text or "")) if part.strip()]


def topic(text: str, *, limit: int = 8) -> list[str]:
    """The content words a line is about, salient ones first.

    Order matters downstream: ``topic_overlap`` weighs a shared salient word
    far more heavily than a shared generic one, because "diamonds" identifies
    a thread through an episode and "wall" does not.
    """
    seen: list[str] = []
    for word in words(text):
        if len(word) < 3 or word in STOPWORDS:
            continue
        if word not in seen:
            seen.append(word)
    salient = [word for word in seen if word in SALIENT]
    rest = [word for word in seen if word not in SALIENT]
    return (salient + rest)[:limit]


def topic_overlap(left: Iterable[str], right: Iterable[str]) -> float:
    """0..1 similarity between two topics.

    A shared salient word is worth three generic ones. Two lines that share
    only generic vocabulary score low on purpose: that is not a thread, it is
    two people speaking English.
    """
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    shared = a & b
    if not shared:
        return 0.0
    weight = sum(3.0 if word in SALIENT else 1.0 for word in shared)
    scale = sum(
        3.0 if word in SALIENT else 1.0 for word in (a if len(a) <= len(b) else b)
    )
    return min(1.0, weight / scale) if scale else 0.0


def shared_salient(left: Iterable[str], right: Iterable[str]) -> list[str]:
    """The salient words two topics have in common. Empty is a weak link."""
    return sorted((set(left) & set(right)) & SALIENT)


def condense(text: str, *, limit: int = 60) -> str:
    """Trim a spoken line to something that fits on a card, without inventing.

    Cuts at a word boundary and adds an ellipsis. It never rewrites, reorders
    or paraphrases: whatever comes out is a contiguous prefix of what was said,
    so a caption built from it cannot claim something nobody said.
    """
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit].rsplit(" ", 1)[0].rstrip(",;:-")
    return (cut or cleaned[:limit]).rstrip() + "..."


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

#: Capitalised words that are not people. Everything here would otherwise be
#: read as a co-op partner the first time it starts a sentence.
_NOT_NAMES = frozenset("""
i i'm ive the a an and but so okay ok oh ah um uh yeah yes no not now then
this that these those there here what when where which who how why we you he
she it they minecraft nether end overworld java bedrock youtube twitch
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november
december god jesus christ lord
""".split())

_CAPITALISED = re.compile(r"\b([A-Z][a-z]{2,15})\b")


def candidate_names(text: str) -> list[str]:
    """Capitalised words that might be people.

    Deliberately weak. It is one channel -- the transcript -- so anything it
    produces is capped below the edit threshold and lands in the report marked
    for review. A name lifted from a sentence start is a guess; saying so is
    better than a heuristic that pretends otherwise.
    """
    out: list[str] = []
    for sentence in sentences(text):
        for index, match in enumerate(_CAPITALISED.finditer(sentence)):
            word = match.group(1)
            if word.lower() in _NOT_NAMES:
                continue
            # A capitalised word at the very start of a sentence is usually
            # just a sentence start. Mid-sentence capitals are the real signal.
            if index == 0 and match.start() == 0:
                continue
            if word not in out:
                out.append(word)
    return out[:20]


def repeated_phrases(
    lines: Iterable[str], *, min_words: int = 2, max_words: int = 4,
    min_count: int = 2,
) -> list[tuple]:
    """Word sequences said more than once, longest and most frequent first.

    This is how a running joke gets found without a model: someone says the
    same odd thing three times. Phrases made entirely of stopwords are dropped,
    which removes "and then we" and keeps "the diamond curse".
    """
    counts: dict[tuple, int] = {}
    for line in lines:
        tokens = words(line)
        for size in range(min_words, max_words + 1):
            for start in range(0, max(0, len(tokens) - size + 1)):
                gram = tuple(tokens[start:start + size])
                if all(token in STOPWORDS for token in gram):
                    continue
                if not any(len(token) >= 4 for token in gram):
                    continue
                counts[gram] = counts.get(gram, 0) + 1

    hits = [
        (" ".join(gram), count)
        for gram, count in counts.items() if count >= min_count
    ]
    hits.sort(key=lambda pair: (-pair[1], -len(pair[0])))

    # Drop a phrase entirely contained in a longer, equally frequent one --
    # "the diamond" adds nothing once "the diamond curse" is already listed.
    kept: list[tuple] = []
    for phrase, count in hits:
        if any(
            phrase in longer and count <= other for longer, other in kept
        ):
            continue
        kept.append((phrase, count))
    return kept[:30]


def first_quote(entries: Iterable, start: float, end: float) -> Optional[str]:
    """The first spoken line overlapping ``[start, end)``, if any."""
    for entry in entries:
        if entry.overlaps(start, end) > 0:
            return entry.text
    return None
