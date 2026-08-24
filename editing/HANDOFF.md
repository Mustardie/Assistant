# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-24 (Session 11).

---

## Start here

```cmd
cd /d E:\Assistant
python -m pytest tests/editing -q --basetemp=%TEMP%\pt    REM expect 2060 passed

REM hear the footage first -- the story layer is blind without this:
pip install faster-whisper
python -m editing.cli transcribe folder D:\Footage\test --model small

REM the whole pipeline, planning only, with nothing installed:
python -m editing.cli auto run --folder D:\Footage\test --mock --no-premiere --transcribe
python -m editing.cli auto report

REM watch what it decided, without opening Premiere:
python -m editing.cli render roughcut
python -m editing.cli render open

REM have a model choose the cut instead of a threshold:
python -m editing.cli director status
python -m editing.cli director plan --backend mock
python -m editing.cli director compare-heuristic

REM and shape it like an episode: cold open, compressed sag, protected setups
python -m editing.cli retention plan                  REM decides, changes nothing
python -m editing.cli retention plan --mode retention
python -m editing.cli retention compare

REM add the little that goes on top -- a few captions, a few sound cues:
python -m editing.cli auto run --folder D:\Footage	est --mock --no-premiere ^
    --retention-cut --retention-mode retention --render-proxy ^
    --captions key_moments --audio-polish placeholders
python -m editing.cli auto show-checks                REM is it usable?
python -m editing.cli review open-latest              REM one folder, one index

REM or do the whole library at once:
python -m editing.cli auto batch --root E:\Clips --dry-run

REM then tell it what you think of the result:
python -m editing.cli feedback start
python -m editing.cli feedback queue --limit 20
```

Everything below is detail behind that.

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all fourteen sessions and 2060 passing editing tests.

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

## What was built, in fourteen sessions

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
                                                    ↓
     Session 10B renders any rough cut to a proxy MP4 with FFmpeg and writes a
     timestamped review file beside it. No Premiere, no GPU, no model — the
     first pass whose output is something you watch.
                                                    ↓
     Session 10C stops choosing footage with a threshold. A model reads the
     whole structured episode — transcript, beats, loops, setups, payoffs,
     risks, your prose style guide — and decides what the cut is. Twelve
     deterministic checks then decide what it is allowed to do, and every
     refusal names the rule. The heuristic remains the fallback, always.
                                                    ↓
     Session 10D finally spends what Session 8 found. The strongest hook moves
     to the front as a cold open, the sagging stretches are compressed, the
     setups a kept payoff needs are claimed before anything that removes
     footage runs, and ordinary silence is cut harder — while the silence
     that is doing a job is left alone. Counts what changed; predicts nothing.
                                                    ↓
     Session 11 adds no brain layer at all. A handful of captions for the
     moments that carry the episode and a handful of sound cues for the ones
     that land — both subtractive, both refusing most of what they consider
     and recording why. Then fifteen checks asking whether the run produced a
     usable thing, one review folder with an index that answers the five
     questions in order, and batch mode over a whole library of footage.
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
| 10B | `render/` | FFmpeg proxy renders, review notes, the render cache |
| 10C | `director/` | a model chooses the cut; twelve rules decide what it may do |
| 10D | `retention/` | cold open, compressed sag, protected setups, harder dead air |
| 11 | `polish/`, `reliability/`, `review/`, `batch/` | key-moment captions, restrained sound, fifteen checks, a review folder, batch mode |

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

### Session 10B -- the FFmpeg proxy render

The second Week 1 feature, and the one that changes how the whole thing is
used: every pass before this produces a *plan*, and the only way to see one was
to open Premiere and execute against it. Now a rough cut becomes a watchable
MP4 in about a minute per ten minutes of video, with no Premiere, no GPU and no
model.

```
change a rule -> build the plan -> render -> watch -> change a rule
```

- `editing/render/`
  - `schema.py` -- `RenderConfig` and the seven records; the cache-key subset;
    the quality table, the encoder dialects, and what a flat proxy cannot show
  - `convert.py` -- `RoughCutPlan` -> `RenderSegment[]`: ordering, speed
    resolution, truncation, and the unsupported-feature list
  - `commands.py` -- every FFmpeg invocation, built as pure data. Nothing here
    shells out
  - `runner.py` -- the only subprocess in the package, plus the mock runner
  - `sources.py` -- measuring the footage, and the four-part cache key
  - `store.py` -- job folders, the cache, artifacts, temp and cleaning
  - `notes.py` -- `review_notes.md`, the file you write on while watching
  - `report.py` -- `report.md` and the terminal view
  - `run.py` -- the orchestration: convert, check, measure, key, build, encode,
    join, verify, write
- `editing/config.py` -- `render_dir`
- `editing/pipeline.py` -- `render_config|status|roughcut`, `render_plan_file`,
  `render_jobs|job|result|report|notes`, `clean_renders`
- `editing/cli.py` -- `render roughcut|from-plan|status|list|show|report|
  notes|open|clean`, and `_feedback_pipeline` generalised to
  `_run_scoped_pipeline` so `--run` works for both
- `editing/auto/` -- one more stage, `render_proxy`, after `retention_plan`,
  opt-in behind `--render-proxy`, plus a `WATCH IT` section in every run report
  whether or not it ran
- `tests/editing/test_editing_render.py` -- 169 tests, plus 12 in the auto suite

**Verified against real FFmpeg.** A three-clip cut mixing 1080p60-with-audio
and 720p30-with-no-audio rendered in 1.2s to a 15.06s file against a planned
15.0s, joined by stream copy, with a continuous audio track across all three
segments. `auto run --render-proxy` completed all 23 stages on the same
footage. The test suite itself needs no FFmpeg: every subprocess goes through
an injected runner.

### Session 10C -- the director pass

