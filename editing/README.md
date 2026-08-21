# Editing Brain V1

Turns a folder of Minecraft footage into a machine-readable timeline of **what
happens on screen**, **what is being said**, and **what is heard** — proposes
the edits worth making, with the evidence for each one — assembles them into a
real Premiere rough cut, looks at that cut and improves it once, layers a
chosen editing style over it, and fills that style's placeholders from a local
library of your own music, effects and graphics.

**Start here:** one command plans the whole thing.

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --style cinematic_minecraft
```

It executes nothing. See [§0 Auto mode](#0-auto-mode) — the rest of this
document is the stage-by-stage detail behind it.

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
                                                    ↓
                     Qwen3-VL critic → findings → revision recommendations
                                                    ↓
              offline dry-run → (explicit --yes only) apply to the same sequence
                                                    ↓
         style preset → seven layers: captions, emphasis, audio, cards, markers
                                                    ↓
        density ceilings enforced → offline dry-run → (--yes only) applied on top
                                                    ↓
      local asset library → matched per placeholder → mixing safety rules
                                                    ↓
   real SFX/music/graphics on their own tracks → dry-run → (--yes only) placed
```

**Nothing runs unless you say so, twice.** Every plan is validated offline
first, execution needs an explicit `--yes`, and the target is always a scratch
sequence — one the rough cut creates itself, and the only one the revision pass
is allowed to touch. Your open sequence is never edited. Everything the layer
writes is JSON, plus two plain-text reports.

**A style can only make the edit quieter.** Every number in a preset is a
ceiling, never a target: the compiler removes candidates to fit inside one, and
never invents an edit to fill a quota. So a style cannot make the system busier
than the evidence justifies — which is the difference between an edit that
feels intentionally styled and one that feels randomly over-edited.

**The style pass cannot change timing.** Its operation allowlist contains no
`clip.*` operation at all. It adds a track, scales clips already on the
timeline, writes audio keyframes, places overlays and drops markers — so the
rough cut's layout is untouched underneath it, and the whole pass can be undone
by deleting one track and its markers.

**Assets are local files only.** Nothing is ever downloaded, and nothing in
your library is ever modified — trimming, gain, fades and ducking are all
expressed as operations on the *placed clip*. A library with no files in it
still produces a complete plan: every placeholder becomes a marker, and that
list doubles as a shopping list.

**The critic pass is one iteration, not a loop.** It exists to catch the obvious
mistakes an automatic assembly makes — a zoom that crops the HUD, a caption over
the action, a beat cut a moment early. It is not trying to converge on a
finished edit, and most of what it finds it deliberately hands back to you
rather than fixing.

---

## 0. Auto mode

Six sessions produced about forty commands. `auto` is the thing that remembers
which order they go in.

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --style cinematic_minecraft
```

That runs eighteen stages — discovery, transcripts, audio, vision, timeline,
recommendations, rough cut, critic, style layers, asset placement, episode
memory, retention plan — and writes
a plan and an offline dry run for each of the four things that could touch
Premiere.

**It executes nothing.** Not by accident, and not by default. Execution is four
separate decisions:

```bash
python -m editing.cli auto show-gates
python -m editing.cli auto execute-stage roughcut --yes
python -m editing.cli auto resume                    # rebuild now the sequence exists
python -m editing.cli auto execute-stage layers   --yes
python -m editing.cli auto execute-stage assets   --yes
```

There is deliberately no `--execute-everything`. The four passes carry
genuinely different risk — the rough cut builds a sequence, the revision pass
can ripple timing, the style pass is additive, the asset pass places clips —
and one switch for all of them would mean approving the riskiest by approving
the safest.

### Try it with nothing installed

No GPU, no model server, no Premiere:

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --mock --no-premiere
```

`--mock` swaps in the deterministic vision and critic stand-ins; `--no-premiere`
makes every gate permanently shut, so the run *cannot* execute anything even by
accident. This is the mode for finding out whether the pipeline works on your
footage before committing an afternoon to it.

### What a run looks like

```
  + doctor                 passed   ffmpeg=True, ffprobe=True
  + discover               passed   files=3, total_seconds=120.0
  + analyze                passed   segments=12, covered_seconds=120.0
  + recommend              passed   total=33, accepted=30
  + roughcut_build         passed   clips=9, cut_duration=99.0
  + roughcut_dry_run       passed   dry_run_passed=True, operations=40
  + review_export_frames   passed   frames=29
  + review_critique        passed   mock=True, findings=9
  + review_plan            passed   revisions=9, accepted=0
  + review_dry_run         passed   operations=4
  + layers_build           passed   planned=36, deferred=13
  + layers_dry_run         passed   operations=29
  + assets_index           passed   total=0
  + assets_plan            passed   placeholders=11, placed=0, missing=11
  + assets_dry_run         passed   operations=12
  + report                 passed
```

### The run folder

Every run gets its own directory under `data/editing/auto/runs/<run_id>/`:

```
config.json      exactly what the run was invoked with
state.json       every stage result and gate, rewritten after each stage
checkpoints/     one file per completed stage, with artifact fingerprints
artifacts/       the run's own output_dir -- timelines, plans, reports
reports/         report.json and report.txt
logs/            run.log
```

The run ID is `<timestamp>-<folder hash>-<style>`, so runs sort by time, group
by footage, and are told apart by style.

**Each run is hermetic.** `artifacts/` is that run's `output_dir`, so two runs
over the same footage with different styles cannot overwrite each other's
plans, and deleting a run folder removes everything it produced and nothing it
did not.

The one deliberate exception is the **analysis cache**, which stays shared at
`data/editing/cache`. Making it per-run would mean paying hundreds of model
calls again on every run.

### Checkpoints and resume

A stage is skipped only when its checkpoint can be *proved* still valid:

- the artifacts it named still exist,
- their fingerprints still match, and
- the config fields that stage depends on have not changed.

Deleting a timeline by hand re-runs the analysis. A checkpoint that will not
parse re-runs its stage.

```bash
python -m editing.cli auto resume                          # continue
python -m editing.cli auto resume --style fast_funny       # restyle in place
python -m editing.cli auto resume --refresh roughcut_build # and everything after it
python -m editing.cli auto status
python -m editing.cli auto list-runs
```

**Restyling is cheap.** `auto resume --style <preset>` changes the style on the
existing run: the style is one of the fields the layer and asset stages
fingerprint, so exactly those checkpoints go stale and the analysis is reused.

`auto run --style <other>` instead starts a *new* run — the style is part of
the run ID — so it re-runs every stage. That is usually still fast, because the
expensive part (frame extraction, model calls, probes) comes from the **shared
cache** rather than from the run's own checkpoints, but `resume --style` is the
cheaper and more direct way to compare two styles over one analysis.

`resume` retries failed **and blocked** stages, because the usual reason to
type it is that you just installed the missing thing.

### Failure output

Every stopping point carries what failed, why, whether the run can resume, and
the command to try next:

```
Stage discover found no footage
  why    : there are no video files in 'D:/Footage/ep12'.
  resume : yes
  next   : python -m editing.cli discover --folder <folder>
  log    : .../logs/run.log
  report : .../reports/report.txt
```

A traceback reaching you is a bug in this package.

**Not every stage is critical.** The review pass needs FFmpeg and a model
server, and neither is guaranteed. When one is missing those stages go
`blocked` with a reason, the run *continues* to the style and asset passes, and
the report says what was lost. A missing critic costs you the critic, not the
run.

### Gates

```bash
python -m editing.cli auto show-gates
```

```
- roughcut   Build the rough cut sequence in Premiere
    plan       : .../artifacts/roughcut/structure.json
    dry run    : passed
    sequence   : Nova Rough Cut
    operations : 40
    riskiest   : clip.remove -- deletes a clip
    on scratch : True
    run        : python -m editing.cli auto execute-stage roughcut --run <id> --yes
```

Every gate reports the **riskiest operation it would perform**, because that is
the question a person actually has before typing `--yes`.

A gate refuses when: the dry run has not passed in this run, the plan is
missing or empty, the target is not provably a scratch sequence, the run was
created with `--no-premiere`, Premiere is unreachable, the stage was already
executed, or `--yes` was omitted.

**The rough cut comes first, and the others wait for it.** The style, review and
asset plans each record whether the sequence exists in Premiere. Until the
rough cut is executed they say "no" and their gates stay shut, naming the
command that opens them. After it is executed those plans are marked stale, and
one `auto resume` rebuilds them:

```
execute roughcut  ->  31 operations
auto resume       ->  layers and assets gates open
execute layers    ->  16 operations
```

### Recovering from common failures

| What you see | What to do |
|---|---|
| `discover found no footage` | check the folder path; `auto resume` after fixing |
| `could not reach the vision model` | start the server, or re-run with `--mock` |
| `could not analyse the footage` (FFmpeg) | install FFmpeg, put it on PATH, `auto resume` |
| `review_*` blocked | optional pass; the run continued without it |
| `there is no asset library at ...` | `assets init`, add files, `auto resume` |
| `Premiere Bridge is unreachable` | start Premiere, open the Nova panel, re-run the gate |
| `this plan was built before the rough cut was executed` | `auto resume` |
| `corrupted state.json` | `auto clean --run <id> --yes`, then start again |

`auto explain-failure` prints every failed and blocked stage with the command
for each.

### Cleaning up

```bash
python -m editing.cli auto clean            # dry run: shows what it would remove
python -m editing.cli auto clean --yes      # remove incomplete runs
python -m editing.cli auto clean --all --yes  # completed ones too
```

Completed runs, and runs that have executed anything against Premiere, are kept
unless you pass `--all` — the irreversible mistake this guards against is
clearing a run's artifacts while a sequence built from them is still open.

### What auto mode does not do

- **It makes no editing decisions of its own.** Every stage is a thin adapter
  over the pass that already existed; the value is ordering, checkpointing and
  failure messages.
- **It never executes anything without a per-stage `--yes`.**
- **It does not re-run the critic in a loop.** One pass, as designed.
- **It does not know your Premiere track layout.** The asset pass assumes
  V1/A1 belong to the rough cut.
- **It cannot undo an execution.** Delete the added tracks and markers by hand.

---

## Quick start

```bash
python -m editing.cli doctor                              # check FFmpeg / model / Premiere
python -m editing.cli discover --folder D:/Footage/ep12   # find and map the footage
python -m editing.cli transcript status                   # what transcript data exists
python -m editing.cli audio                               # silence, spikes, reactions
python -m editing.cli analyze                             # run Qwen3-VL over sampled windows
python -m editing.cli timeline                            # combine all three channels
python -m editing.cli recommend --with-plan               # propose edits + dry-run a plan
python -m editing.cli roughcut build                      # assemble a rough cut plan
python -m editing.cli roughcut dry-run                    # validate it offline
python -m editing.cli roughcut execute --yes              # build it in Premiere

python -m editing.cli review export-frames                # frames worth a second look
python -m editing.cli review critique                     # what the critic thinks
python -m editing.cli review plan                         # findings → revisions
python -m editing.cli review dry-run                      # validate the revisions
python -m editing.cli review execute --yes                # apply them

python -m editing.cli style list                          # the editing styles
python -m editing.cli layers build --style fast_funny     # captions, emphasis, audio
python -m editing.cli layers show-density                 # is it over-edited?
python -m editing.cli layers dry-run                      # validate the layers
python -m editing.cli layers execute --yes                 # apply them

python -m editing.cli assets init                         # make the folders
python -m editing.cli assets index                        # scan your files
python -m editing.cli assets plan                         # match placeholders
python -m editing.cli assets show-missing                 # what to go and find
python -m editing.cli assets dry-run
python -m editing.cli assets execute --yes                # place them
```

Then read the results:

```bash
python -m editing.cli top          # the moments most worth using
python -m editing.cli reactions    # moments the audio made interesting
python -m editing.cli removed      # what the safety pass threw out, and why
python -m editing.cli draft        # the draft Premiere plan (executes nothing)
python -m editing.cli review show-issues   # what the critic found, worst first
python -m editing.cli review report        # the full revision report
python -m editing.cli layers report        # every layer, and what was held back
python -m editing.cli layers show-deferred # what the style refused, and why
python -m editing.cli assets report        # library contents and coverage
python -m editing.cli assets show-deferred # every placeholder that placed nothing
```

Export any artefact to a path of your choosing:

```bash
python -m editing.cli export timeline        --out D:/handoff/ep12_timeline.json
python -m editing.cli export recommendations --out D:/handoff/ep12_recs.json
python -m editing.cli export report          --out D:/handoff/ep12_report.txt
python -m editing.cli export plan            --out D:/handoff/ep12_plan.json
```

If you build a timeline *before* running `audio`, fold the audio in afterwards
without re-analysing anything:

```bash
python -m editing.cli audio
python -m editing.cli attach
```

Or all of it in one call:

```bash
python -m editing.cli run --folder D:/Footage/ep12 --recommend
```

Try the whole pipeline without a GPU — real discovery, sampling, caching and
alignment, with canned model answers:

```bash
python -m editing.cli run --folder D:/Footage/ep12 --backend mock
```

Mock output is always marked `mock: true` in each event's notes, so it can
never be mistaken for a real analysis.

---

## 1. Where to put footage

Anywhere. Point `--folder` at it, or set a default:

```bash
set EDITING_FOOTAGE_DIR=D:\Footage\ep12        # Windows
export EDITING_FOOTAGE_DIR=/mnt/footage/ep12   # Linux/macOS
```

Recognised: `.mp4 .mov .mkv .avi .m4v .mts .m2ts .mxf .webm .wmv .flv`.
Sub-folders are scanned unless you pass `--no-recursive`. Hidden files and
macOS resource forks (`._clip.mp4`) are skipped.

With no `--folder` and no `EDITING_FOOTAGE_DIR`, the media already imported into
the **open Premiere project** is used — that is what makes "analyse what I'm
already editing" work with no arguments.

Each file gets a stable `asset_id` derived from its path, so transcripts and
analyses stay attached to it across runs.

### Premiere mapping

If Premiere is running with the Nova Premiere Bridge panel open, each file is
matched to its project item (name, bin, media type) and to the **active**
sequence if it is used there. Only the active sequence is read — confirming
membership in every sequence would mean activating each one and visibly
changing what you are looking at. So an empty `sequences` list means *not
confirmed*, not *unused*.

All Premiere access in this layer is **read-only**. It is safe to run against a
project you are actively editing. (There is a test that asserts this: every op
the layer issues must be non-mutating in the catalog.)

Premiere not running is a normal state — everything still works from disk.

---

## 2. Transcripts

### Premiere Speech to Text

Premiere's transcription lives in **Text panel → Transcript tab** (newer builds
also call this Text-Based Editing). **Adobe exposes no documented ExtendScript
API for reading it.**

Rather than pretend otherwise, this layer measures what your build actually
offers:

```bash
python -m editing.cli transcript status
```

```
Premiere Speech to Text support:
  reachable : True
  readable  : True
  version   : 25.1.0
  manual    : Text panel > Transcript tab > ... menu > Export
```

Where Premiere has run speech analysis, the result is stored in the project
item's XMP as one marker **per word**; `transcript.read` returns those raw, and
they are grouped into readable lines on the Python side (same speaker, pause
under 0.65s, under 90 characters, breaking after sentence-ending punctuation).

```bash
python -m editing.cli transcript pull
```

Where nothing is reachable, you get `found: false` and the manual export path —
never an invented transcript.

### Importing a transcript file

The reliable path today. In Premiere: **Text panel → Transcript tab → … menu →
Export**, then:

```bash
python -m editing.cli transcript import --file exported.srt --for session_01.mp4
```

Accepted formats — all normalised into one schema:

| Format | Notes |
|---|---|
| `.srt` | SubRip. Speaker prefixes (`>> Steve:`) are detected. |
| `.vtt` | WebVTT, including `<v Speaker>` voice spans and `NOTE` blocks. |
| `.json` | Whisper, Deepgram and similar. The cue list is found by shape, so no dialect flag is needed. |
| `.csv` / `.tsv` | Premiere's Transcript CSV export and anything with start/text columns. |
| `.txt` | **Only with timestamps** — see below. |

The extension is a hint, not the decision: a WebVTT file saved as `.txt` is
parsed as WebVTT.

### Sidecar auto-discovery

A transcript sitting next to the footage (`session_01.srt` beside
`session_01.mp4`) is picked up automatically. SRT and VTT are preferred over
plain text because they carry per-line timing.

### Timing is never invented

A plain-text transcript with **no timestamps is refused**, with an explanation.
Spreading untimed text evenly across the runtime would misalign every segment
downstream and be invisible in the output. Export with timecode, or use SRT.

---

## 3. Visual analysis

Uses **Qwen3-VL-8B-Instruct**. This layer never loads weights — it talks to
whatever is already serving the model:

| Backend | For | Default URL |
|---|---|---|
| `openai` (default) | vLLM, LM Studio, llama.cpp server, SGLang | `http://localhost:8000/v1` |
| `ollama` | Ollama | same base, `/api/chat` |
| `mock` | tests and dry runs | — |

```bash
set EDITING_VISION_BACKEND=openai
set EDITING_VISION_BASE_URL=http://localhost:8000/v1
set EDITING_VISION_MODEL=Qwen3-VL-8B-Instruct
```

Weights are expected at `E:\Assistant\AI_Models\editingllm` — used only to
write a better error message when the server is unreachable.

### How long video is handled

Full videos are never fed to the model. A 40-minute 60fps recording is ~144,000
frames; the model sees a few hundred.

1. **Motion scan.** FFmpeg's scene metric over a 160px-wide copy, decoding
   **keyframes only** — a 40-minute 4K file scans in a couple of minutes rather
   than an hour. Cached per file.
2. **Uniform windows** across the whole recording, with a small overlap so an
   event straddling a boundary is seen whole by at least one window.
3. **Densification.** Windows above the motion threshold are re-cut shorter and
   given more frames. Fights, falls and deaths get more evidence than corridors.
4. **Frames** are extracted at the centres of equal sub-slices — never on a
   window boundary, where a hard cut or a loading screen tends to be.

Coverage is always complete. When a plan exceeds `--max-windows-config`, the
window length is scaled up and the plan rebuilt — the whole file stays covered,
just more coarsely. It never silently stops at minute 12 of a 40-minute file.

Preview the cost before committing to a long run:

```bash
python -m editing.cli plan --folder D:/Footage/ep12
```

```
  session_01.mp4: 312 window(s), 936 frame(s), ~7.5s each
Total: 312 model call(s), 936 frame(s).
```

### Tuning

| Flag | Env | Default | Effect |
|---|---|---|---|
| `--window-seconds` | `EDITING_WINDOW_SECONDS` | 8.0 | Coarsest time resolution of the output |
| `--window-overlap` | `EDITING_WINDOW_OVERLAP` | 0.5 | Overlap between windows |
| `--frames` | `EDITING_FRAMES_PER_WINDOW` | 3 | Frames for a normal window |
| `--dense-frames` | `EDITING_DENSE_FRAMES` | 5 | Frames for a high-motion window |
| `--dense-window-seconds` | `EDITING_DENSE_WINDOW_SECONDS` | 4.0 | Window length inside busy stretches |
| `--motion-threshold` | `EDITING_MOTION_THRESHOLD` | 0.30 | Scene score that triggers densification |
| `--max-windows-config` | `EDITING_MAX_WINDOWS` | 400 | Ceiling per file |
| `--frame-width` | `EDITING_FRAME_WIDTH` | 768 | Keeps coordinates and item counts legible |

Useful extras: `--max-windows 20` (trial run on the first N windows),
`--keep-frames` (keep the JPEGs to see what the model saw), `--no-motion`
(uniform sampling only).

### The visual event schema

One event per window:

```json
{
  "event_id": "e_9f2c1a8b4d10",
  "source_file": "D:/Footage/ep12/session_01.mp4",
  "asset_id": "a_3f8c21d0e4b7a915",
  "start": 312.0, "end": 320.0, "duration": 8.0,
  "confidence": 0.82,
  "environment": "cave",
  "raw_environment": "a deep dark cavern",
  "actions": ["fighting", "escaping"],
  "raw_actions": ["fighting off creepers", "running away"],
  "entities": ["creeper", "zombie"],
  "threats": ["creeper"],
  "ui": {
    "inventory_open": false, "crafting_open": false, "chest_open": false,
    "death_screen": false, "achievement_toast": false, "low_health": true,
    "chat_open": false, "map_open": false,
    "coordinates": "X: 112 Y: -54 Z: 88",
    "hotbar": ["diamond pickaxe", "torch"],
    "any_screen_open": false
  },
  "camera": { "motion": "erratic", "intensity": 0.8 },
  "importance": "danger",
  "raw_importance": "very close call",
  "suggested_range": { "start": 313.5, "end": 319.0, "duration": 5.5 },
  "notes": "Hearts down to 2. A creeper is flashing white beside the player.",
  "model": "Qwen3-VL-8B-Instruct",
  "frame_times": [313.33, 316.0, 318.67],
  "dense": true,
  "motion_score": 0.71,
  "error": ""
}
```

**Closed vocabularies.** `environment`, `actions`, `importance` and
`camera.motion` are drawn from fixed sets, so a downstream planner never has to
pattern-match free text. The model's own wording is kept in the `raw_*` fields —
coercion never destroys what it actually said.

- **environment** — cave, mineshaft, stronghold, nether, nether_fortress,
  bastion, end, village, forest, jungle, swamp, desert, plains, mountains,
  snow, ocean, underwater, river, base, farm, structure, menu, unknown
- **actions** — mining, building, fighting, looting, exploring, escaping,
  crafting, dying, travelling, farming, searching, trading, enchanting,
  brewing, redstone, eating, idle, talking, unknown
- **importance** — boring, setup, tension, payoff, funny, danger, reveal
- **camera.motion** — static, pan, tilt, orbit, walk, run, fly, fall, shake,
  swing, erratic, unknown

Window timestamps always override whatever the model says about `start` and
`end` — it is asked what it sees, not when. `suggested_range` is clamped inside
its own window, so a range can never point at footage the model did not see.

A window whose analysis fails becomes an event with `error` set and
`confidence: 0.0` — an **honest hole**, not five silently missing seconds.
Failed windows are never cached, so a server that was down at 3am does not
poison the results forever.

---

## 4. Audio analysis

Transcript alone misses a lot: laughter, screams, dead air, the silence right
before something goes wrong. This layer reads the audio track alongside it.

```bash
python -m editing.cli audio
```

```
17 audio event(s) across 1 file(s).
  ep12.mp4: clipping=1, long_pause=4, loudness_spike=4, music_region=2,
            possible_laughter=1, silence=1, speech_sparse=2, sudden_reaction=1
```

### What it measures, and what it guesses

This distinction runs through the whole layer and is enforced in code, not just
documented.

| Type | Kind | Typical confidence |
|---|---|---|
| `silence`, `long_pause` | **measured** | 0.80–0.90 |
| `loudness_spike`, `sudden_reaction` | **measured** | 0.75–0.80 |
| `clipping` | **measured** | 0.85 |
| `low_energy`, `speech_dense`, `speech_sparse` | **measured** | 0.65–0.70 |
| `possible_laughter`, `possible_scream`, `music_region` | **inferred** | ≤ 0.45 |

Anything inferred from the *shape* of a loudness curve is named `possible_*`
where that applies and is capped by `AudioConfig.max_inferred_confidence`
(default 0.45). The layer is structurally incapable of asserting laughter as
confidently as it asserts silence. Every event carries `is_measured` so
downstream code can branch on it.

**This is not emotion detection and does not pretend to be.** A laughter
cluster is "several short loud bursts close together", which is also what a
stuttering engine or a burst of gunfire looks like.

### Transcript markers beat heuristics

When a transcript contains `[laughs]`, `[music]` or `[sighs]`, that event is
recorded with `detection: "transcript_marker"` at confidence **0.85** — someone
(or something trained on speech) listened and named the sound. A heuristic
guess covering the same moment is dropped, so one laugh is not counted twice as
evidence.

This is why the transcript normaliser deliberately *keeps* marker-only cues
instead of discarding them as non-speech. Only genuinely contentless cues
(`[inaudible]`) are dropped.

### How it works

Two FFmpeg passes, both read-only:

1. **`astats`** over a mono 8 kHz copy, reset every `sample_interval`, giving an
   RMS/peak reading per 0.25s.
2. **`silencedetect`**, which finds quiet stretches more precisely than
   re-deriving them from the envelope.

Then pure detectors run over that envelope. Two are worth calling out:

- **A "sudden reaction" requires a quiet run-up.** A 15 dB jump inside an
  already-loud fight is just more fight; the same jump out of near-silence is
  the moment a viewer's head comes up.
- **Music regions are cut at spikes, not discarded.** A steady bed interrupted
  by one shout is still a bed either side of it.

Everything is relative to the file's own **median** level. An absolute dBFS
threshold behaves completely differently on a quiet recording and a hot one.

Audio degrades honestly: no audio track, an undecodable codec, or FFmpeg
missing all produce a stated warning and whatever transcript markers exist,
rather than a silent empty result.

### Tuning

