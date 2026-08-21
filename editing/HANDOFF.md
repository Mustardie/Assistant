# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-20 (Session 7).

---

## Start here

```cmd
cd /d E:\Assistant
python -m pytest tests/editing -q --basetemp=%TEMP%\pt    REM expect 992 passed

REM the whole pipeline, planning only, with nothing installed:
python -m editing.cli auto run --folder D:\Footage\test --mock --no-premiere
python -m editing.cli auto report
```

Everything below is detail behind that.

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all seven sessions and 992 passing editing tests.

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

## What was built, in seven sessions

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
      Session 7 wraps all of it: one command, sixteen checkpointed stages,
      four named execution gates, resumable, with a report that says what it
      did not do
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

---

## Current state

- **992 editing tests**, ~60s, needing no FFmpeg, GPU, model server, Premiere
  or real media
- `tests/premiere/` passes too (1269 across both suites)
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated
- `editing/README.md` is the full user documentation (~2370 lines); §0 covers
  auto mode

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path.

**Verified on real footage**: `auto run` completes all sixteen stages on three
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
| `--skip-review` / `--skip-assets` | skip a whole pass |
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

The stage-by-stage commands from Sessions 1–6 all still work unchanged, and
`auto` is a thin layer over them — anything it does can be done by hand, and
the failure messages say which command to run.
