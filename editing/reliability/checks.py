"""The fifteen checks, one function each.

Each takes a :class:`GateInputs` and returns exactly one
:class:`GateResult`. They are pure functions of numbers, which is what makes
them cheap to test: a situation is six assignments rather than a pipeline.

## The bar for failing

A gate **fails** only when the run's output is not a usable thing. In practice
that is a very short list: no footage, a render that claims a video it does not
have, a cut with no runtime, an episode compressed to nothing. Everything else
warns, because a check that stops an overnight run over a caption density is a
check somebody will disable, and a disabled check protects nothing.

A gate **skips** when the pass it is about did not run. "Captions are not too
dense" is not a true statement about a run with no captions -- it is a question
that does not apply, and saying ``pass`` would make a report of fifteen ticks
that means nothing.
"""
from __future__ import annotations

from editing.reliability.schema import GateInputs, GateResult, gate, skipped

#: A cut shorter than this is not an episode, it is a clip. Warned about
#: rather than failed: a deliberate short is a real thing to want.
SHORT_EPISODE = 30.0

#: A reshaped cut that kept less than this share of the cut it was built from
#: has not been compressed, it has been deleted.
MIN_RETENTION_SHARE = 0.35

#: A cut that uses less than this share of a long recording is either a very
#: aggressive edit or a threshold that is set wrong.
LOW_USE_SHARE = 0.05

#: Mean ASR confidence below which the story layers are reading noise.
LOW_CONFIDENCE = 0.55

#: Words a transcript needs before the story layers have anything to read.
THIN_TRANSCRIPT_WORDS = 40

#: A rendered file smaller than this is not a video, whatever the extension
#: says. A one-second 720p proxy is comfortably over a megabyte.
TINY_RENDER_MB = 0.25

#: How far the rendered runtime may drift from the planned one before it is
#: worth understanding, as a share.
DURATION_DRIFT = 0.05

#: And the floor under that share, so a one-second drift on a short cut is not
#: reported as a 20% error.
DURATION_DRIFT_FLOOR = 1.5

#: Captions above this rate stop being punctuation whatever the style says.
HARD_CAPTION_RATE = 6.0

#: Effects above this rate are the edit rather than marking it.
HARD_SFX_RATE = 6.0


def check_footage(inputs: GateInputs) -> GateResult:
    if not inputs.footage_files:
        return gate(
            "footage", "fail",
            "No footage was found, so nothing downstream means anything.",
            evidence={"folder": inputs.footage_folder, "files": 0},
            fix="Check the folder path and that it holds video files: "
                "python -m editing.cli discover --folder <folder>",
            can_continue=False,
        )
    if inputs.probe_errors:
        return gate(
            "footage", "warn",
            f"{inputs.probe_errors} of {inputs.footage_files} file(s) could "
            "not be probed, so their duration and streams are guesses.",
            evidence={"files": inputs.footage_files,
                      "probe_errors": inputs.probe_errors,
                      "seconds": round(inputs.footage_seconds, 1)},
            fix="Check those files open in a player. FFprobe failing usually "
                "means a truncated recording.",
        )
    return gate(
        "footage", "pass",
        f"{inputs.footage_files} file(s), "
        f"{inputs.footage_seconds / 60.0:.0f} minute(s) of footage.",
        evidence={"files": inputs.footage_files,
                  "seconds": round(inputs.footage_seconds, 1)},
    )