The Week 2 feature, and the first one that changes *what the edit is* rather
than what can be done with it. Selection up to here was local: `usefulness >=
0.40`, dead air goes, danger stays, judged eight seconds at a time. That cannot
see that a dull stretch at 04:12 is the setup for the thing at 31:40, that the
episode opens on walking, or that the same joke has landed three times.

A model now reads the whole structured episode and decides what the cut is.
Then twelve deterministic checks decide what it is allowed to do.

- `editing/director/`
  - `schema.py` -- `DirectorConfig` and the ten records; the closed action,
    reason and viewer-effect vocabularies; `NOT_MEASURED`
  - `style_guide.py` -- prose editing rules, from four places, with a built-in
    default that has real rules in it
  - `context.py` -- the brief: merge, trim, thin, and say what was left out
  - `prompt.py` -- the instruction, and the context renderer the budget is
    measured against
  - `backends.py` -- any OpenAI-compatible endpoint, plus the mock that
    decides by four fixed rules and says so
  - `parse.py` -- untrusted text to decisions: resolve, repair, discard
  - `safety.py` -- the twelve checks, in a fixed order
  - `convert.py` -- accepted ranges into Session 3's builder; hybrid merging
  - `compare.py` -- director cut against threshold cut
  - `store.py` -- the plan, the prompt as text, the cache
  - `report.py` -- decisions, rejections, and what to type next
  - `run.py` -- the orchestration, and every failure as a result
- `editing/config.py` -- `director_dir`
- `editing/roughcut/build.py` -- `RoughCutOptions.mode` and `_select`; the
  heuristic path is byte-for-byte unchanged
- `editing/pipeline.py` -- `director_config|status|context|plan`,
  `load_director_plan`, `director_plan_or_none`, `director_report`,
  `compare_director`, `style_guide`, `clear_director_cache`; `rough_cut`
  gained `director_plan`
- `editing/cli.py` -- `director build-context|plan|report|show-decisions|
  show-rejected|show-style|compare-heuristic|render|status|clear-cache`
- `editing/auto/` -- one more stage, `director_plan`, before `roughcut_build`,
  opt-in behind `--director`, plus a `WHO CHOSE THIS CUT` section in every run
  report whether or not it ran
- `tests/editing/test_editing_director.py` -- 179 tests, plus 9 in the auto
  suite

**Verified against a real HTTP endpoint.** Five tests run an
OpenAI-compatible server on loopback and assert the whole envelope: the URL,
the message roles, the JSON-mode hint, the `Authorization` header, and
unwrapping an answer that arrives fenced with a sentence either side. No model
is involved and nothing leaves the machine.

**Not verified against a real model.** Whether a 14B or 70B model actually
makes better editing choices than the threshold is unknown, and
`director compare-heuristic` plus rendering both cuts is the only way to find
out. That is now gap 23 and the highest-value next action in the whole system.

### Session 10D -- retention structure wiring

The Week 3 feature, and the one that closes the oldest open loop in the
project. Session 8 built the retention planner -- hooks, risk zones, setups and
payoffs, a peak, an ending -- and executed nothing. Every handoff since has
said "nothing consumes the retention suggestions". This is the consumer.

A cut is now reshaped around what that planner found: the strongest hook moves
to the front as a cold open, sagging stretches are compressed, setups a payoff
needs are protected, and ordinary silence is cut harder than the general
selector dares.

- `editing/retention/`
  - `schema.py` -- `RetentionCutConfig` and the ten records; the closed action,
    source, viewer-effect and refusal vocabularies; `NOT_MEASURED`
  - `resolve.py` -- episode time to real footage, through the `EpisodeTrack`,
    with the timebase read rather than guessed
  - `coldopen.py` -- hook selection, six vetoes, and the duplication policy
  - `sag.py` -- risk zones to cuts and retimes, with the ceiling
  - `protect.py` -- setups, payoffs, callbacks, the peak, and two warnings
  - `deadair.py` -- silence, and what it is for
  - `compile.py` -- the seven-step compiler and the apply pass
  - `compare.py` -- reshaped cut against the original, in counts only
  - `report.py` -- the report and four focused views
  - `store.py` -- the plan, the variant cut, the comparison
  - `run.py` -- base selection, orchestration, and the seam to Session 3
- `editing/config.py` -- `retention_dir`
- `editing/roughcut/build.py` -- a `preselected` mode, so a caller that already
  knows its ranges still goes through the same assembly, operations, dry run
  and guards
- `editing/pipeline.py` -- `retention_config`, `retention_cut`,
  `load_retention_cut_plan`, `retention_roughcut_or_none`,
  `retention_report`, `compare_retention`
- `editing/cli.py` -- `retention plan|report|show-cold-open|show-compression|
  show-protected|show-rejected|compare|render`
- `editing/auto/` -- one more stage, `retention_cut`, between
  `retention_plan` and `render_proxy`, opt-in behind `--retention-cut`, plus a
  `RESHAPED FOR RETENTION` section in every run report whether or not it ran.
  `render_proxy` now renders the *reshaped* cut when there is one.
- `tests/editing/test_editing_retention.py` -- 128 tests, plus 10 in the auto
  suite

**Verified end to end on real footage.** `auto run --retention-cut
--retention-mode retention --render-proxy` completes all 25 stages, and the
rendered proxy opens on the cold open -- `review_notes.md` starts at
`00:00-00:12` on footage lifted from later in the episode.

### Session 11 — reliability and polish

Four packages, none of which adds a brain layer. This session is about making
what exists **usable, reviewable and reliable**.

- `editing/polish/`
  - `schema.py` — `CaptionConfig`, `CaptionDecision`, `CaptionPlan`,
    `AudioPolishConfig`, `AudioCue`, `AudioPolishPlan`, the nine key moments,
    the closed refusal vocabularies, per-style defaults
  - `captions.py` — which spoken lines are the episode and which are talking
  - `audio.py` — which moments earn a sound, and which cues would be spam
  - `sidecar.py` — the caption plan as an `.srt` beside the proxy
  - `store.py`, `report.py`, `run.py`
