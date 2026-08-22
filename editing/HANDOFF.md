# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-22 (Session 10A).

---

## Start here

```cmd
cd /d E:\Assistant
python -m pytest tests/editing -q --basetemp=%TEMP%\pt    REM expect 1318 passed

REM hear the footage first -- the story layer is blind without this:
pip install faster-whisper
python -m editing.cli transcribe folder D:\Footage\test --model small

REM the whole pipeline, planning only, with nothing installed:
python -m editing.cli auto run --folder D:\Footage\test --mock --no-premiere --transcribe
python -m editing.cli auto report

REM then tell it what you think of the result:
python -m editing.cli feedback start
python -m editing.cli feedback queue --limit 20
```

Everything below is detail behind that.

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all ten sessions and 1318 passing editing tests.

**Do not build on `vishal-session3-roughcut`.** It has the Session 3 rough-cut
code but is missing the entire `editing/audio/` and `editing/recommend/`
packages that Session 3 imports from. It cannot import, let alone run:

```
editing/roughcut/build.py:13  from editing.recommend.schema import RecommendationSet
                              ^ that package is not on that branch
```

It looks like a squash or cherry-pick that dropped Session 2. Recover it with:

```
git checkout vishal-session3-roughcut
git reset --hard origin/claude/editing-brain-v1-structure-7p33pm
git push --force-with-lease origin vishal-session3-roughcut
```

---

## What was built, in ten sessions

```
footage → Premiere mapping → transcript → Qwen3-VL vision ─┐
                                                            ├→ structure timeline
                                            audio events ──┘
                                                    ↓
                       six recommendation layers → safety pass
                                                    ↓
                     rough cut: selected ranges → scratch sequence plan
                                                    ↓
             review frames → Qwen3-VL critic → revision recommendations
                                                    ↓
        style preset → seven layers: captions, emphasis, audio placeholders,
        title/chapter cards, structure and polish markers
                                                    ↓
       local asset library matched per placeholder → real SFX, music beds,
       ambience and graphics on their own tracks
                                                    ↓
   every pass: offline dry run → (explicit per-stage --yes) applied to the
                                 same scratch sequence
                                                    ↓
      Session 7 wraps all of it: one command, eighteen checkpointed stages,
      four named execution gates, resumable, with a report that says what it
      did not do
                                                    ↓
     Session 8 reads the whole thing as one episode: beats, objectives, open
     loops, callbacks → risk zones, hook candidates, a peak, an ending →
     suggestions a later pass can consume. Executes nothing.
                                                    ↓
     Session 9 turns all of that around and asks *you*: a ranked review queue
     over every decision → ratings, reasons, corrections, appended to a log
     that is never rewritten → preference signals, training signals, exports.
     Trains nothing.
```

| Session | Package | What it added |
|---|---|---|
| 1 | `discovery`, `transcripts/`, `visual/`, `align`, `cache` | the structure timeline |
| 2 | `audio/`, `recommend/` | audio events, six recommendation layers, a safety pass |
| 3 | `roughcut/` | selection, layout maths, catalog ops, four execution modes |
| 4 | `critic/` | coverage frames, the Qwen3-VL critic, one revision pass |
| 5 | `style/` | four presets, seven layers, density ceilings, additive-only |
| 6 | `assets/` | local library, sidecars, matching, real SFX/music/graphics |
| 7 | `auto/` | orchestration, checkpoints, resume, gated execution, reports |
| 8 | `episode/` | story beats, open loops, retention risks, hooks, suggestions |
| 9 | `feedback/` | the review queue, the append-only log, preference and training signals |
| 10A | `transcribe/` | local faster-whisper, the cache, batches, the durable-transcript seam |

### Session 7 — the auto pipeline

- `editing/auto/`
  - `schema.py` — `AutoRunConfig`, `AutoStage`, `AutoStageResult`,
    `AutoCheckpoint`, `AutoRunState`, `AutoExecutionGate`, `AutoFailure`,
    `AutoRunReport`, and the operation risk table
  - `store.py` — the run folder, run IDs, atomic state, listing, cleaning
  - `stages.py` — the pipeline as a table, plus one runner per stage
  - `runner.py` — ordering, checkpoint validation, resume
  - `gates.py` — what may be executed, why not, and executing exactly one thing
  - `report.py` — the JSON and human-readable run reports