def check_transcript(inputs: GateInputs) -> GateResult:
    if not inputs.transcribed and not inputs.transcript_words:
        return gate(
            "transcript", "warn",
            "This run has no transcript, so every layer that reads words -- "
            "captions, the story layer, the retention planner -- worked "
            "blind.",
            evidence={"words": 0, "transcribed": False},
            fix="python -m editing.cli auto run --folder <folder> "
                "--transcribe",
        )
    if inputs.transcript_mock:
        return gate(
            "transcript", "warn",
            "The transcript was fabricated by the mock backend. Nothing in "
            "this run heard the footage.",
            evidence={"words": inputs.transcript_words, "mock": True},
            fix="Re-run without --transcribe-backend mock once "
                "faster-whisper is installed.",
        )
    if inputs.transcript_words < THIN_TRANSCRIPT_WORDS:
        return gate(
            "transcript", "warn",
            f"Only {inputs.transcript_words} word(s) were transcribed "
            f"{_across(inputs)}. That is not enough for the story layers to "
            "read.",
            evidence={"words": inputs.transcript_words,
                      "files": inputs.transcript_files,
                      "segments": inputs.speech_segments,
                      "failed": inputs.transcript_failed},
            fix="Check the footage actually has commentary on it, and that "
                "the microphone track survived: python -m editing.cli "
                "transcribe status",
        )
    if inputs.transcript_failed:
        return gate(
            "transcript", "warn",
            f"{inputs.transcript_failed} file(s) failed to transcribe; the "
            "rest produced "
            f"{inputs.transcript_words} word(s).",
            evidence={"words": inputs.transcript_words,
                      "failed": inputs.transcript_failed},
            fix="python -m editing.cli transcribe status",
        )
    return gate(
        "transcript", "pass",
        f"{inputs.transcript_words} word(s) {_across(inputs)}.",
        evidence={"words": inputs.transcript_words,
                  "files": inputs.transcript_files,
                  "segments": inputs.speech_segments},
    )


def _across(inputs: GateInputs) -> str:
    """"across three files", or "across nine segments", or neither.

    Which one is known depends on where the words came from: the
    transcription stage counts files, the timeline counts segments, and a run
    that got its transcript from an ``.srt`` beside the footage only has the
    second.
    """
    if inputs.transcript_files:
        return f"across {inputs.transcript_files} file(s)"
    if inputs.speech_segments:
        return f"across {inputs.speech_segments} segment(s)"
    return "in this episode"


def check_transcript_confidence(inputs: GateInputs) -> GateResult:
    if not inputs.transcript_words:
        return skipped(
            "transcript_confidence",
            "There is no transcript, so there is no confidence to judge.")
    if inputs.transcript_confidence < 0:
        return gate(
            "transcript_confidence", "skipped",
            "This transcript carries no confidence figures, so nothing can "
            "be said about how well it heard the footage.",
            evidence={"confidence": None, "words": inputs.transcript_words},
            fix="faster-whisper reports confidence; a hand-written SRT does "
                "not.",
        )
    if inputs.transcript_confidence < LOW_CONFIDENCE:
        return gate(
            "transcript_confidence", "warn",
            f"Mean speech confidence is {inputs.transcript_confidence:.2f}, "
            f"below {LOW_CONFIDENCE:.2f}. Captions and story findings built "
            "on this are reading a guess.",
            evidence={"confidence": round(inputs.transcript_confidence, 3),
                      "words": inputs.transcript_words},
            fix="Try a larger Whisper model, or supply a vocabulary prompt: "
                "python -m editing.cli transcribe folder <folder> "
                "--model medium",
        )
    return gate(
        "transcript_confidence", "pass",
        f"Mean speech confidence {inputs.transcript_confidence:.2f}.",
        evidence={"confidence": round(inputs.transcript_confidence, 3)},
    )


def check_hook(inputs: GateInputs) -> GateResult:
    if not inputs.retention_enabled and not inputs.hooks_found:
        return skipped(
            "hook",
            "The retention pass did not run, so no hook was looked for.")
    if not inputs.hooks_found:
        return gate(
            "hook", "warn",
            "No opening hook was found, so the episode opens on whatever "
            "happens to be first.",
            evidence={"hooks": 0},
            fix="python -m editing.cli episode show-hooks -- if the list is "
                "empty the footage may genuinely have no strong opening, and "
                "a hand-picked one is the fix.",
        )
    if inputs.retention_applied and not inputs.cold_open:
        return gate(
            "hook", "warn",
            f"{inputs.hooks_found} hook(s) were found and none was used, so "
            "the opening is unchanged.",
            evidence={"hooks": inputs.hooks_found, "cold_open": False},
            fix="python -m editing.cli retention show-rejected -- every "
                "refusal names the rule that made it.",
        )
    return gate(
        "hook", "pass",
        f"{inputs.hooks_found} hook candidate(s)"
        + (f", opening on one ({inputs.cold_open_seconds:.0f}s)."
           if inputs.cold_open else "."),
        evidence={"hooks": inputs.hooks_found,
                  "cold_open": inputs.cold_open,
                  "cold_open_seconds": round(inputs.cold_open_seconds, 1)},
    )