| Flag | Env | Default | Effect |
|---|---|---|---|
| `--silence-db` | `EDITING_SILENCE_DB` | -45 | dBFS floor for silence |
| `--min-silence` | `EDITING_MIN_SILENCE` | 0.8s | Shortest silence worth reporting |
| `--long-pause` | `EDITING_LONG_PAUSE` | 2.5s | Transcript gap that counts as dead air |
| `--spike-db` | `EDITING_SPIKE_DELTA_DB` | 8 | dB above baseline that counts as a spike |
| `--audio-interval` | `EDITING_AUDIO_INTERVAL` | 0.25s | Loudness sample spacing |

### The audio event schema

```json
{
  "event_id": "au_4f2c9a1b3d70",
  "source_file": "D:/Footage/ep12/session_01.mp4",
  "start": 41.0, "end": 43.0, "duration": 2.0,
  "type": "possible_laughter",
  "confidence": 0.85,
  "loudness_db": -11.2, "peak_db": -2.0,
  "baseline_db": -24.0, "relative_db": 12.8,
  "speech_density": 1.5,
  "edit_value": "comedy",
  "detection": "transcript_marker",
  "is_measured": false,
  "notes": "transcript marker: [laughs]",
  "evidence": { "marker": "laughs", "line": "[laughs] oh my god" }
}
```

`edit_value` is what the event is worth to an editor: `boring`, `tension`,
`comedy`, `impact`, `pause`, `transition`, `emphasis`.

### What audio changes on the timeline

Audio events attach to timeline segments by overlap, and genuinely change the
verdicts:

- a **wordless scream over a creeper** becomes `alignment: match` — invisible to
  a text-only check
- **laughter over an uneventful shot** becomes `contrast`
- **dead air** scores 0.0 and is never `usable`, however good the picture is
- an audible reaction **lifts** a visually ordinary moment

---

## 5. The combined timeline

```bash
python -m editing.cli timeline
```

Segments are built from the visual events, with adjacent similar events merged
(same place, same action, same importance, no gap, capped at 30s) so a
two-minute cave exploration is one range to trim rather than fifteen. Transcript
lines are attached by time overlap — a line spanning two segments appears in
both, because it is genuinely audible over both.

```json
{
  "segment_id": "s_1a2b3c4d5e6f",
  "asset_id": "a_3f8c21d0e4b7a915",
  "source_file": "D:/Footage/ep12/session_01.mp4",
  "start": 312.0, "end": 328.0, "duration": 16.0,
  "said": "this is completely safe nothing to worry about",
  "speech_entries": [ /* full TranscriptEntry records */ ],
  "events":         [ /* full VisualEvent records */ ],
  "importance": "danger",
  "alignment": "contrast",
  "alignment_reason": "Narration plays down a dangerous moment: safe, nothing to worry",
  "usefulness": 0.574,
  "usable": true,
  "reasons": [
    "importance=danger (0.80) at confidence 0.80",
    "6 words of narration",
    "alignment=contrast",
    "threats: creeper"
  ]
}
```

### Do narration and visuals agree?

This is the point of combining the two channels:

| Verdict | Meaning | Example |
|---|---|---|
| `match` | Narration refers to what is on screen | "mining for diamonds" over mining |
| `contrast` | Narration clashes with the picture | "totally safe" over a creeper |
| `neutral` | Speech present but unrelated | "like and subscribe" over mining |
| `unknown` | No speech, or no usable analysis | silence |

**`contrast` scores as highly as `match`** — a mismatch between narration and
picture is usually the funniest thing in the recording.

Contrast is detected three ways: narration playing down a genuinely dangerous
moment, narration hyping an uneventful one, and narration naming a different
place than the footage shows. Agreement is always checked first, so "that
creeper nearly got me but we're safe now" is a match, not a contrast.

The classification is a documented keyword heuristic, not a second model call:
deterministic, free, and arguable — every verdict carries its evidence in
`alignment_reason`.

### Usefulness

A weighted, explainable sum — never a black box:

- 45% what is happening (importance × confidence)
- 15% narration present (word count, capped)
- 15% the narration/visual relationship
- 10% camera and motion intensity
- +10% visible threats, +10% death screen, +5% achievement toast
- −15% a full-screen UI covering the segment, −10% shorter than a second

Every contribution appears in `reasons`. Tune the cutoff with `--threshold`
(default 0.45).

```bash
python -m editing.cli show --highlights      # usable segments, best first
```

```
    [ 312.00- 328.00] danger   cave/fighting    contrast this is completely safe not...
```

---

## 6. Edit recommendations

```bash
python -m editing.cli recommend --with-plan
```

Six layers run in order over the combined timeline. The first five propose
generously; the sixth is strict, and that is where most of the quality comes
from.

| Layer | Decides |
|---|---|
| 1. **story** | What kind of moment is this? Markers on payoffs, reveals, danger, deaths; cuts where the story actually turns |
| 2. **pacing** | Cut, trim dead air, **hold**, speed up, preserve anticipation before a payoff |
| 3. **visual** | Punch-in, slow push-in, freeze frame, text overlay, caption emphasis, visual callout |
| 4. **audio** | Music cue, beat marker, impact sound, comedic sting, ducking, audio fade, and deliberate silence before a payoff |
| 5. **polish** | Shadow lifts in caves, clipping fixes, "don't crop the HUD here" warnings |
| 6. **safety** | Which of the above is actually a bad idea |

House style is cinematic Minecraft: clean pacing, tension and payoff respected,
punch-ins that mean something, readable text, no spam.

### `hold` is a real answer

A planner that can only say "cut here" will edit everything. `hold` is a
first-class category: a payoff the viewer is already invested in gets an
explicit *leave this alone*, and later layers respect it. Cutting into a moment
to look busy is the most common way an edit gets worse.

### The safety pass

Six checks, cheapest and most certain first. **Nothing is deleted** — items are
marked `rejected` or `downgraded` with a reason, so the output always shows what
was considered.

1. **No evidence** → rejected.
2. **Transcript-only and contradicted** → rejected. If the words say
   "terrifying" while the picture and audio are calm, the words are the weakest
   of the three channels.
3. **Covers gameplay** → rejected. A zoom or overlay over an open inventory, or
   a punch-in that would crop visible low health out of frame.
4. **Weak single-channel** → downgraded. One channel at low priority is a hunch.
5. **Repetition** → downgraded. Same edit type within `--repeat-gap` (12s).
6. **Density budget** → downgraded, weakest first, until active edits fit one
   per `--budget-seconds` (20s) of footage.

Markers and holds are exempt from 4–6: they change nothing, and an editor is
well served by plenty of both.

An edit pushed all the way down becomes a `hold` — the report keeps those
separate from deliberate holds, so it never claims restraint the planner was
actually forced into.

```bash
python -m editing.cli removed
```

```
1 recommendation(s) removed or softened:
  [rejected  ]  37.50s punch_in    Threat on screen (zombie)
      why: Low health is visible and is the reason this moment is tense; a
           zoom risks cropping the HUD out of frame.
```

### The recommendation schema

```json
{
  "recommendation_id": "r_8c31f0a92e4d",
  "asset_id": "a_3f8c21d0e4b7a915",
  "source_file": "D:/Footage/ep12/session_01.mp4",
  "start": 40.0, "end": 50.0, "duration": 10.0,
  "category": "punch_in",
  "priority": 0.7,
  "reason": "Threat on screen (creeper)",
  "evidence": {
    "visual_event_ids": ["e_9f2c1a8b4d10"],
    "transcript_quotes": ["this is completely safe nothing to worry about"],
    "audio_event_ids": ["au_4f2c9a1b3d70"],
    "audio_types": ["sudden_reaction"],
    "channels": ["visual", "transcript", "audio"],
    "summary": "Threat on screen (creeper)"
  },
  "intensity": "medium",
  "effects": ["tension", "impact"],
  "risks": ["hides_gameplay"],
  "status": "accepted",
  "status_reason": "",
  "layer": "visual",
  "premiere_ops": [],
  "has_evidence": true,
  "is_actionable": true
}
```

Categories: `structure_cut`, `trim_dead_air`, `hold`, `punch_in`,
`slow_push_in`, `speed_ramp`, `freeze_frame`, `text_overlay`,
`caption_emphasis`, `marker`, `visual_callout`, `music_cue`, `beat_marker`,
`sound_effect`, `ducking`, `audio_fade`, `color_adjust`, `transition`.

Two are placeholders with real timing but no chosen asset: `visual_callout`
(no graphic designed) and `beat_marker`. **`beat_marker` does not do beat
tracking** — there is no tempo estimation anywhere in this layer. It marks
where a music-like bed *begins*, which is the one musical position the audio
layer actually knows, and says so in its notes.

Effects: `clarity`, `tension`, `comedy`, `impact`, `pacing`, `explanation`,
`anticipation`, `payoff`. Risks: `over_editing`, `hides_gameplay`,
`text_unreadable`, `bad_timing`, `unnecessary`, `low_confidence`,
`audio_masking`, `repetitive`.

### Tuning

```bash
python -m editing.cli recommend --budget-seconds 40   # calmer edit
python -m editing.cli recommend --repeat-gap 20       # more spacing
python -m editing.cli recommend --no-safety           # raw proposals, inspection only
```

`--no-safety` prints a loud warning and must not be used to build a plan.

---

## 7. The draft Premiere plan

```bash
python -m editing.cli draft
```

```
Draft Premiere plan (structure)
  operations     : 8
  dry run        : valid
  executed       : False  <- nothing has been applied
  kept as recs   : 16
```

**Nothing is executed.** The plan is built in the existing `premiere.catalog`
vocabulary, validated through `premiere.validator` at 30 fps **offline** — no
Premiere, no bridge, no open project — and written to disk. The plan itself
carries `dry_run: true`, so even a careless caller passing it to the engine
would validate rather than edit.

### What converts today, and what does not

Recommendations are in **source file time**. Most Premiere operations act on
**clips already on a sequence** — until the footage is assembled, a punch-in has
no clip to apply itself to. Rather than emit operations that would fail or hit
the wrong clip:

- **`marker`, `structure_cut` and `beat_marker`** → `marker.add` on the **project item**, which
  works in source time with no sequence at all. The marker comment carries the
  reason, the evidence channels and the priority, so a human editor can judge it
  in Premiere without opening any JSON.
- **`hold`** → zero operations, deliberately. A hold means "do nothing here", so
  no operations is the correct output, not a failure.
- **Everything else** → kept as a recommendation, listed with the specific
  reason it cannot convert yet.

```
  Kept as recommendations (no operation yet):
    color_adjust       needs a clip on a sequence to apply Lumetri to
    ducking            needs the music and speech clips on a sequence
    music_cue          placeholder only -- no track has been chosen
    punch_in           needs a clip on a sequence to animate Motion > Scale on
```

---

## 8. The rough cut

The draft plan in section 7 is markers only. This is the one that builds a
sequence you can actually watch.

```bash
python -m editing.cli roughcut build
python -m editing.cli roughcut dry-run
python -m editing.cli roughcut execute --yes
```

```
Rough cut: Nova Rough Cut
  6 clip(s), 84.5s from 132.0s of footage
  3 protected, 2 sped up, 17 marker(s)
  31 operation(s), 4 recommendation(s) unconverted
  dry run    : passed
  on scratch : True  (nothing has been applied)
```

### What goes in the cut

Selection is a set of stated rules, not a score threshold alone:

| Footage | Decision |
|---|---|
| Dead air (silent, no narration) | **dropped** |
| An accepted `trim_dead_air` range | **dropped** |
| Segments whose visual analysis failed | **dropped** — a failure is not evidence |
| A deliberate `hold` | **kept, protected** — no effects, no retime |
| Payoff / reveal / danger / funny | **kept, protected** at full speed |
| The beat *before* a payoff | **kept, protected** — anticipation is what makes the payoff land |
| Scores above `--keep-threshold` | kept at full speed |
| Dull but with narration | kept at full speed — sped-up dialogue is unusable |
| Dull and silent | kept at `--filler-speed` (default 2x), or dropped with `--drop-filler` |

Adjacent kept ranges merge, so the cut does not contain a seam every eight
seconds where two analysis windows happened to meet. Each range gets a small
handle either side (`--handle`, default 0.25s) so cuts do not land on a window
boundary — and where handles push two ranges into each other, **the protected
one keeps the contested frames**, so a slice of a payoff can never end up inside
the sped-up filler clip next to it.

### The layout is computed, not observed

Every sequence position is arithmetic done before Premiere is touched: a clip
sped 2x occupies half its source duration, and everything after it moves. That
is what makes markers, punch-ins and review frames placeable offline, and the
whole plan dry-runnable.

```bash
python -m editing.cli roughcut placements
```

```
  [    0.00-    5.12] <-     0.00-   10.25  filler         2x
      p_38f5ef75c862  recs=0 segments=1
  [    5.12-   35.62] <-    19.75-   50.25  hold           protected
      p_cf5d7c4dbcca  recs=3 segments=4
```

