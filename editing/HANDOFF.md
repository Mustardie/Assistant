# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-20 (Session 5).

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all five sessions and 804 passing editing tests.

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

## What was built, in five sessions

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
              offline dry-run → (explicit --yes only) apply to the same
                                scratch sequence, under an op allowlist
                                                    ↓
        style preset → seven layers of candidates: captions, visual emphasis,
        audio placeholders, title/chapter cards, structure and polish markers
                                                    ↓
     dedupe → per-minute density ceilings → ordered, additive-only operations
                                                    ↓
              offline dry-run → (explicit --yes only) applied on top of the
                                same scratch sequence, changing no timing
```

### Session 1 — structure layer (`aa29677`)

- `editing/discovery.py`, `premiere_link.py` — footage scan, ffprobe metadata,
  **read-only** Premiere project mapping
- `editing/transcripts/` — SRT/VTT/CSV/JSON/timestamped-TXT, Premiere
  Speech-to-Text via XMP word markers, sidecar auto-discovery
- `editing/visual/` — sampling planner, Qwen3-VL client, analyzer
- `editing/align.py` — combined timeline with match/contrast/neutral alignment
- `editing/cache.py` — keyed on fingerprint + model + sampling config

### Session 2 — audio + recommendations (`9cfac35`, `bf1ca21`, `b50cad6`)

- `editing/audio/` — silence, spikes, sudden reactions, clipping, low energy,
  speech density, plus inferred laughter/scream/music
- `editing/recommend/` — six layers producing `EditRecommendation` records

### Session 3 — rough cut (`05baf82`, `1066d33`)

- `editing/roughcut/` — range selection, sequence layout maths, conversion to
  catalog ops, four execution modes, review frame export

### Session 4 — critic + one revision pass

- `editing/critic/` — coverage frames, the Qwen3-VL critic, findings →
  revisions, the revision plan, three execution modes with an op allowlist

### Session 5 — style presets + layered execution

- `editing/style/`
  - `presets.py` — four `StylePreset`s, as numbers, with validation
  - `schema.py` — `LayerItem`, `LayeredEditPlan`, seven layers
  - `captions.py` — which lines earn text, condensing, safe zones
  - `emphasis.py` — punches and pushes, stated as refusals
  - `audio.py` — placeholders, plus the two fades that are real
  - `cards.py` — title/chapter cards at genuine section boundaries
  - `compile.py` — dedupe, density enforcement, ordered operations
  - `execute.py` — three modes, the additive-only allowlist, the guards
  - `report.py` — layer-by-layer output, density view, deferred view
- `editing/config.py` — `layers_dir`
- `editing/pipeline.py` — `layers`, `run_layers`, load/write pairs
- `editing/cli.py` — `style list|show`, and `layers build|report|export|
  dry-run|execute --yes|show-deferred|show-density`
- `tests/editing/test_editing_style.py` — 126 tests

---

## Design decisions worth not re-litigating

**Inferred audio is capped at 0.45 confidence.** Silence and clipping are
*measured*; laughter and screaming are guessed. Enforced in code
(`AudioConfig.max_inferred_confidence`), and it survives all the way into the
style layer — a guessed SFX placeholder always ranks below a measured one.

**`hold` is a first-class recommendation category.** A planner that can only
say "cut here" edits everything.

**Nothing is ever deleted, only marked.** The Session 2 safety pass, the
Session 4 critic and the Session 5 compiler all record what they refused and
why. Three passes, one rule.

**Sequence layout is computed offline.** All the arithmetic happens before
Premiere is touched, which is what makes every plan dry-runnable.

**Execution guards, in all three passes:** no default runs anything; a dry run
must pass in the *same* call (a stored pass is not evidence about this plan);
the target must be provably the scratch sequence; a refusal is a returned
result with a reason.

### From Session 4

**A finding is not a fix.** `CriticFinding` and `RevisionRecommendation` are
separate records and the conversion is explicit rules.

**A fix may only act on a premise the plan confirms.** A critic hallucinating a
zoom must not be able to make the system edit one that never existed.

### From Session 5

**Ceilings only ever subtract.** Every density field in a preset is a maximum.
The compiler removes candidates to fit; it never invents one to fill a quota.
A style cannot make the system busier than the evidence justifies — only
quieter. This is the difference between "intentionally styled" and "randomly
over-edited", and there is a test asserting it for all four presets on the same
input.

**The style pass is additive only.** Its allowlist contains no `clip.*`
operation, so it cannot trim, retime, move, split or remove a clip. Two things
follow: nothing ripples (so unlike Session 4 there are no marker positions to
correct and no unverified assumption), and the whole pass can be undone by
deleting one track and its markers.

**Markers are free; picture changes are expensive.** Markers are not counted
against any ceiling, and `is_active` is false for a marker-only item — so a
caption that could not be placed safely does not spend the budget that would
have let the next real edit through.

**Refusing to place text is a first-class outcome.** When a menu is open, when
the critic flagged the moment, or when no safe zone is left, the caption
becomes a marker *carrying the line*. Never text placed hopefully over the
game.

**A section boundary held back for room still leaves a marker.** Everything
else deferred stays deferred, but a documentary that silently loses a chapter
has lost the thing the style was chosen for.

**Stacking is per sense.** Two things happening to the picture at once fight
each other; an audio fade under a title card is ordinary editing. Enforcing one
global stack rule made the pass drop fades for no reason.

**Density is a window count, not a derived spacing.** Deriving "60/rate seconds
apart" from the per-minute ceiling forced perfectly even distribution, and a
style advertising seven edits a minute delivered four. The rate is now a count
inside a rolling 60-second window (the whole cut, when the cut is shorter than
a minute, so the headline figure stays honest); *spacing* comes from the
style's own `min_edit_spacing` / `min_caption_spacing` fields, which is what
those fields are for. The one exception is a rate below one per minute, which
cannot be a window count at all and does imply spacing.

---

## Current state

- **804 editing tests**, ~9s, needing no FFmpeg, GPU, model server or Premiere
- `tests/premiere/` passes too (1081 across both suites)
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated to this work
- `editing/README.md` is the full user documentation (~1740 lines); sections 11
  and 12 cover the style layer

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path:

```
python -m pytest tests/editing -q --basetemp=%TEMP%\pt
```

---

## The honest gaps

1. **Never tested with a real Qwen3-VL critic.** The critic pass has been run
   end to end on real footage with real FFmpeg, but only with `MockCritic`.
   **The critic prompt has never been seen by a model.** Still the
   highest-value next action.
2. **Never executed against real Premiere.** Every plan in Sessions 3–5
   dry-runs clean, but the only thing that has run against the real host is the
   Premiere self-test. The first real `execute --yes` will find things the
   validator cannot.
3. **`text.create` uses the rasterised path**, so captions are PNG overlays and
   are **not editable in Premiere after placement**. Live text needs a
   registered `.mogrt` template.
4. **Style numbers are opinions**, not measured optima. They are meant to be
   edited.
5. **Caption selection is keywords**, English-only, tuned for Minecraft
   commentary. It will miss sarcasm and running jokes.
6. **Chapter detection is structural** (dimension change, death, stated
   objective). A tonal section change is invisible to it.
7. **Single video track for overlays**, single video track for the assembly.
   No B-roll, no picture-in-picture.
8. **No music, SFX or callout graphics exist.** Every audio cue except the two
   fades is a marker; usually most of a styled pass is placeholder.
9. **The style layer cannot re-cut.** `trim_aggression` and
   `dead_air_tolerance` are carried and reported but not acted on — changing
   the assembly's pacing means rebuilding the rough cut.
10. **Marker positions after a Session 4 timing change are computed, not
    observed** (unchanged from Session 4; `review plan --no-timing` avoids it).
11. **Speed ripple is still assumed, not verified** (Session 3's gap).

---

## Natural next steps

- **Run the critic against a real Qwen3-VL server**, then read the findings.
  Expect the prompt to need tuning.
- **Execute a styled pass against real Premiere**, starting with
  `layers build --style minimal_clean --markers-only` — which draws nothing,
  scales nothing, and only places markers. If that lands cleanly, step up to
  `cinematic_minecraft` and then to a style that draws text.
- **Register a `.mogrt` template** so captions become live text.
- **Tune the presets against real footage.** The accept/defer ratio per style
  is the number to watch: `layers show-deferred` groups it by reason.
- **Audio mixing** — turn the placeholders into real operations once a library
  exists. The schema and the ranges are already there.

---

## Testing on real footage — the short version

```cmd
cd /d E:\Assistant
git checkout claude/editing-brain-v1-structure-7p33pm

python -m pytest tests/editing -q --basetemp=%TEMP%\pt   REM expect 804 passed
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
python -m editing.cli style show cinematic_minecraft
python -m editing.cli layers build --style minimal_clean --markers-only
python -m editing.cli layers show-density
python -m editing.cli layers show-deferred
python -m editing.cli layers report
python -m editing.cli layers dry-run
python -m editing.cli layers execute --yes
```

Restyling is free and non-destructive: `layers build --style <other>` replaces
the layer plan and touches nothing else. The rough cut, the critique and the
revisions all survive it.

If a styled pass looks too busy, the dials in order of bluntness are
`--markers-only` (draws nothing at all), `--no-text` / `--no-zooms`,
`--max-edits-per-minute N`, or simply a tighter preset.