def check_director(inputs: GateInputs) -> GateResult:
    if not inputs.director_enabled:
        return skipped(
            "director",
            "The director pass was not asked for; the cut came from the "
            "rule-based selector.")
    if not inputs.director_ran:
        return gate(
            "director", "warn",
            "A director pass was asked for and did not run, so the cut was "
            "chosen by thresholds.",
            evidence={"ran": False},
            fix="python -m editing.cli director status",
        )
    if not inputs.director_accepted:
        return gate(
            "director", "warn",
            f"The director made {inputs.director_decisions} decision(s) and "
            "the rules accepted none of them, so the cut is entirely the "
            "rule-based one.",
            evidence={"decisions": inputs.director_decisions, "accepted": 0},
            fix="python -m editing.cli director show-rejected --run "
                f"{inputs.run_id or '<run_id>'}",
        )
    if inputs.director_mock:
        return gate(
            "director", "warn",
            f"{inputs.director_accepted} decision(s) were accepted, but the "
            "director was in mock mode: they came from four fixed rules and "
            "not from a model.",
            evidence={"accepted": inputs.director_accepted, "mock": True},
            fix="Point --director-backend at a real endpoint and re-run.",
        )
    return gate(
        "director", "pass",
        f"{inputs.director_accepted} of {inputs.director_decisions} director "
        "decision(s) survived the rules.",
        evidence={"decisions": inputs.director_decisions,
                  "accepted": inputs.director_accepted},
    )


def check_retention_length(inputs: GateInputs) -> GateResult:
    if not inputs.retention_applied:
        return skipped(
            "retention_length",
            "Nothing was reshaped, so there is no new length to judge.")
    if inputs.cut_duration <= 0:
        return gate(
            "retention_length", "fail",
            "The reshaped cut has no runtime at all.",
            evidence={"cut_duration": 0.0,
                      "base_duration": round(inputs.base_duration, 1)},
            fix="python -m editing.cli retention plan --mode report_only "
                "-- decide everything and change nothing, then read the "
                "refusals.",
            can_continue=False,
        )
    if inputs.base_duration > 0:
        share = inputs.cut_duration / inputs.base_duration
        if share < MIN_RETENTION_SHARE:
            return gate(
                "retention_length", "warn",
                f"The reshaped cut is {share:.0%} of the cut it was built "
                f"from ({inputs.cut_duration:.0f}s of "
                f"{inputs.base_duration:.0f}s). That is not compressing a "
                "sag.",
                evidence={"share": round(share, 3),
                          "cut_duration": round(inputs.cut_duration, 1),
                          "base_duration": round(inputs.base_duration, 1)},
                fix="Lower the compression ceiling, or run "
                    "--retention-mode report_only and read what it removed.",
            )
    if inputs.cut_duration < SHORT_EPISODE:
        return gate(
            "retention_length", "warn",
            f"The reshaped cut is {inputs.cut_duration:.0f}s, which is a clip "
            "rather than an episode.",
            evidence={"cut_duration": round(inputs.cut_duration, 1)},
            fix="Check the keep threshold: python -m editing.cli roughcut "
                "build --keep-threshold 0.2",
        )
    return gate(
        "retention_length", "pass",
        f"{inputs.cut_duration:.0f}s, from a "
        f"{inputs.base_duration:.0f}s base cut.",
        evidence={"cut_duration": round(inputs.cut_duration, 1),
                  "base_duration": round(inputs.base_duration, 1)},
    )