- `editing/pipeline.py` — `index_assets(previous=...)` so a per-run scan
  reuses the shared index instead of re-probing every file
- `editing/cli.py` — `auto run|status|list-runs|resume|report|show-gates|
  execute-stage --yes|clean|explain-failure`
- `tests/editing/test_editing_auto.py` — 68 tests

**No new Premiere primitives.** Session 7 executes nothing itself; it delegates
to the four existing executors, each of which keeps its own guards.

### Session 8 — episode memory and the retention planner

- `editing/episode/`
  - `schema.py` — the fifteen record types, the closed vocabularies, the
    confidence cap, and `NOT_ANALYTICS`
  - `language.py` — cue families matched longest-first with span claiming, topic
    extraction, question detection, repeated-phrase finding
  - `track.py` — one linear episode clock from the rough cut, or a *labelled*
    synthetic one from the raw timeline
  - `beats.py` — eighteen beat kinds, scored per channel, merged, with the
    climax marked at most once
  - `loops.py` — open loops, topical resolution, setup/payoff linking, callbacks
  - `memory.py` — objectives, places, people, motifs, and the assembly
  - `risks.py` — thirteen risk detectors and the one function that decides
    whether a fix is safe automatically
  - `hooks.py` — hook candidates, the climax report, the ending, the midpoint
  - `suggest.py` — findings become `RetentionSuggestion` records
  - `plan.py` — assembly, and saying what the plan could not see
  - `report.py` — both reports plus five focused views for the CLI
- `editing/config.py` — `episode_dir`
- `editing/pipeline.py` — `episode_memory`, `retention_plan`, their read/write
  pairs, and `retention_suggestions_for(stage)` as the downstream seam
- `editing/cli.py` — `episode build-memory|plan-retention|report|show-beats|
  show-risks|show-hooks|show-open-loops|show-callbacks|export`
- `editing/auto/` — two more stages, `episode_memory` and `retention_plan`,
  non-critical, with `--skip-episode`
- `tests/editing/test_editing_episode.py` — 141 tests

**Executes nothing.** No dry run, no `--yes`, no gate — there is nothing to run.
It produces records; a later session decides what an operation looks like.

### Session 9 -- the feedback collector and the human review loop

- `editing/feedback/`
  - `schema.py` -- the eleven record types, the 33 ratings with their
    polarities, the fourteen reason categories, `NOT_MEASURED`
  - `store.py` -- the session folder, and the append-only log
  - `targets.py` -- the `Artifacts` bundle, and resolving one ID to the record
    it names across every collection
  - `queue.py` -- eight generators, dedupe, collapse, group, and the selection
    that decides what a person is actually asked
  - `collect.py` -- what one identifier on the command line refers to, and
    turning a rating, note or correction into an appended item
  - `signals.py` -- 30-odd rules reading feedback as `(dimension, direction)`
    preferences, with disagreement counted
  - `training.py` -- one signal per rating, usable or explicitly not, with the
    reason on both sides
  - `export.py` -- jsonl / json / csv, each with a manifest
  - `report.py` -- `summary.json`, `report.md`, and five CLI views
- `editing/config.py` -- `feedback_dir`
- `editing/pipeline.py` -- `feedback_artifacts`, `feedback_start`,
  `feedback_queue`, `feedback_items`, `feedback_signals`, `feedback_summary`,
  `feedback_export`, `feedback_estimate`
- `editing/cli.py` -- `feedback start|queue|show|rate|note|correct|list|
  report|export|stats`
- `editing/auto/` -- three more stages, opt-in behind `--feedback`, plus a
  `WORTH A HUMAN LOOK` section in every run report whether or not they ran
- `tests/editing/test_editing_feedback.py` -- 84 tests, plus 11 in the auto suite

**Trains nothing, applies nothing, executes nothing.** It reads JSON other
passes wrote and appends what a person said about it.

### Session 10A -- local Whisper transcription

The first Week 1 feature: the system could *read* a transcript from five
formats and could not *make* one, so footage with no SRT beside it left the
whole story layer silent.