- `editing/reliability/`
  - `schema.py` — `GateInputs`, `GateResult`, `GateReport`
  - `checks.py` — fifteen checks, one pure function each
  - `run.py` — gathering what they read; `report.py` — the readable report
- `editing/review/`
  - `schema.py` — `ReviewItem`, `ReviewPackage`
  - `build.py` — gathering a run into one folder; `index.py` — `review_index.md`
  - `store.py` — where it lives: inside the run it is about
- `editing/batch/`
  - `schema.py` — `BatchConfig`, `BatchCandidate`, `BatchEntry`, `BatchSummary`
  - `discover.py` — which folders under a root hold footage
  - `run.py` — the loop and the four decisions; `store.py`, `report.py`
- Four new stages: `caption_polish`, `audio_polish`, `reliability_gates`,
  `review_package` — 29 in total
- New commands: `polish captions|audio|show-rejected|show-missing`,
  `review package|summary|open-latest`, `auto show-checks|batch|list-batches|
  batch-report`
- `editing/config.py` gains `polish_dir`; `editing/schema.py` gains
  `as_text_list`
- 229 new tests (2060 total)

```cmd
python -m editing.cli auto run --folder D:\Footage\ep12 ^
    --retention-cut --render-proxy --captions key_moments ^
    --audio-polish placeholders --no-premiere
python -m editing.cli review open-latest
```

**Everything here is subtractive.** Both polish passes generate candidates from
what the earlier passes recorded, then refuse most of them against named rules,
and every refusal stays in the plan with the rule that made it. A pass that
placed four captions out of sixty candidates and a pass that is broken both
print "4"; only the refusal list tells them apart.

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

### From Session 10B

**Rendering is not executing.** A proxy is a disposable answer to "does this
cut work"; an execution is an edit somebody then has to finish or undo. So the
render path has no gate, no `--yes` and no Premiere -- there is nothing to
guard, because nothing outside `data/editing/render/` is touched. That is also
why `render clean --yes` exists and `roughcut execute` has no equivalent: the
one you can safely delete is the one worth having a delete command for.

**Encode each clip, then join, rather than one filtergraph.** The filtergraph
version is faster and fails on real footage: a folder of captures mixes
resolutions, mixes files with and without a microphone track, and has clips
whose audio starts a few hundred milliseconds late. FFmpeg's answer to that is
a message about stream layouts, forty seconds into a decode, with nothing on
disk. Normalising every segment first makes the join a stream copy that cannot
fail on mismatch, and makes a failure name the clip that caused it.

**Every segment gets an audio stream, even the silent ones.** The concat
demuxer refuses to join files whose stream layouts differ, so one clip recorded
without a microphone breaks the entire render. `anullsrc` for those. This is
the single most common way a naive version of this package fails, and the
reason each source is probed once before anything encodes.

**The job folder *is* the cache entry.** The job ID is derived from the cache
key, so re-rendering the same cut with the same settings lands in the same
folder. A separate cache index would introduce a second place for "there is a
render for this key" and "there is a video on disk" to disagree, and that
disagreement is how a tool starts handing back paths to files that were deleted
to free space.

**The cache key spells its numbers, rather than hashing them.** A plan built in
memory carries `source_in=1` where the same plan read back from JSON carries
`1.0`, and `repr` spells those differently -- so the key missed on the one path
it exists for: build a plan, save it, render it later. Found by rendering the
same cut twice and watching it re-encode.
`test_a_plan_fingerprint_survives_a_trip_through_json` pins it.

**The plan fingerprint reads placements and ignores operations.** Re-running
the style pass rewrites two hundred operations and changes not one frame of the
V1 assembly. Keying on the whole plan would re-render an hour of footage every
time somebody changed a caption preset, which would make the cache worthless
exactly when it matters.

**An impossible speed renders at 1x rather than clamping.** Clamping a 20x
timelapse to 8x produces a video that looks like the *cut* is wrong. Refusing
it and saying so does not.

**A mock render is never called a render.** `mocked` is its own status, and
`rendered` stays False. "Did this complete" and "is there something to watch"
have different answers for a placeholder, and one field cannot say both. Same
rule as Session 10A's mock transcript, learned the same way.

**What FFmpeg cannot show is listed, and the cut still renders.** By Session 6 a
rough cut carries captions, cards, markers, sound effects and graphics.
Refusing to render it would make this package useless exactly when it became
valuable; rendering it silently would misrepresent what is being judged. So
every report carries a `NOT IN THIS VIDEO` list.

**Intermediates are deleted on success and kept on failure.** They are the
whole render again in disk terms, and the question that follows a failed join
is always "which clip is wrong" -- which should not cost a second render to
answer.

### From Session 10C

**The model proposes; the deterministic layer disposes.** A `DirectorDecision`
arrives with `accepted=False` and only a check can change that. This is the
same structure Session 4 gave the critic, and it is what makes a language model
safe to have near an edit at all: a model asked to be creative will
occasionally invent a payoff that is not there, and a system that acts on the
invention has no way to tell afterwards.

**A decision names segment ids, never timestamps.** Times come from the
timeline the context was built from. That single choice removes a whole class
of failure -- a hallucinated range resolves to nothing and is rejected with a
reason rather than becoming footage that does not exist. There is exactly one
place a number from the model becomes a time (`shorten`), and it is clamped
inside the segments the decision named, so the worst a wrong number can do is
choose a different part of footage that genuinely exists.

**Merge before you drop.** The context builder's biggest reduction is folding
adjacent segments that would get the same verdict into one candidate, and it
loses nothing -- the director would have made one decision about them anyway.
Everything after that (thinning, shortening speech, dropping sections) loses
something, and is ordered worst-value-first with every reduction recorded on
the plan.