def check_cold_open_duplicate(inputs: GateInputs) -> GateResult:
    if not inputs.cold_open:
        return skipped(
            "cold_open_duplicate",
            "No cold open was used, so no footage could be duplicated by "
            "one.")
    if inputs.duplicate_seconds > 0.5:
        return gate(
            "cold_open_duplicate", "warn",
            f"{inputs.duplicate_seconds:.1f}s of the cold open plays again "
            "later in the cut. Deliberate that reads as a teaser; accidental "
            "it reads as a mistake.",
            evidence={"duplicate_seconds": round(inputs.duplicate_seconds, 2),
                      "cold_open_seconds": round(inputs.cold_open_seconds, 1)},
            fix="python -m editing.cli retention show-cold-open --run "
                f"{inputs.run_id or '<run_id>'}",
        )
    return gate(
        "cold_open_duplicate", "pass",
        f"The opening ({inputs.cold_open_seconds:.0f}s) was moved, not "
        "copied.",
        evidence={"duplicate_seconds": 0.0,
                  "cold_open_seconds": round(inputs.cold_open_seconds, 1)},
    )


def check_story_warnings(inputs: GateInputs) -> GateResult:
    if not inputs.retention_ran:
        return skipped(
            "story_warnings",
            "The retention pass did not run, so setups and payoffs were "
            "never paired.")
    if inputs.unresolved_warnings:
        return gate(
            "story_warnings", "warn",
            f"{inputs.unresolved_warnings} unresolved story warning(s): a "
            "payoff without its setup, or a setup that never pays off.",
            evidence={"unresolved": inputs.unresolved_warnings},
            fix="python -m editing.cli episode show-open-loops -- then watch "
                "those moments in the proxy before trusting the cut.",
        )
    return gate(
        "story_warnings", "pass",
        "No unpaired setup or payoff was reported.",
        evidence={"unresolved": 0},
    )


def check_compression(inputs: GateInputs) -> GateResult:
    if inputs.cut_duration <= 0:
        return gate(
            "compression", "fail",
            "The cut has no runtime, so there is no episode here.",
            evidence={"cut_duration": 0.0, "clips": inputs.clips},
            fix="python -m editing.cli roughcut build --keep-threshold 0.2",
            can_continue=False,
        )
    if inputs.source_duration > 0:
        share = inputs.cut_duration / inputs.source_duration
        if share < LOW_USE_SHARE:
            return gate(
                "compression", "warn",
                f"The cut keeps {share:.1%} of the footage it drew on "
                f"({inputs.cut_duration:.0f}s of "
                f"{inputs.source_duration:.0f}s). Either the recording is "
                "mostly nothing, or the thresholds are too tight.",
                evidence={"share": round(share, 4),
                          "cut_duration": round(inputs.cut_duration, 1),
                          "source_duration": round(inputs.source_duration, 1)},
                fix="python -m editing.cli roughcut build "
                    "--keep-threshold 0.25",
            )
    if inputs.cut_duration < SHORT_EPISODE:
        return gate(
            "compression", "warn",
            f"The cut is {inputs.cut_duration:.0f}s across {inputs.clips} "
            "clip(s), which is short enough to be worth checking on purpose.",
            evidence={"cut_duration": round(inputs.cut_duration, 1),
                      "clips": inputs.clips},
            fix="python -m editing.cli roughcut placements",
        )
    return gate(
        "compression", "pass",
        f"{inputs.cut_duration:.0f}s from {inputs.clips} clip(s).",
        evidence={"cut_duration": round(inputs.cut_duration, 1),
                  "clips": inputs.clips,
                  "source_duration": round(inputs.source_duration, 1)},
    )


