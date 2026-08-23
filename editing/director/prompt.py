"""What the director is actually asked.

Two halves, kept apart on purpose.

``render_context`` turns a ``DirectorContext`` into text. It is pure
formatting: no instruction, no opinion, and it is also what the context
builder measures its budget against, which is why it lives here rather than
next to the prompt strings.

``build`` wraps that in the instruction. The instruction is the part with
opinions in it, and the opinions are these:

* **Be an editor, not a summariser.** The failure mode of a model given an
  episode and asked for decisions is a beautiful description of the episode
  and twelve decisions that all say "keep". So the prompt asks for a target
  shape, insists on cuts, and gives it a vocabulary of actions rather than a
  free-text field.
* **Cite the segment, never the timestamp.** Every decision must name segment
  IDs from the context. This is the anti-hallucination guarantee, and it is
  cheap: a model that invents ``seg_9999`` produces a decision that resolves
  to nothing and is rejected with a reason, rather than a cut of footage that
  does not exist.
* **Say why, in a closed vocabulary.** ``reason.category`` is one of sixteen
  words so fifty decisions can be counted; ``reason.text`` is the sentence a
  person reads.
* **Never claim analytics.** The prompt says so explicitly, because a model
  asked about retention will happily write "this will improve retention by
  15%", and that sentence in a report is worse than no report.
* **JSON only.** With an example, because an example is worth three sentences
  of schema description to every model anybody will point at this.
"""
from __future__ import annotations

from typing import Optional

from editing.director.schema import (
    ACTIONS, NOT_MEASURED, REASON_CATEGORIES, VIEWER_EFFECTS, DirectorConfig,
    DirectorContext, DirectorPrompt,
)

#: Rough characters-per-token. Used only to say "this prompt is enormous"
#: before a call, never for billing.
CHARS_PER_TOKEN = 3.6

SYSTEM = """\
You are an experienced YouTube video editor cutting a Minecraft episode. You \
are not a summariser and not an assistant: you are the person who decides what \
ends up in the video.

You are given a structured description of everything in the raw footage: \
candidate ranges with what is on screen and what was said, the story the \
episode tells, where it sags, and what a simple rule-based selector already \
thinks. You decide what the cut is.

Judge like an editor:
- Does the opening earn the next thirty seconds?
- Is a viewer curious about anything, and does the episode answer it?
- Which dull-looking stretch is load-bearing setup, and which is just dull?
- Where does the pacing sag, and what would you take out to fix it?
- Is the same beat happening three times? Cut two of them.
- Does a joke need the moment before it to land? Keep both.
- Does the episode escalate towards something, and does it end on it?
- Would a viewer who joined at minute six understand what is happening?

Rules you must follow:
1. Answer with a single JSON object and nothing else. No prose before or \
after, no markdown fence.
2. Every decision must name one or more segment ids from the CANDIDATE RANGES \
list, exactly as written. Never invent an id, and never give a decision \
without one.
3. Never write timestamps as the location of a decision. The segment ids are \
the location.
4. Make real cuts. A cut that keeps everything is not a cut. Say what goes as \
clearly as what stays.
5. Never speed up a range where somebody is talking.
6. Never claim to know what viewers will do. You may say a decision opens a \
question or keeps momentum. You may not predict retention, watch time or \
audience numbers, and you may not give any figure for them.
7. If you are not sure, use the action needs_human_review and say what you \
would need to know. That is a better answer than a confident guess.
8. Follow the style guide. When a decision follows a specific line of it, \
quote that line in reason.style_rule.\
"""

