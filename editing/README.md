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
footage → Premiere mapping → local Whisper transcript → Qwen3-VL vision ─┐
                                                            ├→ structure timeline
                                            audio events ──┘
                                                    ↓
                       six recommendation layers → safety pass
                                                    ↓
     optionally: a director pass reads the whole episode and chooses the
     ranges, with deterministic checks deciding what it is allowed to do
                                                    ↓
                     rough cut: selected ranges → scratch sequence plan
                                                    ↓
      optionally: the retention findings are wired into the cut -- a cold open
      at the front, sagging stretches compressed, setups protected
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
                                                    ↓
        the whole thing read as one episode: beats, open loops, risk zones,
        hook candidates, a peak, an ending → suggestions a later pass can read
                                                    ↓
      review queue → what you said about each decision, appended to a log that
      is never rewritten → preference signals, training signals, exports
                                                    ↓
      and, at any point after the rough cut: FFmpeg renders it to a proxy MP4
      you can actually watch, with timestamped review notes beside it
                                                    ↓
      optionally: a few key-moment captions and a few restrained sound cues --
      never subtitles, never a whoosh at every cut, every refusal recorded
                                                    ↓
        fifteen reliability checks: is what this produced actually usable?
                                                    ↓
       one review folder with an index: what to watch, what changed, what is
       weak, and what needs you -- and the whole thing again over every folder
       under a root, in batch
                                                    ↓
      optionally: the creative visual layer reads every plan above and decides
      where the edit should point at something -- a zoom onto the creeper, a
      card naming the objective -- refusing most of what it considers
                                                    ↓
     cut + captions + sound + visuals -> one FinalEditPlan, a Premiere
     operation plan validated offline, and an honest note that none of it
     has been drawn
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