def check_caption_density(inputs: GateInputs) -> GateResult:
    if not inputs.captions_enabled:
        return skipped(
            "caption_density",
            "Captions are off for this run, so there is no density to check.")
    if not inputs.captions_placed:
        return gate(
            "caption_density", "pass",
            "No caption cleared every rule. That is a normal result for a "
            "pass that only captions key moments.",
            evidence={"placed": 0},
        )
    if inputs.captions_per_minute > HARD_CAPTION_RATE:
        return gate(
            "caption_density", "warn",
            f"{inputs.captions_per_minute:.1f} captions a minute is one "
            "every ten seconds. A viewer is reading rather than watching.",
            evidence={"per_minute": round(inputs.captions_per_minute, 2),
                      "ceiling": round(inputs.caption_ceiling, 2),
                      "placed": inputs.captions_placed},
            fix="Use --captions key_moments, or lower "
                "--max-captions-per-minute.",
        )
    if (inputs.caption_ceiling > 0
            and inputs.captions_per_minute > inputs.caption_ceiling + 0.05
            and inputs.captions_placed > 1):
        return gate(
            "caption_density", "warn",
            f"{inputs.captions_per_minute:.2f} captions a minute is above "
            f"this style's own ceiling of {inputs.caption_ceiling:.2f}.",
            evidence={"per_minute": round(inputs.captions_per_minute, 2),
                      "ceiling": round(inputs.caption_ceiling, 2),
                      "placed": inputs.captions_placed},
            fix="Read the caption plan: the budget floor allows one caption "
                "on a short cut, which can read above the rate.",
        )
    return gate(
        "caption_density", "pass",
        f"{inputs.captions_placed} caption(s), "
        f"{inputs.captions_per_minute:.2f} a minute.",
        evidence={"placed": inputs.captions_placed,
                  "per_minute": round(inputs.captions_per_minute, 2),
                  "longest_seconds": round(inputs.longest_caption, 2)},
    )


def check_sfx_density(inputs: GateInputs) -> GateResult:
    if not inputs.audio_enabled:
        return skipped(
            "sfx_density",
            "Audio polish is off for this run, so nothing was placed.")
    if inputs.sfx_per_minute > HARD_SFX_RATE:
        return gate(
            "sfx_density", "warn",
            f"{inputs.sfx_per_minute:.1f} effects a minute stops marking "
            "moments and starts being the edit.",
            evidence={"per_minute": round(inputs.sfx_per_minute, 2),
                      "ceiling": round(inputs.sfx_ceiling, 2),
                      "placed": inputs.cues_placed},
            fix="Lower --max-sfx-per-minute, or pick a quieter style.",
        )
    if (inputs.sfx_ceiling > 0
            and inputs.sfx_per_minute > inputs.sfx_ceiling + 0.05
            and inputs.effects_placed > 1):
        return gate(
            "sfx_density", "warn",
            f"{inputs.sfx_per_minute:.2f} effects a minute is above this "
            f"style's ceiling of {inputs.sfx_ceiling:.2f}.",
            evidence={"per_minute": round(inputs.sfx_per_minute, 2),
                      "ceiling": round(inputs.sfx_ceiling, 2)},
            fix="Read the audio plan: the budget floor allows one effect on "
                "a short cut, which can read above the rate.",
        )
    return gate(
        "sfx_density", "pass",
        f"{inputs.cues_placed} cue(s), {inputs.sfx_per_minute:.2f} effect(s) "
        "a minute.",
        evidence={"placed": inputs.cues_placed,
                  "effects": inputs.effects_placed,
                  "per_minute": round(inputs.sfx_per_minute, 2)},
    )


def check_missing_assets(inputs: GateInputs) -> GateResult:
    if not inputs.audio_enabled:
        return skipped(
            "missing_assets",
            "Audio polish is off, so nothing needed an asset.")
    if inputs.audio_mode == "placeholders":
        return gate(
            "missing_assets", "skipped",
            "Audio polish is in placeholder mode: no library was read and "
            "nothing plays, by design.",
            evidence={"mode": "placeholders", "cues": inputs.cues_placed},
        )
    if inputs.missing_assets:
        return gate(
            "missing_assets", "warn",
            f"{inputs.missing_assets} cue(s) have no sound behind them. They "
            "are notes about the edit, not something that will play.",
            evidence={"missing": inputs.missing_assets,
                      "cues": inputs.cues_placed},
            fix="python -m editing.cli polish show-missing --run "
                f"{inputs.run_id or '<run_id>'} -- that list is a shopping "
                "list.",
        )
    return gate(
        "missing_assets", "pass",
        f"Every one of the {inputs.cues_placed} cue(s) has a file behind it.",
        evidence={"missing": 0, "cues": inputs.cues_placed},
    )