- `editing/transcribe/`
  - `schema.py` -- `TranscriptionConfig` and the seven records; the cache-key
    subset; `as_transcript()`, the bridge to what every pass consumes
  - `backends.py` -- faster-whisper (late import, device/compute resolution,
    CUDA-to-CPU fallback) and the mock that stamps everything it makes
  - `audio.py` -- media discovery, readability checks, FFmpeg extraction into
    the cache and never beside the footage
  - `formats.py` -- SRT to spec, VTT, readable text with provenance
  - `store.py` -- job folders, the cache, and `publish()` -- the seam
  - `run.py` -- one file (raises), a folder (never raises for a file)
- `editing/schema.py` -- `whisper` added to `TRANSCRIPT_SOURCES`
- `editing/pipeline.py` -- `transcribe_file|folder|assets`, `transcribe_status`,
  `transcription_config|jobs|job|result`, `export_transcription`
- `editing/cli.py` -- `transcribe file|folder|status|show|export|clear-cache`
- `editing/auto/` -- one more stage, `transcribe`, before `analyze`, opt-in
  behind `--transcribe`
- `tests/editing/test_editing_transcribe.py` -- 82 tests, plus 8 in the auto
  suite

**Verified on real speech.** Piper synthesises a commentary track, Whisper
reads it back: 100% word recovery with `tiny`, and `small` gets "nether" and
"netherite" right. The transcript reaches the Session 8 episode layer, which
finds the stated objective and the open loop from it.

---

## Design decisions worth not re-litigating

**Inferred audio is capped at 0.45 confidence**, and the cap survives all the
way into asset ranking.

**Nothing is ever deleted, only marked.** The Session 2 safety pass, the
Session 4 critic, the Session 5 compiler and the Session 6 placer all record
what they refused and why. Four passes, one rule.

**Execution guards, everywhere:** no default runs anything; a dry run must pass
in the *same* call; the target must be provably the scratch sequence; a refusal
is a returned result with a reason.

### From Sessions 4–6

**A finding is not a fix**, and **a fix may only act on a premise the plan
confirms** — a critic hallucinating a zoom cannot make the system edit one.

**Ceilings only ever subtract.** A style makes the edit quieter than the
evidence justifies, never busier.

**The style pass is additive only** (no `clip.*` at all). The asset pass adds
exactly one `clip.*` — `clip.overwrite`, which does not ripple — and never
touches V1 or A1.

**Bad silence is better than random annoying SFX.** Five outcomes per asset
placeholder, four of which place nothing.

### From Session 7

**Planning and execution are separate verbs.** `auto run` builds every plan and
validates it offline. It never touches Premiere. There is deliberately no
`--execute-everything`: the four passes carry different risk and one switch
would mean approving the riskiest by approving the safest.

**A checkpoint is a claim, verified before it is trusted.** Artifacts must
still exist, still match their fingerprints, and still have been built from the
same configuration. Changing `--style` therefore rebuilds the style and asset
passes and leaves the analysis alone, with no flag to remember.

**A failure is a record with a command attached.** Every stopping point carries
what failed, why, whether the run can resume, and the exact next thing to type.

**Not every stage is critical.** The review pass needs FFmpeg and a model
server. When either is missing those stages block, the run continues to style
and assets, and the report says what was lost.

**Each run is hermetic, except the cache.** Per-run `artifacts/` so two runs
cannot collide; shared `cache/` because paying for hundreds of model calls
twice is the worst thing this could do to an afternoon.

**A dry run must never write the execution report.** They were the same
file: a `resume` after an execution overwrote `executed: true` with
`false`, the later passes then believed the sequence had never been built,
and every downstream gate was permanently blocked with no way to clear it.
`test_a_resume_never_erases_the_record_of_an_execution` pins it.

**A dry-run stage must write nothing at all.** The first fix above made it
re-save the *plan* instead, which changed that file's fingerprint and so
invalidated the **build** stage's checkpoint -- every resume then rebuilt the
rough cut, the revisions, the layers and the asset plan for no reason. The
build stages validate and save once; the dry-run stages re-validate and
persist nothing.

**Staleness is read from the plan, not compared by timestamp.** An earlier
version compared build time against execution time, both recorded to the
second, so a plan rebuilt in the same second looked fresh when it was not.

### From Session 8