**Speech is summarised head *and* tail.** The end of a stretch of commentary is
where the reaction usually is, and a director judging whether a joke lands
needs the punchline more than the setup. Nothing is ever paraphrased: a
paraphrase is a claim about what somebody said.

**The style guide is read by the model and not parsed by the rules.** A rule
this system cannot check is a rule it must not claim to enforce. So prose
changes which decisions get *proposed*, and the report shows which lines were
actually cited -- which is how a person finds out whether their guide is being
used at all.

**Hybrid is the default in auto mode, not `director`.** A director given 160
candidates makes forty decisions, not 160 -- the prompt explicitly says it does
not need one per range. In `director` mode the other 120 are simply absent,
which is a short, choppy cut. In `hybrid` they fall through to the rule the
system has always used. Footage the director explicitly *cut* is never re-added.

**`required_setup` is the check that justifies the layer.** Cutting the setup
for a payoff that stays in makes the payoff arrive from nowhere, and no local
heuristic can see it: the setup looks like nothing and the only reason to keep
it sits twenty minutes later. If this layer earns its cost anywhere, it is
there.

**Anything that keeps a payoff ends up holding it.** A plain `keep` over a
payoff is still unprotected, so a later style or asset pass could zoom or duck
it. Found because an earlier check (speech, retiming) can downgrade a decision
to `keep` before `protected_payoff` ever sees it.

**A keep reasoned as "pacing" is not grind.** The grind budget originally
counted it, and "pacing" is the natural category for an ordinary keep -- so the
budget rejected most of a normal cut. Only `speed_up`, and a keep the director
itself called `boring_repetition`, count.

**The answer cache stores raw text, not parsed decisions.** The parser and the
safety pass will both change as this is tuned, and a cache of parsed output
would mean fixing a parser bug did not fix anything already cached. Re-parsing
on every hit is free.

**The context fingerprint spells its numbers.** The same int-versus-float bug
the render cache had: a context built in memory carries `start=0` where the
same context read from JSON carries `0.0`, and `repr` spells those differently.
Second time this exact bug has appeared in two sessions; worth checking for a
third time in any new fingerprint.

**A blocked director stage still leaves its plan behind**, and keying the cut's
selection mode on the plan *existing* rather than on it having ranges reported
a threshold cut as a director cut. The whole layer's credibility rests on the
report saying which selector actually ran, so this is now pinned by a test.

**No transcript means no director cut, and the plan says why once.** Footage
with only picture gives every decision one channel of evidence, which caps at
0.45 -- below the 0.55 needed to change a frame. That is Session 8's rule
producing the correct outcome (a director working from pictures alone is
guessing) and a baffling one to read as twelve separate "confidence too low"
rejections. So the cause is named once, with the fix.

**Nothing here says which cut is better.** `compare-heuristic` measures
*disagreement* -- agreement on footage, what each side kept that the other
dropped, and how many decisions rest on something no threshold could see. There
is no quality metric, and inventing one would be the mistake Session 8 refused
to make about retention. Render both and watch them.

### From Session 10D

**Protection is applied before anything that removes, and that ordering *is*
the safety model.** A setup whose payoff is in the cut is claimed first; every
rule afterwards checks the claim. There is no negotiation later, because a
negotiation is a place for a bug to live. Reversing the two steps would work
most of the time, which is worse than not working at all.

**Protection is measured by share, not by any overlap.** Marking a whole clip
protected because it *contains* a ten-second setup made every later rule treat
four minutes of grind as untouchable, and nothing could be compressed at all.
A range is flagged when protection covers half of it; removal is checked span
by span, which is both stricter and narrower -- the protected seconds cannot be
cut, and the rest of the clip can.

**A cold open moves footage; it does not copy it.** The default removes the
original, and carving the teased seconds out of where they used to be is the
one place this layer trims something protection claimed. The justification is
narrow and worth keeping stated: the footage is not being removed from the
episode, it has moved to the front. Duplication is then checked on the
*finished cut* rather than trusted from the policy.

**Two cases override the duplication policy**: a hook that is the peak, and a
hook on protected footage. Removing either takes the payoff out of the episode
to put it at the front, so the original is shortened instead and the plan
records that the policy was overridden rather than silently obeyed.

**`HookCandidate.setup_seconds` does not mean what it says.** Its docstring
says "seconds of prior context needed"; Session 8 sets it to
`round(beat.start, 3)` -- the beat's position in the episode. Reading it as
documented refused every hook past the first few seconds, which is every hook
worth having, and the cold-open feature was dead on arrival until it was found.
Standing-on-its-own is now judged from whether the footage carries speech or a
strong label. **Worth checking any cross-session field whose name and value
were written by different sessions.**

**A `roughcut` memory without a rough cut is refused, not resolved.** Sequence
times read against the synthetic timeline ordering would place every finding
somewhere; every number would still be a number and all of them would be wrong.
The resolver flags the fallback and the compiler refuses to act on it.

**Cut or speed up is decided by what the footage is for.** Speech: neither.
The picture changing -- a tunnel getting longer, a build going up: speed up,
because a viewer needs to see it happened and not watch it happen. Nothing
changing: cut, because there is nothing to preserve.

**Silence has no properties of its own.** The pause after a scream and the
pause in an empty tunnel are the same measurement; only the context tells them
apart. So purpose is read from what surrounds a silence -- a preceding
reaction, a nearby payoff, a change of place -- and purposeful silence is
*capped* rather than exempt, because a four-second pause becomes its own
problem.

**Silence is trimmed to the limit, never deleted, and never into speech.** The
first second of a pause is usually the reaction to whatever just happened, and
a gap with talking across it is speech pacing.

