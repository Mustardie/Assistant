# Editing Brain V1 — Structure, Recommendations, Rough Cut, Critic

Turns a folder of Minecraft footage into a machine-readable timeline of **what
happens on screen**, **what is being said**, and **what is heard** — proposes
the edits worth making, with the evidence for each one — assembles them into a
real Premiere rough cut, and then looks at that cut and improves it once.

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
```

**Nothing runs unless you say so, twice.** Every plan is validated offline
first, execution needs an explicit `--yes`, and the target is always a scratch
sequence — one the rough cut creates itself, and the only one the revision pass
is allowed to touch. Your open sequence is never edited. Everything the layer
writes is JSON, plus two plain-text reports.

**The critic pass is one iteration, not a loop.** It exists to catch the obvious
mistakes an automatic assembly makes — a zoom that crops the HUD, a caption over
the action, a beat cut a moment early. It is not trying to converge on a
finished edit, and most of what it finds it deliberately hands back to you
rather than fixing.

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
```

Then read the results:

```bash
python -m editing.cli top          # the moments most worth using
python -m editing.cli reactions    # moments the audio made interesting
python -m editing.cli removed      # what the safety pass threw out, and why
python -m editing.cli draft        # the draft Premiere plan (executes nothing)
python -m editing.cli review show-issues   # what the critic found, worst first
python -m editing.cli review report        # the full revision report
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

## 11. Where outputs go

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

## 12. Caching

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

## 13. Tests

```bash
python -m pytest tests/editing -q        # 678 tests, ~9s
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