**It can hear the footage.** Local Whisper turns your capture into a
transcript with word-level timing, offline, with nothing uploaded — and that
transcript is what the story layer reads. See
[§2.1 Making one with Whisper](#21-making-one-with-whisper).

**Selection can be a judgement rather than a threshold.** A model reads the
whole structured episode -- transcript, beats, open loops, setups and payoffs,
risk zones, your own prose style guide -- and decides what the cut is: what to
open on, what to protect, what to compress, what to cut. Then twelve
deterministic checks decide which of those it is allowed to do. The model
proposes; the rules dispose, and every refusal names the rule. See
[§18 the director pass](#18-the-director-pass).

**The cut can be shaped like an episode rather than like a timeline.** The
strongest moment moves to the front as a cold open, the stretches the retention
planner called sagging get compressed, the setups a payoff needs are protected
before anything that removes footage runs, and ordinary silence is cut harder
than the general selector dares. Nothing in it claims to predict retention --
it counts what changed in the edit. See
[§19 retention structure wiring](#19-retention-structure-wiring).

**You can watch the cut without Premiere.** FFmpeg renders any rough cut to a
proxy MP4 in about a minute per ten minutes of video, with a timestamped review
file beside it to write on. That turns the loop from "open Premiere, execute,
undo" into "change a rule, render, watch". Nothing is executed and no host
application is involved. See
[§17 the proxy render](#17-watching-it-the-ffmpeg-proxy-render).

**Your review is the only thing here that cannot be regenerated.** Every other
file under `data/editing/` is derived from the footage and can be rebuilt by
re-running a pass. Human feedback cannot, so it is written to an append-only
log that nothing in this system ever rewrites — and nothing trains on it either.
See [§16 Feedback](#16-feedback-and-the-human-review-loop).

**Text and sound are punctuation, not decoration.** The caption pass will only
caption nine kinds of moment -- a death, a reveal, the stated objective, a
payoff, a callback, a named threat -- inside a per-minute budget it will
usually lose, and every refused line stays in the plan with the rule that
refused it. The audio pass places a riser, a sting, a whoosh at a real section
change and at most one bed, and refuses anything that would land on a spoken
word. Both are off by default. See [§20 key-moment captions](#20-key-moment-captions)
and [§21 minimal music and SFX polish](#21-minimal-music-and-sfx-polish).

**Every run says whether it produced something usable.** Fifteen checks look at
the footage, the transcript, the decisions and the output, and each one carries
what it measured and the command that would fix it. A warning never stops a
run; only five conditions can say "do not review this". See
[§22 reliability checks](#22-reliability-checks).

**A finished run is one folder with an index.** What video was produced, what
changed, what to watch for, where it is weak, and what needs a human decision
-- five questions in that order, with the small reports copied in beside them.
See [§23 the review package](#23-the-review-package).

**You can point it at a library rather than a folder.** Batch mode applies one
configuration to every footage folder under a root, skips what is already done,
survives a folder that breaks, and never overwrites finished work. See
[§24 batch mode](#24-batch-mode).

**The edit can point at things.** The creative visual layer reads every plan
above -- the director's decisions, the cold open, the captions, the sound, the
episode memory, the vision events -- and finds the moments that earn emphasis:
a death worth freezing, a creeper worth ringing, an objective worth naming. It
then refuses most of what it considered, and says why for each. Off by default.
See [§26 the creative visual layer](#26-the-creative-visual-layer).

**Nothing that layer plans is ever drawn.** Its Premiere operations are
proposals validated offline against the catalog; its FFmpeg side is a
capability statement and a marker file beside the proxy. `burned_in` is False
everywhere and no code path sets it True. See
[§28 the Premiere visual plan](#28-the-premiere-visual-plan) and
[§29 FFmpeg preview](#29-ffmpeg-preview-what-it-can-and-cannot-show).

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

That runs thirty-one stages — discovery, transcripts, audio, vision,
timeline, recommendations, rough cut, critic, style layers, asset placement,
episode memory, retention plan, caption polish, audio polish, the visual layer,
the final edit plan, proxy render, reliability checks, review package — and
writes a plan and an offline dry run for each of the four things that could
touch Premiere.

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
  . transcribe             skipped  --transcribe was not set
  + analyze                passed   segments=12, covered_seconds=120.0
  + recommend              passed   total=33, accepted=30
  . director_plan          skipped  --director was not set
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
  + episode_memory         passed   beats=7, open_loops=3, callbacks=1
  + retention_plan         passed   risks=4, hooks=3, suggestions=9
  . retention_cut          skipped  --retention-cut was not set
  . caption_polish         skipped  --captions was not set
  . audio_polish           skipped  --audio-polish was not set
  . visual_plan            skipped  --visual-layer was not set
  . final_edit_plan        skipped  --visual-layer was not set
  . render_proxy           skipped  --render-proxy was not set
  + reliability_gates      passed   status=warn, usable=True, passed=9
  . feedback_start         skipped  --feedback was not set
  . feedback_queue         skipped  --feedback was not set
  . feedback_report        skipped  --feedback was not set
  + review_package         passed   items=20, present=11, has_video=False
  + report                 passed
```

`transcribe`, `director_plan`, `retention_cut`, `caption_polish`,
`audio_polish`, `visual_plan`, `final_edit_plan`, `render_proxy` and the three
`feedback_*` stages are the opt-*in* ones, each for its own reason. The retention wiring reshapes the
episode, which is not something to do to somebody's footage by default.
Transcription loads a speech model and takes minutes per episode, so it waits
to be asked — but the story layer is blind without it, so turn it on unless you
already have transcripts. The director needs a model endpoint. Putting text and
sound on your video is not a default, and deciding where it should zoom, flash
and point at things is the least default-able thing in the system. Rendering
costs minutes of CPU and
hundreds of megabytes. Feedback starts a review a person is expected to finish,
so `auto run --feedback` asks for it and the run report otherwise just tells
you how much would be worth reviewing.

`reliability_gates` and `review_package` are the two exceptions: they are
opt-*out*. Neither creates anything new, both cost a fraction of a second, and
together they are the difference between a run you can inspect and forty files
whose layout you have to learn. `--no-review-package` turns the second off.

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 \
    --transcribe --director --retention-cut --render-proxy \
    --captions key_moments --audio-polish placeholders \
    --visual-layer balanced --no-premiere
```

That hears the footage, has a model choose the cut, reshapes it like an
episode, captions the handful of moments that carry it, marks where sound
belongs, renders it, checks whether the result is usable, and leaves you one
folder with an index — without Premiere being opened once. See
[§18 the director pass](#18-the-director-pass),
[§19 retention wiring](#19-retention-structure-wiring),
[§20 captions](#20-key-moment-captions),
[§21 sound](#21-minimal-music-and-sfx-polish),
[§22 the checks](#22-reliability-checks),
[§23 the review package](#23-the-review-package) and
[§26 the visual layer](#26-the-creative-visual-layer).

### The run folder

Every run gets its own directory under `data/editing/auto/runs/<run_id>/`:

```
config.json      exactly what the run was invoked with
state.json       every stage result and gate, rewritten after each stage
checkpoints/     one file per completed stage, with artifact fingerprints
artifacts/       the run's own output_dir -- timelines, plans, reports
reports/         report.json report.txt checks.json checks.txt
review/          review_index.md, and a copy of every small report
logs/            run.log
```

`review/review_index.md` is the one to open. See
[§23 the review package](#23-the-review-package).

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
- **It does not review the edit for you, and does not learn from you doing it.**
  `--feedback` opens a review session and builds its queue; reading it and
  answering is yours, and nothing trains on the answers.
- **It does not put captions in the video.** The caption pass writes a plan and
  a sidecar `.srt`; the proxy carries the cut and its original audio.
- **It does not play any sound it plans.** The audio pass produces notes and,
  in `assets` mode, matches. Nothing is mixed, levelled or heard.
- **Its reliability checks judge shape, not taste.** Fifteen green ticks mean
  the output is well-formed, not that the edit is good.
- **It draws no visual effect.** The visual layer plans; the Premiere
  operations are proposals validated offline and the FFmpeg side is a
  capability statement.

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
python -m editing.cli director plan                       # let a model choose the cut
python -m editing.cli director show-rejected              # what the rules refused
python -m editing.cli roughcut build                      # assemble a rough cut plan
python -m editing.cli roughcut dry-run                    # validate it offline
python -m editing.cli retention plan                      # what a cold open would change
python -m editing.cli retention plan --mode retention     # and reshape the cut
python -m editing.cli render roughcut                     # watch it, without Premiere
python -m editing.cli render open                         # play the proxy
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

python -m editing.cli polish captions --captions key_moments   # the few lines
python -m editing.cli polish show-rejected                # and every refusal
python -m editing.cli polish audio --audio-polish placeholders # where sound goes
python -m editing.cli polish show-missing                 # the shopping list

python -m editing.cli visuals plan --visual-layer balanced # where to point
python -m editing.cli visuals show-rejected               # and what it refused
python -m editing.cli visuals export-premiere-plan        # what Premiere could do

python -m editing.cli auto show-checks                    # is the output usable?
python -m editing.cli review package                      # gather it into one folder
python -m editing.cli review open-latest                  # and open the index

python -m editing.cli auto batch --root E:/Clips --dry-run     # a whole library
python -m editing.cli auto batch --root E:/Clips --render-proxy --no-premiere
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

**The transcript is the most load-bearing input in this system.** Objectives,
open loops, callbacks, setup and payoff, and half the retention risks are all
read off it — so footage with no transcript makes the entire story layer go
quiet, and says so rather than guessing.

There are two ways to get one: make it locally with Whisper (below), or bring
one you already have ([§2.2](#22-bringing-a-transcript-you-already-have)).

### 2.1 Making one with Whisper

Local, offline, no API, no key, nothing uploaded. Needs no Premiere.

```bash
pip install faster-whisper
python -m editing.cli transcribe file "D:/Footage/ep12/session_01.mp4"
python -m editing.cli transcribe folder "D:/Footage/ep12"
```

```
  file      : D:\Footage\ep12\session_01.mp4
  backend   : faster_whisper / small  (cpu/int8)
  language  : en (p=1.00)
  media     : 2412.0s
  speech    : 1533.4s (64% of runtime)
  segments  : 412  (11 low confidence, 38 dropped)
  words     : 4180
  confidence: 0.81 mean
  took      : 561.0s (4.3x realtime)  -- a 40-minute episode would take ~9 min
```

That `x realtime` figure is the one worth reading: it is measured on *your*
machine and answers "how long will my episode take" without guesswork.

#### What to use on this PC

Measured here, CPU only (`torch 2.13.0+cpu`, so CUDA is unavailable):

| Model | Speed | Use it when |
|---|---|---|
| `tiny` | ~15x realtime | you want a transcript in seconds and do not care about proper nouns |
| `base` | ~8x realtime | quick passes while you are iterating |
| **`small`** | **~4x realtime** | **the default. Gets "nether", "netherite", "creeper" right** |
| `medium` | ~1.5x realtime | a final pass on an episode that matters |
| `large-v3` | slower than realtime | not worth it on CPU |

A 40-minute episode with `small` is about **9 minutes**. The first run of any
size downloads the model once (~500 MB for `small`).

**With a CUDA GPU**, expect roughly 5–10x that, and `medium` becomes the sane
default. Auto-detection goes through `torch`, so if you have a CUDA-capable
CTranslate2 but no CUDA build of `torch`, pass `--device cuda` explicitly.

#### The flag that matters most

Whisper mis-hears domain nouns constantly — "creeper" as "creature", "nether"
as "never", "diamonds" as "dynons". A vocabulary hint fixes most of it:

```bash
python -m editing.cli transcribe folder "D:/Footage/ep12" \
    --prompt "Minecraft, creeper, enderman, nether, netherite, redstone, diamonds"
```

Verified on this machine: `tiny` heard *"I found Dynons, actually that is
Netherite"*; `small` with the hint got both words right.

#### Everything else

| Flag | Why |
|---|---|
| `--model small` | size, or a path to a local CTranslate2 model |
| `--device auto\|cuda\|cpu` | `auto` picks CUDA only when it is genuinely usable |
| `--compute-type auto` | `float16` on CUDA, `int8` on CPU |
| `--language en` | skips detection, which occasionally guesses wrong on a quiet intro |
| `--no-word-timestamps` | ~10–15% quicker; you lose per-word timing |
| `--no-vad` | decodes silence too. Whisper hallucinates into silence, so leave VAD on |
| `--extract-audio` | convert to WAV with FFmpeg first, instead of decoding the container |
| `--force` | ignore the cache and re-transcribe |

#### Where it goes

```
data/editing/transcripts/
├── <asset_id>.json          ← the durable transcript every pass reads
└── <job_id>/                ← the record of one transcription
    ├── transcript.json        segments, word timings, probabilities
    ├── transcript.srt         standard SRT
    ├── transcript.txt         readable, with a provenance header
    ├── metadata.json          config, timings, device, cache key
    └── warnings.json          everything the run wanted to say
```

Two stores, and they do different jobs. The **job folder** records *how* a
transcript was made. `<asset_id>.json` is *the transcript*, in the place
`resolve()` looks first — writing it is the actual seam into the pipeline.

`transcript.json` carries both a `segments` list (rich) and an `entries` list
(the canonical shape), so it parses with the same normaliser every other
transcript in the system goes through — no bridge, no special case.

The job ID comes from the **cache key**, not a timestamp: transcribing the same
file with the same settings lands in the same folder, and changing the model
produces a different one.

```bash
python -m editing.cli transcribe status              # is it installed, what has it done
python -m editing.cli transcribe show <job_id>
python -m editing.cli transcribe export <job_id> --out subs.srt --format srt
python -m editing.cli transcribe clear-cache --yes
```

#### Caching, and what invalidates it

Keyed on the file's **content hash** plus every setting that changes a word:
model, backend, language, beam size, VAD, word timestamps, compute type and the
vocabulary prompt. `--force` bypasses it.

- Re-export or re-encode the file → **misses**, correctly. You never get a
  transcript of the old audio.
- Change the model or the prompt → misses.
- Change `--timeout`, or turn the cache off → **hits**. Neither changes a word,
  and invalidating on them would throw away hours of work for nothing.

A batch also **skips** any clip that already has a current transcript — so
running it twice over a folder costs a fingerprint check per file. A *stale*
transcript, made from audio that has since changed, never counts as present.

#### Batches survive their worst file

```
  5 file(s): 3 done, 1 from cache, 0 skipped, 1 failed
------------------------------------------------------------------------------
FAILED (1)
  broken.mp4
    why : 'broken.mp4' is empty
    fix : The file is zero bytes -- check the copy or export finished.
```

Thirty clips where two are corrupt is an ordinary afternoon. The batch never
aborts, and every failure carries the fix.

#### With auto mode

```bash
python -m editing.cli auto run --folder "D:/Footage/ep12" --transcribe --transcribe-model small
```

Opt-in, because it loads a speech model and takes minutes. The stage is
non-critical: if faster-whisper is missing the stage blocks, the run continues,
and analysis, the rough cut, the style pass and the asset pass all still work —
they are just working deaf, and the report says so.

Safe to leave on: a second run transcribes nothing.

#### What it does not do

- **No diarisation.** `speaker` is always `null`. Nothing here can tell two
  voices apart, and inventing a label would be worse than admitting it.
- **No translation.** It transcribes in whatever language it hears.
- **No punctuation repair, no profanity filter, no re-timing.** Cues come out
  exactly where the model put them.
- **`--backend mock` fabricates text.** It exists for tests and for machines
  with no model, and it stamps `mock: true` on the result, the JSON, the text
  header and the transcript note. Never use it for an edit.
- **Accuracy is Whisper's, not this layer's.** Fast excited commentary over
  game audio is the hard case. `small` plus a vocabulary prompt is the best
  cost/quality point measured here; the fix for a bad transcript is a bigger
  model, not a different setting.

### 2.2 Bringing a transcript you already have

#### Premiere Speech to Text

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

#### Importing a transcript file

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

#### Sidecar auto-discovery

A transcript sitting next to the footage (`session_01.srt` beside
`session_01.mp4`) is picked up automatically. SRT and VTT are preferred over
plain text because they carry per-line timing.

#### Timing is never invented

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

## 16. Feedback and the human review loop

Everything up to here is the system's opinion. This layer records **yours**, in
a shape a later session can learn from.

```bash
python -m editing.cli feedback start --run <run_id>
python -m editing.cli feedback queue --limit 20
python -m editing.cli feedback rate <prompt-id> good --reason pacing --note "This cut feels clean"
python -m editing.cli feedback report
python -m editing.cli feedback export dataset.jsonl
```

### What it does

Takes everything the run produced — the cut, the recommendations, the critic's
findings, the captions, the sounds, the beats, the risks, the hooks — works out
which of those decisions are actually worth asking a person about, asks, and
appends the answers to a log that is never rewritten.

Each answer carries a rating, a reason, an optional correction, and **the record
it was about**: a placement ID, a caption ID, an asset placement, a hook
candidate, or failing all of those a range of the timeline. That link is the
whole point. A note that says "this bit drags" is a diary entry; a note attached
to `p_3`, `[84.00–112.00]`, in `roughcut/structure.json`, alongside what the
system thought it was doing there, is something a dataset builder can use.

### What it does not do

**It trains nothing.** No weights, no tuning, no fine-tuning, nothing. No pass
in this system changes its behaviour because of anything you say here.

**It applies nothing.** The preference signals it extracts — "prefers slower
holds", "dislikes forced callbacks" — are written to a report and an export and
read by nobody. A signal can come out marked
`safe_to_apply_automatically: true`, and that is a statement about the *evidence*
(enough of it, consistent, and about something reversible), not permission this
layer has granted itself. The field says so in its own text.

**It executes nothing.** No Premiere, no FFmpeg, no model, no GPU, no footage.
Every input is JSON another pass already wrote.

**It is not analytics.** A rating of `weak_retention` records what you thought
while watching. Nothing here has seen a retention graph, a view count or an
audience, and every report says so in the same fixed words.

### Starting a session

```bash
python -m editing.cli feedback start --title "ep12 first pass" --limit 20
```

This creates `data/editing/feedback/sessions/<session_id>/`, records what
existed to be reviewed, and builds the queue. Later commands default to the most
recent session, so `--session` is only needed when you have more than one open.

What it records at the start matters: a session over a run where the critic
never ran is a *different* review from one where it did, and the report has to
be able to say so rather than showing a short list of complaints and letting you
conclude the edit was fine.

### The review queue

A finished run makes a few hundred decisions. Asking about all of them is the
same as asking about none of them — the review is abandoned at item forty and
what you do get is from the least interesting end of the list.

So the queue is a *selection*, not a listing. It is deliberately not the top N
by score, because a pure ranking fails three ways:

| Failure | The rule |
|---|---|
| One pass floods it — the style layer makes the most items, so a ranking is thirty captions and nothing else | Reserved slots for structural, risky, uncertain, setup/payoff and refused items, filled before the ranking gets the rest |
| Only failures get asked about, which teaches what you dislike and nothing about what to keep doing | ~15% of the queue is decisions that look **right**, marked `positive_sample` |
| The same moment is asked about six times — a caption, an SFX, a beat and a risk zone at 4:12 | Near-identical prompts collapse; merely adjacent ones share a group, so you read one moment at a time |

Each question carries **what the system decided and how sure it was**. That is
not decoration: feedback given without seeing what the system thought is
feedback about the video, and feedback given with it is feedback about the
decision. Only the second kind can teach anything, so a prompt with no recorded
decision is a bug — and a rating collected outside the queue is honestly marked
unusable for training for exactly that reason.

```
  -- moment 1, around 0:00.00 ----------------------------------
  q_bc74743513  [   0.00-  28.00] roughcut  ?*     p=1.00  This clip runs 28.0s (25% of the cut). Right...
  q_f418442853  [   0.00- 112.00] edit      ^      p=0.68  Overall: would you publish this cut?
  q_7c19c4a6e6  [  12.00-  15.00] style     *!+    p=0.58  Caption "RUN" -- right words, right moment?
```

Flags: `?` uncertain · `*` high impact · `!` decided automatically ·
`^` structural (hook, peak, ending) · `r` retention risk · `p` setup/payoff ·
`x` refused · `+` a decision that looks right.

Groups are ordered by priority, not by the clock, so "moment 2" can sit earlier
in the episode than "moment 1". That is why the divider is numbered.

```bash
python -m editing.cli feedback queue --limit 30 --category caption
python -m editing.cli feedback queue --source assets --unanswered
python -m editing.cli feedback queue --regenerate --no-positive
python -m editing.cli feedback show q_bc74743513      # one question in full
```

Regenerating a queue is always safe — it holds no feedback. The previous one is
kept beside it as `queue.1.json`, because a rating references a prompt ID and
that question has to stay readable afterwards.

### Rating a decision

```bash
python -m editing.cli feedback rate q_bc74743513 good --reason pacing --note "This cut feels clean"
python -m editing.cli feedback rate li_danger bad --reason boring --correction "cut this shorter"
python -m editing.cli feedback rate 120-155 boring --reason retention
python -m editing.cli feedback rate whole good
```

The first argument is anything that identifies something: a prompt ID from the
queue, a record ID from any artifact, a range like `120-155`, or `whole`. You
should not have to remember which pass produced an ID in order to talk about it.

Ratings, grouped by what they judge:

| | |
|---|---|
| **verdict** | `good` `bad` `okay` `unsure` |
| **action** | `keep` `cut` `shorten` `extend` `move_earlier` `move_later` |
| **amount** | `too_much` `too_little` |
| **placement** | `wrong_moment` `wrong_style` |
| **tone** | `funny` `boring` `confusing` `hype` |
| **context** | `useful_context` `useless_context` |
| **captions** | `good_caption` `bad_caption` |
| **sound** | `good_music_sfx` `bad_music_sfx` |
| **hooks** | `good_hook` `bad_hook` |
| **pacing** | `bad_pacing` |
| **payoff** | `strong_payoff` `weak_payoff` |
| **callbacks** | `good_callback` `forced_callback` |
| **retention** | `strong_retention` `weak_retention` |

Reason categories: `pacing` `story` `clarity` `retention` `comedy` `emotion`
`visual` `audio` `caption` `timing` `style` `safety` `technical` `preference`.
You can also pass a rating word as a reason — `--reason boring` files it under
`pacing` and keeps the word — and anything unrecognised is kept as free text
rather than dropped.

Useful flags: `--confidence 0.4` (how sure *you* are — separate from how
emphatic the rating is), `--priority`, `--no-training` to keep something out of
any future dataset, `--follow-up` to flag it for another look, and
`--allow-unknown` to keep a rating whose ID matches no record.

### Notes and corrections

```bash
python -m editing.cli feedback note li_danger "the RUN caption sits over the hotbar"
python -m editing.cli feedback correct p_0 "cut this shorter" --seconds -4
python -m editing.cli feedback correct li_react "different music" --action replace
```

A note keeps whatever rating already stands, and when there is none it records
one that is explicitly **not** training material: a sentence about a moment is
not a judgement of a decision, and recording `okay` as a label would put an
opinion in the dataset that you never gave.

A correction answers the question a directional rating leaves open. `shorten`
alone tells a later session which way to move and not how far, so an item
carrying one and no correction is flagged for follow-up until you add one. With
`--seconds`, the correction produces a concrete before **and** after; without
one, it produces a before and an honest gap.

### Nothing is ever overwritten

The log is `feedback.jsonl`, opened in append mode and nowhere else. There is no
update, no delete and no rewrite.

- Changing your mind **appends** a new item whose `supersedes` names the old
  one. `feedback list` shows what stands; `feedback show <id>` shows how it got
  there.
- Starting a session over one that holds feedback is refused, and not even
  `--force` will do it.
- An export never replaces an earlier export; a numbered suffix is added.
- A corrupt line costs that line and is reported, rather than aborting the read.

This is the only artifact in the system that cannot be regenerated by re-running
a pass. Everything else under `data/editing/` is derived from the footage.

```
data/editing/feedback/sessions/<session_id>/
├── session.json     what this review is about, and what existed to review
├── queue.json       the questions, as generated (queue.1.json = the previous)
├── feedback.jsonl   ← the record. append-only, never rewritten
├── summary.json     derived: counts, coverage, preference and training signals
├── report.md        derived: the readable version
└── exports/         what was exported, each with a manifest
```

Only `feedback.jsonl` is the record. Lose the derived files and
`feedback report` rebuilds them exactly; lose the log and the review has to
happen again.

### Reports and preference signals

```bash
python -m editing.cli feedback report
python -m editing.cli feedback stats --preferences
python -m editing.cli feedback list --follow-up
```

`report.md` leads with **what this feedback cannot support** — how many ratings
could not become training data and why, how much of the queue went unanswered,
and which passes the session never saw. A page of preference signals reads as
"the system now knows what I like" unless the reader is told first that four of
the eleven ratings could not be joined to a record.

A preference is a `(dimension, direction)` pair rather than a sentence, because
"dislikes too many captions" and "wants fewer captions" are one preference
written twice, and a later session counting sentences would treat them as two.

| Field | Meaning |
|---|---|
| `dimension` / `direction` | e.g. `caption_density` + `less` |
| `statement` | the sentence, generated from one table so the wording cannot drift |
| `evidence_count`, `positive_examples`, `negative_examples` | what it is built on |
| `agreement`, `contradictions` | how consistently the evidence pointed one way |
| `confidence` | your average certainty × agreement, capped by evidence count |
| `is_style_specific`, `style` | whether it is about this preset or about editing |
| `scope` | `episode`, or `global` once it spans sessions |
| `safe_to_apply_automatically` | whether the *evidence* would support it |

Two rules keep this honest. **Disagreement is counted, not filtered** — a
dimension you have gone both ways on produces one signal in the majority
direction with the dissent recorded, rather than a manufactured consistency.
And **a preference about timing is never automatically safe**, at any
confidence: the same marker-versus-timing split §15 uses, from the other
side. A wrong preference about caption tone costs a caption nobody liked; a
wrong preference about pace costs footage.

### How this becomes training data

Each usable rating becomes a `TrainingSignal` — preparation for the dataset
builder, deliberately not a training example:

```bash
python -m editing.cli feedback export dataset.jsonl --include feedback,preferences,training
python -m editing.cli feedback export --format csv        # lossy, for a spreadsheet
```

A signal carries the input references (artifact + ID, never copies), the system
decision and its confidence, your rating, reasons, note and correction, the
before/after where the correction had a size, and a task:

`ranking` · `classification` · `edit_decision` · `caption_decision` ·
`retention_decision` · `hook_selection` · `episode_memory_judgment` ·
`callback_decision` · `asset_matching` · `critique` · `style_preference`

The task comes from the decision, not just the record type — a caption and a
punch-in are both `LayerItem` rows and are not the same question.

**Unusable signals are emitted too, with their reason.** A signal that says "not
usable because the rating was `unsure`", or "because the target could not be
joined to a record", tells the next session what this collector is *losing*,
which is the difference between fixing it and guessing at it. Feedback is
excluded when:

- the rating is `unsure`, or your own confidence is at or below 0.40;
- the target could not be joined to a record, a range or any source ID;
- there was no recorded system decision to disagree with;
- you passed `--no-training`.

### With auto mode

The feedback stages are **opt-in**, and the only ones in the pipeline that are:

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --feedback
```

Every other pass produces a file; this one starts a review a person is then
expected to finish, and creating one nobody asked for would leave a trail of
abandoned sessions. So without `--feedback` the three stages are skipped, and
the run report tells you how to start a review instead of starting one:

```
------------------------------------------------------------------------------
WORTH A HUMAN LOOK
------------------------------------------------------------------------------
  34 decision(s) in this run are worth reviewing:
      12  a viewer would notice if it were wrong
       9  the system was not sure
       5  decided automatically
       3  hook, peak or ending
       2  flagged as a place the episode may sag
  No review has been started for this run.
    start: python -m editing.cli feedback start --run 20260821T1827-...
  Queue          : python -m editing.cli feedback queue --run 20260821T1827-...
  Feedback lands : data/editing/auto/runs/<run_id>/artifacts/feedback/sessions
  Collects human review only. Nothing in this system trains on it, and nothing
  reads the preferences it produces.
```

The count is an estimate computed from artifacts already on disk; it creates
nothing. All three stages are non-critical: a review that falls over costs the
review and not the run. And `feedback_start` is idempotent rather than
resumable — a resumed run adds to the existing review instead of splitting one
review across two logs, which the append-only rule would make unmergeable.

### Limitations

1. **Nothing consumes any of this yet.** The preference signals and training
   signals are built, tested and exported, and Session 10 is what reads them.
2. **The queue's priorities are opinions.** The reserved-slot counts, the
   ~15% positive sample, the 0.50 uncertainty line and the flag boosts are
   calibrated against intuition, not against anyone's actual review behaviour.
3. **A preference signal is one person's.** There is no notion of multiple
   reviewers, and the scope field only distinguishes one session from several.
4. **Preference rules are keyword-assisted.** Distinguishing "the danger caption
   was bad" from "captions are bad" reads the item's label for words like
   `danger_text` and `whoosh`. The general rules are structural; the specific
   ones are heuristics and are marked as such in the code.
5. **Correction text is parsed, not understood.** The action is a longest-phrase
   guess and the text is always kept beside it, so a wrong guess loses nothing —
   but grouping by action will be imperfect.
6. **No undo.** By design. A mistaken rating is corrected by rating again, and
   both stay in the log.
7. **CSV export is lossy** and says so in its manifest: one row per rating,
   corrections flattened to two columns, signals omitted entirely.
8. **The whole-edit question is not much of a question.** "Would you publish
   this?" collects a verdict that cannot be reconstructed from the parts, and
   one bit of information.

---

## 17. Watching it: the FFmpeg proxy render

Everything above this line produces *plans*. Until now the only way to see one
was to open Premiere and execute against it, which is the slowest and riskiest
loop in the system: minutes of setup, a host application, and an edit you then
have to undo.

This is the short loop.

```
change a rule → build the plan → render → watch → change a rule
```

A rough cut already carries exact source ranges, their order and their speeds.
FFmpeg can turn that into a watchable MP4 in about as long as it takes to make
a coffee, with no Premiere, no GPU and no model.

```bash
python -m editing.cli roughcut build
python -m editing.cli render roughcut
python -m editing.cli render open
```

**This is not a delivery render.** It is scaled down, encoded fast, and meant
to be watched once and thrown away. The question it answers is "does this cut
work", not "is this ready to upload".

### What you get

```
data/editing/render/jobs/<job_id>/
├── render.mp4            the proxy — watch this
├── review_notes.md       timestamped sections to write on while watching
├── report.md             what was produced, and what could not be
├── result.json           the machine-readable result
├── segments.json         every source range, in play order
├── ffmpeg_commands.json  every invocation, in order, as it ran
├── config.json           the settings this used
├── logs/ffmpeg.log       FFmpeg's own output
└── temp/                 per-clip intermediates (deleted on success)
```

### Recommended settings

`proxy` is the default and is what the loop is built around: 720p, CRF 28,
`veryfast`. On a laptop CPU that renders a ten-minute cut in about a minute and
looks fine on a second monitor.

| Quality | CRF / preset | Use it for |
|---|---|---|
| `draft` | 32 / `ultrafast` | "did the selection change do anything" |
| `proxy` | 28 / `veryfast` | **the default** — judging pacing and cuts |
| `preview` | 23 / `fast` | showing somebody else |
| `high` | 18 / `medium` | reading on-screen text in the proxy itself |

```bash
python -m editing.cli render roughcut --quality proxy --height 720
python -m editing.cli render roughcut --quality draft --height 480
python -m editing.cli render roughcut --max-seconds 90     # just the opening
python -m editing.cli render roughcut --encoder h264_nvenc # if your build has it
```

`--max-seconds` is the fastest possible look at whether an opening works: it
renders the first N seconds of the *cut*, trimming the clip that straddles the
boundary rather than dropping it.

### How it renders, and why that way

Two strategies exist. One filtergraph with every source as an input, or encode
each segment and join them. **This does the second**, and the reason is game
capture: a folder of recordings routinely mixes 1080p60 with 1440p60, files
with and without a microphone track, and clips whose audio starts a few
hundred milliseconds after the video. The one-filtergraph version of that fails
with a message about stream layouts, forty seconds into a decode, with nothing
usable on disk. The per-segment version normalises every clip to identical
streams first, so the join is a stream copy that cannot fail on mismatch — and
a failure names the clip that caused it.

It is slower. It also finishes.

Three details are load-bearing:

- **`-ss` and `-t` go before `-i`.** A 30-second segment from the middle of a
  40-minute file then costs 30 seconds of decoding rather than twenty minutes.
- **Every segment gets an audio stream, even silent ones.** A clip with no
  microphone track gets `anullsrc`. Without this the concat demuxer refuses the
  join outright, which is the most common way a naive version of this fails.
- **`aresample=async=1:first_pts=0` on every audio chain.** Capture software
  frequently starts audio slightly after video; without this the offset
  accumulates across a hundred segments into visible desync.

Speed changes use `setpts` for video and a chained `atempo` for audio — 4x is
`atempo=2.0,atempo=2.0`, because one `atempo` only covers 0.5x–2.0x. Anything
outside 0.1x–8x is rendered at 1x with a warning rather than clamped: clamping
a 20x timelapse to 8x looks like a bug in the *cut*.

The finished file is probed and compared against what the plan predicted.
"The encoder exited 0" and "the video is the length the cut says" are different
claims, and only the second is worth putting in a report.

### What is *not* in the video

A rough cut by §14 carries captions, cards, markers, sound effects, music and
graphics. A flat proxy has one video stream and one audio stream, so none of
that is in it. The renderer lists what it could not show and renders the cut
anyway:

```
NOT IN THIS VIDEO
  - 12 x text and captions
  - 4 x ducking under speech
  - 3 x clips on overlay tracks (SFX, music, B-roll)
  - 18 sequence marker(s) -- the review notes beside this video carry the same
    information in a form you can read while watching.
```

That list is not a gap to close later. A proxy exists to answer "does this cut
work", and it answers it in exactly the terms the rough cut decided: these
ranges, in this order, at these speeds.

### The review notes

`review_notes.md` lands beside the video with a section per clip, timestamped
to match it exactly — the timecodes are computed from the same segment list the
render was built from.

```md
## 00:18-00:42  (24.0s)

- Source: `ep12_part1.mp4` 412.0-436.0s @ 2x
- Kept because: filler
- keep / cut / shorten / extend:
- Notes:
```

Each section already says where the clip came from and why the system kept it,
so "why is this here" never means opening another file. The shorthand at the
top — `good moment`, `too slow`, `cut grind`, `strong payoff`, `wrong hook` —
is the same vocabulary the §16 feedback collector reasons about, so notes
written here type straight back in as ratings.

`--notes-interval 30` writes fixed 30-second sections instead of one per clip,
which suits a long cut made of few, long takes. `render notes` rewrites the
file blank without re-encoding anything.

### Reuse

Renders are cached, and the cache is the job folder: the job ID is derived from
the cache key, so re-rendering the same cut with the same settings lands in the
same folder and is handed straight back.

The key is four things, and every one of them has produced a wrong render in
somebody's pipeline:

- **the cut** — ranges, order and speeds (markers, operations and the dry-run
  state are deliberately excluded, so re-running the style pass does not cost
  an hour of re-encoding)
- **the sources** — content hashes, so a re-exported clip misses correctly
- **the settings** — everything that changes a pixel; timeouts and the notes
  interval are excluded
- **the FFmpeg build** — a new version can legitimately produce a different
  file from identical inputs

```bash
python -m editing.cli render roughcut            # reuses if nothing changed
python -m editing.cli render roughcut --force    # re-encodes anyway
```

A job whose `render.mp4` you deleted to free space misses rather than handing
back a path to nothing.

### Disk

The intermediates are the whole render again — a ten-minute proxy leaves about
the same in `temp/`. They are deleted after a successful join, kept after a
failure (the question that follows a failed join is always "which clip is
wrong"), and kept always with `--keep-temp`.

```bash
python -m editing.cli render list
python -m editing.cli render status                   # is FFmpeg here, what is on disk
python -m editing.cli render clean --temp-only --yes  # keep the videos
python -m editing.cli render clean --keep-latest 3 --yes
```

### In auto mode

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --transcribe \
    --style cinematic_minecraft --render-proxy --no-premiere
```

That produces a watchable video without Premiere ever being opened. The stage
is opt-in — it is the only one that costs minutes of CPU and hundreds of
megabytes — and non-critical: a machine with no FFmpeg still produces every
plan, and the run report says the render was blocked and why.

The run report gains a `WATCH IT` section with the video, the notes, what could
not be shown, and the exact command to open it:

```
WATCH IT
  video   : ...\artifacts\render\jobs\structure-67a45726\render.mp4
  notes   : ...\artifacts\render\jobs\structure-67a45726\review_notes.md
  3 clip(s), 47s, 1.94 MB
  3 planned feature(s) are not in the video; render show has the list.
  open    : python -m editing.cli render open structure-67a45726 --run <run_id>
```

Each run is hermetic, so its render lives in that run's artifacts — which is
why every `render` command takes `--run`, exactly as `feedback` does. Reaching
the stage again on a resume costs a cache lookup rather than a re-encode.

### How this differs from executing into Premiere

|  | `render roughcut` | `roughcut execute --yes` |
|---|---|---|
| Needs Premiere | no | yes |
| Changes anything outside `data/editing/` | no | yes, it builds a sequence |
| Shows captions, SFX, music, graphics | no | yes, once the later passes run |
| Undo | delete the folder | delete the sequence and its tracks |
| Time to see the cut | ~1 min for 10 min of video | minutes, plus setup |
| What it is for | judging the cut | producing the edit |

They are not alternatives. The proxy is how you decide whether a cut is worth
executing; executing is how you get an edit you can finish.

### Limitations

1. **V1 only.** Overlay tracks, B-roll and picture-in-picture are not
   rendered, because a flat proxy has one video stream.
2. **Every cut is a hard cut.** No transitions, dissolves or fades.
3. **Speed changes are `setpts`/`atempo`, not Premiere's retime.** Expect the
   timing to be close, not identical — the report prints the drift.
4. **No captions or graphics.** They are planned by §12 and §14 and are not in
   this video. Burned-in captions are a later session.
5. **Nothing measures loudness.** Levels are whatever the sources had.
6. **The render is not colour-managed.** A proxy of HDR capture will look
   wrong, and that is a property of the proxy, not of the cut.
7. **A cut whose sources have moved cannot render**, and says which files it
   could not find. Rebuilding the rough cut after re-running discovery is the
   fix.
8. **Hardware encoders are used only when named.** `--encoder h264_nvenc` falls
   back to libx264 with a warning if the FFmpeg build lacks it.

---

## 18. The director pass

Everything above chooses footage the same way: **locally**. `usefulness >=
0.40`, dead air goes, danger stays, a spike is interesting. Every one of those
judgements is made by looking at eight seconds and nothing else, and no amount
of tuning fixes what that cannot see:

- that a dull stretch at 04:12 is the setup for the thing at 31:40
- that the episode opens on walking
- that the same joke has now landed three times
- that a question was asked at 06:00 and never answered
- that the objective was never actually stated
- that the best moment is nine minutes in and nothing before it earns the wait

A director reads the whole episode and *then* decides. This pass is that.

```bash
python -m editing.cli director plan --style cinematic_minecraft
python -m editing.cli director show-decisions
python -m editing.cli director show-rejected
python -m editing.cli director render --quality proxy
```

### The rule that makes it safe

**The model proposes; deterministic rules dispose.**

A `DirectorDecision` arrives with `accepted = False` and stays that way unless
twelve checks say otherwise. The model never touches a timeline, never emits an
operation, and never supplies a timestamp.

Two structural guarantees do most of the work:

**A decision names segment ids, not times.** Every decision cites ids from the
brief it was given, and the times come from the timeline those ids resolve to.
A model that invents `seg_9999` produces a decision that resolves to nothing
and is rejected with a reason — rather than a cut of footage that does not
exist. There is exactly one place a number from the model becomes a time
(`shorten`), and it is clamped inside the segments the decision named.

**A decision cannot be its own justification.** A decision with no reason
becomes a note for a person rather than an edit, and a decision whose premise
the plan does not confirm — cutting a payoff that the episode memory does not
record — cannot act on it. That is Session 4's rule, applied to a different
model.

### What it decides

| Action | Means | Becomes |
|---|---|---|
| `keep` | use this range | a clip at 1x |
| `cut` | do not use it | nothing |
| `shorten` | use part of it | a clip over the sub-range |
| `speed_up` | use it retimed | a clip at 2x (never over speech) |
| `hold` | use it, protected | a clip nothing may retime or effect |
| `hook` | use it, **first** | the opening clip, whatever its source time |
| `setup` | keep, something later needs it | a protected clip |
| `payoff` | keep, this is what it built to | a protected clip |
| `callback` | keep, it calls back | a clip, marked |
| `marker_only` | change no frame | a note |
| `needs_human_review` | it is not sure | a note, and a flag |

Each decision carries a **reason from a closed vocabulary** — `setup_payoff`,
`boring_repetition`, `comedy_timing`, `hook_strength`, `confusion_risk`,
`objective_clarity` and eleven others — so fifty decisions can be counted, and
the model's own sentence, which is the part a person reads.

### The twelve checks

Run in a fixed order: **validity, then premise, then conflict, then ceilings.**

| Check | Protects against |
|---|---|
| `resolvable` | a decision about footage that does not exist |
| `valid_range` | reversed, empty or out-of-bounds ranges |
| `confidence` | a low-confidence guess changing a frame |
| `evidence` | a decision that cites nothing |
| `speech_speed` | sped-up dialogue, which is unusable |
| `protected_payoff` | cutting or retiming what the episode built to |
| `required_setup` | cutting the setup for a payoff that stays in |
| `overlap` | two kept ranges over the same footage |
| `hook_ceiling` | three hooks, which is not a hook |
| `callback_ceiling` | a running joke run into the ground |
| `grind_budget` | forty minutes of tunnelling at 2x |
| `duration` | a cut over its runtime cap |

`required_setup` is the one that most justifies the whole layer. The setup
looks like nothing; the only reason to keep it sits twenty minutes later; no
local heuristic can see it.

**Nothing is deleted.** A rejected decision stays in the plan with the check
that refused it and why — the same rule Session 2's safety pass follows.

```bash
python -m editing.cli director show-rejected
```

```
d_9f2a1c4b  CUT
  range     : 251.0-274.0s
  segments  : s_0031, s_0032
  reason    : [pacing] three minutes of walking back to base with nothing said
  REFUSED   : cuts the setup (set_0007) for a payoff that stays in the cut
              (pay_0004); the payoff would arrive from nowhere
```

### Your style guide

The director reads a **prose** style guide. Prose, because "I hold two beats
after deaths" is a rule this system has no field for and could not have
guessed, and writing it costs you nothing.

```bash
python -m editing.cli director show-style
python -m editing.cli director plan --style-guide docs/my_editing_style.md
```

A guide is found, in order: `--style-guide`, then `EDITING_STYLE_GUIDE`, then
`docs/editing_style.md` beside the project, then the built-in one. The built-in
guide is deliberately opinionated rather than neutral — a guide saying "make
good choices" tells a model nothing, and disagreeing with a specific rule is
how you discover you want to write your own.

```markdown
I hold two beats after deaths. I cut grind to under 20 seconds. I never open
on walking. I like clean Minecraft HUD visibility. I prefer story over spammy
captions.
```

The model is asked to quote the line it is following, and the report shows
which lines were actually cited — which is how you find out whether your guide
is being used at all.

**The rules do not parse it.** A rule this system cannot check is a rule it
must not claim to enforce, so the guide changes which decisions get *proposed*
and never what the safety layer allows.

### The model

Any OpenAI-compatible endpoint. vLLM, LM Studio, llama.cpp's server, SGLang,
OpenRouter, Together, Groq, OpenAI — "which provider" is a base URL and a model
name, and nothing in this package names a vendor.

```bash
export EDITING_DIRECTOR_BASE_URL=http://localhost:8000/v1
export EDITING_DIRECTOR_MODEL=qwen2.5-14b-instruct
export EDITING_DIRECTOR_API_KEY=...        # only if the endpoint needs one

python -m editing.cli director status
python -m editing.cli director plan --backend mock   # no model at all
```

The settings are deliberately separate from the vision model's: the two jobs
want different models, and a machine serving Qwen3-VL on `:8000` may well want
something else here.

**The mock backend decides by four fixed rules** and stamps `mock` on the plan,
the report, the comparison and the auto stage summary. It is for exercising the
pipeline, never for an edit — a mock plan that read as a real one would be the
worst artifact in this system, because every decision in it would look
considered.

### The brief

A 40-minute episode is ~300 segments, 8000 words of transcript, a hundred
visual events and a whole episode memory. Handing that over raw costs a fortune
and makes the decisions *worse* — a model given three hundred near-identical
mining segments writes three hundred shallow judgements.

So the context is compacted, in this order:

1. **Merge before you drop.** Adjacent segments that would get the same verdict
   become one candidate. Biggest reduction, loses nothing.
2. **Summarise speech, never invent it.** Lines are trimmed head-and-tail to a
   budget (the punchline is usually at the end) and never paraphrased.
3. **Keep the story layer whole.** Beats, open loops, setups, payoffs and risks
   are small and are the entire reason this beats a threshold.
4. **Say what was left out.** Every reduction is listed on the plan.

```bash
python -m editing.cli director build-context               # calls no model
python -m editing.cli director build-context --show-prompt # the whole thing
```

The brief also carries **what the rule-based pass already thinks**, per range,
so the model can disagree with the existing system rather than start from
nothing — and so the comparison below can count how often it did.

### Three modes

| Mode | Ranges come from |
|---|---|
| `heuristic` | the thresholds, unchanged. The fallback, always |
| `director` | only what the director asked for and the rules accepted |
| `hybrid` | those, plus the thresholds for everything it did not mention |

**Hybrid is the default in auto mode.** A director given 160 candidates makes
forty decisions, not 160 — the prompt says it does not need one per range. In
`director` mode the other 120 are simply absent, which is a short, choppy cut.
In `hybrid` they fall through to the rule the system has always used, and the
director overrides wherever it spoke. Footage the director explicitly **cut**
is never re-added.

Everything after selection is identical in all three: the same layout
arithmetic, the same operations, the same dry run, the same execution guards. A
director cut is a rough cut whose ranges came from somewhere else — not a
second path into Premiere.

### Is it doing anything?

The only question worth asking about a creative layer. A director that agrees
with `usefulness >= 0.40` everywhere is an expensive threshold.

```bash
python -m editing.cli director compare-heuristic
```

```
                              director     heuristic
  ranges kept                       23            31
  cut runtime (s)                  512           874
  agreement on footage : 61%
  only in the director cut : 84s
  only in the heuristic cut: 446s

DECISIONS NO THRESHOLD COULD MAKE (7)
    251-274      setup        setup_payoff
          Looks like nothing, but it is where the diamonds go in the chest
          that gets blown up at the end.
          style: "Protect setups."
```

The last section is the interesting one: a decision citing a setup/payoff link,
an open loop, a callback or the style guide rests on something the heuristic
has no access to. Whatever else is true, that decision is new information.

**Nothing here says which cut is better.** There is no metric for that, and
inventing one would be the mistake Session 8 refused to make about retention.
Render both and watch them:

```bash
python -m editing.cli roughcut build           # the threshold cut
python -m editing.cli render roughcut
python -m editing.cli director render          # the director cut
```

### In auto mode

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --transcribe \
    --director --render-proxy --style cinematic_minecraft --no-premiere
```

That transcribes the footage, reads the episode, has a model choose the cut,
checks every decision, builds the rough cut from what survived, and renders a
watchable proxy — without Premiere being opened.

The stage is opt-in (it needs a model endpoint) and non-critical: an
unreachable model blocks the stage, the rough cut falls back to the thresholds,
and the run report says which selector actually chose the cut.

```
WHO CHOSE THIS CUT
  A director pass ran with qwen2.5-14b-instruct (openai).
  47 decision(s): 39 accepted, 6 rejected by the rules, 2 modified.
  Style guide: my_editing_style.  Ranges chosen by: hybrid.
  why each : python -m editing.cli director show-rejected --run <run_id>
  compare  : python -m editing.cli director compare-heuristic --run <run_id>
```

### Limitations

1. **The model has not seen a frame or heard a second.** It reads a written
   description built by the other passes. An error there is an error it
   inherits.
2. **Decisions are checked for structure, not for taste.** No rule can tell a
   good creative call from a confident bad one. That is what
   `show-rejected`, the comparison and rendering both cuts are for.
3. **The style guide is read, not enforced.** The rules cannot parse prose and
   do not pretend to.
4. **No transcript means no director cut.** With only picture to go on, every
   decision has one channel of evidence, caps at 0.45, and cannot reach the
   0.55 needed to change a frame. The plan says so rather than leaving twelve
   "confidence too low" rejections to be pieced together. Transcribe first.
5. **Nothing measures retention.** No figure in a director plan is a
   prediction of anything.
6. **Quality depends on the model.** A 7B model produces decisions that read
   like a threshold. This has been verified against the API contract and
   against fixtures; it has **not** been run against a large model on a real
   episode.
7. **One pass, not a conversation.** The director does not see the render and
   revise. That is a later session.
8. **Cost is per episode, not per range**, and the brief for a 40-minute
   episode is roughly 15–25k tokens.

---

## 19. Retention structure wiring

Session 8 built the retention planner: hook candidates, risk zones, setups and
payoffs, open loops, a peak, an ending. It executed nothing, and for four
sessions nothing read it. The handoff said so, every time.

This is the consumer. It reshapes a cut around what that planner found:

```bash
python -m editing.cli retention plan                  # decide, change nothing
python -m editing.cli retention show-cold-open
python -m editing.cli retention plan --mode retention  # and apply it
python -m editing.cli retention compare
python -m editing.cli retention render --quality proxy
```

Four things happen, in this order:

1. **Protect** — a setup whose payoff is in the cut is claimed, along with the
   payoff, the peak and anything a callback refers to.
2. **Cold open** — the strongest hook moves to the front, and the footage it
   came from does not play twice.
3. **Compress** — stretches the planner called sagging are sped up when the
   picture is changing and cut when it is not.
4. **Dead air** — silence that is doing nothing is trimmed hard; silence that
   is doing something is left alone.

**The order is the safety model.** Protection runs before anything that removes
footage, so a compression pass literally cannot take a setup out — the claim
already exists when the question is asked. Reversing those two steps would work
most of the time, which is worse than not working at all.

### Report-only is the default

```bash
python -m editing.cli retention plan          # mode: report_only
```

Every decision is made, recorded and reported, and **no frame changes**. That
is the setting to look at first, because this pass reshapes an episode and you
should read what it wants to do before letting it.

| Mode | What it does |
|---|---|
| `off` | nothing, not even deciding |
| `report_only` | decides everything, changes nothing (**default**) |
| `retention` | applies on the rule-based cut |
| `director_retention` | applies on the director's cut; fails clearly without one |
| `hybrid` | applies on the director's cut if there is one, else the heuristic |

`director_retention` and `hybrid` differ only in what happens when the director
pass is missing. One says "I asked for the director's cut and did not get it";
the other says "use whatever is best available". Both are reasonable to want,
and guessing would leave somebody with a threshold cut they believed was
directed.

**The cut it reads is never touched.** The result is a variant, written to
`retention/<name>.roughcut.json`, so disagreeing with this pass costs nothing.

### The cold open

The highest-leverage edit in the system, and one a chronological clip cutter
can never make: the best thing in the episode is usually nine minutes in, and a
viewer who leaves at fifteen seconds never sees it.

Every rule below can veto, and each refusal is recorded with its reason:

- **5–20 seconds.** Long enough to land, short enough to be a tease.
- **Not boring.** A hook over walking, sorting or a menu is refused however
  well it scored — unless the footage is *also* labelled danger, a reveal or a
  payoff, because a creeper explosion is a cold open even if the model
  mentioned walking.
- **Stands on its own.** Something has to be said over it, or the footage has
  to carry a strong label. A viewer arriving cold has the picture and the
  commentary and nothing else.
- **Not already the opening.** Moving the first ten seconds to the front is a
  no-op with extra steps.
- **Not the ending.** The last tenth of the episode is a spoiler, not a tease.
- **Goes somewhere.** A hook opening a question the episode never answers is
  used and warned about loudly — it is a promise to a viewer the video does not
  keep.

```bash
python -m editing.cli retention show-cold-open
```

```
  chosen   : danger [hook_00a4]  score 0.96, confidence 0.80
  was at   : 140.0-170.0s
  now      : the first 12.0s of the cut
  question : is that a creeper behind him?
  answered : 202.0s
  original : shorten, shortened to 12s
```

**Duplication.** A cold open lifted from minute nine is the same footage as
minute nine. Left in both places it plays twice, which reads as a teaser when a
channel does it deliberately and as a bug when it does not. So the original is
removed by default; `--duplicate-policy shorten` trims it instead, and
`--allow-duplicates` keeps both as a deliberate teaser.

Two cases override the policy and say so: a hook that **is** the peak, and a
hook sitting on **protected** footage. Removing either would take the payoff
out of the episode to put it at the front, so the original is shortened
instead.

This is also the one place the layer trims something protection claimed —
carving the teased seconds out of where they used to be. The justification is
narrow and worth stating: the footage is not being removed from the episode, it
has *moved*.

### Sag compression

Session 8 marks thirteen kinds of risk. Seven describe footage that is too long
for what it contains, and those get compressed. The other six describe problems
compression cannot fix — a confusing transition does not get better by being
shorter — and become markers.

**Cut, or speed up?** Decided by what the footage is *for*:

| The footage | What happens | Why |
|---|---|---|
| somebody is talking | neither, marker only | sped-up dialogue is unusable |
| the picture is changing (a tunnel getting longer, a build going up) | **speed up** | a viewer needs to see it happened, not watch it happen |
| nothing changes | **cut** | there is nothing to see, so nothing to preserve |

Every compressed stretch keeps `--keep-context 2` seconds at each end, so the
cut does not read as a missing scene. And `--max-compression 0.5` caps how much
of the cut this pass may remove in total: a retention pass that removes 80% of
an episode has not compressed a sag, it has deleted the video.

```bash
python -m editing.cli retention show-compression
```

### Setup and payoff protection

The check that most justifies this layer, because no local heuristic can see
it: the setup looks like nothing, and the only reason to keep it sits twenty
minutes later.

- a setup whose payoff is **in the cut** is protected
- a setup whose payoff was **cut** is not — it is footage with no destination,
  and saying so is more useful than defending it
- a payoff is kept whole and never retimed; so is the peak
- a callback protects the moment it calls back to

Two warnings come out of it, and neither is automatically fixable:

```
! A payoff at 202s is in the cut and its setup is not. A viewer reaches the
  moment without knowing why it matters.
! A setup at 41s is in the cut and never pays off. A viewer is left waiting
  for something that does not come.
```

Both are exactly the sort of thing a person watching the proxy will feel and
not be able to name.

```bash
python -m editing.cli retention show-protected
```

### Dead air

The general selector already drops dead air: Session 3 refuses any segment
where measured silence covers most of it. That is conservative and leaves a lot
behind — 1.5 seconds between two sentences is not "most of a segment", and
forty of those across an episode is a minute of a viewer waiting.

This pass is harder, and the whole difficulty is that **some of that silence is
the edit**. The beat after a death is the joke. The pause before a reveal is
the tension. So every stretch is asked what it is for, from evidence the
earlier passes recorded:

| Context | Purpose | Kept up to |
|---|---|---|
| follows a scream, laughter or a reaction | `aftermath` | `--max-purposeful-silence` (2.5s) |
| sits around a payoff, reveal or danger | `tension` | 2.5s |
| bridges two different places | `transition` | 2.5s |
| inside protected footage | `setup_payoff_timing` | 2.5s |
| nothing | *ordinary* | 0.6s / 1.2s / 2.0s |

```bash
python -m editing.cli retention plan --dead-air-aggressiveness high
```

| Setting | Ordinary silence cut past |
|---|---|
| `low` | 2.0s |
| `medium` | 1.2s (default) |
| `high` | 0.6s |

Silence is **trimmed to the limit**, not deleted — the first second of a pause
is usually the reaction to whatever just happened. And nothing is ever trimmed
into speech: a gap with talking across it is speech pacing, not dead air, and
clipping it is how an edit starts sounding wrong.

### Comparing

```bash
python -m editing.cli retention compare
```

```
                                before       after
  ranges                            31          27
  runtime (s)                    874.0       690.5

WHAT CHANGED
  seconds removed          : 152
  seconds sped up          : 88
  risk zones compressed    : 4
  risk zones marked only   : 2
  silence trimmed          : 19
  silence kept on purpose  : 6
  setups protected         : 3
  actions refused          : 5
```

**There is no score.** No grade, no percentage, nothing that could be read as
an audience prediction. "Risk zones compressed: 4" is a count of what changed
in the edit; "retention improved 12%" would be a fabrication, because nothing
in this system has ever seen a viewer.

The comparison also checks the **finished cut** for duplicated footage rather
than trusting the cold-open policy, and lists every refused action with the
rule that refused it.

### In auto mode

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 --transcribe \
    --director --retention-cut --retention-mode retention \
    --render-proxy --style cinematic_minecraft --no-premiere
```

The stage is opt-in and non-critical: a run with no usable retention plan still
produces every other plan, and the cut it would have reshaped is untouched.
When the wiring does apply, `render_proxy` renders **the reshaped cut** — a
report saying a cold open was chosen next to a video that opens on walking
would be the worst possible outcome.

```
RESHAPED FOR RETENTION
  Applied on the director cut in retention mode: 874s -> 690s.
  Opens on a danger (12s) lifted from later in the episode.
  4 risk zone(s) compressed, 152s removed, 19 stretch(es) of silence trimmed.
  3 setup(s) and 2 payoff(s) protected; 5 action(s) refused.
  why each : python -m editing.cli retention show-rejected --run <run_id>
```

Flags: `--retention-cut`, `--retention-mode`, `--no-cold-open`,
`--max-cold-open-seconds`, `--dead-air-aggressiveness`.

### Limitations

1. **Nothing here measures retention.** Every count is a count of what changed
   in the edit. No number in any output is a prediction about an audience.
2. **The findings it acts on are Session 8's**, whose thresholds are calibrated
   against intuition rather than against outcomes. An error there is an error
   this layer inherits and amplifies, because now it cuts on it.
3. **A cold open is chosen by a scoring formula.** It has no idea what your
   channel usually opens on. Write a style guide and use the director pass if
   you want taste in the decision.
4. **Silence is judged by what surrounds it.** A pause that is funny for a
   reason the footage does not show will read as dead air and be cut.
5. **Compression can still confuse.** Two seconds of context at each end is a
   rule of thumb, not an understanding of the scene.
6. **A `roughcut` memory needs a rough cut.** Resolving sequence times against
   the synthetic timeline ordering would place every finding wrongly, so the
   pass refuses rather than guessing.
7. **Nobody has watched a retention cut yet.** It produces the right shape on
   fixtures and on generated footage. Whether an episode reshaped this way is
   actually better is a question only a person watching both proxies can
   answer.

---

## 20. Key-moment captions

Session 5's caption layer answers "which spoken lines *could* carry text". This
pass answers a narrower question: **which lines are the episode**. It is the
difference between a styled edit and subtitles, and it is off by default.

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 \
    --captions key_moments --no-premiere
```

### Three modes

| `--captions` | What it does |
|---|---|
| `off` | Nothing. The default: putting text on your video is not a default |
| `key_moments` | Only the nine moments below, inside a small per-minute budget |
| `dense` | Every line the style's own caption rules would allow — close to subtitles, and every plan it produces says so |

### The nine moments

A line has to be one of these, argued for by the picture, the audio and the
words **together**. "oh my god" over a crafting table is not a reaction caption.

| Moment | What earns it |
|---|---|
| `funny_reaction` | Laughter around a short line, or a reaction over something absurd |
| `death_or_fail` | A death screen on screen, or the line that admits one |
| `objective` | The line that states what the episode is trying to do |
| `reveal` | A reveal in the picture and a line pointing at it |
| `payoff_line` | A payoff landing and the line that names it |
| `callback` | A line referring back, where the episode memory recorded a callback |
| `danger` | A named threat, at the moment it is on screen |
| `meme_quote` | Short, said with force, over a moment that lands |
| `transition_setup` | The hinge between two sections |

### Three gates, in order

**1 — Is it legible?** Long lines, filler, ASR uncertainty markers and
low-confidence speech are refused before anything looks at what they mean. A
caption that misquotes the audio is worse than no caption, because a viewer can
hear the difference.

**2 — Is it a key moment?** One of the nine above. In `key_moments` mode, a
line that is none of them is refused as `not_a_key_moment` however clearly it
was said.

**3 — Does it fit the budget?** Candidates are ranked strongest-first and the
per-minute ceiling is applied. Structural moments (the objective, a reveal, a
payoff, a callback, a transition) beat purely reactive ones on a tie: a viewer
who loses the objective line loses the plot, and one who loses an "oh my god"
loses nothing.

### What is refused, and why

Every refusal is kept in the plan with the rule that made it. That is the whole
point — a pass that placed four captions out of sixty candidates and a pass
that is broken both print "4".

```bash
python -m editing.cli polish show-rejected --run <run_id>
```

```
7 line(s) were refused a caption:

  -    5.50  -             "...we go that is what..."   duplicate_line: already captioned
  -   13.00  objective     "right so today we are..."   density_limit: the budget for this
                                                        cut is 1 caption(s) (1.5 a minute
                                                        over 40s), and stronger moments
                                                        filled it

  by reason:
       4  not_a_key_moment
       2  duplicate_line
       1  density_limit
```

| Refusal | Means |
|---|---|
| `not_a_key_moment` | Nothing made it one of the nine |
| `boring_explanation` | It explains rather than lands |
| `too_long` | The line runs over eight seconds: a paragraph |
| `too_many_words` | Over 22 words. Condensing that to five picks a phrase and calls it a sentence |
| `unclear_transcript` | The transcript carries `[inaudible]` or similar |
| `low_confidence` | ASR confidence below the floor |
| `background_speech` | Quiet, uncertain speech over a low-energy stretch |
| `repeated_filler` | The whole line is "um" / "okay so" / an annotation |
| `duplicate_line` | The same text is already captioned |
| `cut_from_the_edit` | The line is not in the cut. Nudging it would caption footage nobody kept |
| `blocked_by_ui` | A full-screen menu is open there |
| `style_forbids_text` | This style puts no text on screen |
| `no_safe_zone` | No safe place on screen for it in this style |
| `density_limit` | The budget was full and stronger moments filled it |
| `too_close_to_another` | Inside the style's caption spacing |

### The numbers, and where they come from

The style preset is the source of truth wherever it has one — a style that says
five words is not overruled. `key_moments` only ever *tightens*: the rate is
capped at 1.5 a minute and the spacing floored at 6 seconds, whatever the style
allowed.

| Flag | Default |
|---|---|
| `--max-captions-per-minute` | the style's, capped at 1.5 in `key_moments` |
| `--max-caption-seconds` | the style's caption duration + 1s, 2–5s |
| `--max-caption-words` | the style's `max_caption_words` |
| `--min-caption-confidence` | `0.6` |
| `--require-caption-confidence` | off — a hand-written `.srt` carries no confidence and is usually more trustworthy than a machine one |

A rate that would round down to zero on a short cut still allows one caption,
and the plan says so rather than printing a density that looks like a broken
limit.

### Captions are not in the video

**The proxy render never burns them in.** The renderer encodes each segment and
joins them with the concat demuxer, which is what makes it survive a folder of
mismatched game capture; adding text would mean a second full re-encode of the
joined file with a subtitle filter, and that fails on exactly the fonts-and-
libass edge cases you cannot debug from here.

So the pass writes a **sidecar `.srt` beside the video**, in sequence time:

```
render/jobs/<job_id>/render.mp4
render/jobs/<job_id>/render.srt     ← open the mp4 in VLC or MPV
```

`polish/<name>.captions.srt` is the same file inside the run's artifacts, and
the review folder holds a copy as `subtitles.srt`.

---

## 21. Minimal music and SFX polish

Five things, placed rarely, and none of them plays.

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 \
    --audio-polish placeholders --no-premiere
```

| Cue | When |
|---|---|
| `riser` | Three seconds into a payoff or a reveal |
| `hit` | On a fail or a reveal, at the frame it lands |
| `whoosh` | Where the cut moves to a *different source file* — a real section change, not a trim |
| `ambience` / `music_bed` | Under the cut, when the style allows one. At most one bed, ever |
| `silence_drop` | Half a second of nothing before a reveal or a death, so it lands |

### Two rules do the work

**A cue names the moment it is for.** There is no "place one at every cut"
path. A candidate is generated by something the earlier passes recorded — a
death screen, a payoff beat, a change of source file — and a cue with nothing
behind it is refused as `no_moment` rather than placed hopefully.

**A cue may not land on a word.** Hits, risers, whooshes and silence drops are
checked against the transcript with a guard band either side. A sting over the
middle of a sentence is the single most obvious way an automated edit announces
itself. Beds are the exception and are ducked instead — which the cue says in
its own safety notes rather than in a footnote.

### Two modes

| `--audio-polish` | What it does |
|---|---|
| `off` | Nothing. The default |
| `placeholders` | Every cue is a note naming the sound that belongs there. **No library is read at all** |
| `assets` | Cues are matched against your local library (§13). Anything unmatched stays a note and lands on the shopping list |

```bash
python -m editing.cli polish show-missing --run <run_id>
```

```
2 sound(s) to go and find:

    1 x a hard one-shot sting
          kind hit, first needed at 26s
    1 x a loopable low-intensity bed
          kind music_bed, first needed at 0s
```

### Density and taste

| Style | Effects a minute | Bed allowed | Spacing |
|---|---|---|---|
| `cinematic_minecraft` | 0.8 | yes | 10s |
| `fast_funny` | 2.0 | yes | 4s |
| `documentary_story` | 0.6 | yes | 12s |
| `minimal_clean` | 0.25 | no | 30s |

Which *kinds* a style tolerates is read off the preset's own `audio_kinds` and
`forbidden_kinds`, not restated here — so `cinematic_minecraft`, which forbids
`whoosh`, forbids it in this pass too without anybody having to say it twice.

Override with `--max-sfx-per-minute`, `--no-music-bed`, `--no-ducking`.

### What it never does

* **Nothing plays.** This pass produces a plan. No operation in it is executed
  by this package, and nothing it plans is in the rendered proxy.
* **No level is measured.** Not the bed's, not the sting's, not the
  commentary's. Every accepted cue says so in its own safety notes.
* **Nothing has been listened to.** In `assets` mode a match is made on tags,
  folder and duration, exactly as §14 describes.
* **A bed is tiled, not crossfaded.** A file that does not loop cleanly has an
  audible seam, and the plan says that too.

---

## 22. Reliability checks

Fifteen checks on whether a run produced a **usable** thing. Not to be confused
with the execution gates of §0, which are about *permission* — what may be run
against Premiere. These are about *validity*.

```bash
python -m editing.cli auto show-checks --run <run_id>
```

```
overall  : warn
gates    : 11 passed, 1 warned, 0 failed, 3 not applicable
usable   : yes

  WARN     story_warnings
      what : Setups and payoffs are still paired
      why  : 2 unresolved story warning(s): a payoff without its setup, or a
             setup that never pays off.
      saw  : unresolved=2
      fix  : python -m editing.cli episode show-open-loops
```

### Four statuses

| Status | Means |
|---|---|
| `pass` | The check looked and found nothing wrong |
| `warn` | Worth knowing, and the output is still usable |
| `fail` | The output is not valid for this reason |
| `skipped` | The pass this is about did not run, so the question does not apply |

**A gate about a pass that did not run says `skipped`, never `pass`.** Fifteen
green ticks that mean nothing is worse than five that mean something.

### The fifteen

| Check | Asks |
|---|---|
| `footage` | Footage was found and probed |
| `transcript` | The episode has words in it |
| `transcript_confidence` | The transcript is confident enough to build on |
| `hook` | An opening hook was found |
| `director` | The director's decisions survived the rules |
| `retention_length` | The reshaped cut is still an episode |
| `cold_open_duplicate` | The cold open does not play twice |
| `story_warnings` | Setups and payoffs are still paired |
| `compression` | The story was not compressed away |
| `caption_density` | Captions are punctuation, not subtitles |
| `sfx_density` | Effects mark moments rather than fill them |
| `missing_assets` | Every placed sound exists |
| `render_output` | The render produced the file it claims |
| `render_size` | The rendered file is big enough to be a video |
| `output_duration` | The output runtime is plausible |

### A warning never stops a run

Only five checks can ever say "do not review this output", and each needs a
condition that is unambiguous:

* `footage` — no footage at all
* `compression` — a cut with no runtime
* `retention_length` — a reshaped cut with no runtime
* `render_output` — the run says it rendered a video and the file is not there
* `render_size` — the rendered file is too small to be video

Everything else warns with a fix attached. A pipeline that refuses to finish an
overnight run over a caption density is one nobody runs twice, and a check
somebody disables protects nothing.

### Every gate carries evidence and a fix

A gate that says "confidence is low" without saying what it was, over how many
words, is an opinion; one with no suggested fix is a complaint. Both fields are
filled by every check, and both are in `reports/checks.json`.

**These look at shape, not at taste.** A run that passes all fifteen can still
be a bad edit, and the only way to find that out is to watch it.

---

## 23. The review package

A run leaves six sub-directories and forty files behind. Knowing that the
retention comparison lives in `artifacts/retention/<name>.compare.json` is a
thing you have to learn — and this exists so you do not have to.

```bash
python -m editing.cli review open-latest       # the newest index
python -m editing.cli review summary --latest  # the short form
python -m editing.cli review package --run <id>   # rebuild it from disk now
```

Every completed run gets one, unless `--no-review-package` says otherwise:

```
data/editing/auto/runs/<run_id>/review/
├── review_index.md      ← open this
├── package.json         the same content, for a script
├── checks.json          the fifteen reliability checks
├── checks.txt           the readable version
├── captions.txt         a copy of the caption report
├── subtitles.srt        the caption sidecar
├── audio.txt            the audio polish report
├── retention.txt        the retention report
├── run_report.txt       the full run report
└── ...                  every other small report the run produced
```

Small reports are **copied in**, so the folder is self-contained. The video is
**pointed at** — copying a proxy to make a folder tidy would be an unkind thing
to do to a disk.

### The index answers five questions, in order

1. **Watch this** — the video, and the subtitle file to load beside it
2. **What changed** — counts, never claims: what was reshaped, compressed,
   protected, captioned, scored
3. **What to watch for** — the specific moments most likely to be wrong,
   starting with the cold open
4. **Weak points** — what the reliability checks found, what stages were
   blocked, and the fix each one suggested
5. **Needs you** — what only a person can settle

Then the files, then the commands.

### It is a view, not a record

`review package` rebuilds it from what is on disk **now**. Delete the proxy and
rebuild, and the index says there is no video. That is the whole point: a
package that listed a file that had been deleted would be worse than no
package.

**Nothing in it is a verdict.** It says what was done, what was refused and
what is worth checking. Whether the edit works is a question only somebody
watching it can answer, and the index says so at the top.

---

## 24. Batch mode

One configuration, many folders, one summary.

```bash
python -m editing.cli auto batch --root E:\Clips --style cinematic_minecraft \
    --director --retention-cut --render-proxy --no-premiere --limit 3
```

**Type `--dry-run` first.** It creates nothing and reports exactly which
folders it would process, which it would skip and why.

### What counts as a folder

A folder that **directly contains video files**. Nested folders are candidates
in their own right; a parent whose clips all live one level down is not, because
running it would process the same footage twice under a different name. Folders
this system or Premiere writes to (`render/`, `data/`, `auto/`, the auto-save
folders) are never scanned.

### The four decisions

| Decision | When |
|---|---|
| `run` | A fresh folder |
| `skip` | A completed run exists, and neither `--force` nor `--resume` was given |
| `resume` | An unfinished run exists and `--resume` was given |
| `force` | A completed run exists and `--force` was given — a **new** run folder beside the old one |

### Options

| Flag | What it does |
|---|---|
| `--limit N` | Process at most N folders |
| `--only-new` | Only folders that have never been run at all |
| `--resume` | Continue unfinished runs instead of skipping them |
| `--force` | Run folders that already completed, in new run folders |
| `--dry-run` | Say what would happen and create nothing |
| `--style` | The style every run uses |
| `--director` / `--retention-cut` / `--render-proxy` | What each run does |
| `--captions` / `--audio-polish` | Polish, applied to every run |
| `--no-premiere` | Never talk to Premiere |

### Four properties

**Nothing is ever overwritten.** A completed folder is skipped; `--force` gives
it a new run beside the old one. There is no path through batch mode that
writes over finished work — including the batch's own summary, which waits for
a new second rather than reusing an ID.

**One failure does not stop the batch.** A folder that raises is recorded with
its reason and the next one starts. The most useful property of an overnight
run is that it is still going in the morning.

**Every folder ends in a named state** — `completed`, `failed`, `skipped`,
`planned` — each carrying why. A summary where something silently did not
happen would be worse than no summary.

**It is sequential.** No concurrency, no retries, no dependency graph. Two runs
at once contend for the same analysis cache and the same GPU, and the failure
mode of a parallel batch — two half-finished runs and no way to tell which log
belongs to which — is worse than the wall-clock it saves.

### The summary

```
data/editing/auto/batches/<batch_id>/
├── summary.json     rewritten after every folder
├── summary.txt      the readable version
└── batch.log        one line per folder
```

It leads with failures, then the completed runs worth looking at (a failed
stage, or a check saying the output is unusable), then the watchable ones with
their review indexes, then every folder.

```bash
python -m editing.cli auto list-batches
python -m editing.cli auto batch-report --batch <batch_id>
```

A batch creates nothing an ordinary `auto run` would not create, which is what
makes every other command in this system work unchanged on its output.

---

## 25. Running without Premiere

Premiere is optional everywhere, and has been since Session 3. There are four
export modes:

| Mode | Command | What you get |
|---|---|---|
| **Plan only** | `auto run --folder <f> --no-premiere` | Every plan, validated offline. No video, nothing executed |
| **Proxy render** | add `--render-proxy` | A watchable MP4 built with FFmpeg, plus review notes and a caption sidecar |
| **Premiere plan export** | `layers export`, `roughcut report`, `assets plan` | The JSON a Premiere session would consume |
| **Premiere execution** | `auto execute-stage <stage> --run <id> --yes` | The only path that touches Premiere, one stage at a time |

**Nothing executes by default.** `--no-premiere` shuts every gate for the life
of the run; without it, the gates are *computed* and still need an individual
`--yes` each.

The whole of Week 4 — captions, audio polish, the reliability checks, the
review package, batch mode — needs no Premiere, no GPU, no model server and no
asset library. The one thing that needs an external tool is the proxy render,
which needs FFmpeg.

### The honest gap in the Premiere path

`text.create` uses the rasterised path, so captions placed by the style pass
are not editable in Premiere afterwards, and `.mogrt` templates are matched and
never placed. **The caption polish pass does not place text in Premiere at
all** — it produces a plan and a sidecar subtitle file. Wiring it into the
style pass's operation list is a real piece of work that has not been done, and
this section says so rather than implying the two are connected.

---

## 26. The creative visual layer

Everything before this decided *what footage is in the edit*. This decides
where the edit should **point at something** — a zoom onto the creeper, a card
naming the objective, an arrow at the thing nobody would otherwise notice. It
is off by default.

```bash
python -m editing.cli auto run --folder D:/Footage/ep12 \
    --director --retention-cut --captions key_moments \
    --audio-polish placeholders --visual-layer balanced --no-premiere
```

**Nothing it plans is drawn, rendered or executed.** The Premiere operations
are proposals validated offline; the FFmpeg side is a capability statement and
a marker file. There is no code path in the package that burns an effect into a
video, and `burned_in` is `False` everywhere.

### Four rules

**Every treatment names the moment it is for.** No effect comes from a clock, a
beat grid or "every N seconds". A candidate exists because the director
accepted a decision there, the retention pass moved that footage to the front,
the caption pass found a payoff line, or the vision pass saw a creeper. No
evidence, no effect.

**Every refusal is kept** — including the moments the style never offered
anything for. "Two effects" and "twenty candidates, eighteen refused, here is
why" are different reports, and only the second one distinguishes taste from a
bug.

**The HUD is protected before anything else.** Minecraft's health, hunger and
hotbar are information the viewer is reading. No style may override the check
that keeps them on screen.

**One moment gets one gesture.** A freeze frame *with* a label is a single
effect in the library, not two stacked on the same second. The second candidate
for a moment is refused with `too_close_to_another`, and the report says so in
those words.

### Four layers

| `--visual-layer` | What it does |
|---|---|
| `off` | Nothing. The default |
| `minimal` | Cards and the occasional slow zoom. Almost nothing else |
| `balanced` | The intended setting: emphasis where the episode earns it |
| `high` | More of everything, still inside every safety rule. Not a default, and every plan says so |

The layer scales the *style's own* density rather than replacing it, so
`--visual-layer high` on `minimal_clean` is still quieter than `balanced` on
`fast_funny`. Picking a style keeps meaning something.

### Twenty moments

Detected only from what an earlier pass recorded — the vision events, the audio
events, the transcript, the director's accepted decisions, the retention cut,
the caption plan, the episode memory:

`panic` · `death_or_fail` · `danger` · `reveal` · `discovery` · `payoff` ·
`callback` · `funny_reaction` · `banter_spike` · `objective_start` ·
`objective_complete` · `confusing_transition` · `grind_montage` ·
`boring_compression` · `important_find` · `villager_chaos` · `near_death` ·
`cliffhanger` · `opening_hook` · `recap`

Two layers noticing the same death produce **one** moment carrying both pieces
of evidence — otherwise a single death earns a zoom, a freeze frame and an
arrow.

Episode-layer findings are resolved through `segment_ids` unless the memory's
own `timebase` says its numbers are sequence time. Guessing would place every
finding *somewhere*, and every number would be a number and all of them wrong.

### Thirty-four treatments

| Family | Effects |
|---|---|
| emphasis | `zoom_punch` `quick_punch_in` `slow_zoom_hold` `crop_pan` `freeze_frame` `freeze_frame_label` |
| callout | `arrow_callout` `circle_highlight` `box_highlight` `label_tag` `danger_warning_label` `objective_label` `entity_callout` |
| card | `title_card` `objective_card` `progress_card` `recap_card` `chapter_card` `setup_payoff_card` `later_card` |
| motion | `screen_shake` `impact_flash` `speed_ramp` `montage_marker` `letterbox` `dramatic_pause` |
| replay | `replay_marker` `instant_replay` |
| minecraft | `hardcore_warning` `totem_reminder` `health_emphasis` `villager_danger_meter` `progression_counter` `build_progress_card` `day_counter` `coordinates_card` |

**A card with nothing to say is not built.** Its words come from the objective
somebody stated, the entity the vision pass named, or a caption line that was
already approved. There is no path that writes copy the footage cannot support
— the same rule Session 8's hook text follows.

### What each style is for

| Style | Reaches for | Never uses |
|---|---|---|
| `cinematic_minecraft` | slow zooms, letterbox, cards, montage markers, dramatic pauses | screen shake, impact flash, freeze-frame labels, punch zooms |
| `fast_funny` | punch zooms, freeze-frame labels, impact flashes, arrows, replay markers | letterbox, recap cards, coordinate cards |
| `documentary_story` | objective cards, recap cards, chapter cards, progress cards, slow zooms | screen shake, impact flash, punch zooms, instant replay |
| `minimal_clean` | title and objective cards, slow zooms, chapter cards, montage markers | everything else |

`fast_funny` is the only style whose defaults turn meme effects on — it is the
one style that reads a freeze-frame label as the point rather than as noise.

### The fourteen safety rules

They run in a fixed order and record what they saw whether or not they acted.

| Refusal | Means |
|---|---|
| `hides_hud` | It would cover a full-screen menu, or scale the HUD off the frame |
| `unknown_target` | A callout with nothing named on screen to point at |
| `low_confidence` | The moment was not certain enough |
| `low_transcript_confidence` | The speech behind it was unclear |
| `weak_visual_label` | The vision pass named nothing, and this changes the picture |
| `clip_too_short` | The clip cannot carry it |
| `too_long` | Longer than the duration ceiling |
| `interrupts_action` | A freeze mid-fight stops the thing the viewer came for |
| `shake_during_combat` | Shaking the frame while there is something to aim at |
| `caption_overlap` | A caption is already on screen there |
| `repeated_effect` | The same effect too many times |
| `hook_already_polished` | The opening already carries a cold open, a caption and a sting |
| `too_close_to_another` | Too close to another effect, or a second gesture on one moment |
| `density_limit` | The budget was full and stronger moments filled it |
| `style_forbids` / `layer_forbids` | This style or this run does not use that effect |
| `no_evidence` | Nothing to fill the effect with |

**Lower before refusing.** A punch too strong for the moment becomes a *softer*
punch, not no punch. A card too long for the clip is shortened. Only the checks
where a softer version would still be wrong — a callout pointing at nothing, an
overlay across an open inventory — refuse outright, and every softened
treatment carries the rule that softened it.

### Reading it

```bash
python -m editing.cli visuals plan --visual-layer balanced --style fast_funny
python -m editing.cli visuals report --latest
python -m editing.cli visuals show-accepted --latest
python -m editing.cli visuals show-rejected --latest
python -m editing.cli visuals show-rejected --latest --reason hides_hud
python -m editing.cli visuals show-final --latest
python -m editing.cli visuals export-premiere-plan --latest
```

```
MOMENTS THAT GOT NOTHING (8)
  [   0.00] opening_hook         right so today we are going to find some dia
      why  : the opening already carries 2 other treatment(s) -- a caption, a
             sting, a cold open -- and a viewer meets all of them in the first
             few seconds
  [   7.50] reveal               ...
      why  : a circle_highlight needs something to point at and the vision
             pass named nothing on screen here
  [  27.50] payoff               ...
      why  : a caption ("...we go that is what...") is on screen at 25.0s, and
             a setup_payoff_card there would be a second thing to read at once
```

---

## 27. The final edit plan

The composer assembles the cut, the captions, the sound and the visuals into
one object, so there is one file to read to find out what the edit *is* rather
than five and a mental model of how they relate.

```bash
python -m editing.cli visuals show-final --latest
```

| `--visual-mode` | What it produces |
|---|---|
| `off` | Nothing |
| `plan_only` | The `FinalEditPlan` alone. The default |
| `proxy_preview` | Also the FFmpeg capability statement and a marker file |
| `premiere_plan` | Also a Premiere operation plan, validated offline |
| `hybrid` | Both |

**None of them executes anything.** `premiere_plan` produces a *plan*; running
it is a separate, explicit act behind its own `--yes`.

Each segment carries the treatment, caption and cue ids landing on it, plus
notes for the three situations worth a second look: a clip the pacing layer
asked to be left alone now carrying a treatment, a retimed clip carrying one
too, and a clip carrying more than two things at once.

```
[  25.00-  35.00] payoff           BUSY
    visuals : 2 slow_zoom_hold, label_tag
    captions: 1
    note    : 2 treatment(s) and 1 caption(s) on one clip: this is the first
              place to look if the edit feels over-worked.
```

---

## 28. The Premiere visual plan

```bash
python -m editing.cli visuals export-premiere-plan --latest
```

Every accepted treatment, as operations from the catalog in §0 — validated
offline, inspectable before anything runs, and **executed by nothing**.

| Treatment | Operation |
|---|---|
| zooms and pushes | `animate` on `Motion > Scale` with an easing curve |
| freeze frames | `clip.freeze`, plus `text.create` when it carries a label |
| callouts | `graphic.shape` (arrow, circle, rounded rect) |
| labels and cards | `text.create`, with `graphic.shape` for the plate |
| impact flash | a full-frame `graphic.shape` at high opacity |
| letterbox | two black `graphic.shape` bars |
| speed ramps | `clip.speed_ramp` with three points |
| markers | `marker.add` |

Overlays land on **V3** — above the rough cut's V1 and the style layer's V2 —
so the whole visual pass can be removed by deleting one track.

### What the catalog cannot express

Listed with the reason rather than approximated:

* **`screen_shake`** — a shake is a per-frame position wobble. `animate` moves
  a parameter from one value to another along a curve, and the catalog has no
  primitive for generating thirty small moves.
* **`instant_replay`** — replaying a range means playing footage twice, which
  is a change to the *cut* rather than an overlay on it.
* **`crop_pan`** — a pan across a scaled frame needs Position and Scale
  animated together with a known crop origin, and this system does not know
  where in the frame the subject is.

### A callout knows *what*, never *where*

The vision pass names entities; it does not localise them. Every callout
operation therefore lands at the centre of the frame with
`POSITION IS A GUESS, move it by hand` in its note, and the plan warns once for
the batch. Emitting a confident-looking coordinate would be the one dishonest
thing in the file.

---

## 29. FFmpeg preview: what it can and cannot show

**Nothing is burned in.** The proxy renderer encodes each segment and joins
them with the concat demuxer, which is what makes it survive a folder of
mismatched game capture. Overlaying anything would mean a second full re-encode
of the joined file with a filtergraph — a different strategy with its own
failure modes, and one this session does not build.

So `--visual-mode proxy_preview` produces three things:

1. a **capability statement** per treatment,
2. a **marker file** written beside the proxy as `render.visuals.md`,
3. `burned_in = False`, with no code path that sets it True.

| Verdict | Meaning | Examples |
|---|---|---|
| `burn_in` | A documented filter exists and would be clean on one segment | cards, labels, letterbox, impact flash, box highlight, freeze frames |
| `sidecar` | Representable only as a marker beside the video | zooms, speed ramps, screen shake, replay and montage markers |
| `none` | FFmpeg has no way to express it at all | arrows, circles, entity callouts, instant replay |

The filter fragments for the `burn_in` set are **recorded rather than run**, so
a later session wiring a real preview render does not have to re-derive them
and a reader can see exactly what was and was not claimed.

```
| time     | effect            | what                     | shown in the proxy         |
|----------|-------------------|--------------------------|----------------------------|
| 00:30.00 | `freeze_frame`    | FREEZE FRAME (low)       | no — could be, and is not  |
| 01:00.00 | `quick_punch_in`  | QUICK PUNCH IN (low)     | no — this line is the only sign of it |
```

### How the preview differs from a final edit

The proxy is **the cut and its original audio**. It has no captions in it, no
sound effects, no music, no graphics and no visual treatment. Watching it tells
you whether the *cut* works; it tells you nothing about whether the edit does,
and every report in this system repeats that rather than assuming you
remembered.

---

## 30. What the visual layer does not do yet

* **It has never been run against real footage with a real vision model.**
  Twenty moment kinds, thirty-four effects, fourteen safety rules and a
  scoring formula, all calibrated against fixtures and generated footage.
* **It draws nothing.** No preview render, no Premiere execution. The plan is
  intentions.
* **It does not know where anything is on screen.** Every callout is a target
  without a position.
* **It does not look at the frame before deciding.** The HUD checks read what
  the vision pass recorded, not the picture.
* **It has no memory across effects.** Each treatment is checked against the
  ones already kept and against nothing else, so six safe zooms are six safe
  zooms and possibly one too many. `visuals report` flags that under "what
  might be overdone" and stops there.
* **It cannot invent a shake, a replay or a crop pan** — see §28.
* **Nothing measures whether any of it helps.** It counts what it planned.

---

## 31. Where outputs go

Default root `data/editing/` (`--output-dir` or `EDITING_OUTPUT_DIR`):

```
data/editing/
├── assets.json                     discovered footage + Premiere mapping
├── transcripts/<asset_id>.json     normalised transcripts (durable)
├── transcripts/<job_id>/           ← one local Whisper transcription
│   ├── transcript.json  transcript.srt  transcript.txt
│   └── metadata.json    warnings.json
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
├── feedback/sessions/<session_id>/ ← human review; the one thing not derived
│   ├── session.json  queue.json
│   ├── feedback.jsonl              ← append-only, never rewritten
│   ├── summary.json  report.md     derived from the log on demand
│   └── exports/                    each with a manifest
├── retention/structure.plan.json   ← every retention decision and refusal
├── retention/structure.plan.txt    ← the readable retention report
├── retention/structure.roughcut.json ← the reshaped cut, as a variant
├── retention/structure.compare.json ← reshaped cut against the original
├── director/structure.plan.json    ← every decision, accepted and rejected
├── director/structure.plan.txt     ← the readable director report
├── director/structure.context.json ← exactly what the model was shown
├── director/structure.prompt.txt   ← exactly what it was sent
├── director/structure.compare.json ← director cut against threshold cut
├── polish/structure.captions.json  ← every line judged, accepted and refused
├── polish/structure.captions.txt   ← the readable caption report
├── polish/structure.captions.srt   ← the sidecar subtitles, in sequence time
├── polish/structure.audio.json     ← every cue judged, and what is missing
├── polish/structure.audio.txt      ← the readable audio polish report
├── visuals/structure.visuals.json  ← every moment and treatment, refusals too
├── visuals/structure.visuals.txt   ← the readable visual report
├── visuals/structure.visuals.md    ← the marker file, for watching the proxy
├── visuals/structure.final.json    ← the FinalEditPlan: cut + captions + sound
├── visuals/structure.final.txt     ← the readable final-edit report
├── visuals/structure.premiere.json ← operations Premiere could run. Not run
├── visuals/structure.compare.json  ← the visual layer against the bare cut
├── render/jobs/<job_id>/           ← one proxy render, and how it was made
│   ├── render.mp4                  ← watch this
│   ├── render.srt                  ← load this beside it: captions are not in it
│   ├── render.visuals.md           ← and this: no effect is in it either
│   ├── review_notes.md             ← write on this while you watch
│   ├── report.md   result.json     what was produced, and what was not
│   ├── segments.json               every source range, in play order
│   ├── ffmpeg_commands.json        every invocation, as it ran
│   ├── config.json   logs/         the settings, and FFmpeg's own output
│   └── temp/                       intermediates, deleted on success
├── auto/runs/<run_id>/            ← one self-contained folder per auto run
│   ├── config.json  state.json
│   ├── checkpoints/  artifacts/  logs/
│   ├── reports/                    report.json report.txt checks.json checks.txt
│   └── review/                    ← open review_index.md first
│       ├── review_index.md         the five questions, in order
│       ├── package.json            the same content, for a script
│       └── <item>.txt/.json/.srt   a copy of every small report
└── auto/batches/<batch_id>/       ← one batch over a library of folders
    ├── summary.json                rewritten after every folder
    ├── summary.txt                 the readable summary
    └── batch.log
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

## 32. Caching

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

Director answers are cached on the context fingerprint -- which covers the
footage, the analysis, the story layer *and* the style guide -- plus everything
about the model configuration that changes a word of the reply. Editing your
style guide therefore correctly misses. The cache stores the model's **raw
text**, not parsed decisions, so fixing a parser or tightening a safety check
re-applies to everything already cached instead of preserving the old verdict.

Proxy renders are cached the same way and in the same spirit, except that the
cache entry *is* the job folder — the job ID is derived from the key, so there
is no second place for the cache and the video to disagree. The key adds the
FFmpeg version to the usual three parts, because a new build can legitimately
produce a different file from identical inputs. See
[§17](#17-watching-it-the-ffmpeg-proxy-render).

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

## 33. Tests

```bash
python -m pytest tests/editing -q        # 2060 tests, ~130s
```

**No FFmpeg, no GPU, no model server and no Premiere required.** Every external
edge has a stub: `MockVisionModel` for Qwen3-VL, `StubFrameSource` for frame
extraction, `FakeBridge` for the Premiere panel, a `FakeRunner` that records
FFmpeg commands instead of running them, and a patched ffprobe. The
stubs mirror the real interfaces exactly, so a test passing against a stub is
asserting on the same call shape the real component receives.

| File | Covers |
|---|---|
| `test_editing_schema.py` | coercion, clamping, lossless round-trips |
| `test_editing_transcripts.py` | all five formats, word grouping, the store |
| `test_editing_transcribe.py` | Whisper config, **the cache key**, batch resilience, SRT to spec, missing dependencies |
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
| `test_editing_feedback.py` | the review queue, **the append-only log**, target resolution, preference and training signals, exports |
| `test_editing_retention.py` | resolving episode time to footage, cold-open vetoes, **protection before compression**, purposeful silence, the comparison's refusal to claim analytics |
| `test_editing_director.py` | context compaction, **the segment-id guarantee**, invalid JSON, the twelve safety checks, the three modes, the HTTP contract |
| `test_editing_render.py` | plan-to-segment conversion, **the FFmpeg commands**, speed chaining, the render cache, review notes, missing FFmpeg |
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
- `test_a_plan_fingerprint_survives_a_trip_through_json` — a plan built in
  memory carried ints where the same plan read back from disk carried floats,
  so the render cache missed on the one path it exists for
- `test_a_source_with_no_audio_gets_a_generated_silent_track` — the concat
  demuxer refuses to join files whose stream layouts differ, so one clip
  recorded without a microphone would otherwise break a whole render
- `test_a_re_exported_source_invalidates_the_render` — showing you a video of a
  cut you no longer have is the worst thing this package could do
- `test_the_mock_runner_completes_and_claims_no_video` — a placeholder is never
  reported as something to watch
- `test_protection_is_claimed_before_compression_runs` — the ordering the
  whole retention layer rests on, asserted from both sides
- `test_the_cold_open_footage_is_carved_out_of_where_it_used_to_be` — checked
  on the finished cut rather than trusted from the policy
- `test_a_roughcut_memory_without_a_rough_cut_is_refused` — sequence times
  read against a synthetic ordering are all wrong, and every one of them would
  still look like a number
- `test_the_setup_seconds_field_is_not_read_as_context_needed` — Session 8
  sets it to the beat's position despite the name, and reading it as
  documented refused every hook worth having
- `test_silence_after_a_reaction_is_kept_as_aftermath` — the beat after a
  scream is the joke
- `test_the_comparison_never_claims_analytics` — scanned over the whole
  rendered comparison, not spot-checked
- `test_times_come_from_the_context_not_from_the_model` — the whole
  anti-hallucination guarantee, in one assertion
- `test_cutting_the_setup_for_a_kept_payoff_is_refused` — the check that most
  justifies asking a model at all: no local heuristic can see it
- `test_a_plan_with_no_ranges_produces_a_threshold_cut_that_says_so` — a
  blocked director pass still leaves a plan behind, and keying on the object
  rather than its ranges reported a threshold cut as a directed one
- `test_speech_is_never_sped_up` — the judgement survives, the remedy does not
- `test_an_ordinary_keep_is_not_counted_as_grind` — "pacing" is the natural
  category for an ordinary keep, and counting it made the grind budget reject
  most of a normal cut
- `test_nothing_but_the_runner_shells_out` — walked over the package's AST, so
  the render strategy stays testable without FFmpeg installed
- `test_a_cue_never_lands_on_a_spoken_word` — asserted against the transcript
  rather than against the cue list, because "a sting over the middle of a
  sentence" is the single most obvious way an automated edit announces itself
- `test_covering_speech_is_a_named_refusal` — the overlap check returned the
  speech's start time as its "yes", so a line spoken at exactly 0.0s was waved
  through by a falsy sentinel
- `test_a_long_paragraph_is_refused_rather_than_condensed` — condensing thirty
  words to five picks a phrase and presents it as the sentence
- `test_what_survives_the_ceiling_is_what_scored_best` — a density limit that
  kept whatever happened to be early would be a different feature
- `test_only_a_short_list_of_checks_may_ever_block` — every check run against
  every way its subject can go wrong, asserting that the set of gates which can
  refuse an output stays the five that should
- `test_a_check_that_raises_becomes_a_skipped_gate` — one broken check must not
  cost the other fourteen
- `test_a_dry_run_creates_nothing` — asserted on the filesystem, not on the
  summary: it is the command a person types first over a library they care
  about
- `test_a_batch_continues_after_a_folder_fails` — the property that makes an
  overnight batch worth starting
- `test_rebuilding_a_package_says_what_is_true_now` — a review index that
  listed a video which had been deleted would be worse than no index
- `test_the_review_index_reports_a_settled_run_status` — "running" is true for
  about four milliseconds and misleading afterwards

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

**Nothing learns from your feedback yet.** §16 collects it, links it to the
decisions it was about, and exports it. Nothing in this system trains on it,
and nothing reads the preference signals it produces — that is the next
session's job. A preference marked "safe to apply automatically" is a statement
about how much evidence agreed, not permission anything has been given.

**The review queue's priorities are calibrated against intuition.** The
reserved-slot counts, the share kept for good decisions, and the line between
"uncertain" and "confident" are numbers somebody chose. Nobody has yet done
twenty reviews and found out whether the queue puts the right things first.

**Nobody has watched a retention cut.** The wiring produces the right shape on
fixtures and on generated footage: a cold open at the front, compressed sag,
protected setups. Whether an episode reshaped this way is actually better is a
question only a person watching both proxies can answer, and no number this
system produces will ever answer it.

**Captions are never in the video, and are not placed in Premiere either.** The
caption polish pass produces a plan and a sidecar `.srt`. Burning them into the
proxy would mean a second full re-encode of the joined file; placing them in
Premiere would mean wiring this pass into the style layer's operation list.
Neither is built.

**Nothing in the audio polish plays.** No level is measured, no file is
listened to, and no cue it plans reaches the proxy — which carries the cut and
its original audio and nothing else. In `assets` mode a match is tags, folder
and duration, exactly as §14 describes.

**The caption and cue heuristics have never been checked against a real
episode.** Nine moment kinds, four keyword lists, a scoring formula and a
per-minute budget, all calibrated against intuition and fixtures. Whether the
lines it picks are the lines you would have picked is unknown, and
`polish show-rejected` exists mostly so you can find out.

**The reliability checks look at shape, not at taste.** Passing all fifteen
says the output is well-formed. It says nothing about whether the edit works,
and the report says so on every page.

**"Background speech" is a heuristic with no diarisation behind it.** There is
no speaker separation anywhere in this system. What the caption pass can
actually see is speech the ASR was unsure of over audio measured as quiet, and
it calls that background — which is two pieces of evidence pointing the same
way, not a detection.

**Batch mode is sequential and has no retry.** Twenty folders in order. A
folder that fails is recorded and skipped past; re-running with `--resume` is a
separate decision you make after reading the summary.

**The visual layer draws nothing, and has never seen real footage.** Twenty
moment kinds, thirty-four effects, fourteen safety rules and a scoring formula,
calibrated against fixtures. Its Premiere operations validate against the
catalog and have never been executed; its FFmpeg side is a capability statement
with the filters recorded rather than run. See §30 for the full list.

**A callout knows what to point at and never where.** The vision pass names
entities and does not localise them, so every callout lands at the centre of
the frame with a note saying so and a person moves it.

**The retention wiring inherits Session 8's calibration.** Its risk thresholds,
hook scores and severity bands were tuned against intuition. Until now that
cost a marker in the wrong place; now it cuts footage, so a wrong finding is
more expensive than it was.

**The director has never been run against a large model on a real episode.**
The API contract is verified against a real HTTP endpoint and the decisions are
verified against fixtures, but whether a 14B or 70B model actually makes better
editing choices than `usefulness >= 0.40` is unknown. `director
compare-heuristic` and rendering both cuts is how you find out; nothing in this
system can tell you.

**Nothing checks a director decision for taste.** The rules check structure --
does this range exist, is the payoff protected, is the setup still in, does the
cut fit its runtime. A confident bad creative call passes every one of them.

**The proxy is the cut, not the edit.** It renders the V1 assembly and nothing
else, so captions, sound effects, music, graphics and markers are all absent
from a video that otherwise looks finished. The report lists what it could not
show, every time, for exactly this reason. Judging "does this need music" from
a proxy works; judging "is the music right" does not.

**Character names are the weakest thing here.** They come from capitalised words
in one channel, so every one caps below the edit threshold and arrives flagged
for review. A name is a name because a person says so, and nothing has asked one.

**Transcription accuracy is Whisper's.** Fast, excited commentary over game
audio is the hard case for any speech model. `small` plus a vocabulary prompt
is the best cost/quality point measured on this machine, and the fix for a bad
transcript is a bigger model rather than a different setting. There is no
diarisation: `speaker` is always `null`.

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
| `EDITING_RETENTION_MODE` | `report_only` | `off`, `report_only`, `retention`, `director_retention`, `hybrid` |
| `EDITING_RETENTION_COLD_OPEN` | `true` | Move the best hook to the front |
| `EDITING_MAX_COLD_OPEN_SECONDS` | `20` | Ceiling on the opening |
| `EDITING_DEAD_AIR_AGGRESSIVENESS` | `medium` | `low`, `medium`, `high` |
| `EDITING_RETENTION_COMPRESS` | `true` | Compress sagging stretches |
| `EDITING_DIRECTOR_BACKEND` | `openai` | `openai` or `mock` |
| `EDITING_DIRECTOR_BASE_URL` | `http://localhost:8000/v1` | Any OpenAI-compatible endpoint |
| `EDITING_DIRECTOR_MODEL` | `qwen2.5-14b-instruct` | Model name at that endpoint |
| `EDITING_DIRECTOR_API_KEY` | `not-needed` | Only if the endpoint requires one |
| `EDITING_DIRECTOR_MODE` | `director` | `heuristic`, `director` or `hybrid` |
| `EDITING_DIRECTOR_TEMPERATURE` | `0.2` | Low: this is a structured task |
| `EDITING_DIRECTOR_CONTEXT_CHARS` | `60000` | Ceiling on the brief |
| `EDITING_STYLE_GUIDE` | -- | Path to your prose editing rules |
| `EDITING_RENDER_QUALITY` | `proxy` | `draft`, `proxy`, `preview`, `high` |
| `EDITING_RENDER_HEIGHT` | `720` | Proxy output height |
| `EDITING_RENDER_FPS` | `30` | One frame rate for the whole render |
| `EDITING_RENDER_ENCODER` | `auto` | `auto` is libx264; a hardware encoder falls back if absent |
| `EDITING_RENDER_AUDIO` | `true` | Include audio in the proxy |
| `EDITING_RENDER_KEEP_TEMP` | `false` | Keep per-clip intermediates |
| `EDITING_RENDER_BACKEND` | `ffmpeg` | `mock` writes placeholders and says so |

Sampling variables are in the tuning table in section 3; audio variables in
section 4; Whisper variables in section 2.1.

### Where `AI_Models/editingllm` fits

`E:\Assistant\AI_Models\editingllm` holds model weights, model config, prompts
and any future training data. **No importable Python lives there** — all code is
in `E:\Assistant\editing`. To keep generated outputs and caches alongside the
model data instead of in `data/editing`:

```bash
set EDITING_OUTPUT_DIR=E:\Assistant\AI_Models\editingllm\runs
```
