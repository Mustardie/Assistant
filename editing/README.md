# Editing Brain V1 — Structure + Recommendations

Turns a folder of Minecraft footage into a machine-readable timeline of **what
happens on screen**, **what is being said**, and **what is heard** — then
proposes the edits worth making, with the evidence for each one.

```
footage → Premiere mapping → transcript → Qwen3-VL vision ─┐
                                                            ├→ structure timeline
                                            audio events ──┘
                                                    ↓
                       six recommendation layers → safety pass
                                                    ↓
                    draft Premiere plan → offline dry-run (never executed)
```

**Nothing here applies an edit.** The draft plan is built, validated offline and
written to disk; running it is a separate decision for a person. Everything the
layer writes is JSON, plus one plain-text report.

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
```

Then read the results:

```bash
python -m editing.cli top          # the moments most worth using
python -m editing.cli reactions    # moments the audio made interesting
python -m editing.cli removed      # what the safety pass threw out, and why
python -m editing.cli draft        # the draft Premiere plan (executes nothing)
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

## 8. Where outputs go

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
├── plans/structure.json            ← draft Premiere plan + dry-run result
├── frames/                         extracted JPEGs (deleted unless --keep-frames)
└── cache/
    ├── probe/       ffprobe results
    ├── motion/      motion scans
    ├── audio/       loudness analyses
    ├── transcript/
    └── visual/      one JSON per analysed window
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

## 9. Caching

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

## 10. Tests

```bash
python -m pytest tests/editing -q        # 476 tests, ~2s
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
- `test_audio_events_survive_the_export_round_trip` — the deliverable carries
  the audio channel, not just computes with it
- `test_dead_air_is_never_usable_however_good_the_picture` — audio can veto a
  visually strong segment

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

**Most recommendations cannot become Premiere operations yet.** Only markers
convert, because everything else needs the footage on a sequence. That is a real
gap, reported per category rather than hidden.

**Sound and music libraries are not wired up.** `music_cue`, `beat_marker`,
`sound_effect`, `ducking` and `visual_callout` are placeholders with real timing
and no chosen asset or graphic.

**No editing.** By design. This layer describes footage and proposes edits; it
does not cut anything, and it never executes the plan it writes.

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
