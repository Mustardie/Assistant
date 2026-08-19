# Nova Premiere Bridge

CEP extension that lets the assistant edit in Adobe Premiere Pro through a
strict structured interface. The editing model sends **data**, never
ExtendScript — see `premiere/catalog.py` for the operations it can express.

## Install

On the machine running Premiere:

```
python -m premiere.install
```

This copies the panel into your CEP extensions folder and enables
`PlayerDebugMode` (required — Premiere refuses to load unsigned extensions
without it).

Then:

1. **Fully quit and restart Premiere** (closing the project is not enough).
2. Open **Window → Extensions → Nova Premiere Bridge**.
3. Leave the panel open while the assistant is editing.

Verify from Python:

```
python -m premiere.selftest          # read-only checks
python -m premiere.selftest --full   # exercise editing, then restore
```

## Architecture

```
Python (premiere/)                    Premiere
──────────────────                    ─────────
EditPlan
  → validator.py   (rejects bad plans before anything runs)
  → engine.py      (expands animate/duck/text/... into primitives)
  → bridge.py ──HTTP──→ main.js  (Node server inside the panel)
                          → evalScript → host/index.jsx
                                          → host/modules/*.jsx
                                             → Premiere timeline
```

* `main.js` — Node HTTP server on `127.0.0.1:8089`, loopback only. Forwards
  requests to ExtendScript and runs the file operations (checkpoints) that
  ExtendScript handles badly.
* `host/index.jsx` — the single entry point, `NovaBridge.dispatch(json)`. It
  dispatches from a fixed operation table; there is no path by which arbitrary
  code can be executed inside Premiere.
* `host/modules/` — `timeline` (clip resolution, snapshots, revisions),
  `params` (the generic property/keyframe engine), `clips` (arrangement),
  `effects`, `media` (overlays, MOGRT, captions), `project`, `caps`.

## Why some things go through QE

Premiere's documented DOM cannot razor a clip, apply an effect, add a
transition or set clip speed. The undocumented QE DOM can, and is the only
mechanism that exists. Every QE call is feature-detected and reports precisely
what failed. `caps.probe` tells you whether QE is available on the running
build — if it is not, restarting Premiere usually restores it.

## Ports

The panel listens on `127.0.0.1:8089`. Override on the Python side with
`PREMIERE_BRIDGE_PORT`; if you change it, change `PORT` in `main.js` to match.

## Troubleshooting

| Symptom | Cause |
|---|---|
| "Premiere bridge unreachable" | Panel not open, or Premiere not restarted after install |
| Panel shows "Node.js is not enabled" | Manifest lost its `--enable-nodejs`; reinstall |
| Panel missing from the Extensions menu | `PlayerDebugMode` not set, or Premiere not restarted |
| "Port 8089 already in use" | A second Premiere instance is running the bridge |
| Razor/effects/transitions fail | QE unavailable — check `caps.probe`, restart Premiere |