**Every decision arrives refused and something has to say yes.** The validation
pass accepts everything its producing module did not already refuse, then takes
some back. That was a bug before it was a design: skipping decisions that "had
not been accepted yet" meant the protection and cold-open decisions were never
accepted at all, and the whole layer silently did nothing while reporting
success.

**A record and its decision are two objects describing one judgement.**
Validation can refuse a decision after the dead-air sweep accepted its record;
left alone they disagree and the report adds up seconds that were never
removed. They are synced explicitly.

**The reshaped cut is a variant, in its own directory.** `roughcut/<name>.json`
is never overwritten, so disagreeing with this pass costs nothing -- which is
what makes it safe to try, and why `report_only` is the default mode.

**`report_only` is the default and applies nothing.** Every other layer in this
system defaults to producing a plan nobody has to act on; this one reshapes an
episode, so the same principle costs one extra flag.

**The comparison has no score.** No grade, no percentage, nothing that could be
read as an audience prediction. The temptation with a retention feature is to
produce a number that goes up, and a number that goes up is exactly what
somebody would trust without checking. What it reports is what changed, and
what was refused.

### From Session 11

**A caption is punctuation, not a subtitle.** The whole pass is built against
one failure mode: a system that *can* put a transcript on screen will put the
whole transcript on screen. So a line does not earn a caption by being audible
— it has to be one of nine key moments, argued for by the picture, the audio
and the words together, and then win a per-minute budget it will usually lose.
`dense` mode exists, is never a default, and says so in every plan it produces.

**Every refusal is kept, with the rule that made it.** In both polish passes,
in the checks, in the batch summary. "Nothing was added" and "sixty things were
considered and all sixty were refused, here is why" are different reports, and
only the second one distinguishes restraint from a bug. This is Session 2's
rule applied to two more layers.

**A long line is refused, not condensed.** Session 5 condenses to the strongest
phrase and that is right for a nine-word line. Past about twenty-two words,
condensing to five does not summarise the sentence — it picks a phrase out of
it and presents that as the sentence. The refusal is `too_many_words` and it is
deliberate.

**`TranscriptEntry.confidence` defaults to 1.0, which means "nobody said".**
Reading the default as a measurement would let a hand-typed SRT outrank a
Whisper transcript that honestly reported 0.9. `_has_confidence` tests for
`< 1.0`, and `--require-caption-confidence` is the switch for people who would
rather refuse than trust.

**A cue names the moment it is for, or it is not placed.** There is no
"place one at every cut" path anywhere in the audio pass. A whoosh needs the
cut to move to a *different source file* — two clips from one recording is a
trim, and marking a trim with a whoosh is exactly the spam this pass exists not
to produce.

**`_covers_speech` returned the speech's start time as its "yes".** A line
spoken at exactly 0.0s was therefore waved through by a falsy sentinel, and a
sting landed on the first word of the episode. It returns `Optional[float]`
now. **Worth checking any predicate whose "true" value is a measurement.**

**Polish reads the cut this run produced, not the cut it started from.** The
retention variant when there is one, the rough cut otherwise, and the base is
recorded on the plan. Planning captions against the pre-retention cut while the
render showed the reshaped one would put every caption at the wrong moment, and
would do it silently.

**Captions are not burned into the proxy, and the reason is the render
strategy.** The renderer encodes each segment and joins them with the concat
demuxer, which is what makes it survive mismatched game capture; burning text
in would mean a second full re-encode of the joined file with a subtitle
filter. The sidecar `.srt` beside the video is the honest alternative, it costs
nothing, and `burned_in` is a field on every plan so nothing can quietly start
claiming otherwise.

**A reliability gate is about validity; `auto/gates.py` is about permission.**
Two things called gates, deliberately in separate packages, because conflating
"may this run touch Premiere" with "did this run produce something usable"
would make both harder to reason about.

**A warning never stops a run, and only five conditions may.** No footage, a
cut with no runtime, a reshaped cut with no runtime, a render claiming a file
it does not have, and a file too small to be video. Everything else warns with
a fix attached — because a pipeline that refuses to finish an overnight run
over a caption density is one nobody runs twice, and a check somebody disables
protects nothing. `test_only_a_short_list_of_checks_may_ever_block` asserts
this by exercising every check against every way its subject can fail.

**A gate about a pass that did not run says `skipped`, never `pass`.**
"Captions are not too dense" is not a true statement about a run with no
captions; it is a question that does not apply. Fifteen green ticks that mean
nothing are worse than five that mean something.

**Every gate carries evidence and a fix.** A gate that says "confidence is low"
without saying what it was over how many words is an opinion, and one with no
suggested fix is a complaint. Both fields are filled by all fifteen and
asserted by a test.

**The transcript gate reads the timeline, not the transcription stage.** A
transcript arrives three ways — Whisper, Premiere, or an `.srt` beside the
footage — and reading only the Whisper stage reported "this run has no
transcript" over an episode whose every line had been read from a sidecar file.
`analyze` now counts words and the gate reads that. **Worth checking any check
that infers a fact from whether a stage ran.**

**The review package is a view, not a record.** `review package` rebuilds it
from what is on disk now, so deleting the proxy and rebuilding says there is no
video. It lives *inside* the run folder because it is about that run, and
deleting the run deletes it — which is the right coupling for a view over
artifacts that would no longer exist.

**Small reports are copied into the review folder; the video is pointed at.**
Copying a proxy to make a folder tidy would be an unkind thing to do to a disk.

**A stage runner's `pipeline.config` is scoped to `artifacts/`, not to the run
folder.** The review package and the checks write *beside* the artifacts, so
they need the shared config, which is seeded into the stage context as
`shared_config`. Without it the review folder landed at
`runs/<id>/artifacts/auto/runs/<id>/review` — one level inside the run it was
describing. **Worth checking any new stage that writes outside `artifacts/`.**