def check_render_output(inputs: GateInputs) -> GateResult:
    if not inputs.render_enabled:
        return skipped(
            "render_output",
            "No render was asked for, so there is no video to check.")
    if inputs.render_mock:
        return gate(
            "render_output", "warn",
            "The render ran in mock mode. The file it wrote is a placeholder "
            "and no video exists.",
            evidence={"mock": True, "path": inputs.render_path},
            fix="Install FFmpeg and re-run: python -m editing.cli render "
                "roughcut --run " + (inputs.run_id or "<run_id>"),
        )
    if not inputs.render_ran:
        return gate(
            "render_output", "warn",
            "The render was asked for and did not run, so there is nothing "
            "to watch.",
            evidence={"ran": False},
            fix="python -m editing.cli auto explain-failure --run "
                + (inputs.run_id or "<run_id>"),
        )
    if inputs.render_claimed and not inputs.render_exists:
        return gate(
            "render_output", "fail",
            "The run says it rendered a video and the file is not there.",
            evidence={"path": inputs.render_path, "exists": False},
            fix="python -m editing.cli render roughcut --run "
                + (inputs.run_id or "<run_id>") + " --force",
            can_continue=False,
        )
    return gate(
        "render_output", "pass",
        f"A video exists at {inputs.render_path}.",
        evidence={"path": inputs.render_path, "exists": True,
                  "size_mb": round(inputs.render_size_mb, 2)},
    )


def check_render_size(inputs: GateInputs) -> GateResult:
    if not inputs.render_enabled or not inputs.render_exists:
        return skipped(
            "render_size",
            "There is no rendered file to measure.")
    if inputs.render_mock:
        return skipped(
            "render_size",
            "The render was mocked, so its size means nothing.")
    if inputs.render_size_mb < TINY_RENDER_MB:
        return gate(
            "render_size", "fail",
            f"The rendered file is {inputs.render_size_mb:.2f} MB, which is "
            "too small to be video.",
            evidence={"size_mb": round(inputs.render_size_mb, 3),
                      "floor_mb": TINY_RENDER_MB,
                      "path": inputs.render_path},
            fix="python -m editing.cli render show --run "
                + (inputs.run_id or "<run_id>")
                + " -- the FFmpeg commands and their output are in the job "
                  "folder.",
            can_continue=False,
        )
    return gate(
        "render_size", "pass",
        f"{inputs.render_size_mb:.1f} MB.",
        evidence={"size_mb": round(inputs.render_size_mb, 2)},
    )


def check_output_duration(inputs: GateInputs) -> GateResult:
    if not inputs.render_enabled or not inputs.render_ran:
        return skipped(
            "output_duration",
            "Nothing was rendered, so there is no runtime to compare.")
    planned = inputs.render_planned_duration or inputs.cut_duration
    if planned <= 0 or inputs.render_duration <= 0:
        return gate(
            "output_duration", "skipped",
            "Either the planned or the rendered runtime is unknown, so the "
            "two cannot be compared.",
            evidence={"planned": round(planned, 2),
                      "rendered": round(inputs.render_duration, 2)},
        )
    drift = abs(inputs.render_duration - planned)
    if drift > max(DURATION_DRIFT_FLOOR, planned * DURATION_DRIFT):
        return gate(
            "output_duration", "warn",
            f"The video runs {inputs.render_duration:.1f}s against "
            f"{planned:.1f}s planned -- {drift:.1f}s of drift. Timing read "
            "off this proxy is not exact.",
            evidence={"planned": round(planned, 2),
                      "rendered": round(inputs.render_duration, 2),
                      "drift": round(drift, 2)},
            fix="Speed changes in the proxy are setpts/atempo rather than "
                "Premiere's retime; check the clips that were sped up.",
        )
    return gate(
        "output_duration", "pass",
        f"{inputs.render_duration:.1f}s rendered against {planned:.1f}s "
        "planned.",
        evidence={"planned": round(planned, 2),
                  "rendered": round(inputs.render_duration, 2),
                  "drift": round(drift, 2)},
    )


