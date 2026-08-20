# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Written 2026-08-20.

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`** — head `1066d33`.
This has all three sessions and 562 passing tests.

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

## What was built, in three sessions

```
footage → Premiere mapping → transcript → Qwen3-VL vision ─┐
                                                            ├→ structure timeline
                                            audio events ──┘
                                                    ↓
                       six recommendation layers → safety pass
                                                    ↓
                     rough cut: selected ranges → scratch sequence plan
                                                    ↓
              offline dry-run → (explicit command only) execute → review frames
```

### Session 1 — structure layer (`aa29677`)

- `editing/discovery.py`, `premiere_link.py` — footage scan, ffprobe metadata,
  **read-only** Premiere project mapping
- `editing/transcripts/` — SRT/VTT/CSV/JSON/timestamped-TXT, Premiere
  Speech-to-Text via XMP word markers, sidecar auto-discovery
- `editing/visual/` — sampling planner (motion-driven densification),
  Qwen3-VL client (OpenAI-compatible / Ollama / mock), analyzer
- `editing/align.py` — combined timeline with match/contrast/neutral alignment
- `editing/cache.py` — keyed on fingerprint + model + sampling config

Added two Premiere ops: `transcript.caps` and `transcript.read`
(`extensions/PremiereBridge/host/modules/transcript.jsx`). Adobe documents no
transcript API, so `caps` *measures* what the running build exposes.

### Session 2 — audio + recommendations (`9cfac35`, `bf1ca21`, `b50cad6`)

- `editing/audio/` — silence, spikes, sudden reactions, clipping, low energy,
  speech density, plus inferred laughter/scream/music
- `editing/recommend/` — six layers (story, pacing, visual, audio, polish,
  safety) producing `EditRecommendation` records with evidence

### Session 3 — rough cut (`05baf82`, `1066d33`)

- `editing/roughcut/` — range selection, sequence layout maths, conversion to
  catalog ops, four execution modes, review frame export

---

## Design decisions worth not re-litigating

**Inferred audio is capped at 0.45 confidence.** Silence and clipping are
*measured*; laughter and screaming are guessed from a loudness curve. The cap
is enforced in code (`AudioConfig.max_inferred_confidence`), not just
documented. A transcript marker (`[laughs]`) scores 0.85 and supersedes the
heuristic guess so one laugh is not counted twice.

**`hold` is a first-class recommendation category.** A planner that can only
say "cut here" edits everything.

**The safety pass marks, never deletes.** Rejected and downgraded
recommendations stay in the output with a reason.

**Sequence layout is computed offline.** A clip sped 2x occupies half its
source duration and everything after it moves — all arithmetic, done before
Premiere is touched. That is what makes the plan dry-runnable.

**Speed ops run back-to-front with ripple.** Rippling shifts later clips, so
working backwards means each clip is still where the plan says. Markers and
zooms come after all retiming.

**Execution has four guards, each refusing rather than warning:** no default
runs anything; a dry run must pass in the *same* call; the target must
*structurally* create and activate its own sequence (checked, not trusted from
a flag); a refusal is a result with a reason.

---

## Current state

- **562 tests**, ~5s, needing no FFmpeg, GPU, model server or Premiere
- `tests/premiere/` has 30 pre-existing failures, unchanged from before this
  work — they are not from these sessions
- `editing/README.md` is the full user documentation (~1100 lines)

---

## The honest gaps

1. **Only markers convert to Premiere ops in the *draft* plan.** The rough cut
   converts more (append, speed, zooms, markers) but still not text, colour,
   transitions or audio — each reported per category with its reason.
2. **Speed ripple is assumed, not verified against real Premiere.** The dry run
   validates operation *shape*, not runtime behaviour. Check the first executed
   cut against `roughcut placements`.
3. **Single video track.** Everything assembles onto V1.
4. **Audio is carried, not mixed.** No ducking, levelling or music bed.
5. **Punch-ins are blind to composition.** Refusal rules cover UIs, low health,
   protected and short clips — not where the subject sits in frame.
6. **No critic pass.** Review frames are exported and manifested; nothing looks
   at them yet.
7. **Never tested on real footage.** Everything above is verified against
   synthetic fixtures and the mock backend.

---

## Natural next steps

- **Test on real clips** (see README §Quick start). This is the highest-value
  next action and has not happened.
- **Critic pass** — feed review frames back to Qwen3-VL, judge the cut, revise.
- **Sequence-aware conversion** — once footage is on a timeline, the
  currently-unconvertible categories become reachable.
- **Audio mixing** — turn the ducking/music placeholders into real operations.

---

## Testing on real footage — the short version

```cmd
cd /d E:\Assistant
git checkout claude/editing-brain-v1-structure-7p33pm
git pull

python -m pytest tests/editing -q      REM expect 562 passed
python -m editing.cli doctor           REM what is actually available
```

Put 3–4 short clips (1–3 min each) in one folder, then:

```cmd
REM Stage 1 — plumbing, no GPU needed
python -m editing.cli run --folder D:\Footage\test --recommend ^
    --backend mock --max-windows 8 --no-premiere

REM Stage 2 — real vision model
python -m editing.cli run --folder D:\Footage\test --recommend --no-premiere

REM Stage 3 — rough cut
python -m editing.cli roughcut build
python -m editing.cli roughcut dry-run
python -m editing.cli roughcut execute --yes
python -m editing.cli review
```

`--backend mock` marks every event `mock: true`, so mock output can never be
mistaken for real analysis. FFmpeg is required for audio and review frames;
without it the audio layer degrades to transcript markers only and says so.