**The summarising stages settle the run status before they read it.** `report`
and `review_package` both describe the run, and `state.status` was still
`"running"` while they ran — true for about four milliseconds and misleading
afterwards. It is computed before those two and recomputed after the loop, so a
summarising stage that blocks still changes the final answer.

**A batch never overwrites anything, including its own summary.** A completed
folder is skipped; `--force` gives it a new run beside the old one. The batch
ID carries a per-second timestamp, so `_unique_batch_id` waits for the next
second rather than reusing a folder.

**A batch that reshapes the cut applies it.** `--retention-cut` in batch mode
sets `retention_mode=retention` rather than `report_only`: forty folders of
decisions nobody asked for and no edit is not what somebody typing that flag
over a library wants.

**Batch mode is sequential and unclever on purpose.** No concurrency, no
retries, no dependency graph. Two runs at once contend for the same analysis
cache and the same GPU, and the failure mode of a parallel batch — two
half-finished runs and no way to tell which log belongs to which — is worse
than the wall-clock it saves.

**`as_str_list` is for labels, `as_text_list` is for prose.** The first splits
on commas, strips trailing full stops and caps each entry at eighty characters,
which is right for "creeper, skeleton" and wrong for a sentence. The review
package's five lists round-tripped through the first one and came back
truncated mid-word.

**A batch entry whose run failed stages is still `completed`.** The batch asked
for that folder to be run and it was; whether the edit is any good is what the
stage counts and the checks are for. Conflating the two would make a batch
summary that could not distinguish "this folder crashed" from "this folder
produced an edit with a blocked critic".

---

## Current state

- **2060 editing tests**, ~130s, needing no FFmpeg, GPU, model server, Premiere,
  Whisper or real media
- `tests/premiere/` passes too (277 there, 2337 across both suites)
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated
- `editing/README.md` is the full user documentation (~4750 lines); §0 covers
  auto mode, §16 the feedback collector, §17 the proxy render, §18 the
  director pass, §19 the retention wiring, §20 key-moment captions, §21 the
  audio polish, §22 the reliability checks, §23 the review package, §24 batch
  mode and §25 running without Premiere

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path.

**Verified on real footage**: `auto run` completes all 29 stages on real MP4s
with real FFmpeg, in mock mode, in about a minute -- including the proxy
render, which produced a 47s video from a three-clip cut. Session 11 was
verified the same way on generated footage with subtitles: a full run with
`--captions key_moments --audio-polish placeholders --retention-cut
--render-proxy` produced a proxy, a caption sidecar beside it, 11 of 15 checks
passing with one warning, and a review index answering all five questions;
`auto batch` over two folders completed both in 7 seconds and skipped them both
on the next pass.
`render roughcut` was verified separately on footage deliberately mixing
1080p60-with-audio and 720p30-with-no-audio: 15.06s produced against 15.0s
planned, joined by stream copy, audio continuous across all three segments. The full gate chain
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
20. **The proxy render shows the cut, not the edit.** V1 only: captions, cards,
    sound effects, music, graphics and markers are all absent from a video that
    otherwise looks finished. Every report lists what it could not show, but a
    person watching still has to remember it.
21. **Speed changes in the proxy are `setpts`/`atempo`, not Premiere's
    retime.** The rendered timing is close, not identical -- the report prints
    the drift, and more than a second or two is worth understanding before
    trusting a timing read off the proxy.
22. **The proxy has never been watched by anyone.** It renders correctly on
    real mixed footage and the numbers check out; nobody has yet sat down with
    a real episode's proxy and said whether the *cut* was any good. That is the
    entire point of the feature and is now the cheapest way to find out whether
    threshold selection produces watchable video.
23. **The director has never been run against a real model on a real episode.**
    The HTTP contract is verified against a loopback OpenAI-compatible server
    and the decisions against fixtures, but whether a 14B or 70B model makes
    better editing choices than `usefulness >= 0.40` is unknown. This is now
    the highest-value next action in the system, and it is one command plus an
    endpoint: `director plan`, then `director compare-heuristic`, then render
    both cuts and watch them.
24. **Nothing checks a director decision for taste.** The twelve rules check
    structure -- does this range exist, is the payoff protected, is its setup
    still in, does the cut fit its runtime. A confident bad creative call
    passes every one of them, which is why `show-rejected` and the comparison
    exist.
25. **The director is one pass, not a conversation.** It does not see the
    render and revise. Feeding the proxy, or the Session 9 feedback log, back
    into a second director pass is the obvious next step and is not built.
26. **The prompt has been tuned against nothing.** Its instructions, its
    ceilings and its output shape are opinions, and the first real run will
    say which of them a model actually follows.
27. **Nobody has watched a retention cut.** The wiring produces the right shape
    on fixtures and on generated footage -- a cold open at the front,
    compressed sag, protected setups -- and whether an episode reshaped this
    way is *better* is a question only a person watching both proxies can
    answer. No number this system produces will ever answer it.
28. **The retention wiring inherits Session 8's calibration, and now spends
    it.** Its risk thresholds, hook scores and severity bands were tuned
    against intuition. Until this session a wrong finding cost a marker in the
    wrong place; now it cuts footage, so the same error is more expensive than
    it was. That makes gap 11 -- the episode layer never checked against a
    real edit -- considerably more urgent than it looked.
29. **The cold open has no taste.** It is chosen by a scoring formula and six
    vetoes, and it has no idea what your channel usually opens on. Pointing
    the director pass at it with a style guide is the intended fix and is not
    wired: the two layers do not talk to each other about the opening.
30. **Nothing reads the feedback log yet**, which is now the oldest unspent
    signal in the system -- the position the retention plan held until this
    session.
31. **Captions are never in the video and are not placed in Premiere.** The
    polish pass produces a plan and a sidecar `.srt`. Burning them into the
    proxy would mean a second full re-encode of the joined file; placing them
    in Premiere would mean wiring this pass into the style layer's operation
    list. Neither is built, and the reports say so on every page.
