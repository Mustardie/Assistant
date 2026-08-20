# Editing Brain V1 — session handoff

Context for continuing this work in a new chat. Updated 2026-08-20 (Session 4).

---

## Where the work lives

**Good branch: `claude/editing-brain-v1-structure-7p33pm`.**
This has all four sessions and 678 passing editing tests.

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

## What was built, in four sessions

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

### Session 4 — critic + one revision pass

- `editing/critic/` — the whole pass:
  - `schema.py` — `CriticFinding`, `RevisionRecommendation`, `RevisionPlan`
  - `frames.py` — coverage rules and context enrichment (pure)
  - `prompt.py` — what the critic is asked
  - `critic.py` — the model call, the coercion, `MockCritic`
  - `revise.py` — findings → revisions; **the safety rules live here**
  - `plan.py` — accepted revisions → one ordered operation plan
  - `execute.py` — three modes, the allowlist, the guards
  - `report.py` — the human-readable output
- `editing/roughcut/review.py` — `ReviewFrame` extended with context fields;
  `export_frames(frames=...)` is the seam the critic plans through
- `editing/roughcut/schema.py` — `ClipPlacement.sequence_to_source`, the
  inverse the review pass needs
- `editing/config.py` — `critic_dir`
- `editing/pipeline.py` — `critique`, `revise`, `run_revisions`, and the
  load/write pairs for each artefact
- `editing/cli.py` — `review` became a subcommand group
  (`export-frames | critique | plan | dry-run | execute --yes | report |
  show-issues`); a bare `review` still means `export-frames`
- `tests/editing/test_editing_critic.py` — 116 tests

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
zooms come after all retiming. Revision trims follow the same rule.

**Execution has four guards, each refusing rather than warning:** no default
runs anything; a dry run must pass in the *same* call; the target must
*structurally* create and activate its own sequence (checked, not trusted from
a flag); a refusal is a result with a reason.

### New in Session 4

**A finding is not a fix.** `CriticFinding` (what the model saw) and
`RevisionRecommendation` (what the system proposes) are separate records, and
the conversion is a set of explicit rules in `revise.py`. This is what makes
"unsafe findings stay recommendations" enforceable rather than aspirational.

**Confidence gates action; severity does not.** 0.60 to change the edit at all,
0.70 for timing, 0.80 to cut footage. Severity only decides how loudly it is
reported and whether it earns a marker.

**A fix may only act on a premise the plan confirms.** `reduce_zoom` needs a
zoom in the plan at that moment; `trim_dead_air` needs an audio event;
`extend_hold` needs source headroom. Without it the fix is refused with
`not_verifiable`. **A critic hallucinating a zoom must not be able to make the
system edit one that never existed** — this is the single most important rule
in the session and it has a test named after it.

**Amounts are fixed, not suggested.** 106% for a reduced zoom, ≤0.5s for a hold
extension, ≤1.0s for a trim, never leaving a clip under 1.0s. "Make it a bit
less" from a VLM is not a number.

**One frame per model call.** A small VLM shown six stills attributes a problem
in frame four to frame one. A finding at the wrong moment is worse than none.

**The revision allowlist is the whole guarantee.** A rough cut proves safety by
creating its own sequence; a revision cannot, because it edits one that already
exists. Instead: the first op must be `sequence.activate` naming the rough
cut's sequence, and every op must be one of six
(`sequence.activate`, `property.reset`, `animate`, `clip.trim`, `marker.add`,
`marker.remove`). Nothing on that list can reach another sequence or the disk.
A test asserts the set of ops the code can emit equals the allowlist.

**The rough cut's report is never overwritten.** Critic output lives in
`data/editing/critic/`. A second opinion that destroyed its own baseline would
be worthless.

**Review-frame dedupe is per-clip, never across a cut.** The two edge probes
either side of a cut are always closer together than the collapse threshold, so
a global dedupe silently drops *every* incoming-cut frame in the review. This
was a real bug found by running the CLI on real footage, and
`test_both_sides_of_a_cut_are_sampled` is what stops it coming back.