The chain **source file → source range → recommendation → sequence position →
operation** is preserved end to end. Every clip in the cut can be traced back to
the evidence that justified keeping it.

### Operation order

Order is load-bearing and fixed by the builder:

1. `project.import` — media must be in the project before it can be placed
2. `sequence.create` from the first clip, so the scratch sequence inherits its
   resolution and frame rate (`--preset` uses a `.sqpreset` instead)
3. `sequence.activate`
4. `clip.remove` — Premiere puts the source clip on the timeline when creating
   a sequence from it; the assembly starts empty
5. `clip.append` per range, in playback order
6. `clip.speed` for retimed clips, **in reverse order with ripple** — rippling
   shifts everything after a clip, so working backwards means each clip is
   still where the plan says when its turn comes
7. Punch-ins, targeted by sequence time
8. `marker.add` last, at final post-retime positions

Steps 7 and 8 come after all retiming precisely because retiming moves clips. A
marker placed earlier would end up pointing at whatever slid into that spot.

### What converts, and what refuses

Only conservative edits become real picture changes. A zoom is **refused** when:

- the clip is a protected hold — zooming would edit a moment the pacing layer
  said to leave raw
- the clip is retimed — that compounds two edits on the same footage
- the clip is shorter than 1.5s on the timeline — a zoom that brief reads as a
  glitch
- a full-screen UI is open anywhere in the clip, or low health is visible

That last check is re-run against the *merged* clip, not just the original
recommendation: the cut may have joined segments, so a clip can span footage the
original recommendation never saw.

Scale is capped at **115%** for a punch and **108%** for a push. Refusals become
a `ZOOM?` marker so a human editor still sees where emphasis was wanted.

Everything else — music cues, SFX, text, callouts, ducking, colour — becomes a
**marker** carrying its reason, evidence channels and priority. That is the
honest form of "something belongs here but the asset does not exist yet".

```bash
python -m editing.cli roughcut unconverted
```

### Execution modes and their guards

| Mode | Validates | Runs |
|---|---|---|
| `--plan-only` | no | no |
| `roughcut dry-run` | yes | no |
| `roughcut execute --yes` | yes, in the same call | yes, on a scratch sequence |

Four guards, each of which **refuses** rather than warns:

1. **No default runs anything.** Building and dry-running are the only things a
   bare command does.
2. **A dry run must pass in the same invocation.** A previously-recorded pass is
   not accepted — the plan may have been rebuilt since.
3. **The target must provably be a scratch sequence.** This is checked
   *structurally* — the plan must create and activate its own sequence before
   placing anything — not trusted from a flag. Editing the sequence you have
   open requires `--allow-active-sequence`.
4. **A refusal is a result, not a crash.** The execution report says what it
   declined to do and why.

```bash
python -m editing.cli roughcut report
```

---

## 9. Review frames

```bash
python -m editing.cli review export-frames          # the frames worth looking at
python -m editing.cli review export-frames --list   # see the choice without extracting
python -m editing.cli review export-frames --simple # one frame per clip, no context
```

A bare `python -m editing.cli review` still means `review export-frames`.

**Frames come from the source files, not from Premiere.** The cut is an assembly
of source ranges with known in/out points, so a frame at sequence time *t* is a
frame at a computable source time. That means review frames can be exported from
a plan that was **never executed** — which is what makes them useful for
checking a cut before committing to it.

### Which moments get a frame

Not one per clip. The mistakes an automatic assembly makes cluster in specific
places, so frames are chosen by rule:

| Kind | Where | Why |
|---|---|---|
| `clip_start` | 0.35s after the incoming cut | did the next shot land? |
| `clip_end` | 0.35s before the outgoing cut | was the beat clipped or held too long? |
| `zoom` | at the **end** of a punch-in or push-in | the only point that answers "too far?" |
| `marker` | at each planned marker | does the picture match what the plan claims? |
| `text_placeholder` | at a text/caption/callout marker | is there room for it here? |
| `speed_change` | mid-clip on a retimed clip | does the footage still read at 2x? |
| `high_priority` | mid-clip on a payoff/reveal/danger/funny | where a defect costs most |
| `sanity` | pseudo-random inside long stretches | a critic that only looks where problems are expected confirms the plan instead of testing it |

Sanity probes are random in *placement* and deterministic in *result*: the
offsets come from a hash of the clip's ID, so the same cut always samples the
same moments and the critic's cache stays useful.

Two probes landing on the same moment **in the same clip** collapse into the
more specific one. Two probes either side of a **cut** never collapse — they are
different shots from different files answering different questions, and the two
edge probes are always closer together than the collapse threshold.

Turn individual rules off with `--no-cut-points`, `--no-markers`, `--no-zooms`,
`--no-speed`, `--no-text`, `--no-priority`, `--no-sanity`, and cap the whole
pass with `--max-frames` (default 120; trimmed least-informative-kind first).

### What each frame carries

A vision model looking at one still cannot see that it is mid-punch-in, sped to
2x, or two frames before a cut. So every frame is exported with its context
attached, and the critic prompt states it plainly:

- the sequence, the clip placement, and the source file and time it came from
- the recommendation IDs and segment IDs behind the clip
- **applied edits at that moment** — zoom (with the recommendation that asked
  for it), speed, protected-hold status, and any marker within a second
- **nearby transcript text**, **nearby audio events** (with type, span and
  confidence), and **nearby visual events** (environment, actions, entities,
  threats, HUD flags)
- how long the clip runs on the timeline, and why the frame was picked

Where the pipeline does not know something — no transcript, no audio pass, no
timeline — the line is simply absent rather than present and empty. An empty
field reads to a model as *there is nothing there*, which is a different claim
from *I do not know*.

A `review.json` manifest sits alongside the JPEGs with all of it.

---

## 10. The critic pass

```bash
python -m editing.cli review critique                 # Qwen3-VL looks at the frames
python -m editing.cli review show-issues              # one line per issue, worst first
python -m editing.cli review show-issues --severity high
python -m editing.cli review plan                     # findings → revisions → a plan
python -m editing.cli review dry-run                  # validate it offline
python -m editing.cli review execute --yes            # apply it
python -m editing.cli review report                   # the whole story, in text
```

`--backend mock` works here exactly as it does for `analyze`: it exercises the
whole path without a GPU, and every finding is marked `mock` so it can never be
read as a real visual judgement.

### What the critic looks for

One frame per model call. Batching is cheaper and much worse — a small VLM shown
six stills reliably attributes a problem in frame four to frame one, and a
finding pointing at the wrong moment is worse than no finding.

The vocabulary is closed. Anything the model invents coerces to
`needs_human_review` rather than being dropped, with its original wording kept
in `raw_issue`:

```
bad_crop              hud_hidden            action_hidden
zoom_too_strong       text_unreadable       text_placed_badly
caption_covers_gameplay                     too_dark / too_bright
boring_too_long       cut_too_early         cut_too_late
marker_mismatch       callout_needed        hold_longer
remove_edit           needs_human_review
```

Every finding carries a **severity**, a **confidence** and its **evidence** —
what in the frame the model says shows it.

### Findings are not fixes

This is the line the whole pass is built around. A *finding* is what the critic
saw. A *revision* is what the system proposes doing about it, and the conversion
between them is a set of explicit rules — not a model's opinion.

**Confidence gates action; severity does not.** A high-severity finding the
critic is 40% sure about becomes a note for a person, not an edit. Severity only
decides how loudly it is reported and whether it earns a marker on the timeline.

| Threshold | Value | Applies to |
|---|---|---|
| change the edit at all | 0.60 | every automatic fix (`--min-confidence`) |
| change timing | 0.70 | extending a hold |
| cut footage out | 0.80 | trimming dead air |

**A fix may only act on something the plan knows is there.** `reduce_zoom`
requires a zoom in the plan at that moment; `trim_dead_air` requires an audio
event confirming dead air; `extend_hold` requires source footage past the out
point. Without the premise the fix is refused with `not_verifiable` — a critic
hallucinating a zoom cannot make the system edit one that never existed.

**Amounts are fixed, not suggested.** Zooms reduce to a flat 106% rather than to
whatever the model proposed; holds extend by at most 0.5s; trims take at most
1.0s and never leave a clip under a second long.

### What converts, and what does not

| Fix | Becomes | Notes |
|---|---|---|
| `remove_zoom` | `property.reset` on Motion > Scale | one reversible reset |
| `reduce_zoom` | `property.reset` **then** `animate` to 106% | resetting first is why the result is one gentle push rather than two stacked ones |
| `move_text_placeholder` | `marker.remove` + `marker.add` | no text exists on this timeline; this re-sites the **placeholder marker**, and says so |
| `color_marker` | `marker.add` `COLOR` | "too dark" carries no number; guessing an exposure lift would be inventing a grade |
| `callout_marker` | `marker.add` `CALLOUT` | no callout graphic exists yet |
| `review_marker` | `marker.add` `REVIEW` | put it in front of a person at the right moment |
| `extend_hold` | `clip.trim` `edge=out`, negative, rippled | needs source headroom and 0.70 confidence |
| `trim_dead_air` | `clip.trim` on a clip edge, rippled | needs an audio event and 0.80 confidence |
| `shorten_section` | **nothing** | a timing change big enough that a person should make it |
| `reframe` | **nothing** | this system cannot re-compose a shot |

Everything in the second group — and everything that fails a gate above — stays
a **revision recommendation** with `status: needs_human_review` and the reason
attached. Nothing is silently dropped and nothing is faked: a finding with no
safe automatic form does not quietly become a marker and get counted as fixed.

Deferred findings at `medium` severity or above (and at least 0.45 confidence)
*additionally* get a `REVIEW` marker on the timeline, so a problem the system
could not fix is still visible where it happens rather than only in a report.
Turn that off with `--no-review-markers`.

### The revision schema

```json
{
  "revision_id": "rv_8c1f0a2b4d3e",
  "source_recommendation_id": "r_punch_31",
  "finding_id": "cf_3a91...", "frame_id": "rf_...", "placement_id": "p_...",
  "start": 31.4, "end": 32.4,
  "issue": "hud_hidden", "severity": "high", "confidence": 0.78,
  "visual_evidence": "the hearts are cropped off the bottom left",
  "transcript_evidence": "oh my god that was close",
  "audio_evidence": ["sudden_reaction"],
  "suggested_fix": "remove_zoom",
  "fix_detail": "Reset Motion > Scale on the clip at 31.90s, clearing the 115% zoom.",
  "risks": ["removes_an_edit"],
  "status": "accepted",
  "status_reason": "A zoom is planned here and removing it is a single reversible property reset.",
  "premiere_ops": [{"op": "property.reset", "...": "..."}],
  "is_actionable": true
}
```

`status` is one of `accepted`, `rejected`, `needs_human_review`, and it is the
gate: only `accepted` **with operations** reaches the plan.

### Operation order, and the one thing it cannot verify

The revision plan fixes its own order, because the order is load-bearing:

1. `sequence.activate` — the rough cut's sequence, **by name**. A revision plan
   never *creates* a sequence.
2. Zoom fixes. They change one clip's Scale and move nothing.
3. Timing fixes, **back to front**. Rippling shifts every later clip, so working
   backwards means each clip is still where the plan says when its turn comes.
4. Marker operations, at positions **corrected for the ripple above**.
5. `REVIEW` markers for what could not be fixed.

Step 4 is the genuinely uncertain part, and the plan warns about it out loud.
Premiere's sequence markers do not ripple with clips, so a trim earlier in the
sequence moves the picture out from under any marker after it. New markers this
plan places are corrected offline; **pre-existing rough-cut markers after a trim
are not** — the plan counts them and tells you. For a pass that cannot cause
this at all, use `--no-timing`, which proposes no trims or extensions and leaves
the cut's timing exactly as it was.

### The guards

The rough cut's three guards, plus one that replaces the guarantee a revision
cannot make. A rough cut *creates* its own scratch sequence, which is what
proves it cannot touch your timeline. A revision edits a sequence that already
exists, so "did you build your own sandbox" is unanswerable. In its place:

- **Nothing runs without an explicit mode**, and the CLI additionally requires
  `--yes`.
- **A dry run must pass in the same call.** A stored pass from an earlier build
  is not evidence about the plan being run now — validation reruns every time.
- **The first operation must be `sequence.activate` naming the rough cut's
  sequence**, so the target is fixed by the plan rather than inherited from
  whatever happens to be open. A second activate anywhere in the plan is refused.
- **Every operation must be on a short allowlist**: `sequence.activate`,
  `property.reset`, `animate`, `clip.trim`, `marker.add`, `marker.remove`.
  Nothing else can affect another sequence or anything on disk, so a plan
  containing an import, a save, a clip removal or a sequence creation is refused
  rather than inspected further.