32. **Nothing in the audio polish plays.** No level is measured, no file is
    listened to, and nothing it plans reaches the proxy. In `assets` mode a
    match is tags, folder and duration -- gap 3, inherited.
33. **The caption and cue heuristics have never seen a real episode.** Nine
    moment kinds, four keyword lists, a scoring formula and a per-minute
    budget, all calibrated against fixtures and intuition. Whether the lines
    it picks are the lines you would have picked is unknown;
    `polish show-rejected` exists largely so that can be found out.
34. **"Background speech" has no diarisation behind it.** What is actually
    detected is low-confidence speech over audio measured as quiet -- two
    pieces of evidence pointing the same way, not a detection. Same honest
    limitation as gap 13.
35. **The reliability checks judge shape, not taste.** All fifteen passing says
    the output is well-formed. It says nothing about the edit, and every
    report repeats that.
36. **The check thresholds are opinions too.** 40 words for a thin transcript,
    0.55 mean confidence, 35% of the base cut, 0.25 MB, 5% duration drift.
    None has been calibrated against a real failure; they are starting points
    chosen to catch obvious breakage without being noisy.
37. **Batch mode is sequential with no retry.** Twenty folders in order, and a
    folder that fails is recorded and skipped past. `--resume` is a separate
    decision made after reading the summary.
38. **Nobody has watched a captioned proxy.** The sidecar loads and the times
    are right on generated footage. Whether the four captions a real episode
    earns are the right four is the same open question as gap 33, and one
    evening with VLC would answer it.

---

## Natural next steps

The system is now usable enough that the next steps are about *reality*, not
features.

- **Watch a captioned proxy with the sidecar loaded.** The cheapest unanswered
  question in the system, and about twenty minutes of work:

  ```cmd
  python -m editing.cli auto run --folder D:\Footage	est --transcribe ^
      --retention-cut --render-proxy --captions key_moments ^
      --audio-polish placeholders --no-premiere
  python -m editing.cli review open-latest
  ```

  Open the proxy in VLC, drop the `.srt` on it, and read every caption against
  what is actually said. Then read `polish show-rejected` and ask whether
  anything in the refusal list should have been kept. Twenty of those would
  calibrate the nine moment kinds better than any amount of further design.
- **Run a batch over a real library overnight.** `--dry-run` first, then
  without it. The property worth confirming is not that it works but that the
  summary is readable the next morning, and that the folders it skipped were
  the right ones.

- **Watch a retention cut against the cut it was built from.** Everything
  Session 10D does is verified structurally and by nobody's eyes:

  ```cmd
  python -m editing.cli retention plan --mode retention
  python -m editing.cli retention compare
  python -m editing.cli retention render --quality proxy
  python -m editing.cli render roughcut          REM the original, to compare
  ```

  Read `retention show-cold-open` first: the opening is the single change most
  likely to be wrong, and the refusal list says what it passed over to get
  there. If the cold open is good and the compression is not, turn compression
  off (`--no-compress`) and keep the rest -- they are independent.
- **Point the director at a real model and compare the two cuts.** This is
  the highest-value action in the system now: the whole of Session 10C is
  built, tested and unverified against an actual model. It needs an endpoint
  and about four commands:

  ```cmd
  set EDITING_DIRECTOR_BASE_URL=http://localhost:8000/v1
  set EDITING_DIRECTOR_MODEL=qwen2.5-14b-instruct
  python -m editing.cli director plan --style-guide docs\my_editing_style.md
  python -m editing.cli director compare-heuristic
  python -m editing.cli director render --quality proxy
  python -m editing.cli render roughcut          REM the threshold cut, to compare
  ```

  Read `compare-heuristic` first: **agreement** near 1.0 means the model
  reproduced the threshold and this layer is not earning its cost, and
  `DECISIONS NO THRESHOLD COULD MAKE` is the column that says otherwise. Then
  watch both proxies, because no number in this system can tell you which cut
  is better.
- **Then tune the prompt against what a model actually does.**
  `editing/director/prompt.py` is one file, the whole instruction is in it, and
  the ceilings (`max_hooks_in_cut`, `max_grind_seconds`, `min_confidence`) are
  opinions in `schema.py`. Expect the first real run to show the model
  over-keeping -- that is the standard failure of a model asked to be an
  editor, and the prompt already argues against it in three places.
- **Render a real episode's rough cut and watch it.** This is now the cheapest
  way to find out whether anything above the rough cut is worth keeping, and it
  needs no GPU, no model server and no Premiere:

  ```cmd
  python -m editing.cli auto run --folder D:\Footage\ep12 --mock --no-premiere ^
      --transcribe --render-proxy
  python -m editing.cli auto report
  python -m editing.cli render open <job_id> --run <run_id>
  ```

  Then write in `review_notes.md` while it plays, and feed the verdicts back
  through `feedback rate`. Expect the `usefulness >= 0.40` threshold in
  `roughcut/select.py` to be the thing that needs changing -- and now there is
  a way to see it.
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

Polish, checks and the review folder -- none of which needs Premiere:

```cmd
python -m editing.cli auto run --folder D:\Footage	est ^
    --captions key_moments --audio-polish placeholders --render-proxy --no-premiere
python -m editing.cli polish show-rejected           REM every refused caption
python -m editing.cli polish show-missing            REM the sound shopping list
python -m editing.cli auto show-checks               REM is the output usable?
python -m editing.cli review summary --latest
python -m editing.cli review open-latest
python -m editing.cli review package --run <run_id>  REM rebuild it from disk now
```

A whole library at once:

