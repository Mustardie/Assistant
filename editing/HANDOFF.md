# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-20 (Session 6).

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all six sessions and 924 passing editing tests.

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

## What was built, in six sessions

```
footage → Premiere mapping → transcript → Qwen3-VL vision ─┐
                                                            ├→ structure timeline
                                            audio events ──┘
                                                    ↓
                       six recommendation layers → safety pass
                                                    ↓
                     rough cut: selected ranges → scratch sequence plan
                                                    ↓
              offline dry-run → (explicit command only) execute
                                                    ↓
             review frames chosen by coverage rule, with their full context
                                                    ↓
                    Qwen3-VL critic (one frame per call) → findings
                                                    ↓
        revision recommendations — safe ones carry draft operations, the rest
        stay recommendations with the reason they could not be automated
                                                    ↓
              offline dry-run → (--yes only) applied to the same sequence
                                                    ↓
        style preset → seven layers: captions, emphasis, audio placeholders,
        title/chapter cards, structure and polish markers
                                                    ↓
     dedupe → per-minute density ceilings → additive-only operations → dry-run
                                → (--yes only) applied, changing no timing
                                                    ↓
       local asset library (indexed, with optional sidecar metadata) matched
       against every placeholder → mixing safety rules → real SFX, music beds,
       ambience and graphics on their own tracks
                                                    ↓
              offline dry-run → (--yes only) placed, never on V1/A1
```

### Sessions 1–3 (`aa29677` … `1066d33`)

- `editing/discovery.py`, `premiere_link.py`, `transcripts/`, `visual/`,
  `align.py`, `cache.py` — the structure layer
- `editing/audio/`, `editing/recommend/` — audio events and six
  recommendation layers with a safety pass
- `editing/roughcut/` — selection, layout maths, catalog ops, four execution
  modes, review frames

### Session 4 — critic + one revision pass

- `editing/critic/` — coverage frames, the Qwen3-VL critic, findings →
  revisions, the revision plan, three execution modes with an op allowlist

### Session 5 — style presets + layered execution

- `editing/style/` — four presets, seven layers, density enforcement, an
  additive-only allowlist (no `clip.*` at all)

### Session 6 — asset library + real placement

- `editing/assets/`
  - `schema.py` — `AssetItem`, `AssetTag`, `AssetLibrary`, `AssetMatch`,
    `AssetPlacement`, `AssetPlacementPlan`
  - `library.py` — the folder layout, `init` (folders + README + example
    sidecar), category inference, the skip list
  - `indexer.py` — scanning, fingerprinting, probing, filename/folder inference
  - `sidecar.py` — `<filename>.asset.json`, parsed so bad JSON never raises
  - `match.py` — the whole matching policy as data, every rejection kept
  - `place.py` — a chosen asset → operations, under the mixing rules
  - `compile.py` — the pass, and the five possible outcomes
  - `execute.py` — three modes, the allowlist, the track and import guards
  - `report.py` — the shopping list, the refusals, and what was placed
- `editing/config.py` — `asset_library_dir`
- `editing/pipeline.py` — `init_assets`, `index_assets`, `asset_plan`,
  `run_assets`, load/write pairs
- `editing/cli.py` — `assets init|index|list|show|validate|report|match|plan|
  dry-run|execute --yes|show-missing|show-deferred`
- `tests/editing/test_editing_assets.py` — 120 tests

**No new Premiere primitives were needed.** Everything the asset pass does uses
ops that already existed: `clip.overwrite`, `graphic.image`, `audio.gain`,
`audio.fade`, `audio.duck`, `track.add`, `project.import`.

---

## Design decisions worth not re-litigating

**Inferred audio is capped at 0.45 confidence**, and the cap survives all the
way into asset ranking: a guessed SFX placeholder scores below a measured one.

**Nothing is ever deleted, only marked.** The Session 2 safety pass, the
Session 4 critic, the Session 5 compiler and the Session 6 placer all record
what they refused and why. Four passes, one rule.

**Execution guards, in all four passes:** no default runs anything; a dry run
must pass in the *same* call; the target must be provably the scratch sequence;
a refusal is a returned result with a reason.

### From Session 4

**A finding is not a fix**, and **a fix may only act on a premise the plan
confirms** — a critic hallucinating a zoom cannot make the system edit one.

### From Session 5

**Ceilings only ever subtract.** A style can make the edit quieter than the
evidence justifies, never busier.

**The style pass is additive only** — no `clip.*` operation at all, so nothing
ripples and nothing it plans can describe a frame that moved.

**"N per minute" means two different things** above and below one: a rolling
window count, or a whole-cut budget plus derived spacing.

### From Session 6

**Bad silence is better than random annoying SFX.** Every rule is written to
make refusing cheap. Five outcomes per placeholder, four of which place
nothing, each naming the rule that stopped it. On most libraries most of a plan
is markers — that is the design working, and the marker list doubles as a
shopping list.

**An empty library is a valid input.** Zero files produces a complete,
dry-run-valid plan of markers. Nobody has a tagged sound library on day one.

**Unreadable metadata is a flag, not a failure, and never "safe".** A file
whose sidecar will not parse is indexed, marked `needs_review`, and held out of
automatic placement — because metadata we could not read is not the same as
metadata that said yes. Individual bad *fields* are dropped with a note while
the rest of the document is kept.

**`clip.overwrite`, never `clip.insert`.** Insert ripples; overwrite does not.
That is what lets this pass place clips without moving anything Sessions 3–5
computed.

