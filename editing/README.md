# Editing Brain V1 — Structure Layer

Turns a folder of Minecraft footage into a machine-readable timeline of **what
happens on screen** and **what is being said over it**.

```
footage → Premiere mapping → transcript → Qwen3-VL vision → structure timeline
```

This layer makes no edits and no creative decisions. It produces the structured
input a later editing layer will plan cuts from. Everything it writes is JSON.

---

## Quick start

```bash
python -m editing.cli doctor                              # check FFmpeg / model / Premiere
python -m editing.cli discover --folder D:/Footage/ep12   # find and map the footage
python -m editing.cli transcript status                   # what transcript data exists
python -m editing.cli analyze                             # run Qwen3-VL over sampled windows
python -m editing.cli timeline                            # combine into the structure timeline
python -m editing.cli show --highlights                   # the moments worth looking at
```

Or all of it in one call:

```bash
python -m editing.cli run --folder D:/Footage/ep12
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

## 4. The combined timeline

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

## 5. Where outputs go

Default root `data/editing/` (`--output-dir` or `EDITING_OUTPUT_DIR`):

```
data/editing/
├── assets.json                 discovered footage + Premiere mapping
├── transcripts/<asset_id>.json normalised transcripts (durable)
├── visual/<asset_id>.json      visual events + sampling plan + warnings
├── timelines/structure.json    ← the deliverable
├── frames/                     extracted JPEGs (deleted unless --keep-frames)
└── cache/
    ├── probe/    ffprobe results
    ├── motion/   motion scans
    ├── transcript/
    └── visual/   one JSON per analysed window
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

## 6. Caching

Re-running does **not** re-analyse unchanged footage. A cache key is the SHA-256
of:

- the file fingerprint (path, size, mtime, content hash)
- the model name
- the sampling configuration
- the schema version

Every one of those genuinely changes the result, so a hit means the stored value
*is* the value this run would have computed. Change the window length, switch
models, or re-export the clip, and the cache correctly misses.

Content hashing reads the **head and tail** of the file plus its size. Fully
hashing a 20GB capture would cost more than the analysis being cached, and a
video container's header shifts whenever the content does.

```bash
python -m editing.cli cache info
python -m editing.cli cache clear --kind visual
python -m editing.cli analyze --no-cache
```

Entries are one JSON file each, sharded two levels deep — when the model says
something strange you can open the exact file that produced it and delete just
that window.

---

## 7. Tests

```bash
python -m pytest tests/editing -q        # 298 tests, ~1s
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
| `test_editing_align.py` | match/contrast/neutral, scoring, merging |
| `test_editing_pipeline.py` | discovery, Premiere mapping, pipeline, CLI |

---

## Current limitations

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

**No editing.** By design. This layer describes footage; it does not cut it.

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

Sampling variables are in the tuning table in section 3.
