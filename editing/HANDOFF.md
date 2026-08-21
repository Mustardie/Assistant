# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-21 (Session 8).

---

## Start here

```cmd
cd /d E:\Assistant
python -m pytest tests/editing -q --basetemp=%TEMP%\pt    REM expect 1133 passed

REM the whole pipeline, planning only, with nothing installed:
python -m editing.cli auto run --folder D:\Footage\test --mock --no-premiere
python -m editing.cli auto report
```

Everything below is detail behind that.

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all eight sessions and 1133 passing editing tests.

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

## What was built, in eight sessions

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

---

## Current state

- **1133 editing tests**, ~60s, needing no FFmpeg, GPU, model server, Premiere
  or real media
- `tests/premiere/` passes too (1410 across both suites)
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated
- `editing/README.md` is the full user documentation (~2370 lines); §0 covers
  auto mode

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path.

**Verified on real footage**: `auto run` completes all eighteen stages on three
real MP4s with real FFmpeg, in mock mode, in about a minute. The full gate
chain (execute rough cut → resume → execute layers) is verified against a fake
engine.

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

---

## Natural next steps

The system is now usable enough that the next steps are about *reality*, not
features.

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

The stage-by-stage commands from Sessions 1–6 all still work unchanged, and
`auto` is a thin layer over them — anything it does can be done by hand, and
the failure messages say which command to run.