**Confidence is a statement about evidence, never about an audience.** The
ceiling is set by how many independent channels agreed: one channel caps at
0.45, which sits below the 0.55 an item needs to affect an edit. So a
keyword-only finding is *structurally* incapable of driving one, and three
agreeing channels still cap below 0.9. A Session 2 recommendation is not a
fourth channel -- it was derived from the same three, and counting it would let
one observation vote twice.

**Memory is what happened; the plan is an opinion about it.** Two artifacts, so
re-planning cannot rewrite the observation and you can disagree with a
suggestion without re-deriving the story.

**One question, one answer.** `beats._mark_climax` decides the peak; the
retention plan *reports* that verdict with its supporting numbers. An earlier
version scored independently and disagreed with the beat list on real footage --
the memory said the payoff at 83% while the plan pointed at a discovery at 36%.
Both now read `CLIMAX_MIN_INTEREST` and `CLIMAX_MIN_MARGIN` from one place.

**A marker is always safe; timing is safe only when measured.** Silence is a
number and boredom is a judgement, so `dead_air` can produce an automatic
shortening and `boring_repetition` never can, at any confidence.

**No phrase can score two cue families.** Matching is longest-first and a match
claims its characters, so "got it back" scores recovery and cannot also score
payoff for the "got it" inside it. That is the structural version of the Session
5 fix, which worked until someone added a word.

**A detector that cannot see stays quiet.** Motion probing off means every score
is 0.0; the low-visual-change detector checks `has_motion` first rather than
firing on the whole episode.

### From Session 9

**The feedback log is the only artifact in the system that cannot be
regenerated.** Everything else under `data/editing/` is derived from the
footage and can be rebuilt by re-running a pass; a person's afternoon of review
cannot. That single asymmetry is the reason for every other rule in the layer:
the log is opened in append mode in exactly one function, changing your mind
appends a superseding item rather than editing one, `summary.json` and
`report.md` are derived and disposable, and starting a session over one that
holds feedback is refused even with `--force`.

**A rating is only worth keeping if you can find the thing it was about.** So
a `FeedbackTarget` names a record, and `resolve` searches every collection for
one ID -- a reviewer who copied an ID out of the queue should not also have to
say which pass produced it. Three states stay distinguishable on purpose:
never looked, looked and did not find, and never had an ID. Session 10 will
want to drop the second and keep the third.

**A question has to carry what the system decided.** Feedback given without
seeing that is feedback about the video; with it, it is feedback about the
decision, and only the second can supervise anything. That is why a rating
collected outside the queue on a bare time range comes out marked *unusable for
training* with that as the reason -- it is a real opinion and not a label.

**The queue is a selection problem, not a listing problem.** A pure top-N
ranking fails three ways and each has a rule: reserved slots stop the style
layer flooding it, a ~15% positive-sample share stops a review made entirely of
complaints, and collapse-plus-group stops one moment being asked about six
times. The numbers are opinions and are meant to be edited.

**"Might be wrong" and "looks right" are exclusive.** An early version flagged
a confidently placed sound effect as both `risky_automatic` and
`positive_sample`, which is two contradictory reasons to be asked about and
made `--no-positive` silently meaningless. Doubt wins, in `_settle_flags`. The
same bug taught the second half: `risky_automatic` had meant "decided
automatically", which is *every* styled item and *every* placed sound, so the
flag was universal and carried no information. It now means decided
automatically **and** carrying a named risk or a low confidence.

**A preference about timing is never automatically safe.** Session 8's
marker-versus-timing split, from the other side. A wrong preference about
caption tone costs a caption nobody liked; a wrong preference about pace costs
footage. `cut_pace` can reach any confidence and still returns false, with the
reason recorded -- and a signal that *would* qualify says instead that nothing
reads preference signals yet, because "the evidence supports this" and "you may
act on it" are different sentences.

**Disagreement is counted, not filtered.** A dimension you have gone both ways
on produces one signal in the majority direction with `contradictions` and a
lowered `agreement`, never a tidy consensus that was never there. Ties are
broken by summed confidence and then recency; an earlier version broke them
alphabetically, which made "more" beat "less" every time two ratings disagreed
-- a coin toss dressed up as a finding.

**An unusable training signal is emitted, with its reason.** Dropping it would
hide what the collector is losing. "3 x the rating was 'unsure'" and "2 x the
target could not be joined to a record" is the difference between fixing this
layer and guessing at it.

