"""What a director pass is, as data.

## The problem this layer exists for

Selection up to now is local. ``usefulness >= 0.40``, dead air goes, danger
stays, a spike is interesting. Every one of those judgements is made by looking
at eight seconds of footage and nothing else, and no amount of tuning fixes
what that cannot see: that a boring stretch is the setup for the thing at 31
minutes, that the episode opens on walking, that the same joke has now landed
three times, that the objective was never actually stated.

A director looks at the whole episode and then decides. This layer is that
pass, and its output is *decisions with reasons*, not a score.

## The rule that makes it safe

**The model proposes; the deterministic layer disposes.**

Nothing here mutates a timeline. A ``DirectorDecision`` is a *request*, and it
carries ``accepted`` set by the safety pass rather than by the model. That is
the same structure Session 4 used for the critic, and it is load-bearing for
the same reason: a language model asked to be creative will occasionally
invent a payoff that is not there, and a system that acts on the invention has
no way to tell the difference afterwards.

Two structural guarantees on top of that:

* **A decision names segment IDs, not timestamps.** Times come from the
  timeline the context was built from, so a decision cannot refer to footage
  that does not exist. A model that hallucinates a range gets an unresolvable
  decision, which is a rejection with a reason -- not an edit.
* **A decision cannot be its own justification.** ``evidence`` must resolve to
  real records, and the safety pass checks the premise before the action, the
  way ``critic.revise`` does.

## Three vocabularies, deliberately closed

``ACTIONS`` is what a decision can ask for. ``REASON_CATEGORIES`` is why.
``VIEWER_EFFECTS`` is what it claims the effect on a viewer would be -- and
that last one is capped language on purpose: this layer may say "a viewer is
more likely to keep watching", and may never say a number, because nothing
here measures retention and Session 8 already established that the honest
answer is to say so.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Sequence

from editing.schema import (
    _slug, as_bool, as_float, as_str_list, clamp01, short_hash,
)

#: Backends that can run a director pass. ``mock`` decides deterministically
#: from the context and is never quiet about being a mock.
BACKENDS = ("openai", "mock")

#: How the rough cut gets its ranges.
#:
#: ``heuristic``  the Session 3 selector, unchanged. The fallback, always.
#: ``director``   only what the director asked for and safety accepted.
#: ``hybrid``     the director's decisions, with the heuristic filling in
#:                every segment the director did not mention.
MODES = ("heuristic", "director", "hybrid")

#: What one decision can ask for.
#:
#: Each maps to something the deterministic layer already knows how to do, and
#: nothing here invents a capability -- ``speed_up`` becomes a retime the rough
#: cut already supports, ``marker_only`` becomes a marker, and
#: ``needs_human_review`` becomes a note that changes no frame.
ACTIONS = (
    "keep",               # use this range at full speed
    "cut",                # do not use this range
    "shorten",            # use part of it -- the selected range says which
    "speed_up",           # use it, retimed
    "hold",               # use it and protect it: no retime, no effects
    "hook",               # use it, and use it first
    "setup",              # keep because something later needs it
    "payoff",             # keep, protected: this is what was built to
    "callback",           # keep, and mark it as calling back to something
    "marker_only",        # change no frame; leave a note for a person
    "needs_human_review",  # the director is unsure and says so
)

#: Actions that put footage in the cut.
KEEPING_ACTIONS = frozenset({
    "keep", "shorten", "speed_up", "hold", "hook", "setup", "payoff",
    "callback",
})

#: Actions that must never be retimed or effected.
PROTECTING_ACTIONS = frozenset({"hold", "payoff", "hook", "setup"})

#: Actions that change no frame of the cut.
PASSIVE_ACTIONS = frozenset({"marker_only", "needs_human_review"})

#: Why a decision was made. Closed, because an open reason field turns into
#: prose nobody can group, count or argue with.
REASON_CATEGORIES = (
    "hook_strength",
    "viewer_curiosity",
    "setup_payoff",
    "callback",
    "pacing",
    "boring_repetition",
    "confusion_risk",
    "comedy_timing",
    "danger_escalation",
    "objective_clarity",
    "climax",
    "ending",
    "dead_air",
    "continuity",
    "style_guide",
    "unknown",
)

#: What a decision claims it does for a viewer. Qualitative on purpose --
#: see ``NOT_MEASURED``.
VIEWER_EFFECTS = (
    "opens_a_question",
    "answers_a_question",
    "raises_tension",
    "releases_tension",
    "lands_a_joke",
    "explains_something",
    "keeps_momentum",
    "removes_a_dull_stretch",
    "protects_a_payoff",
    "restates_the_goal",
    "closes_the_episode",
    "none_stated",
)

#: Said on every plan, every report and every export.
NOT_MEASURED = (
    "Nothing here measures retention, watch time or audience response. These "
    "are one editor's judgements about structure, made by a language model "
    "from a written description of the episode, and checked by rules. No "
    "number in this plan is an analytics prediction."
)

#: Where a director pass can go wrong. Each has a different fix.
FAILURE_STAGES = (
    "config",          # the settings themselves are unusable
    "no_timeline",     # nothing has been analysed yet
    "empty_context",   # there is nothing to decide about
    "no_backend",      # the model is not configured or reachable
    "model",           # the model ran and failed
    "invalid_json",    # it answered, and not with JSON
    "no_decisions",    # it answered with JSON containing nothing usable
    "safety",          # every decision was rejected
    "convert",         # accepted decisions would not become a cut
    "write",           # the plan could not be saved
    "unknown",
)

#: How a decision came to be. ``model`` is the point of the layer; the others
#: exist so a plan is honest about the parts of itself the model did not write.
ORIGINS = ("model", "heuristic", "safety", "fallback")

#: Ceiling on the confidence a decision may claim when its evidence is a
#: single channel. The same rule Session 8 applies to episode findings, for
#: the same reason: one channel agreeing with itself is not corroboration.
SINGLE_CHANNEL_CAP = 0.45

#: A decision below this is never allowed to change a frame.
MIN_ACTIONABLE_CONFIDENCE = 0.55


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _text(value: Any, limit: int = 600) -> str:
    return str(value or "").strip()[:limit]


def coerce_one(value: Any, allowed: Sequence[str], default: str) -> str:
    token = _slug(value)
    return token if token in allowed else default


def _dicts(value: Any) -> list[dict]:
    """The dict members of ``value``, or nothing.

    Every ``from_dict`` here is reachable from a *model response*, which is a
    stronger reason for this guard than usual: a model asked for a list of
    objects will occasionally return a list of strings, and iterating a string
    as characters produces a hundred garbage decisions rather than one clear
    parse failure.
    """
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def _env(name: str, default: str) -> str:
    import os
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DirectorConfig:
    """Everything that decides what the director pass does.

    Frozen and serialised whole into the cache key, so changing the model, the
    style guide or how much context it is shown correctly re-runs rather than
    handing back a plan made under different instructions.

    ``backend`` / ``model`` / ``base_url``
        Deliberately separate from the *vision* model's settings. The two jobs
        want different models -- one reads pictures, one reasons over a
        document -- and a machine serving Qwen3-VL on :8000 may well want a
        different endpoint for this. Defaulting to the vision settings would
        make that impossible to express.
    ``temperature``
        Low. This is a structured-output task, and creativity here shows up as
        invented segment IDs rather than better editing.
    ``mode``
        ``director`` uses only what the director asked for; ``hybrid`` fills
        the gaps from the heuristic selector; ``heuristic`` does not run the
        model at all and exists so the flag has a meaningful off position.
    ``target_duration`` / ``max_duration``
        What the director is aiming for, in seconds. 0 means "no target",
        which is honest rather than helpful -- an editor with no runtime in
        mind makes different decisions.
    """

    backend: str = "openai"
    model: str = "qwen2.5-14b-instruct"
    base_url: str = "http://localhost:8000/v1"
    api_key: str = "not-needed"
    timeout: float = 300.0
    max_retries: int = 2
    temperature: float = 0.2
    max_output_tokens: int = 8000

    mode: str = "director"

    # -- what the model is shown ------------------------------------------
    #: Hard ceiling on candidate segments in the context. A 40-minute episode
    #: is ~300 segments; a model given all of them writes worse decisions
    #: about all of them.
    max_segments: int = 160
    #: Characters of transcript per segment. Enough to know what was said.
    max_transcript_chars: int = 220
    #: Ceilings on each episode-layer list.
    max_beats: int = 40
    max_open_loops: int = 20
    max_risks: int = 20
    max_hooks: int = 8
    max_recommendations: int = 40
    max_preferences: int = 20
    #: Total context budget in characters. Exceeding it drops the lowest-value
    #: sections first, and the plan records what was dropped.
    max_context_chars: int = 60000

    # -- what the cut is aiming at ----------------------------------------
    target_duration: float = 0.0
    max_duration: float = 0.0
    #: Ceiling on how much of the episode may be marked ``hook``.
    max_hooks_in_cut: int = 2
    #: Ceiling on callbacks, which get annoying faster than anything else.
    max_callbacks_in_cut: int = 4
    #: Seconds of grind (``speed_up`` plus low-value keeps) the cut may hold.
    max_grind_seconds: float = 90.0
    #: Speed applied when a decision says ``speed_up`` without one.
    default_speed: float = 2.0

    # -- taste -------------------------------------------------------------
    #: A style preset name, or empty. Threaded in so the director knows what
    #: the later passes will do on top of its cut.
    style: str = ""
    #: Path to a prose style guide. Empty uses the built-in one.
    style_guide_path: str = ""

    #: Decisions below this are recorded and never allowed to change a frame.
    min_confidence: float = MIN_ACTIONABLE_CONFIDENCE
    use_cache: bool = True

    @classmethod
    def from_env(cls) -> "DirectorConfig":
        return cls(
            backend=_env("EDITING_DIRECTOR_BACKEND", "openai"),
            model=_env("EDITING_DIRECTOR_MODEL", "qwen2.5-14b-instruct"),
            base_url=_env("EDITING_DIRECTOR_BASE_URL",
                          "http://localhost:8000/v1"),
            api_key=_env("EDITING_DIRECTOR_API_KEY", "not-needed"),
            timeout=_env_float("EDITING_DIRECTOR_TIMEOUT", 300.0),
            max_retries=_env_int("EDITING_DIRECTOR_RETRIES", 2),
            temperature=_env_float("EDITING_DIRECTOR_TEMPERATURE", 0.2),
            max_output_tokens=_env_int("EDITING_DIRECTOR_MAX_TOKENS", 8000),
            mode=_env("EDITING_DIRECTOR_MODE", "director"),
            style_guide_path=_env("EDITING_STYLE_GUIDE", ""),
            max_context_chars=_env_int("EDITING_DIRECTOR_CONTEXT_CHARS",
                                       60000),
            use_cache=_env_bool("EDITING_DIRECTOR_CACHE", True),
        )

    def validated(self) -> "DirectorConfig":
        """Clamp to values the pass can honour. Never raises."""
        from dataclasses import replace

        return replace(
            self,
            backend=coerce_one(self.backend, BACKENDS, "openai"),
            model=_text(self.model, 120) or "qwen2.5-14b-instruct",
            base_url=_text(self.base_url, 500),
            timeout=max(10.0, as_float(self.timeout, 300.0)),
            max_retries=max(0, min(int(as_float(self.max_retries, 2)), 5)),
            temperature=max(0.0, min(as_float(self.temperature, 0.2), 2.0)),
            max_output_tokens=max(
                512, min(int(as_float(self.max_output_tokens, 8000)), 64000)),
            mode=coerce_one(self.mode, MODES, "director"),
            max_segments=max(1, min(int(as_float(self.max_segments, 160)),
                                    1000)),
            max_transcript_chars=max(
                0, min(int(as_float(self.max_transcript_chars, 220)), 4000)),
            max_beats=max(0, int(as_float(self.max_beats, 40))),
            max_open_loops=max(0, int(as_float(self.max_open_loops, 20))),
            max_risks=max(0, int(as_float(self.max_risks, 20))),
            max_hooks=max(0, int(as_float(self.max_hooks, 8))),
            max_recommendations=max(
                0, int(as_float(self.max_recommendations, 40))),
            max_preferences=max(0, int(as_float(self.max_preferences, 20))),
            max_context_chars=max(
                2000, int(as_float(self.max_context_chars, 60000))),
            target_duration=max(0.0, as_float(self.target_duration)),
            max_duration=max(0.0, as_float(self.max_duration)),
            max_hooks_in_cut=max(1, int(as_float(self.max_hooks_in_cut, 2))),
            max_callbacks_in_cut=max(
                0, int(as_float(self.max_callbacks_in_cut, 4))),
            max_grind_seconds=max(0.0, as_float(self.max_grind_seconds, 90.0)),
            default_speed=max(1.0, min(as_float(self.default_speed, 2.0),
                                       8.0)),
            style=_slug(self.style),
            min_confidence=clamp01(self.min_confidence,
                                   MIN_ACTIONABLE_CONFIDENCE),
        )

    @property
    def runs_model(self) -> bool:
        return self.mode in ("director", "hybrid")

    @property
    def warnings(self) -> list[str]:
        """Things worth saying about these settings before a long call."""
        out: list[str] = []
        if self.backend == "mock":
            out.append(
                "MOCK backend: decisions are derived from the context by a "
                "fixed rule, not by a model. Useful for exercising the "
                "pipeline; never a creative judgement."
            )
        if self.temperature > 0.6:
            out.append(
                f"temperature {self.temperature:g} is high for a structured "
                "task -- expect invented segment IDs rather than better "
                "editing. 0.2 is the default for a reason."
            )
        if self.max_duration and self.target_duration \
                and self.target_duration > self.max_duration:
            out.append(
                f"the target runtime ({self.target_duration:.0f}s) is longer "
                f"than the maximum ({self.max_duration:.0f}s); the maximum "
                "wins and the director will be asked to cut deeper."
            )
        if self.mode == "heuristic":
            out.append(
                "mode is 'heuristic', so no model runs and this plan will "
                "contain no director decisions."
            )
        return out

    def cache_key_part(self) -> dict:
        """The subset of this config that changes what the model answers.

        ``use_cache``, ``timeout`` and ``max_retries`` are absent: none of them
        changes a word of the response, and including them would make turning
        the cache off invalidate everything already in it.
        """
        clean = self.validated()
        return {
            "backend": clean.backend,
            "model": clean.model,
            "temperature": round(clean.temperature, 3),
            "max_output_tokens": clean.max_output_tokens,
            "mode": clean.mode,
            "max_segments": clean.max_segments,
            "max_transcript_chars": clean.max_transcript_chars,
            "max_context_chars": clean.max_context_chars,
            "target_duration": round(clean.target_duration, 2),
            "max_duration": round(clean.max_duration, 2),
            "style": clean.style,
        }

    def to_dict(self) -> dict:
        data = asdict(self)
        # Never write a key to disk, even a placeholder one -- a plan gets
        # pasted into issues and chats.
        data["api_key"] = "***" if self.api_key not in ("", "not-needed") \
            else self.api_key
        return data

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "DirectorConfig":
        data = dict(data or {})
        if data.get("api_key") == "***":
            data.pop("api_key")
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# The style guide
# ---------------------------------------------------------------------------

@dataclass
class StyleGuide:
    """A person's editing habits, in prose, plus where they came from.

    Prose rather than parameters, and that is the point. "I hold two beats
    after deaths" is a rule this system has no field for and could not have
    guessed, and writing it as prose costs the user nothing. The model reads
    it; the deterministic layer does not, because a rule it cannot parse is a
    rule it must not pretend to enforce.
    """

    text: str = ""
    source: str = "builtin"
    path: str = ""
    name: str = "default"

    @property
    def is_default(self) -> bool:
        return self.source == "builtin"

    @property
    def rules(self) -> list[str]:
        """The guide as lines, for the report. Not parsed, only displayed."""
        return [
            line.strip(" -*\t")
            for line in self.text.splitlines()
            if line.strip(" -*\t#")
        ]

    def fingerprint(self) -> str:
        return short_hash(self.text, length=10)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "path": self.path,
            "name": self.name,
            "is_default": self.is_default,
            "fingerprint": self.fingerprint(),
            "rule_count": len(self.rules),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "StyleGuide":
        data = data or {}
        return cls(
            text=_text(data.get("text"), 20000),
            source=_text(data.get("source"), 40) or "builtin",
            path=_text(data.get("path"), 500),
            name=_text(data.get("name"), 120) or "default",
        )


# ---------------------------------------------------------------------------
# The context
# ---------------------------------------------------------------------------

@dataclass
class ContextSegment:
    """One candidate range, as the model sees it.

    Deliberately flat and short. The model has to hold a hundred of these in
    mind at once, so every field here has to earn its characters -- which is
    why there is no evidence payload, no confidence breakdown and no nested
    events, only the things that would change an editing decision.
    """

    segment_id: str = ""
    asset_id: str = ""
    source_file: str = ""
    start: float = 0.0
    end: float = 0.0
    #: Position in the episode, 0..1. What tells the model this is the opening.
    position: float = 0.0
    said: str = ""
    environment: str = ""
    actions: list[str] = field(default_factory=list)
    importance: str = "unknown"
    audio: list[str] = field(default_factory=list)
    usefulness: float = 0.0
    dead_air: bool = False
    #: The episode beat this falls in, if the memory found one.
    beat: str = ""
    #: Heuristic verdict, so the model can agree or disagree with it rather
    #: than start from nothing.
    heuristic: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def line(self) -> str:
        """One line for the prompt. This is the actual context format."""
        parts = [
            f"[{self.segment_id}]",
            f"{self.start:.0f}-{self.end:.0f}s",
            f"({self.duration:.0f}s, {self.position:.0%} in)",
        ]
        if self.importance and self.importance != "unknown":
            parts.append(self.importance)
        if self.environment:
            parts.append(self.environment)
        if self.actions:
            parts.append("/".join(self.actions[:3]))
        if self.audio:
            parts.append("audio:" + ",".join(self.audio[:3]))
        if self.beat:
            parts.append(f"beat:{self.beat}")
        if self.dead_air:
            parts.append("DEAD AIR")
        parts.append(f"heur:{self.heuristic or 'none'}")
        head = "  ".join(parts)
        return f"{head}\n    said: {self.said}" if self.said else head

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ContextSegment":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        return cls(
            segment_id=_text(data.get("segment_id"), 80),
            asset_id=_text(data.get("asset_id"), 120),
            source_file=_text(data.get("source_file"), 500),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            position=clamp01(data.get("position"), 0.0),
            said=_text(data.get("said"), 4000),
            environment=_text(data.get("environment"), 60),
            actions=as_str_list(data.get("actions"), limit=10),
            importance=_text(data.get("importance"), 40) or "unknown",
            audio=as_str_list(data.get("audio"), limit=10),
            usefulness=clamp01(data.get("usefulness"), 0.0),
            dead_air=as_bool(data.get("dead_air")),
            beat=_text(data.get("beat"), 60),
            heuristic=_text(data.get("heuristic"), 60),
        )


@dataclass
class DirectorContext:
    """Everything the director is told, and nothing it is not.

    Built deterministically from artifacts already on disk, then rendered to
    text. Keeping the structured form alongside the text matters: the text is
    what the model reads, and the structured form is what the safety pass
    checks its answer against. Building the second from the first afterwards
    would mean parsing our own prompt.
    """

    name: str = "structure"
    episode_id: str = ""
    duration: float = 0.0
    #: Which artifacts were actually available. A context built without a
    #: transcript is a different thing from one built with, and the report has
    #: to be able to say so.
    sources: dict = field(default_factory=dict)

    summary: str = ""
    objective: str = ""
    objective_status: str = ""
    beats: list[dict] = field(default_factory=list)
    open_loops: list[dict] = field(default_factory=list)
    setups: list[dict] = field(default_factory=list)
    payoffs: list[dict] = field(default_factory=list)
    callbacks: list[dict] = field(default_factory=list)
    risks: list[dict] = field(default_factory=list)
    hook_candidates: list[dict] = field(default_factory=list)
    climax: dict = field(default_factory=dict)
    ending: dict = field(default_factory=dict)
    recommendations: list[dict] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)

    segments: list[ContextSegment] = field(default_factory=list)
    style_guide: StyleGuide = field(default_factory=StyleGuide)
    style_summary: str = ""

    #: What was left out to fit the budget, said plainly.
    dropped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""
    schema_version: int = 1

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def is_empty(self) -> bool:
        return not self.segments

    @property
    def segment_ids(self) -> set:
        return {segment.segment_id for segment in self.segments}

    def segment(self, segment_id: str) -> Optional[ContextSegment]:
        for entry in self.segments:
            if entry.segment_id == segment_id:
                return entry
        return None

    def fingerprint(self) -> str:
        """A hash of everything that would change what the model answers.

        Times are spelled rather than hashed as numbers. A context built in
        memory carries ``start=0`` where the same context read back from JSON
        carries ``0.0``, and ``repr`` spells those differently -- so hashing
        the raw values would make the answer cache miss on the one path it
        exists for: build a context, save it, ask about it later. The render
        layer learned this the same way.
        """
        parts = [
            self.name, self.objective, self.summary,
            repr([(str(item.segment_id), f"{float(item.start):.3f}",
                   f"{float(item.end):.3f}", str(item.said),
                   str(item.importance), str(item.heuristic))
                  for item in self.segments]),
            repr(self.beats), repr(self.open_loops), repr(self.risks),
            repr(self.hook_candidates), repr(self.preferences),
            self.style_guide.fingerprint(), self.style_summary,
        ]
        return short_hash(*parts, length=16)

    def stats(self) -> dict:
        return {
            "segments": len(self.segments),
            "duration": round(self.duration, 2),
            "beats": len(self.beats),
            "open_loops": len(self.open_loops),
            "setups": len(self.setups),
            "payoffs": len(self.payoffs),
            "risks": len(self.risks),
            "hook_candidates": len(self.hook_candidates),
            "recommendations": len(self.recommendations),
            "preferences": len(self.preferences),
            "dropped": len(self.dropped),
            "with_speech": sum(1 for s in self.segments if s.said),
            "dead_air": sum(1 for s in self.segments if s.dead_air),
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "episode_id": self.episode_id,
            "duration": round(self.duration, 3),
            "generated_at": self.generated_at,
            "fingerprint": self.fingerprint(),
            "sources": dict(self.sources),
            "summary": self.summary,
            "objective": self.objective,
            "objective_status": self.objective_status,
            "beats": list(self.beats),
            "open_loops": list(self.open_loops),
            "setups": list(self.setups),
            "payoffs": list(self.payoffs),
            "callbacks": list(self.callbacks),
            "risks": list(self.risks),
            "hook_candidates": list(self.hook_candidates),
            "climax": dict(self.climax),
            "ending": dict(self.ending),
            "recommendations": list(self.recommendations),
            "preferences": list(self.preferences),
            "style_guide": self.style_guide.to_dict(),
            "style_summary": self.style_summary,
            "segments": [segment.to_dict() for segment in self.segments],
            "dropped": list(self.dropped),
            "warnings": list(self.warnings),
            "stats": self.stats(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorContext":
        data = data or {}
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            episode_id=_text(data.get("episode_id"), 80),
            duration=as_float(data.get("duration")),
            sources=dict(data.get("sources") or {}),
            summary=_text(data.get("summary"), 4000),
            objective=_text(data.get("objective"), 600),
            objective_status=_text(data.get("objective_status"), 40),
            beats=_dicts(data.get("beats")),
            open_loops=_dicts(data.get("open_loops")),
            setups=_dicts(data.get("setups")),
            payoffs=_dicts(data.get("payoffs")),
            callbacks=_dicts(data.get("callbacks")),
            risks=_dicts(data.get("risks")),
            hook_candidates=_dicts(data.get("hook_candidates")),
            climax=dict(data.get("climax") or {}),
            ending=dict(data.get("ending") or {}),
            recommendations=_dicts(data.get("recommendations")),
            preferences=as_str_list(data.get("preferences"), limit=60),
            segments=[
                ContextSegment.from_dict(item)
                for item in _dicts(data.get("segments"))
            ],
            style_guide=StyleGuide.from_dict(data.get("style_guide")),
            style_summary=_text(data.get("style_summary"), 2000),
            dropped=as_str_list(data.get("dropped"), limit=60),
            warnings=as_str_list(data.get("warnings"), limit=60),
            generated_at=_text(data.get("generated_at"), 40),
        )


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

@dataclass
class DirectorPrompt:
    """Exactly what was sent, kept so a bad answer is explainable.

    Stored with the plan rather than regenerated for a report, because the
    prompt is the first thing to look at when the decisions are wrong and
    "what did we actually ask it" must not be a reconstruction.
    """

    system: str = ""
    user: str = ""
    context_fingerprint: str = ""
    style_guide_fingerprint: str = ""
    #: Rough, and rough on purpose: this is for spotting "we sent it 400k
    #: characters" rather than for billing.
    approx_tokens: int = 0

    @property
    def characters(self) -> int:
        return len(self.system) + len(self.user)

    def fingerprint(self) -> str:
        return short_hash(self.system, self.user, length=16)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["characters"] = self.characters
        data["fingerprint"] = self.fingerprint()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorPrompt":
        data = data or {}
        return cls(
            system=_text(data.get("system"), 40000),
            user=_text(data.get("user"), 400000),
            context_fingerprint=_text(data.get("context_fingerprint"), 40),
            style_guide_fingerprint=_text(
                data.get("style_guide_fingerprint"), 40),
            approx_tokens=int(as_float(data.get("approx_tokens"))),
        )


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

@dataclass
class DirectorReason:
    """Why a decision was made, in a form that can be counted.

    ``category`` is closed so a report can group fifty decisions; ``text`` is
    the model's own sentence, which is the part a person actually reads;
    ``style_rule`` is the line of the style guide it says it is following,
    which is how a user finds out whether their guide is being used at all.
    """

    category: str = "unknown"
    text: str = ""
    style_rule: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "DirectorReason":
        if isinstance(data, str):
            # A model told to give an object will sometimes give a sentence.
            # Keeping it beats dropping the only explanation on offer.
            return cls(category="unknown", text=_text(data, 600))
        data = data or {}
        return cls(
            category=coerce_one(
                data.get("category"), REASON_CATEGORIES, "unknown"),
            text=_text(data.get("text") or data.get("reason"), 600),
            style_rule=_text(data.get("style_rule"), 300),
        )


@dataclass
class DirectorRange:
    """A concrete stretch of source footage the cut will use.

    The bridge to Session 3: this is what ``convert`` turns into a
    ``SelectedRange`` and therefore into a ``ClipPlacement``. Times here are
    always source time on a real asset, resolved from segment IDs -- never a
    number a model typed.
    """

    asset_id: str = ""
    source_file: str = ""
    start: float = 0.0
    end: float = 0.0
    speed: float = 1.0
    protected: bool = False
    keep_reason: str = "unknown"
    #: Where this sits in the cut. Lower comes first.
    order: int = 100
    #: True when this range is the opening. A flag rather than an inferred
    #: ``order == 0``: a genuine reveal that happens to sort first is not a
    #: hook, and a cut that quietly opened on one would be wrong in a way
    #: nobody could see from the plan.
    is_hook: bool = False
    decision_id: str = ""
    segment_ids: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def cut_duration(self) -> float:
        rate = self.speed if self.speed > 0 else 1.0
        return self.duration / rate

    def overlaps(self, other: "DirectorRange") -> bool:
        return (self.asset_id == other.asset_id
                and self.start < other.end and other.start < self.end)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 3)
        data["cut_duration"] = round(self.cut_duration, 3)
        data["start"] = round(self.start, 3)
        data["end"] = round(self.end, 3)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorRange":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        return cls(
            asset_id=_text(data.get("asset_id"), 120),
            source_file=_text(data.get("source_file"), 500),
            start=start,
            end=max(start, as_float(data.get("end"), start)),
            speed=max(0.05, as_float(data.get("speed"), 1.0)),
            protected=as_bool(data.get("protected")),
            keep_reason=_text(data.get("keep_reason"), 40) or "unknown",
            order=int(as_float(data.get("order"), 100)),
            is_hook=as_bool(data.get("is_hook")),
            decision_id=_text(data.get("decision_id"), 60),
            segment_ids=as_str_list(data.get("segment_ids"), limit=50),
            notes=_text(data.get("notes"), 500),
        )


@dataclass
class DirectorDecision:
    """One judgement about one stretch of the episode.

    ``accepted`` is set by the safety pass and by nothing else. A decision
    arrives from the model with ``accepted=False`` and stays that way unless a
    deterministic check says otherwise -- which is the whole safety model of
    this layer expressed as a default value.
    """

    decision_id: str = ""
    action: str = "keep"
    #: The segments this is about. The source of truth for *where*.
    segment_ids: list[str] = field(default_factory=list)

    # -- resolved from the segments, never from the model ------------------
    asset_id: str = ""
    source_file: str = ""
    start: float = 0.0
    end: float = 0.0
    #: The part of that range to actually use. Equal to (start, end) unless
    #: the action is ``shorten``.
    out_start: float = 0.0
    out_end: float = 0.0
    speed: float = 1.0

    confidence: float = 0.0
    #: 0..1. What the director thinks this matters relative to its others.
    priority: float = 0.5
    reason: DirectorReason = field(default_factory=DirectorReason)
    #: Free-form pointers the model gave: segment IDs, beat IDs, quotes.
    evidence: list[str] = field(default_factory=list)
    viewer_effect: str = "none_stated"

    # -- links into the other layers ---------------------------------------
    beat_id: str = ""
    open_loop_id: str = ""
    setup_id: str = ""
    payoff_id: str = ""
    suggestion_id: str = ""
    recommendation_ids: list[str] = field(default_factory=list)

    # -- what the deterministic layer said ---------------------------------
    origin: str = "model"
    accepted: bool = False
    modified: bool = False
    rejected_reason: str = ""
    safety_notes: list[str] = field(default_factory=list)
    #: What safety changed, in the order it changed it.
    modifications: list[str] = field(default_factory=list)

    order: int = 100
    schema_version: int = 1

    # -- derived -----------------------------------------------------------

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def out_duration(self) -> float:
        return max(0.0, self.out_end - self.out_start)

    @property
    def cut_duration(self) -> float:
        """How long this occupies the finished cut."""
        if self.action not in KEEPING_ACTIONS:
            return 0.0
        rate = self.speed if self.speed > 0 else 1.0
        return self.out_duration / rate

    @property
    def keeps_footage(self) -> bool:
        return self.action in KEEPING_ACTIONS

    @property
    def is_protecting(self) -> bool:
        return self.action in PROTECTING_ACTIONS

    @property
    def changes_nothing(self) -> bool:
        return self.action in PASSIVE_ACTIONS

    @property
    def is_actionable(self) -> bool:
        """Accepted, confident enough, and asking for a change to the cut."""
        return (self.accepted and not self.changes_nothing
                and self.confidence >= MIN_ACTIONABLE_CONFIDENCE)

    def keep_reason(self) -> str:
        """The Session 3 vocabulary word for why this footage is in the cut."""
        mapping = {
            "hook": "reveal", "payoff": "payoff", "setup": "setup",
            "hold": "hold", "callback": "contrast", "speed_up": "filler",
            "shorten": "filler", "keep": "unknown",
        }
        return mapping.get(self.action, "unknown")

    def as_range(self) -> Optional[DirectorRange]:
        """The concrete range this puts in the cut, or ``None``."""
        if not self.keeps_footage or self.out_duration <= 0:
            return None
        return DirectorRange(
            asset_id=self.asset_id,
            source_file=self.source_file,
            start=self.out_start,
            end=self.out_end,
            speed=self.speed,
            protected=self.is_protecting,
            keep_reason=self.keep_reason(),
            order=self.order,
            is_hook=self.action == "hook",
            decision_id=self.decision_id,
            segment_ids=list(self.segment_ids),
            notes=self.reason.text[:300],
        )

    def line(self) -> str:
        mark = "+" if self.accepted else "x"
        if self.modified:
            mark = "~"
        window = f"{self.start:.0f}-{self.end:.0f}s"
        tail = self.reason.text or self.rejected_reason
        return (f"{mark} {self.decision_id[:14]:<14} {self.action:<18} "
                f"{window:<16} {self.confidence:.2f}  {tail[:60]}")

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "action": self.action,
            "segment_ids": list(self.segment_ids),
            "asset_id": self.asset_id,
            "source_file": self.source_file,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "out_start": round(self.out_start, 3),
            "out_end": round(self.out_end, 3),
            "out_duration": round(self.out_duration, 3),
            "cut_duration": round(self.cut_duration, 3),
            "speed": round(self.speed, 4),
            "confidence": round(self.confidence, 3),
            "priority": round(self.priority, 3),
            "reason": self.reason.to_dict(),
            "evidence": list(self.evidence),
            "viewer_effect": self.viewer_effect,
            "beat_id": self.beat_id,
            "open_loop_id": self.open_loop_id,
            "setup_id": self.setup_id,
            "payoff_id": self.payoff_id,
            "suggestion_id": self.suggestion_id,
            "recommendation_ids": list(self.recommendation_ids),
            "origin": self.origin,
            "accepted": self.accepted,
            "modified": self.modified,
            "rejected_reason": self.rejected_reason,
            "safety_notes": list(self.safety_notes),
            "modifications": list(self.modifications),
            "order": self.order,
            "keeps_footage": self.keeps_footage,
            "is_actionable": self.is_actionable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorDecision":
        data = data or {}
        start = max(0.0, as_float(data.get("start")))
        end = max(start, as_float(data.get("end"), start))
        out_start = max(0.0, as_float(data.get("out_start"), start))
        out_end = max(out_start, as_float(data.get("out_end"), end))
        return cls(
            decision_id=_text(data.get("decision_id"), 60),
            action=coerce_one(data.get("action"), ACTIONS, "keep"),
            segment_ids=as_str_list(data.get("segment_ids"), limit=80),
            asset_id=_text(data.get("asset_id"), 120),
            source_file=_text(data.get("source_file"), 500),
            start=start,
            end=end,
            out_start=out_start,
            out_end=out_end,
            speed=max(0.05, as_float(data.get("speed"), 1.0)),
            confidence=clamp01(data.get("confidence"), 0.0),
            priority=clamp01(data.get("priority"), 0.5),
            reason=DirectorReason.from_dict(data.get("reason")),
            evidence=as_str_list(data.get("evidence"), limit=40),
            viewer_effect=coerce_one(
                data.get("viewer_effect"), VIEWER_EFFECTS, "none_stated"),
            beat_id=_text(data.get("beat_id"), 60),
            open_loop_id=_text(data.get("open_loop_id"), 60),
            setup_id=_text(data.get("setup_id"), 60),
            payoff_id=_text(data.get("payoff_id"), 60),
            suggestion_id=_text(data.get("suggestion_id"), 60),
            recommendation_ids=as_str_list(
                data.get("recommendation_ids"), limit=40),
            origin=coerce_one(data.get("origin"), ORIGINS, "model"),
            accepted=as_bool(data.get("accepted")),
            modified=as_bool(data.get("modified")),
            rejected_reason=_text(data.get("rejected_reason"), 600),
            safety_notes=as_str_list(data.get("safety_notes"), limit=20),
            modifications=as_str_list(data.get("modifications"), limit=20),
            order=int(as_float(data.get("order"), 100)),
        )


def decision_id_for(action: str, segment_ids: Sequence[str]) -> str:
    """Stable for one action over one set of segments."""
    return "d_" + short_hash(
        _slug(action), repr(sorted(str(s) for s in segment_ids)), length=8)


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------

@dataclass
class SafetyViolation:
    """One thing a decision did that a rule would not allow."""

    check: str = ""
    decision_id: str = ""
    severity: str = "reject"        # reject / modify / warn
    message: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SafetyViolation":
        data = data or {}
        return cls(
            check=_text(data.get("check"), 60),
            decision_id=_text(data.get("decision_id"), 60),
            severity=coerce_one(
                data.get("severity"), ("reject", "modify", "warn"), "reject"),
            message=_text(data.get("message"), 600),
            detail=dict(data.get("detail") or {}),
        )


@dataclass
class DirectorSafetyReview:
    """What the deterministic layer did to the model's proposals.

    The record that makes this layer auditable. Every rejection names its
    check, so "why is that clip not in the cut" has an answer that is a rule
    rather than a mood.
    """

    checks_run: list[str] = field(default_factory=list)
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    modified: int = 0
    violations: list[SafetyViolation] = field(default_factory=list)
    #: Numbers the ceilings were measured against.
    measurements: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def acceptance_rate(self) -> float:
        return round(self.accepted / self.proposed, 3) if self.proposed else 0.0

    def of_check(self, check: str) -> list[SafetyViolation]:
        return [item for item in self.violations if item.check == check]

    def by_check(self) -> dict:
        out: dict = {}
        for item in self.violations:
            out[item.check] = out.get(item.check, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "checks_run": list(self.checks_run),
            "proposed": self.proposed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "modified": self.modified,
            "acceptance_rate": self.acceptance_rate,
            "by_check": self.by_check(),
            "violations": [item.to_dict() for item in self.violations],
            "measurements": dict(self.measurements),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorSafetyReview":
        data = data or {}
        return cls(
            checks_run=as_str_list(data.get("checks_run"), limit=60),
            proposed=int(as_float(data.get("proposed"))),
            accepted=int(as_float(data.get("accepted"))),
            rejected=int(as_float(data.get("rejected"))),
            modified=int(as_float(data.get("modified"))),
            violations=[
                SafetyViolation.from_dict(item)
                for item in _dicts(data.get("violations"))
            ],
            measurements=dict(data.get("measurements") or {}),
            warnings=as_str_list(data.get("warnings"), limit=60),
        )


# ---------------------------------------------------------------------------
# Failure and result
# ---------------------------------------------------------------------------

@dataclass
class DirectorFailure:
    """Why a director pass did not happen, and what to do about it."""

    stage: str = "unknown"
    code: str = "director_failed"
    message: str = ""
    hint: str = ""
    recoverable: bool = True
    #: The model's answer, when there was one. The first thing to look at.
    response_excerpt: str = ""
    detail: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"{self.stage}: {self.message}"]
        if self.hint:
            lines.append(f"  fix : {self.hint}")
        if self.response_excerpt:
            lines.append(f"  said: {self.response_excerpt[:300]}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> Optional["DirectorFailure"]:
        if not data:
            return None
        return cls(
            stage=coerce_one(data.get("stage"), FAILURE_STAGES, "unknown"),
            code=_text(data.get("code"), 60) or "director_failed",
            message=_text(data.get("message"), 1000),
            hint=_text(data.get("hint"), 1000),
            recoverable=as_bool(data.get("recoverable"), True),
            response_excerpt=_text(data.get("response_excerpt"), 2000),
            detail=dict(data.get("detail") or {}),
        )


@dataclass
class DirectorResult:
    """What one model call produced, before safety touched it.

    Kept separate from the plan on purpose: the plan is what the system will
    act on, and this is what the model actually said. When a plan looks wrong
    the first question is which of the two is at fault, and merging them would
    make that unanswerable.
    """

    backend: str = "openai"
    model: str = ""
    decisions: list[DirectorDecision] = field(default_factory=list)
    #: The model's own summary of its approach, if it gave one.
    approach: str = ""
    raw_response: str = ""
    elapsed: float = 0.0
    cached: bool = False
    mock: bool = False
    #: Decisions the parser could not use, with the reason for each.
    discarded: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failure: Optional[DirectorFailure] = None

    def __len__(self) -> int:
        return len(self.decisions)

    @property
    def ok(self) -> bool:
        return self.failure is None and bool(self.decisions)

    def stats(self) -> dict:
        by_action: dict = {}
        for decision in self.decisions:
            by_action[decision.action] = by_action.get(decision.action, 0) + 1
        return {
            "decisions": len(self.decisions),
            "by_action": by_action,
            "discarded": len(self.discarded),
            "elapsed": round(self.elapsed, 2),
            "cached": self.cached,
            "mock": self.mock,
        }

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model,
            "approach": self.approach,
            "elapsed": round(self.elapsed, 3),
            "cached": self.cached,
            "mock": self.mock,
            "stats": self.stats(),
            "decisions": [d.to_dict() for d in self.decisions],
            "discarded": list(self.discarded),
            "warnings": list(self.warnings),
            "failure": self.failure.to_dict() if self.failure else None,
            # Truncated: a full response is tens of kilobytes and the useful
            # part when something went wrong is the beginning.
            "raw_response": self.raw_response[:4000],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorResult":
        data = data or {}
        return cls(
            backend=coerce_one(data.get("backend"), BACKENDS, "openai"),
            model=_text(data.get("model"), 120),
            decisions=[
                DirectorDecision.from_dict(item)
                for item in _dicts(data.get("decisions"))
            ],
            approach=_text(data.get("approach"), 2000),
            raw_response=_text(data.get("raw_response"), 40000),
            elapsed=as_float(data.get("elapsed")),
            cached=as_bool(data.get("cached")),
            mock=as_bool(data.get("mock")),
            discarded=_dicts(data.get("discarded")),
            warnings=as_str_list(data.get("warnings"), limit=60),
            failure=DirectorFailure.from_dict(data.get("failure")),
        )


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

@dataclass
class DirectorPlan:
    """Every decision, what survived, and the cut that follows from it.

    The artifact this session exists to produce. It contains no operations and
    touches nothing: Session 3's builder turns ``ranges`` into placements, and
    every guard that has always applied to a rough cut still applies.
    """

    name: str = "structure"
    episode_id: str = ""
    mode: str = "director"
    config: DirectorConfig = field(default_factory=DirectorConfig)
    style_guide: StyleGuide = field(default_factory=StyleGuide)

    decisions: list[DirectorDecision] = field(default_factory=list)
    #: What the accepted decisions come to, in play order.
    ranges: list[DirectorRange] = field(default_factory=list)
    safety: DirectorSafetyReview = field(default_factory=DirectorSafetyReview)
    prompt: Optional[DirectorPrompt] = None

    backend: str = ""
    model: str = ""
    mock: bool = False
    cached: bool = False
    approach: str = ""
    elapsed: float = 0.0

    context_fingerprint: str = ""
    context_stats: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)

    generated_at: str = ""
    warnings: list[str] = field(default_factory=list)
    failure: Optional[DirectorFailure] = None
    not_measured: str = NOT_MEASURED
    schema_version: int = 1

    # -- derived -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self.decisions)

    @property
    def ok(self) -> bool:
        return self.failure is None and bool(self.ranges)

    @property
    def accepted(self) -> list[DirectorDecision]:
        return [d for d in self.decisions if d.accepted]

    @property
    def rejected(self) -> list[DirectorDecision]:
        return [d for d in self.decisions if not d.accepted]

    @property
    def modified(self) -> list[DirectorDecision]:
        return [d for d in self.decisions if d.modified]

    @property
    def needs_human_review(self) -> list[DirectorDecision]:
        return [d for d in self.decisions
                if d.action == "needs_human_review" or d.confidence < 0.5]

    @property
    def cut_duration(self) -> float:
        return round(sum(item.cut_duration for item in self.ranges), 3)

    @property
    def source_duration(self) -> float:
        return round(sum(item.duration for item in self.ranges), 3)

    def of_action(self, *actions: str) -> list[DirectorDecision]:
        wanted = set(actions)
        return [d for d in self.decisions if d.action in wanted]

    def decision(self, decision_id: str) -> Optional[DirectorDecision]:
        for entry in self.decisions:
            if entry.decision_id == decision_id:
                return entry
        return None

    def stats(self) -> dict:
        by_action: dict = {}
        by_reason: dict = {}
        for decision in self.decisions:
            by_action[decision.action] = by_action.get(decision.action, 0) + 1
            category = decision.reason.category
            by_reason[category] = by_reason.get(category, 0) + 1
        return {
            "decisions": len(self.decisions),
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "modified": len(self.modified),
            "needs_human_review": len(self.needs_human_review),
            "ranges": len(self.ranges),
            "cut_duration": self.cut_duration,
            "source_duration": self.source_duration,
            "compression": (round(self.cut_duration / self.source_duration, 3)
                            if self.source_duration else 0.0),
            "by_action": by_action,
            "by_reason": by_reason,
            "protected_ranges": sum(1 for r in self.ranges if r.protected),
            "sped_ranges": sum(1 for r in self.ranges if r.speed != 1.0),
            "mock": self.mock,
            "cached": self.cached,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "episode_id": self.episode_id,
            "mode": self.mode,
            "generated_at": self.generated_at,
            "backend": self.backend,
            "model": self.model,
            "mock": self.mock,
            "cached": self.cached,
            "approach": self.approach,
            "elapsed": round(self.elapsed, 3),
            "context_fingerprint": self.context_fingerprint,
            "context_stats": dict(self.context_stats),
            "sources": dict(self.sources),
            "config": self.config.to_dict(),
            "style_guide": self.style_guide.to_dict(),
            "stats": self.stats(),
            "safety": self.safety.to_dict(),
            "decisions": [d.to_dict() for d in self.decisions],
            "ranges": [r.to_dict() for r in self.ranges],
            "prompt": self.prompt.to_dict() if self.prompt else None,
            "warnings": list(self.warnings),
            "failure": self.failure.to_dict() if self.failure else None,
            "not_measured": self.not_measured,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DirectorPlan":
        data = data or {}
        prompt = data.get("prompt")
        return cls(
            name=_text(data.get("name"), 120) or "structure",
            episode_id=_text(data.get("episode_id"), 80),
            mode=coerce_one(data.get("mode"), MODES, "director"),
            config=DirectorConfig.from_dict(data.get("config")),
            style_guide=StyleGuide.from_dict(data.get("style_guide")),
            decisions=[
                DirectorDecision.from_dict(item)
                for item in _dicts(data.get("decisions"))
            ],
            ranges=[
                DirectorRange.from_dict(item)
                for item in _dicts(data.get("ranges"))
            ],
            safety=DirectorSafetyReview.from_dict(data.get("safety")),
            prompt=(DirectorPrompt.from_dict(prompt)
                    if isinstance(prompt, dict) else None),
            backend=_text(data.get("backend"), 40),
            model=_text(data.get("model"), 120),
            mock=as_bool(data.get("mock")),
            cached=as_bool(data.get("cached")),
            approach=_text(data.get("approach"), 2000),
            elapsed=as_float(data.get("elapsed")),
            context_fingerprint=_text(data.get("context_fingerprint"), 40),
            context_stats=dict(data.get("context_stats") or {}),
            sources=dict(data.get("sources") or {}),
            generated_at=_text(data.get("generated_at"), 40),
            warnings=as_str_list(data.get("warnings"), limit=100),
            failure=DirectorFailure.from_dict(data.get("failure")),
        )