#: The output contract. Given as an example rather than as a description,
#: because every model handles an example better than a schema.
OUTPUT_SHAPE = """\
{
  "approach": "One or two sentences on the shape of the cut you are making.",
  "decisions": [
    {
      "segment_ids": ["seg_0012"],
      "action": "hook",
      "reason": {
        "category": "hook_strength",
        "text": "The creeper explosion is the strongest thing in the episode \
and it currently sits nine minutes in. Open on it.",
        "style_rule": "The first fifteen seconds have to earn the next thirty."
      },
      "confidence": 0.8,
      "priority": 0.9,
      "viewer_effect": "opens_a_question",
      "evidence": ["seg_0012", "audio: possible_scream"],
      "order": 0
    },
    {
      "segment_ids": ["seg_0003", "seg_0004", "seg_0005"],
      "action": "speed_up",
      "speed": 2.0,
      "reason": {
        "category": "boring_repetition",
        "text": "Three minutes of tunnelling with nothing said over it.",
        "style_rule": "Cut grind to under twenty seconds."
      },
      "confidence": 0.75,
      "priority": 0.5,
      "viewer_effect": "removes_a_dull_stretch"
    },
    {
      "segment_ids": ["seg_0021"],
      "action": "setup",
      "reason": {
        "category": "setup_payoff",
        "text": "Looks like nothing, but it is where the diamonds are put in \
the chest that gets blown up at the end.",
        "style_rule": "Protect setups."
      },
      "confidence": 0.7,
      "priority": 0.8,
      "payoff_id": "pay_0004",
      "viewer_effect": "protects_a_payoff"
    }
  ]
}\
"""


def render_context(context: DirectorContext) -> str:
    """The context as the model sees it. Pure formatting, no instruction."""
    lines: list[str] = []
    add = lines.append

    add("# EPISODE")
    add(context.summary or "(no summary available)")
    add("")
    add(f"Total footage: {context.duration:.0f}s "
        f"({context.duration / 60:.1f} minutes)")
    if context.objective:
        add(f"Stated objective: {context.objective}"
            + (f"  [{context.objective_status}]"
               if context.objective_status else ""))
    else:
        add("Stated objective: none was found in what was said.")
    missing = [name for name, present in context.sources.items()
               if not present]
    if missing:
        add(f"Not available: {', '.join(sorted(missing))}")
    add("")

    if context.style_summary:
        add("# WHAT HAPPENS AFTER YOUR CUT")
        add(context.style_summary)
        add("")

    if context.beats:
        add("# STORY BEATS")
        for beat in context.beats:
            add(f"- [{beat['id']}] {beat['start']:.0f}-{beat['end']:.0f}s  "
                f"{beat['kind']}: {beat['why']}")
        add("")

    if context.open_loops:
        add("# OPEN QUESTIONS")
        add("Questions the episode raises. An unanswered one is a problem.")
        for loop in context.open_loops:
            state = (f"answered at {loop['resolved_at']:.0f}s"
                     if loop.get("resolved") else "NEVER ANSWERED")
            add(f"- [{loop['id']}] asked at {loop['opened_at']:.0f}s, {state}: "
                f"{loop['question']}")
        add("")

    if context.setups or context.payoffs:
        add("# SETUP AND PAYOFF")
        for setup in context.setups:
            link = (f" -> paid off by {setup['payoff_id']}"
                    if setup.get("payoff_id") else " -> NEVER PAID OFF")
            add(f"- SETUP [{setup['id']}] {setup['start']:.0f}s: "
                f"{setup['what']}{link}")
        for payoff in context.payoffs:
            link = (f" <- set up by {payoff['setup_id']}"
                    if payoff.get("setup_id") else " <- no setup found")
            add(f"- PAYOFF [{payoff['id']}] {payoff['start']:.0f}s: "
                f"{payoff['what']}{link}")
        add("")

    if context.callbacks:
        add("# CALLBACK OPPORTUNITIES")
        for item in context.callbacks:
            add(f"- [{item['id']}] {item['start']:.0f}s: {item['what']}")
        add("")

    if context.risks:
        add("# WHERE IT SAGS")
        for risk in context.risks:
            add(f"- [{risk['id']}] {risk['start']:.0f}-{risk['end']:.0f}s  "
                f"{risk['risk']} ({risk['severity']}): {risk['why']}")
        add("")

    if context.hook_candidates:
        add("# HOOK CANDIDATES")
        add("Ranked by a simple score. You are not obliged to agree with it.")
        for hook in context.hook_candidates:
            add(f"- [{hook['id']}] {hook['start']:.0f}-{hook['end']:.0f}s  "
                f"score {hook['score']}: {hook['why']}")
        add("")

    if context.climax or context.ending:
        add("# PEAK AND ENDING")
        if context.climax:
            add(f"- PEAK [{context.climax.get('id', '')}] "
                f"{context.climax.get('start', 0):.0f}s: "
                f"{context.climax.get('why', '')}")
        if context.ending:
            add(f"- ENDING [{context.ending.get('id', '')}] "
                f"{context.ending.get('start', 0):.0f}s: "
                f"{context.ending.get('why', '')}")
        add("")

    if context.recommendations:
        add("# WHAT THE RULE-BASED PASS ALREADY SUGGESTS")
        add("Existing suggestions. Disagree with them where you should.")
        for entry in context.recommendations:
            add(f"- [{entry['id']}] {entry['start']:.0f}-{entry['end']:.0f}s  "
                f"{entry['category']} (priority {entry['priority']}): "
                f"{entry['why']}")
        add("")

    if context.preferences:
        add("# WHAT THIS EDITOR HAS SAID BEFORE")
        add("Opinions collected from earlier cuts. Evidence about taste, not "
            "instructions.")
        for statement in context.preferences:
            add(f"- {statement}")
        add("")

    add("# STYLE GUIDE")
    add(context.style_guide.text or "(no style guide)")
    add("")

    add("# CANDIDATE RANGES")
    add("Every range you may use, in order. Use these ids and no others.")
    add("Format: [id] start-end (duration, position in episode) importance  "
        "environment  actions  audio  beat  heur:<what the rule-based "
        "selector would do>")
    add("")
    for segment in context.segments:
        add(segment.line())
    add("")

    if context.dropped:
        add("# WHAT WAS LEFT OUT OF THIS BRIEF")
        for entry in context.dropped:
            add(f"- {entry}")
        add("")
    return "\n".join(lines)