**Feedback is the only opt-*in* pass in `auto`.** Every other stage produces a
file; this one starts a review a person then has to finish, and creating one
nobody asked for would leave a trail of abandoned sessions. So the run report
*estimates* how much is worth reviewing -- one cheap pass over artifacts
already on disk -- and prints the command, rather than starting anything.

**`feedback_start` is idempotent rather than resumable.** It is not resumable
(the queue should reflect a resumed run), so a resume reaches it again -- and
opening a second session would split one review across two logs that, being
append-only, could never be merged. It reuses the run's existing session
instead.

### From Session 10A

**The transcript is the most load-bearing input in the system**, and it was the
one thing nothing could produce. Everything that reasons about story reads it,
and every one of those passes fails *quietly* without it -- a folder with no
SRT produced a plausible-looking empty story layer rather than a complaint.

**A fake transcript is worse than no transcript.** Every story finding built on
fabricated text would look sound. So the mock backend stamps `mock=True` on the
result, on `transcript.json`, in the `.txt` header, in the report, in the auto
stage summary, and in the note on the durable transcript. Six places, because
the artifact travels.

**Two stores, doing different jobs.** The job folder records *how* a transcript
was made -- config, device, probabilities, what was dropped. `<asset_id>.json`
*is* the transcript, in the place `resolve()` looks first. Writing the second
is the actual integration; everything else is provenance.

**The cache key is the content hash, not the path.** A re-exported file misses
correctly instead of serving a transcript of audio that no longer exists.
Settings that change a word are in the key; `timeout` and `use_cache` are not,
because invalidating on those would throw away hours for nothing.

**Skipped and cached are different outcomes.** Cached reused a real result;
skipped never needed one because a current transcript already existed. An early
version pre-filtered the skipped files out entirely, and the batch then reported
"no media files found" -- which looks exactly like discovery being broken.

**Confidence is a linear remap of `avg_logprob`, not `exp()`.** Exponentiating
is defensible and compresses everything usable into 0.6-1.0, which makes the
number useless for the only thing it is for: ranking segments by how much to
trust them.

**The heavy import is inside the function.** `faster_whisper` at module scope
would mean importing the CLI fails on a machine that never installed it, taking
every other editing command down with it. A test walks the AST of every module
in the package to keep it that way.

**A batch never raises for a file.** Two corrupt clips out of thirty is an
ordinary afternoon; the useful outcome is twenty-eight transcripts and an exact
account of the two, each with the fix attached.

---

## Current state

- **1318 editing tests**, ~100s, needing no FFmpeg, GPU, model server, Premiere,
  Whisper or real media
- `tests/premiere/` passes too (277 there, 1595 across both suites)
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated
- `editing/README.md` is the full user documentation (~2950 lines); §0 covers
  auto mode and §16 covers the feedback collector

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path.

**Verified on real footage**: `auto run` completes all stages on three real
MP4s with real FFmpeg, in mock mode, in about a minute. The full gate chain
(execute rough cut → resume → execute layers) is verified against a fake
engine. Session 9 was verified end to end through the CLI against hand-built
artifacts -- start, queue, show, rate, note, correct, list, stats, report and
both export formats -- but **not** yet against a real review of real footage.

---

## The honest gaps

1. **Never tested with a real Qwen3-VL critic.** The critic prompt has never
   been seen by a model. Still the highest-value next action.
2. **Never executed against real Premiere.** Every plan dry-runs clean; the
   only thing that has run against the real host is the Premiere self-test.
   The first real `--yes` will find things the validator cannot.
3. **Asset matching is tags and folders, not listening.** Loudness and BPM are
   sidecar-only.
4. **Beds are tiled, not crossfaded.** Seams are audible if a file does not
   loop cleanly.
5. **Asset tracks are assumed (A2/A3/V3)**, not discovered.
6. **Nothing is ever removed.** Re-running a pass places its work again.
7. **`.mogrt` is matched and never placed**; `text.create` uses the rasterised
   path, so captions are not editable in Premiere afterwards.