- **The rough cut must itself have been scratch-safe**, and must actually have
  been executed — otherwise the sequence being activated probably does not exist.
- **A refusal is a returned result, not an exception**, with the reason on it.

```bash
python -m editing.cli review execute            # refuses: needs --yes
python -m editing.cli review execute --yes      # runs, after validating again
```

### The reports

Two files, and the rough cut's own report is **never overwritten** — a second
opinion that destroyed the baseline it was judging would be worthless.

`critic/<name>.revisions.txt` leads with **what it could not fix**, before what
it could. The automatic fixes are bounded and reversible by construction; the
deferred findings are where the real problems with the cut are, and burying them
under a list of successes is how a report stops being read.

---

## 11. Style presets

```bash
python -m editing.cli style list                    # all four, one line each
python -m editing.cli style show cinematic_minecraft # every number, and why
```

A rough cut assembled from recommendations is *correct* and characterless. The
difference between a cut and an edit is not more effects — it is a consistent
set of choices about density, emphasis and restraint, applied everywhere. A
preset is that set of choices, written down.

| Preset | Feel | Edits/min | Captions/min | Zoom | Cards |
|---|---|---|---|---|---|
| `cinematic_minecraft` | let it breathe | 1.8 | 0.8 | ≤108% | title |
| `fast_funny` | keep it moving | 7.0 | 4.0 | ≤118% | title |
| `documentary_story` | explain it clearly | 2.5 | 2.0 | ≤104% | title + chapter |
| `minimal_clean` | get out of the way | 0.8 | 0.4 | **none** | none |

`minimal_clean` is the **default**, deliberately: it draws no text and scales
nothing, so the first style you get without asking is the one that cannot
surprise you.

Each preset defines pacing density, per-minute ceilings, caption length and
placement zones, zoom intensity caps, preferred and forbidden edit kinds,
marker naming, audio placeholder behaviour, and safety thresholds. `style show`
prints all of it.

### Ceilings only ever subtract

This is the property the whole session rests on. The compiler never adds an
edit to reach a quota; it only removes candidates that would exceed one. A
looser style lets more of the evidence through — it cannot manufacture
evidence. So:

- switching from `minimal_clean` to `fast_funny` can only ever **add** edits
  that were already justified and held back;
- a style with `max_zoom_scale: 100.0` emits no zooms at all, in any
  circumstance;
- and if a compiled plan ever exceeds its own style's density, that is a bug,
  not a style choice — there is a test asserting it for every preset.

### Validation

Presets are validated, not trusted. `problems()` returns every fault in plain
English, naming the field:

```
max_zoom_scale is 200.0; past about 125% a 1080p source visibly softens and
the HUD starts leaving the frame.
max_push_scale (120.0) is above max_zoom_scale (105.0); a gradual push must
never end up stronger than a hard punch.
```

A hand-edited preset with one bad number **clamps to something workable**
rather than stopping a run — the same stance `SamplingConfig` and `AudioConfig`
take — and `style show` prints the problems so you know what was clamped.

Override any field inline without editing the preset:

```bash
python -m editing.cli layers build --style fast_funny --max-captions-per-minute 1.5
python -m editing.cli layers build --style documentary_story --no-zooms
```

---

## 12. The layered edit

```bash
python -m editing.cli layers build --style cinematic_minecraft
python -m editing.cli layers show-density      # edits/min against the ceilings
python -m editing.cli layers show-deferred     # what was held back, and why
python -m editing.cli layers report            # the full thing, layer by layer
python -m editing.cli layers export --out D:/handoff/ep12_layers.json
python -m editing.cli layers dry-run
python -m editing.cli layers execute --yes
```

Seven layers, kept separate all the way to the operation plan — because "the
captions are too dense" and "the punch-ins are wrong" are separate judgements
with separate fixes, and a flat list of ninety operations supports neither.

| Layer | What it holds |
|---|---|
| `base` | the rough cut's own clips, as read-only context (no operations) |
| `marker` | structure notes from recommendations nothing else realises |
| `caption` | reaction captions, key phrases, danger text, callout labels |
| `emphasis` | punch-ins, push-ins, and markers for the ones refused |
| `audio` | music/SFX placeholders, plus the two fades that are real |
| `title` | title and chapter cards at genuine section boundaries |
| `polish` | colour and transition notes |
| `deferred` | everything held back, with the reason on each |

### Captions: chosen, condensed, and refused

Captions come **only** from transcript lines that already exist and are already
aligned to the timeline. Nothing is written or paraphrased.

A line is *scored* against what the picture, the audio and the words are all
doing at that moment — an audio spike, a payoff on screen, a reaction phrase, a
named threat, agreement or deadpan disagreement between words and picture — and
anything below the style's `caption_min_priority` never becomes a candidate.
"just walking for a bit here nothing much" over boring footage scores below
every style's bar.

A long line is **condensed to its strongest phrase**, not truncated:

```
"okay so anyway I think that was probably a creeper behind us"
                        ↓ max 4 words
"...that was probably a creeper..."
```

The window with the most keyword hits wins, ties going to the earliest — a
viewer reads the front of a line. Truncating instead would keep "okay so anyway
I think" and throw away the only part worth reading.

Placement is **refused rather than guessed**. Minecraft puts health, hunger and
the hotbar across the bottom centre and the crosshair dead centre, so those are
never caption zones. When a full-screen menu is open, when the critic flagged
text at that moment, or when the style has no safe zone left, the item becomes
a **marker carrying the line** — never text placed hopefully over the game.

Anti-spam is enforced by the compiler, not here: captions per minute, minimum
spacing, and a hard rule that two captions never share the screen.

### Visual emphasis: stated as refusals

| Rule | Protects |
|---|---|
| a protected hold is not zoomed | the pacing layer's decision to leave a moment raw |
| a retimed clip is not zoomed | two edits compounding on one piece of footage |
| a clip with an open UI or low health is not zoomed | what the viewer is reading |
| a moment the critic flagged is not zoomed | not adding a second problem to one |
| zooms do not stack | the style's `min_stack_spacing` |
| the style's ceiling is applied last | no input combination can exceed it |

A refused zoom still leaves a marker saying what was wanted and why it was
declined. "The system decided not to zoom here" and "the system did nothing
here" must not look the same on a timeline — the first is a decision an editor
can overrule in two seconds.

### Audio: honest about what does not exist

No music is on this timeline and no sound library is wired up, so the layer
splits sharply:

- **Marker-only, because the asset does not exist:** music start and rise,
  tension beds, impact and comedic SFX, whooshes, ambience, beat anchors, and
  narration ducking. Each carries type, intensity, reason and evidence.
  `duck_narration` even carries the computed speech ranges `audio.duck` would
  need, so adding a bed by hand is a one-step job.
- **Genuinely convertible:** `audio.fade` at the head and tail of the cut. It
  acts on clips that are already there, it is reversible, and it is the one
  audio operation this system can perform truthfully today. So it does.

Cues are anchored to things the timeline already knows — the top of the cut, a
boundary into a tense stretch, a measured audio spike, a long silence. Nothing
is placed on a grid. A guessed audio event (capped at 0.45 confidence by the
Session 2 rule) always ranks below a measured one.

### Title and chapter cards

Only `documentary_story` and `cinematic_minecraft` turn cards on. A card is the
most intrusive thing this system can place, so the triggers are the strictest:
the opening; after a death, failure or restart; on entering a new dimension or
major location; before a stated objective. Never on a clock.

Two rules keep them rare: a section must be at least `min_section_seconds` long
to earn one, and cards closer than 20 seconds collapse. A run of biome changes
while sprinting is one journey, not six chapters.

Titles come from what is **actually known** — the environment, or the objective
the narration stated, taken verbatim. When there is nothing to say, the card
becomes a marker asking the editor to name it.

### Density: the number that answers the question

```bash
python -m editing.cli layers show-density
```

```
'Nova Rough Cut' in fast_funny -- 200.2s, 33 planned item(s)
  active edits : 2.40/min (ceiling 7.0)
  captions     : 1.80/min (ceiling 4.0)
  zooms        : 1.20/min (ceiling 2.5)
  markers      : 6.29/min

  minute   active  captions  zooms  markers
      0        2         1      0        6  ##
      1        3         2      1        9  ###
      2        2         2      0        6  ##
      3        1         0      0        0  #
```

Per-minute buckets rather than a single average, because an average hides the
case this exists to catch: a calm episode with one frantic minute in the middle.

**Markers are not capped.** They change nothing, and an editor is well served
by plenty of them — the asymmetry between cheap annotation and expensive
picture change runs through the whole layer.

The rules, applied to each candidate **most defensible first**, so a ceiling
removes the weakest ideas rather than whichever came last:

1. style permission — a forbidden kind never gets further;
2. confidence — below the style's `min_confidence`, it is a note, not an edit;
3. duplication — against the rough cut's existing markers *and* this pass;
4. spacing — the style's own `min_edit_spacing` / `min_caption_spacing`;
5. the rate itself, in one of two regimes (below);
6. stacking — two changes to the *same sense* inside `min_stack_spacing`.

Stacking is per sense: two things happening to the picture at once fight each
other, but an audio fade under a title card is ordinary editing.

**"N per minute" means two different things above and below one**, and the
compiler treats them differently:

- **At or above one a minute** it is a *rolling window count* — no more than
  the rate inside any 60 seconds, or inside the whole cut when the cut is
  shorter than that. Spacing stays the style's own explicit field, because how
  tightly a style clusters is a separate choice from how many it allows.
- **Below one a minute** a window count floors to zero and would forbid
  everything, so it becomes a *whole-cut budget* (`rate × minutes`) plus the
  spacing the rate implies (`60 / rate`). "0.4 zooms a minute" means one every
  150 seconds, and none at all in a cut too short to have earned one.

There is deliberately **no "at least one" floor**. A 30-second cut allowed 0.2
zooms gets none — a ceiling a short cut may exceed is not a ceiling.

A **section boundary held back for room still leaves a marker** rather than
vanishing. A documentary that silently loses a chapter has lost the thing the
style was chosen for.

### Executing it

The tightest allowlist of the three passes:

```
sequence.activate   track.add   animate   audio.fade   text.create   marker.add
```

No `clip.*` operation at all, so a style pass **cannot** trim, retime, move,
split or remove a clip. Two consequences worth stating:

- **Nothing ripples**, so no marker or overlay can end up describing a frame
  that moved out from under it. The Session 4 revision pass had to compute
  ripple corrections offline and warn they were unverified; this one has
  nothing to correct.
- **The pass is reversible by hand.** Every overlay lands on one added track
  and every marker carries its item ID.

Otherwise the guards are the familiar ones: no default runs anything, a dry run
must pass *in the same call*, the first operation must be `sequence.activate`
naming the rough cut's own sequence, the rough cut must have been built, and a
refusal is a returned result with its reason.

```bash
python -m editing.cli layers execute          # refuses: needs --yes
python -m editing.cli layers execute --yes    # runs, after validating again
```

Two flags make a pass safer still:

```bash
python -m editing.cli layers build --style fast_funny --markers-only
python -m editing.cli layers build --style fast_funny --no-text --no-zooms
```

`--markers-only` records every choice as a note and draws nothing at all — the
safest possible pass, and a good way to read what a style *wants* to do before
letting it do any of it.

---

## 13. The asset library

```bash
python -m editing.cli assets init          # create the folders + docs
python -m editing.cli assets index         # scan them into an index
python -m editing.cli assets list          # what is in there
python -m editing.cli assets show <asset>  # one file, and where its tags came from
python -m editing.cli assets validate      # what is broken, and how to fix it
python -m editing.cli assets report        # which placeholder kinds you can serve
```

**Local files only.** Nothing is downloaded. Nothing in the library is ever
modified — every trim, level and fade is applied to the *placed clip* in
Premiere, never to your source file.

### Folder structure

`assets init` creates this under `<model dir>/assets` (override with `--root`),
and never overwrites anything you have already put there:

```
assets/
├── README.md               what goes where, written into the folder itself
├── example.asset.json      a filled-in sidecar to copy
├── music/                  tracks and beds
├── sfx/                    short one-shots: impacts, pops, whooshes, stings
├── ambience/               loopable room tone and atmosphere
├── callout/                arrows, circles, labels (PNG with transparency)
├── titles/                 title and chapter card backgrounds
└── transitions/            whoosh-and-wipe overlays
```

The library lives beside the model weights rather than in `data/editing`
because run outputs are disposable and a sound library is not. Only the derived
*index* lands in the run output.

**Subfolders become tags.** A file in `sfx/impacts/heavy/` picks up `impacts`
and `heavy` automatically, so organising by folder is most of the work.