#: Gate name -> the function that answers it. Kept as a table so the report,
#: the CLI and the tests all iterate the same list and a new gate is one entry.
def check_conform_executed(inputs: GateInputs) -> GateResult:
    """Did the decisions reach a timeline, or stop at a plan?

    The check this whole system was missing. Every gate above it can pass on a
    run that produced nothing anybody can watch, because they measure plans.
    This one measures whether the plans became operations and whether Premiere
    accepted them.
    """
    if not inputs.conform_enabled:
        return skipped(
            "conform_executed",
            "The conform pass did not run, so every decision this run made is "
            "still a plan.")
    if not inputs.conform_operations:
        return gate(
            "conform_executed", "warn",
            "The conform pass ran and produced no operations, so nothing from "
            "the caption, sound or visual passes can reach the timeline.",
            evidence={"unconverted": inputs.conform_unconverted},
            fix="python -m editing.cli conform unconverted --run "
                + (inputs.run_id or "<run_id>"),
        )
    if not inputs.conform_executed:
        return gate(
            "conform_executed", "warn",
            f"{inputs.conform_operations} operation(s) are validated and "
            "waiting; nothing has been applied to a timeline yet.",
            evidence={"operations": inputs.conform_operations,
                      "contributions": dict(inputs.conform_contributions)},
            fix="python -m editing.cli auto execute-stage conform --run "
                + (inputs.run_id or "<run_id>") + " --yes",
        )
    if inputs.conform_applied < inputs.conform_operations:
        missed = inputs.conform_operations - inputs.conform_applied
        return gate(
            "conform_executed", "warn",
            f"{inputs.conform_applied} of {inputs.conform_operations} "
            f"operation(s) landed; {missed} failed inside Premiere.",
            evidence={"applied": inputs.conform_applied,
                      "attempted": inputs.conform_operations},
            fix="python -m editing.cli conform report --run "
                + (inputs.run_id or "<run_id>"),
        )
    return gate(
        "conform_executed", "pass",
        f"All {inputs.conform_applied} operation(s) are on the timeline: "
        + ", ".join(f"{layer} {count}" for layer, count
                    in sorted(inputs.conform_contributions.items())),
        evidence={"applied": inputs.conform_applied,
                  "contributions": dict(inputs.conform_contributions)},
    )


def check_delivered(inputs: GateInputs) -> GateResult:
    """Is there a finished video, and is it really there?"""
    if not inputs.delivery_path:
        return skipped(
            "delivered",
            "No export was asked for, so there is no finished video to check.")
    if not inputs.delivered:
        return gate(
            "delivered", "fail",
            "An export was attempted and no usable file exists.",
            evidence={"path": inputs.delivery_path,
                      "error": inputs.delivery_error},
            fix="python -m editing.cli deliver --run "
                + (inputs.run_id or "<run_id>"),
            can_continue=False,
        )
    if inputs.delivery_size_mb < TINY_RENDER_MB:
        return gate(
            "delivered", "warn",
            f"The exported file is {inputs.delivery_size_mb:.1f} MB, which is "
            "small enough to suspect an empty or failed render.",
            evidence={"path": inputs.delivery_path,
                      "size_mb": round(inputs.delivery_size_mb, 2)},
        )
    return gate(
        "delivered", "pass",
        f"A finished video exists at {inputs.delivery_path} "
        f"({inputs.delivery_size_mb:.1f} MB, "
        f"{inputs.delivery_duration:.1f}s).",
        evidence={"path": inputs.delivery_path,
                  "size_mb": round(inputs.delivery_size_mb, 2),
                  "duration": round(inputs.delivery_duration, 2)},
    )


CHECKS = {
    "footage": check_footage,
    "transcript": check_transcript,
    "transcript_confidence": check_transcript_confidence,
    "hook": check_hook,
    "director": check_director,
    "retention_length": check_retention_length,
    "cold_open_duplicate": check_cold_open_duplicate,
    "story_warnings": check_story_warnings,
    "compression": check_compression,
    "caption_density": check_caption_density,
    "sfx_density": check_sfx_density,
    "missing_assets": check_missing_assets,
    "render_output": check_render_output,
    "render_size": check_render_size,
    "output_duration": check_output_duration,
    "conform_executed": check_conform_executed,
    "delivered": check_delivered,
}