8. **Style numbers and mixing limits are opinions**, meant to be edited.
9. **Marker positions after a Session 4 timing change are computed, not
   observed**; **speed ripple is still assumed** (Session 3's gap).
10. **Checkpoints fingerprint size and mtime, not content.**
11. **The episode layer has never been checked against a real edit.** Its
    beats, risks and hooks are plausible on fixtures and on generated footage.
    Nobody has taken a finished video, read the retention plan and said whether
    it was right, so its numbers are calibrated against intuition.
12. **Nothing consumes the retention suggestions.** The seam is built and
    tested; Sessions 3, 5 and 6 do not read it yet.
13. **Character names are guesses** from capitalised words in one channel. They
    cap below the edit threshold and arrive flagged, which is the honest
    handling, not a fix.
14. **Transcription accuracy is Whisper's**, and fast excited commentary over
    game audio is its hard case. No diarisation -- `speaker` is always `null`.
    The vocabulary prompt is the highest-value knob and is empty by default.
15. **The episode layer's loop resolution has not been checked against a real
    transcript.** On a synthesized episode it found the stated objective and
    the open loop, and reported `unresolved_setup` for a question the same
    transcript answers a minute later -- on a two-segment timeline, so this may
    be granularity rather than a defect. Worth a real run.
16. **Nothing learns from the feedback.** Session 9 collects, links and exports
    it; no pass reads a preference signal and nothing has been trained. That is
    Session 10's job and is the point of the whole layer.
17. **Nobody has done a real review yet.** The queue's priorities -- the
    reserved-slot counts, the positive-sample share, the 0.50 uncertainty line,
    the flag boosts -- are calibrated against intuition. Twenty real reviews
    would say whether it puts the right things first. This is now the highest-
    value next action after the critic.
18. **Preference extraction is keyword-assisted.** Telling "the danger caption
    was bad" from "captions are bad" reads the item's label for words like
    `danger_text` and `whoosh`. The general rules are structural; the specific
    ones are heuristics, marked as such.
19. **A preference signal is one person's**, and `scope` only distinguishes one
    session from several. There is no notion of multiple reviewers.

---

## Natural next steps

The system is now usable enough that the next steps are about *reality*, not
features.

- **Transcribe one real episode and read `episode report`.** This is now the
  cheapest way to find out whether the story layer means anything, and it needs
  no GPU and no model server:

  ```cmd
  python -m editing.cli transcribe folder D:\Footage\ep12 --model small
  python -m editing.cli auto run --folder D:\Footage\ep12 --mock --no-premiere
  python -m editing.cli episode report
  ```

  The audio events and the whole story read are *real* in that run; only the
  on-screen analysis is mocked.
- **Run the critic against a real Qwen3-VL server.** `auto run --folder <f>`
  without `--mock`, then read `auto report`. Expect the prompt to need tuning;
  the issue guide and the "most frames are fine" instruction are the levers.
- **Execute against real Premiere, up the ladder.** Each rung is one `--yes`
  and each is undone by deleting a track:
  1. `auto run --style minimal_clean --markers-only` — draws and plays nothing
  2. `auto execute-stage roughcut --yes` — the only rung that builds a sequence
  3. `auto resume` then `auto execute-stage layers --yes`
  4. `auto execute-stage assets --yes`
- **Then loosen one thing at a time**: a style that draws text, then a real
  asset library.
- **Loudness measurement** (`ffmpeg ebur128`) so levels are measured rather
  than assumed. The schema is already shaped for it.
- **Track discovery**, so A2/A3/V3 are read from the sequence.
- **Let a pass read the retention suggestions.** The cheapest first consumer is
  the style pass: `pipeline.retention_suggestions_for("style", safe_only=True)`
  returns markers and cards with ranges, reasons and confidences already
  attached, and the style compiler's density ceilings would still apply on top.
- **Check the episode layer against a video you have already cut.** Run
  `episode report` on footage whose finished edit you know, and see whether the
  risk zones match the places you actually trimmed. That is the only way to
  find out whether the thresholds mean anything.
- **Do one real review, all the way through.** `auto run --feedback`, then
  answer all twenty questions honestly and read `report.md`. Two things come
  out of it that nothing else can produce: whether the queue asks about the
  right things, and the first real material for Session 10. Expect the queue's
  numbers to need tuning -- `RESERVED`, `POSITIVE_RATIO` and
  `UNCERTAIN_AT_OR_BELOW` in `feedback/queue.py` are the levers.
- **Then build the dataset (Session 10).** The seam is
  `feedback export --include training` and `pipeline.feedback_signals`. Read
  the `why_not` fields first: they say what this collector is losing, and
  fixing the collector is cheaper than working around it downstream.

---

## Command reference

Auto mode, in the order you would use it:

```cmd
python -m editing.cli auto run --folder D:\Footage\test --style cinematic_minecraft
python -m editing.cli auto status
python -m editing.cli auto report
python -m editing.cli auto show-gates
python -m editing.cli auto execute-stage roughcut --yes
python -m editing.cli auto resume
python -m editing.cli auto execute-stage layers --yes
python -m editing.cli auto execute-stage assets --yes
```

Useful flags on `auto run`:

| Flag | Effect |
|---|---|
| `--mock` | deterministic vision and critic; no GPU, no server |
| `--no-premiere` | never talk to Premiere; every gate stays shut |
| `--markers-only` | style and asset passes record instead of drawing/playing |
| `--skip-review` / `--skip-assets` / `--skip-episode` | skip a whole pass |
| `--transcribe` | produce transcripts with local Whisper first (opt-in) |
| `--transcribe-model` / `--transcribe-language` | Whisper size, and the language |
| `--feedback` | also open a review session and build its queue (opt-in) |
| `--asset-library <path>` | a library other than `<model dir>/assets` |
| `--max-windows N` | cap analysis windows per file |
| `--force-new-run` | a fresh run even if one exists for this footage+style |

When something stops:

```cmd
python -m editing.cli auto explain-failure
python -m editing.cli auto resume
python -m editing.cli auto resume --style fast_funny      REM restyle in place
python -m editing.cli auto resume --refresh layers_build
python -m editing.cli auto clean --run <run_id> --yes
```

`resume --style` is the cheap way to compare presets: the style is one of the
fields the layer and asset stages fingerprint, so exactly those rebuild and the
analysis is reused. A fresh `auto run --style <other>` is a separate run with
its own checkpoints, which is what you want when you need two styles side by
side.

Episode planning, which executes nothing:

```cmd
python -m editing.cli episode build-memory
python -m editing.cli episode plan-retention
python -m editing.cli episode show-hooks
python -m editing.cli episode show-risks --severity high
python -m editing.cli episode show-open-loops --unresolved
python -m editing.cli episode export for_style.json --suggestions-for style
```

`auto run` builds both as stages 16 and 17; `--skip-episode` turns them off.

Transcription, which is where a real episode now starts:

```cmd
pip install faster-whisper
python -m editing.cli transcribe status
python -m editing.cli transcribe folder D:\Footage\ep12 --model small ^
    --prompt "Minecraft, creeper, nether, netherite, diamonds"
python -m editing.cli transcribe show <job_id>
python -m editing.cli transcribe export <job_id> --out subs.srt
```

Measured on this machine, CPU only: `tiny` ~15x realtime, `small` ~4.3x,
so a 40-minute episode with `small` is about nine minutes. `torch` here is
`2.13.0+cpu`, so CUDA auto-detection reports false; `--device cuda` forces it
if CTranslate2 can use a GPU without torch.

Human review, which trains nothing:

```cmd
python -m editing.cli feedback start --run <run_id> --title "ep12 first pass"
python -m editing.cli feedback queue --limit 20
python -m editing.cli feedback show <prompt_id>
python -m editing.cli feedback rate <prompt_id> good --reason pacing --note "clean"
python -m editing.cli feedback rate li_danger bad --reason boring --correction "cut this shorter"
python -m editing.cli feedback note li_danger "sits over the hotbar"
python -m editing.cli feedback correct p_0 "move it later" --seconds 2
python -m editing.cli feedback list --follow-up
python -m editing.cli feedback stats --preferences
python -m editing.cli feedback report
python -m editing.cli feedback export dataset.jsonl
```

The first argument to `rate` is a prompt ID, any record ID from any artifact, a
range like `120-155`, or `whole`. Sessions live in
`data/editing/feedback/sessions/<id>/`, and `feedback.jsonl` there is the only
file in the whole system that is never rewritten.

The stage-by-stage commands from Sessions 1–6 all still work unchanged, and
`auto` is a thin layer over them — anything it does can be done by hand, and
the failure messages say which command to run.