### Supported files

| Kind | Extensions |
|---|---|
| audio | `.wav` `.mp3` `.m4a` `.aac` `.flac` `.ogg` |
| image | `.png` `.jpg` `.jpeg` `.webp` |
| video | `.mp4` `.mov` `.webm` |
| Motion Graphics | `.mogrt` — indexed and matched, but **placed as a marker only** |

A `.mogrt` is deliberately still matched: you should be told "found your title
template, cannot drive it, here is why" rather than "you have no title
backgrounds". Driving one needs a registered template and a parameter mapping,
which this system does not have.

### What the indexer reads

Four sources, in increasing priority:

1. **the folder** — `sfx/impacts/heavy/` gives a category and two tags;
2. **the filename** — `whoosh_fast_01.wav` gives `whoosh` and `fast`; `_loop`
   marks it loopable; `impact`/`soft` set the intensity;
3. **ffprobe** — a real duration, when ffprobe exists;
4. **the sidecar** — a person typed it, so it wins over all of the above.

Every tag remembers where it came from, which is what makes `assets show`
able to answer *why did this match?*:

```
  tags (and where they came from):
    bed                  sidecar   1.00
    drone                sidecar   1.00
    tension              sidecar   1.00
    loop                 filename  0.60
```

**No FFmpeg is normal, not an error.** Durations stay unknown and duration fit
is simply not judged — which makes matches less precise rather than wrong. Set
a `duration` in a sidecar wherever the length actually matters.

**A file that vanished is marked, not dropped.** Re-indexing keeps the record
with `missing: true`, so a plan that refers to it explains itself instead of
silently losing a placement. Build folders, `node_modules`, media caches and
hidden directories are never descended into.

### Sidecar metadata

Optional. Put `<filename>.asset.json` next to any file:

```
impact_boom.wav
impact_boom.asset.json
```

```json
{
  "category": "sfx",
  "tags": ["impact", "boom", "heavy"],
  "intensity": "high",
  "mood": ["dramatic", "dark"],
  "bpm": null,
  "loopable": false,
  "safe_for_auto": true,
  "preferred_styles": ["cinematic_minecraft"],
  "avoid_styles": ["minimal_clean"],
  "license_notes": "Bought from <somewhere>, licence covers YouTube use.",
  "start_offset": 0.0,
  "end_offset": null,
  "volume_adjust_db": -3.0,
  "hud_risk": false,
  "notes": "Long tail -- leave room after it."
}
```

Every field is optional. Keys starting with `_` are treated as comments.

**An invalid sidecar never crashes anything.** A file whose sidecar will not
parse is still indexed, marked `needs_review`, and **held out of automatic
placement** — because metadata we could not read is not the same as metadata
that said "safe". Individual bad *fields* are dropped with a note while the
rest of the document is kept, so one mistyped `intensity` does not throw away
good tags:

```
sfx/boom.wav: intensity 'ENORMOUS' is not one of: low, medium, high
              bpm 'quite fast' is not a number.
              'looppable' is not a field this system reads; it was ignored.
```

`safe_for_auto: false` is the switch for "this sound is right but I want to
place it myself" — it stays indexed and searchable, and the system leaves a
marker naming it instead of using it.

---

## 14. Placing assets

```bash
python -m editing.cli assets match whoosh    # why would it pick what it picks?
python -m editing.cli assets plan
python -m editing.cli assets show-missing    # the shopping list
python -m editing.cli assets show-deferred   # what placed nothing, and why
python -m editing.cli assets dry-run
python -m editing.cli assets execute --yes
```

Every Session 5 placeholder that could be a sound or a graphic gets exactly one
placement, with exactly one of five outcomes — **four of which place nothing**:

| Outcome | Means | What to do |
|---|---|---|
| `placed` | a real asset, real operations | — |
| `missing` | the library has nothing of that kind at all | go and find one |
| `rejected` | candidates existed, none qualified | tag them better, or lower `--min-score` |
| `unsafe` | good match, wrong moment | usually correct; check the reason |
| `marker_only` | matched and deliberately not placed | `--markers-only`, or a `.mogrt` |

On most libraries most of a plan is markers. That is the design working, not a
failure.

### Matching

Category is a **hard requirement**, never a score: an impact placeholder never
gets a music track however many tags overlap. Beyond that the scoring is small,
additive and fully visible — `assets match <kind>` prints every contribution
for every candidate, winners and losers alike:

```
  + 0.86  whoosh_fast_01.wav
        +0.30  in the preferred category (sfx)
        +0.14  tags match: whoosh
        +0.10  its name says 'whoosh', which is exactly what this kind is
        +0.15  0.80s fits this kind
        +0.12  medium intensity suits this
        +0.05  described by a sidecar, so this is not guesswork
  - 0.30  impact_boom_heavy.wav
        ruled out: scored 0.30, below the 0.50 needed to place a short
        transition whoosh automatically
```

**Rotation, not rationing.** Reusing an asset costs something *while a suitable
alternative is still unused*; once everything has been used equally the penalty
cancels out. A library with one arrow graphic uses that arrow every time, and a
library with three whooshes cycles through them.

**Below the threshold is a refusal, not a best effort.** A weak match placed is
worse than a marker: a marker costs a viewer nothing and a wrong sound costs
them the moment.

### Audio placement

| Placeholder | Becomes |
|---|---|
| `impact_sfx`, `comedic_sfx`, `whoosh` | one `clip.overwrite` at the moment, trimmed to the sound, plus `audio.gain` |
| `tension_bed`, `ambience` | tiled across the range (loopable required), plus `audio.fade` |
| `music_start`, `music_rise` | placed from the cue; if the track is shorter than the section it **runs out and fades**, which is ordinary editing |

Levels are conservative, stated in one table, and overridable per file with
`volume_adjust_db`: beds `-18 dB`, ambience `-26 dB`, one-shots `-8 dB`.

**Ducking now actually works.** Session 5 computed the speech ranges and could
not use them — `audio.duck` needs a bed clip and there was none. Placing one is
what makes it available, so a bed covering dialogue gets a real `audio.duck`
carrying those exact ranges, dipping to `-30 dB` under each line:

```
audio.duck [A3@60s]  under 6 speech range(s)  base -18 dB → duck -30 dB
```

### The mixing safety rules

| Rule | Default | Why |
|---|---|---|
| minimum gap between one-shots | 2.5s | three impacts in four seconds is how an auto-scored edit announces itself |
| one-shots per minute | 5 | — |
| asset clips sounding at once | 2 | a bed plus one effect is a mix; a bed plus four is a mess |
| graphics on screen at once | 1 | — |
| callout on screen | ≤ 2.5s | — |
| **two clips on one track** | never overlap | correctness, not taste: `clip.overwrite` destroys what is under it |

Placeholders are considered **most defensible first**, so when a ceiling bites
it drops the weakest moment rather than whichever came last.

### Visual placement

Graphics use the **same safe zones as captions** — never the centre of frame
(the crosshair) and never the bottom centre (the hotbar and health), because a
callout has exactly the same problem as a caption. A graphic is refused
outright where the analysis pass saw an open menu or low health, where the
critic flagged the moment, or where a sidecar sets `hud_risk`.

### The shopping list

When the library cannot fill something, `assets show-missing` groups it by what
to go and find rather than listing it once per moment:

```
  whoosh  x3
      wanted : a short transition whoosh
      put in : assets/sfx/
      tag as : whoosh, swoosh, swish, transition, sweep, pass
      needed : 20.2s, 30.0s, 150.5s

  tension_bed  x2
      wanted : a loopable low-intensity bed
      put in : assets/music/, assets/ambience/
      tag as : tension, bed, drone, pad, dark, suspense
      must be loopable (name it *_loop, or set loopable in a sidecar)
      needed : 70.2s, 120.2s
```

### Executing it

```
sequence.activate  project.import  track.add  clip.overwrite
graphic.image  audio.gain  audio.fade  audio.duck  marker.add
```

This is the first pass that places *clips*, so it is the first whose allowlist
contains a `clip.*` operation. Two things keep it safe, and both are checked
structurally on every operation rather than trusted:

- **`clip.overwrite`, never `clip.insert`.** Insert ripples; overwrite does
  not. Nothing this plan does can move a clip that was already there, so every
  position computed by Sessions 3–5 stays exactly where those sessions put it.
- **Never V1 or A1.** Those are the rough cut's tracks. Assets land on A2
  (one-shots), A3 (beds) and V3 (graphics) — all added by the plan, all
  configurable, and V1/A1 are rejected even as *configuration*.

Plus one guard unique to this pass: **every `project.import` path must be
inside the library root**, so a plan can never pull arbitrary files into your
project.

Together those mean the pass is undone by deleting the added tracks and the
markers.

```bash
python -m editing.cli assets execute          # refuses: needs --yes
python -m editing.cli assets execute --yes    # runs, after validating again
```

Execution also re-checks that every asset file **still exists**, because a plan
built this morning can name a file that moved this afternoon, and Premiere's
error for a missing import is much less clear than this one.

The safest possible pass draws and plays nothing while still telling you
exactly what it would have used:

```bash
python -m editing.cli assets plan --markers-only
```

---

## 15. Episode memory and the retention planner

Everything up to here thinks in clips, segments and moments. This layer thinks
in one **episode**: a thing with an objective, a middle that can sag, a question
the viewer is waiting to have answered, and an ending that either pays off or
does not.

It executes nothing. There is no dry run and no `--yes`, because there is
nothing to run — it produces two artifacts and a list of suggestions a later
pass can read.

```bash
python -m editing.cli episode build-memory
python -m editing.cli episode plan-retention
python -m editing.cli episode show-hooks
python -m editing.cli episode show-risks
python -m editing.cli episode report
```

`auto run` builds both automatically, as two non-critical stages after the
asset pass. `--skip-episode` turns them off.

### What it can and cannot know

**It cannot know retention.** It has never seen an audience, a retention graph
or a single view, and it is not connected to anything that has. Every "risk"
below is a *creative* risk read off edit evidence — a three-minute grind, a goal
nobody states, a question asked eight minutes before it is answered. Those are
real things worth flagging and they are not predictions of a curve.

That is enforced rather than promised:

* Every report prints the same fixed disclaimer, a constant rather than prose
  written per renderer, so it cannot soften into a claim over time.
* A test scans every generated string in a full plan for phrases like
  "guarantee", "viewers will" and "watch time". It has to stay at zero.
* Confidence never means "how likely is this true about the audience". It means
  **how many independent channels agreed**, and nothing else.

### The confidence rule

This is the spine of the layer, so it is worth stating precisely.

| Channels agreeing | Ceiling |
|---|---|
| none | 0.25 |
| one (e.g. a keyword) | 0.45 |
| two | 0.70 |
| three | 0.88 |

A finding cannot affect an edit below **0.55**. So a keyword-only finding is
*structurally* incapable of driving one — not by policy, by arithmetic. And
three agreeing channels still cap below 0.9, because nothing here is ever
certain.

The three channels are what the vision model saw, what was said, and what was
measured in the audio. A Session 2 recommendation is deliberately **not** a
fourth: it was itself derived from those three, so counting it would let one
observation vote twice. It adds a small bonus and never raises the ceiling.

### Two artifacts, not one

**`EpisodeMemory` — what happened.** Beats, objectives, places, people,
recurring motifs, setups, payoffs, callbacks, open loops, and the measured
interest curve.

**`EpisodeRetentionPlan` — what to do about it.** Risk zones, hook candidates, a
climax, an ending, a midpoint reset, and the suggestions.

The split is deliberate: memory is an observation and survives a restyle, the
plan is an opinion about the observation. Re-planning must never be able to
rewrite what was observed, and you should be able to disagree with a suggestion
without the story underneath it moving.

### Which clock the numbers are in

`timebase` is on both artifacts and it matters more than it looks:

`roughcut`
: Sequence time on the scratch sequence. A later pass can use these numbers
  directly.

`timeline`
: A synthetic ordering — every segment of every asset, laid end to end in
  discovery order. Useful for reasoning about story before a cut exists, and
  **wrong** to send to Premiere, because no sequence looks like this. A consumer
  has to go through `segment_ids`.

Conflating them would put captions in the wrong places on a real edit, so it is
a field rather than a convention. `episode build-memory --no-roughcut` forces
the second.

### Beats

Eighteen kinds: `setup`, `objective_stated`, `plan_explained`, `travel`,
`preparation`, `grind`, `discovery`, `danger`, `failure`, `recovery`,
`escalation`, `joke`, `callback`, `payoff`, `reveal`, `climax`, `resolution`,
`outro` — plus `unknown`.

Four rules shape the detector:

* **Never on keywords alone.** A cue phrase scores like anything else, but a
  transcript-only beat has one channel and is capped below the edit threshold.