**The revision report leads with what it could not fix.** The automatic fixes
are bounded and reversible by construction; the deferred findings are where the
real problems are. Burying them under a list of successes is how a report stops
being read.

---

## Current state

- **678 editing tests**, ~9s, needing no FFmpeg, GPU, model server or Premiere
- `tests/premiere/` passes too (953 across both suites). The 30 failures noted
  in the previous handoff no longer reproduce.
- 13 failures elsewhere in `tests/` (file manager, gmail, agent loop, llm
  client) are pre-existing and unrelated to this work.
- `editing/README.md` is the full user documentation (~1400 lines); sections 9
  and 10 cover the critic pass.

**Note on running the tests here:** pytest's default temp root
(`%LOCALAPPDATA%\Temp\pytest-of-nadel`) is not writable in this environment and
every test errors in setup. Pass `--basetemp` at a writable path:

```
python -m pytest tests/editing -q --basetemp=%TEMP%\pt
```

---

## The honest gaps

1. **Never tested with a real Qwen3-VL critic.** The whole pass has been run end
   to end on real footage with real FFmpeg, but only with `MockCritic`, which
   reads frame metadata rather than pictures. **The critic prompt has never been
   seen by a model.** This is the highest-value next action.
2. **Marker positions after a timing change are computed, not observed.**
   Premiere sequence markers do not ripple with clips. New markers this pass
   places are corrected offline; pre-existing rough-cut markers after a trim are
   counted and warned about, not moved. `--no-timing` avoids it entirely.
3. **Speed ripple is still assumed, not verified** (Session 3's gap, unchanged).
4. **Only markers convert in the Session 2 *draft* plan.** The rough cut and the
   revision pass convert more; text, colour, transitions and audio still do not.
5. **Single video track.** Everything assembles onto V1.
6. **Audio is carried, not mixed.** No ducking, levelling or music bed.
7. **Punch-ins are blind to composition** at planning time. The critic can now
   catch a bad one *after the fact*, which is the mitigation, not a fix.
8. **The pass is one iteration.** Re-critiquing after applying revisions needs
   frames re-exported from an updated cut, which is not automated.
9. **Text fixes move a placeholder marker, not text.** No graphic exists.

---

## Natural next steps

- **Run the critic against a real Qwen3-VL server** on a cut built from real
  footage, and read the findings. Expect the prompt to need tuning — the issue
  guide and the "most frames are fine" instruction are the two levers.
- **Measure the accept/defer ratio on real output.** If nearly everything
  defers, the thresholds are too tight; if a lot converts, look hard at whether
  the premise checks are actually catching hallucinated zooms.
- **A second critic iteration**, driven off a re-export after revisions apply.
- **Sequence-aware conversion** — once footage is on a timeline, the
  currently-unconvertible categories become reachable.
- **Audio mixing** — turn the ducking/music placeholders into real operations.

---

## Testing on real footage — the short version

```cmd
cd /d E:\Assistant
git checkout claude/editing-brain-v1-structure-7p33pm

python -m pytest tests/editing -q --basetemp=%TEMP%\pt   REM expect 678 passed
python -m editing.cli doctor                             REM what is available
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

REM Stage 4 — the critic pass
python -m editing.cli review export-frames
python -m editing.cli review export-frames --list    REM see the choice first
python -m editing.cli review critique --backend mock REM plumbing check
python -m editing.cli review critique                REM the real critic
python -m editing.cli review show-issues --severity medium
python -m editing.cli review plan
python -m editing.cli review report
python -m editing.cli review dry-run
python -m editing.cli review execute --yes
```

`--backend mock` marks every event and every finding `mock: true`, so mock
output can never be mistaken for real analysis. FFmpeg is required for audio and
review frames; without it the audio layer degrades to transcript markers only
and says so.

If a revision pass looks too aggressive, the two dials are
`review plan --no-timing` (no trims or extensions at all) and
`review plan --min-confidence 0.8`.