**Never V1 or A1**, checked structurally on every operation. Assets land on
tracks the plan adds, so the pass is undone by deleting those tracks and the
markers. V1/A1 are rejected even as *configuration*.

**Two clips never overlap on one track** — correctness rather than taste, since
`clip.overwrite` destroys what is under it. Two beds on A3 was the realistic
case and it would have looked fine in the plan.

**Rotation is not rationing.** Reusing an asset costs something only while a
suitable alternative is unused; measured against the *viable* candidates, not
the whole category. Measuring it against the category meant an unused impact
sound made repeating the only whoosh look expensive.

**Ducking is the Session 5 unlock.** `audio.duck` needed a bed clip and there
was none; placing one makes it real, using the exact speech ranges Session 5
already computed and stored in its `duck_narration` placeholder.

---

## Current state

- **924 editing tests**, ~10s, needing no FFmpeg, GPU, model server, Premiere
  or real media
- `tests/premiere/` passes too (1200 across both suites)
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated
- `editing/README.md` is the full user documentation (~2130 lines); sections 13
  and 14 cover the asset system

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path:

```
python -m pytest tests/editing -q --basetemp=%TEMP%\pt
```

---

## The honest gaps

1. **Never tested with a real Qwen3-VL critic.** The critic prompt has never
   been seen by a model. Still the highest-value next action.
2. **Never executed against real Premiere.** Every plan in Sessions 3–6
   dry-runs clean; the only thing that has run against the real host is the
   Premiere self-test. The first real `execute --yes` will find things the
   validator cannot.
3. **Asset matching is tags and folders, not listening.** No audio content
   analysis at all. A badly named file matches badly; the fix is a sidecar.
4. **Loudness and BPM are never measured** — sidecar-only. Levels come from a
   small table of category defaults, which are opinions.
5. **Beds are tiled, not crossfaded.** Seams are audible if a file does not
   loop cleanly, and only the filename or sidecar says whether it does.
6. **Asset tracks are assumed, not discovered** (A2/A3/V3). The plan cannot
   read the sequence's real track layout offline.
7. **Nothing is ever removed.** Re-running the asset pass places assets again
   rather than replacing the previous run's.
8. **`.mogrt` is matched and never placed** — needs a registered template and
   a parameter mapping.
9. **`text.create` uses the rasterised path**, so captions are PNG overlays and
   are not editable in Premiere after placement.
10. **Style numbers and mixing limits are opinions**, meant to be edited.
11. **Marker positions after a Session 4 timing change are computed, not
    observed**; **speed ripple is still assumed** (Session 3's gap).

---

## Natural next steps

- **Run the critic against a real Qwen3-VL server**, then read the findings.
- **Execute against real Premiere, in stages.** The safest ladder is:
  `layers build --style minimal_clean --markers-only` (draws nothing), then
  `assets plan --markers-only` (places nothing), then a real style, then real
  assets. Each step is one `--yes` and each is undone by deleting a track.
- **Put a real sound library together** and read `assets show-missing` — it is
  written to be a shopping list.
- **Loudness measurement.** `loudness_db` exists on every asset and is only
  ever populated by hand; an ffmpeg `ebur128` pass would make levels measured
  rather than assumed, and the schema is already shaped for it.
- **Track discovery**, so A2/A3/V3 are read from the sequence rather than
  assumed.

---

## Testing on real footage — the short version

```cmd
cd /d E:\Assistant
git checkout claude/editing-brain-v1-structure-7p33pm

python -m pytest tests/editing -q --basetemp=%TEMP%\pt   REM expect 924 passed
python -m editing.cli doctor
```

Put 3–4 short clips (1–3 min each) in one folder, then:

```cmd
REM Stages 1-3: structure, recommendations, rough cut
python -m editing.cli run --folder D:\Footage\test --recommend --no-premiere
python -m editing.cli roughcut build
python -m editing.cli roughcut dry-run
python -m editing.cli roughcut execute --yes

REM Stage 4: the critic pass
python -m editing.cli review export-frames
python -m editing.cli review critique
python -m editing.cli review plan
python -m editing.cli review dry-run
python -m editing.cli review execute --yes

REM Stage 5: the styled, layered edit
python -m editing.cli style list
python -m editing.cli layers build --style cinematic_minecraft
python -m editing.cli layers show-density
python -m editing.cli layers dry-run
python -m editing.cli layers execute --yes

REM Stage 6: real sounds and graphics
python -m editing.cli assets init
REM ... copy your own music/SFX/PNGs into the folders it made ...
python -m editing.cli assets index
python -m editing.cli assets report            REM what you can and cannot serve
python -m editing.cli assets validate          REM anything broken?
python -m editing.cli assets plan
python -m editing.cli assets show-missing      REM the shopping list
python -m editing.cli assets show-deferred     REM what it refused, and why
python -m editing.cli assets dry-run
python -m editing.cli assets execute --yes
```

`assets plan --markers-only` matches everything and places nothing — the best
way to read what the pass *wants* to do before letting it do any of it.
`assets match <kind>` prints the full scoring for every candidate, which is the
tool for "why did it pick that?" and "why not mine?".

If a pass is too busy, the dials in order of bluntness are `--markers-only`,
`--min-score 0.7`, `--max-sfx-per-minute 2`, `--min-sfx-gap 6`, or a tighter
style preset.