* **Do not over-label.** A stretch whose best kind scores under the floor stays
  `unknown` and is kept. Labelling everything makes the list as useless as a
  search that matches every document.
* **Preserve uncertainty.** Every beat carries the runner-up kind and the score
  table, so "danger 0.51 / joke 0.49" stays legible as a close call.
* **Merge, do not fragment.** Four consecutive twenty-second mining windows are
  one grind, not four.

Cue phrases are matched **longest first**, and a match claims its characters, so
no stretch of text can score two families. That is the structural fix for the
Session 5 bug where "run" appeared in two lists and double-scored.

### Open loops

A question the episode raises, and whether it ever answers it. Three outcomes,
kept distinct:

`resolved`
: A later moment shares a *salient* word with the question and reads as a
  payoff.

`possibly_resolved`
: A topical link exists but is weak. Flagged for a person.

`open`
: Nothing matched — which is a finding, not a failure. An unanswered question is
  one of the most useful things this layer can tell you.

Resolution is **topical, not positional**. A payoff later in the episode does
not close an earlier loop unless the two are about the same thing, and "the same
thing" means a shared word that identifies a thread rather than a word that
appears in every sentence.

### Risk zones

Thirteen detectors: `weak_hook`, `no_clear_objective`, `boring_repetition`,
`overlong_explanation`, `dead_air`, `low_visual_change`, `confusing_transition`,
`no_stakes`, `payoff_delayed`, `unresolved_setup`, `mid_video_slump`,
`anticlimax`, `unclear_ending`.

A detector that cannot see stays quiet. Motion probing off means every motion
score is `0.0`, and a low-visual-change detector that did not check would fire
on the whole episode — so it checks, and the plan's warnings say it did not run.

### What may be applied without a person

**A marker is always safe.** The worst case of a wrong marker is a marker in the
wrong place.

**A change to timing is safe only where the evidence was measured.** In practice
that means dead air and nothing else: silence is a number, boredom is a
judgement. A `speed_up_grind` suggestion is never automatic at any confidence,
because the risk behind it was inferred.

Every suggestion carries a `marker_fallback` — including the safe ones — because
refusing to act is only useful if something still lands on the timeline for the
person who has to decide.

### The seam for later sessions

Nothing consumes these suggestions yet. The seam exists so the next session does
not have to reshape the artifact to use it:

```bash
python -m editing.cli episode export for_style.json \
    --suggestions-for style --safe-only
```

```python
pipeline.retention_suggestions_for("roughcut", safe_only=True)
```

A `RetentionSuggestion` carries **no Premiere operation** and never will. It
names a range, a type, a reason, evidence, a confidence and which pass would
have to build the operation. Sessions 3, 5 and 6 already know how to turn intent
into operations; duplicating that here would mean two places that can put a
caption on a timeline.

Routing: `keep_setup`, `shorten_boring` and `speed_up_grind` go to the rough cut;
the captions, cards and markers go to the style pass; `add_music_rise_marker` and
`hold_silence_for_comedy` go to assets; `needs_human_review` goes to nobody.

---

## 16. Where outputs go

Default root `data/editing/` (`--output-dir` or `EDITING_OUTPUT_DIR`):

```
data/editing/
├── assets.json                     discovered footage + Premiere mapping
├── transcripts/<asset_id>.json     normalised transcripts (durable)
├── visual/<asset_id>.json          visual events + sampling plan + warnings
├── audio/<asset_id>.json           audio events + baseline + warnings
├── timelines/structure.json        the combined timeline
├── recommendations/structure.json  ← every recommendation, with evidence
├── recommendations/structure.txt   ← the human-readable report
├── plans/structure.json            draft Premiere plan (markers) + dry-run
├── roughcut/structure.json         ← the rough cut: placements + operations
├── roughcut/structure.execution.json  what happened when it was run
├── review/<sequence>/*.jpg         review frames
├── review/<sequence>/review.json   review manifest (frames + their context)
├── critic/structure.critique.json  ← what the critic saw, per frame
├── critic/structure.revisions.json ← revision recommendations, with evidence
├── critic/structure.revisions.txt  ← the human-readable revision report
├── critic/structure.revision-plan.json      the operations + dry-run result
├── critic/structure.revision-execution.json what happened when it was applied
├── layers/structure.json           ← the layered edit: every layer + operations
├── layers/structure.txt            ← the human-readable layered report
├── layers/structure.execution.json what happened when it was applied
├── assets/library.json             ← the asset index (the files live elsewhere)
├── assets/structure.placement.json ← every placeholder resolved, with reasons
├── assets/structure.placement.txt  ← the human-readable asset report
├── assets/structure.placement-execution.json
├── episode/structure.memory.json   ← the story: beats, loops, callbacks
├── episode/structure.memory.txt    ← the human-readable episode report
├── episode/structure.retention.json ← risks, hooks, the peak, suggestions
├── episode/structure.retention.txt ← the human-readable retention report
└── auto/runs/<run_id>/            ← one self-contained folder per auto run
    ├── config.json  state.json
    ├── checkpoints/  artifacts/  reports/  logs/
├── frames/                         extracted JPEGs (deleted unless --keep-frames)
└── cache/
    ├── probe/       ffprobe results
    ├── motion/      motion scans
    ├── audio/       loudness analyses
    ├── transcript/
    ├── visual/      one JSON per analysed window
    └── critic/      one JSON per critiqued frame
```

Each stage writes as it goes, so an interrupted four-hour analysis loses
nothing, and every intermediate file is independently inspectable.

```bash
python -m editing.cli export --out D:/handoff/ep12_structure.json
```

Every command takes `--json` and then prints **one** object on stdout and
nothing else (progress goes to stderr), so this is usable as a subprocess.
Errors exit non-zero with `code` and `hint` fields to branch on.

---

## 17. Caching

Re-running does **not** re-analyse unchanged footage. A cache key is the SHA-256
of:

- the file fingerprint (path, size, mtime, content hash)
- the model name
- the sampling configuration
- the schema version

Every one of those genuinely changes the result, so a hit means the stored value
*is* the value this run would have computed. Change the window length, switch
models, or re-export the clip, and the cache correctly misses.

Critic entries are keyed on the frame image **and the context it was judged
against**. Both matter: a re-exported frame at a different width is a different
picture, and the same picture with a zoom now planned over it is a different
question. Either one changing misses the cache.

Content hashing reads the **head and tail** of the file plus its size. Fully
hashing a 20GB capture would cost more than the analysis being cached, and a
video container's header shifts whenever the content does.

```bash
python -m editing.cli cache info
python -m editing.cli cache clear --kind visual
python -m editing.cli cache clear --kind critic
python -m editing.cli analyze --no-cache
```

Entries are one JSON file each, sharded two levels deep — when the model says
something strange you can open the exact file that produced it and delete just
that window.

---

## 18. Tests

```bash
python -m pytest tests/editing -q        # 1133 tests, ~70s
```

**No FFmpeg, no GPU, no model server and no Premiere required.** Every external
edge has a stub: `MockVisionModel` for Qwen3-VL, `StubFrameSource` for frame
extraction, `FakeBridge` for the Premiere panel, and a patched ffprobe. The
stubs mirror the real interfaces exactly, so a test passing against a stub is
asserting on the same call shape the real component receives.

| File | Covers |
|---|---|
| `test_editing_schema.py` | coercion, clamping, lossless round-trips |
| `test_editing_transcripts.py` | all five formats, word grouping, the store |
| `test_editing_sampling.py` | coverage, densification, bounds, determinism |
| `test_editing_cache.py` | key construction, hit/miss, invalidation, corruption |
| `test_editing_analyzer.py` | messy model output, failures, cache behaviour |
| `test_editing_align.py` | match/contrast/neutral, scoring, merging, **audio attachment** |
| `test_editing_audio.py` | detectors, confidence ceiling, markers, caching |
| `test_editing_recommend.py` | layers, safety pass, dry-run, no execution |
| `test_editing_roughcut.py` | selection, layout maths, conversion, **execution guards** |
| `test_editing_critic.py` | frame coverage, critic coercion, **the finding/fix line**, revision guards |
| `test_editing_style.py` | preset validation, **density ceilings**, caption selection, emphasis safety, layer guards |
| `test_editing_assets.py` | indexing, sidecars, matching, **mixing safety**, track and import guards |
| `test_editing_auto.py` | run state, stage ordering, **checkpoint validation**, resume, execution gates |
| `test_editing_episode.py` | beats, open loops, risk zones, hooks, **the confidence cap**, no fake analytics |
| `test_editing_pipeline.py` | discovery, Premiere mapping, pipeline, CLI |

Tests worth knowing about, because they pin the promises this layer makes:

- `test_inferred_confidence_is_capped` — a guess can never claim as much
  confidence as a measurement
- `test_a_marker_supersedes_the_heuristic_guess` — one laugh is not counted twice
- `test_safety_never_deletes` — rejected recommendations stay visible
- `test_the_budget_removes_the_weakest_first` — the anti-trash pass drops the
  least defensible ideas, not the last ones
- `test_nothing_is_executed` / `test_no_cli_command_ever_executes_a_premiere_edit`
  — asserted at both the plan and the CLI boundary
- `test_premiere_mapping_is_read_only` — every op this layer issues is
  non-mutating in the catalog
- `test_one_channel_can_never_reach_the_edit_threshold` — the episode layer's
  "do not depend only on keywords" rule, as arithmetic rather than a habit
- `test_no_generated_string_claims_to_know_what_viewers_will_do` — scanned over
  every field of a full retention plan, not spot-checked
- `test_a_timing_fix_is_never_safe_on_an_inferred_risk` — silence is measured,
  boredom is a judgement, and only one of them may shorten a clip
- `test_the_plan_reports_the_memorys_climax_rather_than_its_own` — two
  artifacts, one verdict
- `test_audio_events_survive_the_export_round_trip` — the deliverable carries
  the audio channel, not just computes with it
- `test_dead_air_is_never_usable_however_good_the_picture` — audio can veto a
  visually strong segment
- `test_a_stale_dry_run_pass_is_not_trusted` — validation reruns every time
- `test_execution_refuses_a_plan_that_does_not_build_its_own_sequence` — the
  scratch guarantee, checked structurally
- `test_a_protected_range_wins_the_contested_frames` — a payoff can never end up
  inside a sped-up filler clip
- `test_no_cli_command_builds_a_plan_that_edits_the_active_sequence`
- `test_a_zoom_complaint_about_a_frame_with_no_zoom_is_refused` — a hallucinated
  premise cannot cause an edit
- `test_trimming_dead_air_needs_the_audio_layer_to_agree` — two channels have to
  agree before footage is cut
- `test_a_fix_with_no_safe_form_stays_a_recommendation` — nothing is silently
  dropped and nothing is faked
- `test_both_sides_of_a_cut_are_sampled` — the two edge probes are closer than
  the collapse threshold, so this is the test that stops every incoming-cut
  frame quietly vanishing
- `test_the_allowlist_is_the_whole_guarantee` — the set of operations a revision
  can emit and the set it is permitted to emit are the same set
- `test_the_rough_cut_report_survives_the_revision_pass` — a second opinion does
  not overwrite the thing it is judging
- `test_no_style_ever_exceeds_its_own_ceilings` — run for all four presets on
  the same input; this is the premise of the style layer, asserted directly
- `test_a_style_pass_can_never_change_timing` / `test_the_allowlist_is_the_whole_guarantee`
  — the additive-only guarantee, pinned from both ends
- `test_two_captions_never_share_the_screen` and
  `test_a_dull_line_over_dull_footage_never_becomes_a_caption` — the two halves
  of not spamming text
- `test_the_keyword_lists_are_disjoint` — one word in two lists scored an
  utterance twice, which is how a caption reached 1.0 and outranked a chapter
  card
- `test_a_card_held_back_for_room_still_leaves_a_marker` — a documentary never
  silently loses a chapter
- `test_the_rough_cut_survives_being_styled` — restyling replaces the layer
  plan and nothing else
- `test_an_empty_library_still_produces_a_complete_plan` — nobody has a tagged
  sound library on day one, and the pass has to work anyway
- `test_an_invalid_sidecar_never_crashes_the_indexer` — hand-edited JSON will
  have a trailing comma, and the answer is not a stack trace
- `test_two_clips_never_overlap_on_one_track` — `clip.overwrite` destroys what
  is under it, so this is correctness rather than taste
- `test_placing_on_the_rough_cuts_own_track_is_refused` and
  `test_importing_from_outside_the_library_is_refused` — the two guards unique
  to the asset pass
- `test_the_only_asset_of_its_kind_may_repeat` — rotation is not rationing
- `test_a_name_that_says_what_it_is_matches_without_a_probe` — the case that
  silently placed nothing on a machine with no FFmpeg