def build(
    context: DirectorContext, config: Optional[DirectorConfig] = None
) -> DirectorPrompt:
    """The full prompt for one director pass."""
    config = (config or DirectorConfig()).validated()
    body = render_context(context)

    task: list[str] = []
    add = task.append
    add("# YOUR TASK")
    add("")
    add("Decide what this episode's cut is. Work through the candidate ranges "
        "and make a decision about the ones that matter -- you do not need a "
        "decision for every range, and a range you say nothing about keeps "
        "whatever the rule-based selector would have done with it.")
    add("")

    if config.target_duration:
        add(f"Target runtime: about {config.target_duration:.0f} seconds "
            f"({config.target_duration / 60:.1f} minutes). The raw footage is "
            f"{context.duration / 60:.1f} minutes, so most of it has to go.")
    if config.max_duration:
        add(f"Hard maximum runtime: {config.max_duration:.0f} seconds. A cut "
            "longer than this will be trimmed by a rule you do not control, "
            "so decide what goes yourself.")
    if config.target_duration or config.max_duration:
        add("")

    add("Actions available:")
    add("  keep       -- use this range as it is")
    add("  cut        -- do not use it")
    add("  shorten    -- use part of it; give out_start and out_end in "
        "seconds within the range")
    add("  speed_up   -- use it retimed; give speed (2.0 is the usual). "
        "Never over speech.")
    add("  hold       -- use it and protect it: no retime, no effects")
    add("  hook       -- use it, and use it first")
    add("  setup      -- keep it because something later needs it")
    add("  payoff     -- keep it protected; this is what the episode built to")
    add("  callback   -- keep it, and mark that it calls back to something")
    add("  marker_only -- change no footage; leave a note for the human editor")
    add("  needs_human_review -- you are not sure and want a person to look")
    add("")
    add(f"Ceilings: at most {config.max_hooks_in_cut} hook decision(s) and "
        f"{config.max_callbacks_in_cut} callback(s) in the whole cut. Going "
        "over means a rule picks which survive, so choose.")
    add("")
    add("reason.category must be one of: " + ", ".join(REASON_CATEGORIES))
    add("viewer_effect must be one of: " + ", ".join(VIEWER_EFFECTS))
    add("action must be one of: " + ", ".join(ACTIONS))
    add("")
    add("confidence is how sure you are this is the right call (0..1). "
        "priority is how much this decision matters relative to your others "
        "(0..1).")
    add("")
    add(NOT_MEASURED)
    add("")
    add("Answer with exactly this shape, and nothing else:")
    add("")
    add(OUTPUT_SHAPE)

    user = body + "\n\n" + "\n".join(task)
    return DirectorPrompt(
        system=SYSTEM,
        user=user,
        context_fingerprint=context.fingerprint(),
        style_guide_fingerprint=context.style_guide.fingerprint(),
        approx_tokens=int((len(SYSTEM) + len(user)) / CHARS_PER_TOKEN),
    )