```cmd
python -m editing.cli auto batch --root E:\Clips --dry-run
python -m editing.cli auto batch --root E:\Clips --style cinematic_minecraft ^
    --retention-cut --render-proxy --no-premiere --limit 3
python -m editing.cli auto list-batches
python -m editing.cli auto batch-report
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
| `--retention-cut` | reshape the cut: cold open, compressed sag, protected setups |
| `--retention-mode` | `report_only` (default), `retention`, `director_retention`, `hybrid` |
| `--no-cold-open` / `--max-cold-open-seconds` | leave the opening alone, or cap it |
| `--dead-air-aggressiveness` | `low` / `medium` / `high` |
| `--director` | a model reads the episode and chooses the cut (opt-in) |
| `--director-mode` | `hybrid` (default) fills what it did not mention, or `director` |
| `--director-backend` / `--director-model` | which endpoint, and which model |
| `--style-guide <path>` | your prose editing rules, for the director |
| `--target-duration N` | runtime the director should aim at, in seconds |
| `--render-proxy` | also render a watchable proxy MP4 with FFmpeg (opt-in) |
| `--render-quality` / `--render-height` | `draft`/`proxy`/`preview`/`high`, and the height |
| `--feedback` | also open a review session and build its queue (opt-in) |
| `--captions` | `off` (default), `key_moments`, `dense` |
| `--max-captions-per-minute` / `--max-caption-seconds` / `--max-caption-words` | override the style's own ceilings |
| `--min-caption-confidence` / `--require-caption-confidence` | how much ASR confidence a line needs |
| `--audio-polish` | `off` (default), `placeholders`, `assets` |
| `--max-sfx-per-minute` / `--no-music-bed` / `--no-ducking` | how much sound, and whether a bed is allowed |
| `--no-review-package` | do not gather the run into a review folder (on by default) |
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

Retention wiring, which is how the cut stops being chronological:

```cmd
python -m editing.cli retention plan               REM decides, changes nothing
python -m editing.cli retention show-cold-open     REM what it would open on
python -m editing.cli retention show-compression   REM every sagging zone
python -m editing.cli retention show-protected     REM what nothing may touch
python -m editing.cli retention show-rejected      REM and what the rules refused
python -m editing.cli retention plan --mode retention          REM apply it
python -m editing.cli retention plan --mode director_retention REM on the director cut
python -m editing.cli retention compare
python -m editing.cli retention render --quality proxy
```

`report_only` is the default and applies nothing -- read what it wants to do
before letting it. The reshaped cut is written as a *variant*
(`retention/<name>.roughcut.json`); `roughcut/<name>.json` is never touched, so
rejecting the pass costs nothing.

Four things happen, in this order, and the order is the safety model:
**protect** (setups whose payoff is in the cut, payoffs, the peak, callbacks),
**cold open** (the best hook to the front, carved out of where it was),
**compress** (sag: sped up when the picture changes, cut when it does not),
**dead air** (ordinary silence trimmed hard; silence doing a job left alone).

Useful knobs:

```cmd
python -m editing.cli retention plan --mode retention ^
    --dead-air-aggressiveness high ^
    --max-cold-open-seconds 15 ^
    --duplicate-policy shorten ^
    --max-compression 0.4
```

The director pass, which is how the cut stops being a threshold:

```cmd
python -m editing.cli director status              REM is a model reachable
python -m editing.cli director show-style          REM the style guide in force
python -m editing.cli director build-context       REM what it would be shown
python -m editing.cli director build-context --show-prompt
python -m editing.cli director plan                REM ask it, and check it
python -m editing.cli director plan --backend mock REM no model at all
python -m editing.cli director plan --style-guide docs\my_editing_style.md
python -m editing.cli director show-decisions
python -m editing.cli director show-rejected       REM what the rules refused
python -m editing.cli director compare-heuristic   REM is it doing anything
python -m editing.cli director render --quality proxy
```

Every one of these takes `--run <run_id>` to reach an auto run's own plan, the
same way the feedback and render commands do.

Point it at any OpenAI-compatible endpoint -- vLLM, LM Studio, llama.cpp's
server, OpenRouter, Together, OpenAI:

```cmd
set EDITING_DIRECTOR_BASE_URL=http://localhost:8000/v1
set EDITING_DIRECTOR_MODEL=qwen2.5-14b-instruct
set EDITING_DIRECTOR_API_KEY=...
```

The settings are deliberately separate from the vision model's: reading
pictures window by window and reasoning over a whole document want different
models. The brief for a 40-minute episode is roughly 15-25k tokens.

Three modes, and the heuristic never goes away:

```cmd
python -m editing.cli roughcut build                    REM thresholds
python -m editing.cli director plan --mode director     REM only its decisions
python -m editing.cli director plan --mode hybrid       REM its decisions, rest
                                                        REM from the thresholds
```

Proxy rendering, which is how you actually see a cut. No Premiere, no GPU:

```cmd
python -m editing.cli render status                REM is FFmpeg here
python -m editing.cli render roughcut              REM the current rough cut
python -m editing.cli render roughcut --quality draft --height 480
python -m editing.cli render roughcut --max-seconds 90    REM just the opening
python -m editing.cli render from-plan data\editing\roughcut\structure.json
python -m editing.cli render open                  REM play the most recent
python -m editing.cli render open --notes          REM the review file instead
python -m editing.cli render show <job_id>
python -m editing.cli render list
python -m editing.cli render clean --temp-only --yes
```

Every one of these takes `--run <run_id>` to reach an auto run's own render,
the same way the feedback commands do -- each run is hermetic, so its proxy
lives in that run's artifacts and a command without `--run` looks in the shared
directory.

Measured on this machine: a 47-second cut of three clips at 640x360 `draft`
took 2 seconds (27x realtime); at 1280x720 `proxy`, roughly 10x realtime. So a
20-minute cut is about two minutes.

Renders are cached on the cut, the sources, the settings and the FFmpeg
version; `--force` re-encodes anyway. Nothing here executes anything and
nothing writes outside `data/editing/render/`.

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