- `test_a_mock_run_completes_with_nothing_installed` — no FFmpeg, no GPU, no
  model server, no Premiere, no assets, end to end
- `test_a_deleted_artifact_invalidates_its_checkpoint` and
  `test_a_changed_artifact_invalidates_its_checkpoint` — "it passed once" is
  not a reason to skip a stage
- `test_a_resume_never_erases_the_record_of_an_execution` — a dry run and a
  real execution wrote the same file, so every resume after an execution
  permanently blocked every later gate
- `test_a_gate_is_never_executed_twice` — running a gate again would place a
  second copy of everything
- `test_the_later_gates_open_once_the_rough_cut_exists` — the execute, resume,
  execute chain

---

## Current limitations

**The episode layer has never been checked against a real edit.** Its beats,
risks and hooks are plausible on generated footage and on hand-built fixtures.
Nobody has yet taken a finished video, read the retention plan, and said whether
it was right. Until that happens the numbers in it are calibrated against
intuition, not against outcomes.

**Nothing consumes the retention suggestions yet.** The seam is built and
tested; Sessions 3, 5 and 6 do not read it. A suggestion today is something you
read, not something that changes a cut.

**Character names are the weakest thing here.** They come from capitalised words
in one channel, so every one caps below the edit threshold and arrives flagged
for review. A name is a name because a person says so, and nothing has asked one.

**Premiere transcripts.** Adobe exposes no documented API. The XMP route works
where Premiere has run speech analysis and stored word markers; some builds and
some Text-Based-Editing workflows store transcripts where no script can reach
them. `transcript status` tells you which case you are in. **The manual export
path is the reliable one today.**

**Sequence membership** is confirmed only for the active sequence (reading the
others would mean activating them and changing your view). An empty `sequences`
list means *not confirmed*, not *unused*.

**Frame extraction cost.** One FFmpeg process per frame — accurate but not fast.
A 40-minute recording at default settings is roughly 900 extractions (a few
minutes), which is small next to the model calls it feeds. Raise
`vision_concurrency` only against a batching server; local single-GPU serving
saturates at 1–2.

**Motion scanning** decodes keyframes only. On footage with very long GOPs the
signal is coarse, and densification will be less targeted. It degrades to
uniform sampling rather than failing.

**Vision accuracy.** Qwen3-VL-8B is small. Environments and obvious actions are
reliable; specific mob identification, hotbar item names and coordinate OCR are
not always. `confidence` and the `raw_*` fields are there to be checked — treat
the timeline as strong evidence, not ground truth.

**Alignment is a keyword heuristic**, English-only, tuned for Minecraft
commentary. It will miss sarcasm and idiom. It is deterministic and explainable
by design; a second model pass would be more accurate and much slower.

**Audio is only used via the transcript.** No analysis of music, sound effects,
laughter or volume — a scream over silence is invisible to this layer.

**Audio inference is shallow.** Silence, clipping and spikes are solid.
`possible_laughter` fires on any rhythmic burst pattern — a stuttering engine or
gunfire will trigger it. `possible_scream` is "sustained and very loud".
`music_region` is "steady, speech-free energy", which also describes rain, a
generator or a long ambient stretch. All three are capped at 0.45 confidence and
named to say so. If accuracy matters more than cost, feed the audio to a real
classifier and write the results in with `detection: "model"` — the schema is
already shaped for it.

**Music/beat detection does not find beats.** It finds *regions*. There is no
tempo estimation and no beat grid, so `music_cue` recommendations are placed at
story boundaries, not on downbeats.

**Recommendations are rules, not a model.** The planner is deterministic and
free, which makes every suggestion traceable and tunable. It will miss things a
model would catch — subtle comedic timing, running jokes, anything needing
memory across an episode. A model pass can sit on top of these recommendations
later without changing the schema.

**The safety thresholds are opinions.** One active edit per 20 seconds, 12
seconds between repeats — these are defaults for a cinematic style, not
measured optima. Change them with `--budget-seconds` and `--repeat-gap`.

**The rough cut is a rough cut.** It assembles ranges, retimes filler, places
markers and applies conservative zooms. It does not do transitions, colour
grading, text placement, audio mixing or anything requiring taste about
composition. `roughcut unconverted` lists exactly what it declined.

**Speed changes assume ripple works as documented.** The plan retimes clips back
to front so each is still in position when its turn comes. If a Premiere build
ripples differently, the later marker positions drift. The dry run cannot catch
this — it validates operation shape, not Premiere's runtime behaviour. Check the
first executed cut against `roughcut placements` before trusting the layout.

**Punch-ins are applied blind to composition.** The refusal rules cover open
UIs, low health, protected clips and short clips. They do not know where the
subject is in frame, so a 115% punch on a subject already near the edge can
still crop it. Conservative caps limit the damage rather than prevent it.

**Only one video track.** Everything assembles onto V1. Overlays, B-roll and
picture-in-picture need a track model this does not have yet.

**Audio is carried, not mixed.** Appended clips bring their audio with them.
There is no ducking, no levelling, no music bed — those remain markers.

**Sound and music libraries are not wired up.** `music_cue`, `beat_marker`,
`sound_effect`, `ducking` and `visual_callout` are placeholders with real timing
and no chosen asset or graphic.

**Execution is opt-in and scratch-only.** The layer will now build a real
sequence, but only on an explicit `roughcut execute --yes`, only after a dry run
passes in the same call, and only onto a sequence it creates itself. Your open
sequence is never touched without `--allow-active-sequence`. The revision pass
adds an operation allowlist on top of that, and refuses anything outside it.

**The critic is one pass, and a small model.** Qwen3-VL-8B judging a single
still is good at the gross defects — a blown-out frame, a caption over the
crosshair, a subject half out of frame — and unreliable at anything needing
comparison across frames or taste. Most of what it reports is deliberately
handed back to you: on a typical cut, well over half the findings end as
recommendations rather than edits. That ratio is the design working, not the
critic failing.

**The critic cannot see motion.** Everything it is told about timing, speed,
zoom and placement comes from the metadata in the prompt, not from the picture.
If the timeline is missing (no `timeline` built), the frames carry no context
and the pass degrades to bare picture-quality judgements — the command says so
when that happens.

**Marker positions after a timing change are computed, not observed.** Premiere
sequence markers do not ripple with clips, so a trim moves the picture out from
under every marker after it. New markers this pass places are corrected offline;
pre-existing rough-cut markers are counted and warned about, not moved. This has
the same status as the Session 3 ripple assumption: the dry run validates
operation *shape*, not Premiere's runtime behaviour. `--no-timing` avoids the
question entirely.

**Text fixes move a placeholder, not text.** Nothing has placed a title or a
caption on this timeline; text categories are marker placeholders. A
`move_text_placeholder` revision rewrites the marker that tells an editor where
the graphic goes. The revision says so in its own `fix_detail` rather than
letting the count of "fixes applied" imply work that was never done.

**Colour is never corrected automatically.** Lumetri is reachable from the
catalog, but "this frame is too dark" carries no number, and guessing an
exposure lift would be inventing a grade. Brightness findings become markers.

**Nothing here is a loop.** Running `review critique` twice re-judges the same
frames; running it after applying revisions requires re-exporting frames from an
updated cut, which this session does not automate. One pass, on purpose.

**Style presets are opinions, not measured optima.** 1.8 edits a minute for
`cinematic_minecraft` and 7 for `fast_funny` are starting points chosen to feel
distinct, not values derived from anything. They are meant to be edited — that
is why `style show` prints every number and why every field can be overridden
from the command line.

**Caption selection is keywords, not understanding.** The scorer is a
deterministic keyword-and-context heuristic, English-only, tuned for Minecraft
commentary. It will miss sarcasm, running jokes and anything needing memory
across an episode, and it will occasionally caption a line that reads flat on
screen. It is explainable and free, which is why it is a heuristic; a model
pass could sit on top of it without changing the schema.

**Text is rasterised, not live.** `text.create` uses `engine="render"`, which
produces a PNG overlay — available on every install, and **not editable inside
Premiere after placement**. The MOGRT path gives live text but needs a
registered template (`premiere.styles.set_default_mogrt`); until one is
registered, restyling a caption means deleting it and rebuilding.

**Everything the style layer draws lands on one added video track.** There is
no track model beyond that: no B-roll, no picture-in-picture, no per-layer
track assignment. It does mean the whole pass can be removed by deleting that
track.

**No music, no sound effects, no callout graphics exist.** Every audio cue
except the two fades is a marker, and every callout is a label naming what to
point at. `layers show-deferred` and the `marker_only` count in the report say
exactly how much of a pass is placeholder rather than edit — usually most of it.

**Chapter detection is structural, not semantic.** It fires on dimension
changes, deaths and stated objectives. A section boundary a human would feel —
a change of goal, a shift in tone — is invisible to it unless the narration
says so out loud.

**Auto mode adds no editing behaviour.** It is ordering, checkpointing and
failure messages over the six passes that already existed. If a pass makes a
bad choice by hand it makes the same bad choice under `auto`.

**A run's checkpoints trust file fingerprints, not content.** Size and mtime,
not a hash — enough to catch a deleted, truncated or rebuilt artifact between
two stages of one run, which is the whole set of things that actually happens.
An artifact edited in place to the same byte length within the same second
would be reused.

**Asset matching is tags and folders, not listening.** Nothing here analyses
audio content: an "impact" is a file in `sfx/` whose name or sidecar says
impact. A badly named file in the right folder will match badly, and the fix is
a sidecar rather than a smarter matcher. `assets match <kind>` shows the whole
scoring so a surprising choice can be read rather than guessed at.

**Loudness and BPM are never measured.** Both fields exist and are only ever
populated from a sidecar. Levels are applied from a small table of category
defaults (`-18` beds, `-26` ambience, `-8` one-shots), which are opinions, not
measurements — expect to set `volume_adjust_db` on anything that matters.

**Beds are tiled, not crossfaded.** A loopable bed under a long stretch becomes
several copies end to end, up to twelve. If the file does not loop cleanly the
seams will be audible, and the system has no way to tell whether it does — only
the filename or the sidecar says so.

**Music that is shorter than its section simply ends.** It is placed once and
faded out rather than looped, because looping a non-loop track sounds worse
than silence. The report says which placements stop early.

**`.mogrt` templates are matched and never placed.** Driving one needs a
registered template and a parameter mapping. The marker names the template.

**Asset tracks are assumed, not discovered.** The plan writes to A2, A3 and V3
and assumes the rough cut occupies V1/A1 only. It cannot read the sequence's
real track layout offline, so if you have added tracks by hand, set
`--sfx-track` / `--music-track` / `--visual-track` to match. V1 and A1 are
rejected outright.

**Nothing is ever removed.** Re-running the asset pass places assets again
rather than replacing the previous run's. Delete the added tracks first, or
work on a fresh rough cut.

**The style layer does not re-cut.** `trim_aggression` and `dead_air_tolerance`
are carried on the preset and reported, but this pass cannot act on them: it
has no `clip.*` operation. Changing the pacing of the assembly itself means
rebuilding the rough cut with different `--keep-threshold` and `--filler-speed`
values.

---

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `EDITING_OUTPUT_DIR` | `data/editing` | Where everything is written |
| `EDITING_FOOTAGE_DIR` | — | Default footage folder |
| `EDITING_VISION_BACKEND` | `openai` | `openai`, `ollama`, `mock` |
| `EDITING_VISION_BASE_URL` | `http://localhost:8000/v1` | Model server |
| `EDITING_VISION_MODEL` | `Qwen3-VL-8B-Instruct` | Model name (also in cache keys) |
| `EDITING_VISION_API_KEY` | `not-needed` | If your server requires one |
| `EDITING_VISION_TIMEOUT` | `180` | Seconds per model call |
| `EDITING_VISION_CONCURRENCY` | `1` | Windows analysed in parallel |
| `EDITING_MODEL_DIR` | `E:\Assistant\AI_Models\editingllm` | Named in error messages |
| `EDITING_FFMPEG` / `EDITING_FFPROBE` | `ffmpeg` / `ffprobe` | Full paths if not on PATH |
| `EDITING_USE_PREMIERE` | `true` | Talk to Premiere at all |

Sampling variables are in the tuning table in section 3; audio variables in
section 4.

### Where `AI_Models/editingllm` fits

`E:\Assistant\AI_Models\editingllm` holds model weights, model config, prompts
and any future training data. **No importable Python lives there** — all code is
in `E:\Assistant\editing`. To keep generated outputs and caches alongside the
model data instead of in `data/editing`:

```bash
set EDITING_OUTPUT_DIR=E:\Assistant\AI_Models\editingllm\runs
```
