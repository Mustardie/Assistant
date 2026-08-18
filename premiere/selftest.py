"""Verify the Premiere layer against a REAL Premiere host.

    python -m premiere.selftest              # read-only checks
    python -m premiere.selftest --full       # also exercise editing operations
    python -m premiere.selftest --full --json report.json

Why this exists: the unit tests prove our side of the contract (validation,
expansion, curve maths, error handling) but they run against a fake bridge and
therefore cannot prove that Premiere accepts any given call. Only a real host
can answer that, and the answer differs by Premiere version -- QE is
undocumented, Lumetri parameter names move, MOGRT and caption APIs come and go.

So this harness exercises each capability against the live application and
reports one of three verdicts:

    OK           the operation ran and did what it claimed
    UNSUPPORTED  Premiere genuinely cannot do it on this build (with the
                 alternative the layer falls back to)
    FAIL         it should have worked and did not -- a real bug

``--full`` mutates the timeline. It takes a checkpoint first and restores it at
the end, so the project is left as it was found; even so, run it on a scratch
project the first time.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

from premiere.engine import EditEngine, engine as default_engine
from premiere.errors import PremiereError, UnsupportedError

OK = "OK"
FAIL = "FAIL"
UNSUPPORTED = "UNSUPPORTED"
SKIP = "SKIP"


class Report:
    def __init__(self):
        self.rows: list = []
        self.started = time.time()

    def add(self, group: str, name: str, status: str, detail: str = "",
            data=None) -> None:
        self.rows.append({"group": group, "name": name, "status": status,
                          "detail": detail, "data": data})
        symbol = {OK: "  ok  ", FAIL: " FAIL ", UNSUPPORTED: " n/a  ",
                  SKIP: " skip "}[status]
        line = f"[{symbol}] {group:<12} {name}"
        if detail:
            line += f"  -- {detail}"
        print(line, flush=True)

    def counts(self) -> dict:
        out = {OK: 0, FAIL: 0, UNSUPPORTED: 0, SKIP: 0}
        for row in self.rows:
            out[row["status"]] += 1
        return out

    def summary(self) -> str:
        counts = self.counts()
        return (f"{counts[OK]} ok, {counts[FAIL]} failed, "
                f"{counts[UNSUPPORTED]} unsupported, {counts[SKIP]} skipped "
                f"in {time.time() - self.started:.1f}s")


class SelfTest:
    def __init__(self, engine: Optional[EditEngine] = None, full: bool = False):
        self.engine = engine or default_engine
        self.full = full
        self.report = Report()
        self.checkpoint = None
        self.video_clip = None
        self.audio_clip = None
        self.fps = 30.0

    # ------------------------------------------------------------------

    def check(self, group: str, name: str, fn, *, expect_unsupported=False):
        """Run one probe and classify the outcome."""
        try:
            result = fn()
        except UnsupportedError as exc:
            self.report.add(group, name, UNSUPPORTED,
                            exc.alternative or exc.message)
            return None
        except PremiereError as exc:
            status = UNSUPPORTED if expect_unsupported else FAIL
            self.report.add(group, name, status, exc.message)
            return None
        except Exception as exc:  # noqa: BLE001 - a crash is a finding
            self.report.add(group, name, FAIL, f"crashed: {exc}")
            return None

        if isinstance(result, dict) and result.get("success") is False:
            code = result.get("code")
            detail = result.get("error", "")
            if code == "unsupported":
                self.report.add(group, name, UNSUPPORTED,
                                result.get("alternative") or detail)
            else:
                self.report.add(group, name, FAIL, detail)
            return None

        detail = ""
        if isinstance(result, dict):
            note = result.get("note")
            if note:
                detail = str(note)[:110]
        self.report.add(group, name, OK, detail, data=_small(result))
        return result

    def edit(self, ops, **kw):
        return self.engine.run({"ops": ops, **kw})

    # ------------------------------------------------------------------

    def run(self) -> Report:
        if not self.connectivity():
            return self.report
        self.discovery()
        self.capabilities()
        self.timeline_read()

        if not self.full:
            print("\n(read-only mode -- pass --full to exercise editing)")
            return self.report

        if not self.pick_clips():
            return self.report

        self.take_checkpoint()
        try:
            self.properties()
            self.keyframes()
            self.animation()
            self.effects()
            self.transitions()
            self.audio()
            self.speed()
            self.color()
            self.text_and_graphics()
            self.captions()
            self.markers()
            self.arrangement()
            self.safety()
        finally:
            self.restore_checkpoint()
        return self.report

    # -- groups --------------------------------------------------------

    def connectivity(self) -> bool:
        from premiere.tool import premiere_status
        status = premiere_status()
        if not status.get("connected"):
            self.report.add("connect", "bridge", FAIL,
                            status.get("reason") or status.get("error", ""))
            print("\nCannot reach Premiere -- nothing else can be tested.")
            print(status.get("hint", ""))
            return False
        self.report.add("connect", "bridge", OK,
                        f"Premiere {status.get('premiere_version')}")
        if not status.get("project_open"):
            self.report.add("connect", "project", FAIL, "no project open")
            return False
        self.report.add("connect", "project", OK, str(status.get("project")))

        settings = status.get("sequence_settings") or {}
        if not settings.get("name"):
            self.report.add("connect", "sequence", FAIL, "no active sequence")
            return False
        self.fps = float(settings.get("fps") or 30.0)
        self.report.add(
            "connect", "sequence", OK,
            f"{settings.get('name')} @ {self.fps:g}fps "
            f"{settings.get('width')}x{settings.get('height')}")
        return True

    def discovery(self):
        self.check("discover", "project.assets",
                   lambda: self.edit([{"op": "project.assets"}]))
        self.check("discover", "sequence.list",
                   lambda: self.edit([{"op": "sequence.list"}]))

    def capabilities(self):
        from premiere.tool import premiere_capabilities
        probe = self.check("caps", "caps.probe",
                           lambda: premiere_capabilities("summary", refresh=True))
        if probe:
            qe = (probe.get("qe_dom") or {}).get("available")
            self.report.add("caps", "QE DOM", OK if qe else UNSUPPORTED,
                            "razor/effects/transitions/speed depend on this")
            interp = probe.get("interpolation_modes") or {}
            if interp.get("tested"):
                self.report.add("caps", "interpolation modes", OK,
                                "accepted: " + ", ".join(interp.get("accepted", [])))
            else:
                self.report.add("caps", "interpolation modes", SKIP,
                                interp.get("note", "not measured"))

        self.check("caps", "caps.effects",
                   lambda: premiere_capabilities("effects"))
        self.check("caps", "caps.transitions",
                   lambda: premiere_capabilities("transitions"))
        self.check("caps", "caps.fonts", lambda: premiere_capabilities("fonts"))

    def timeline_read(self):
        snapshot = self.check("read", "timeline.snapshot",
                              lambda: self.edit([{"op": "timeline.snapshot"}]))
        if snapshot:
            tracks = (snapshot["results"][0]["result"] or {}).get("tracks", [])
            clips = sum(len(t.get("clips", [])) for t in tracks)
            self.report.add("read", "timeline contents", OK,
                            f"{len(tracks)} tracks, {clips} clips")

    def pick_clips(self) -> bool:
        """Find a video and (optionally) an audio clip to experiment on."""
        result = self.edit([{"op": "timeline.snapshot",
                             "include_effects": False}])
        if not result.get("success"):
            self.report.add("setup", "find clips", FAIL, result.get("error", ""))
            return False
        tracks = (result["results"][0]["result"] or {}).get("tracks", [])
        for track in tracks:
            for clip in track.get("clips", []):
                selector = {"track": track["track"], "index": clip["index"]}
                if track["type"] == "video" and self.video_clip is None:
                    self.video_clip = selector
                if track["type"] == "audio" and self.audio_clip is None:
                    self.audio_clip = selector

        if not self.video_clip:
            self.report.add("setup", "find clips", FAIL,
                            "no video clip on the timeline to test against")
            print("\nPut at least one video clip on the timeline and re-run.")
            return False
        self.report.add("setup", "find clips", OK,
                        f"video={self.video_clip}, audio={self.audio_clip}")
        return True

    def take_checkpoint(self):
        result = self.check("safety", "checkpoint",
                            lambda: self.edit([{"op": "history.checkpoint",
                                                "name": "selftest"}]))
        if result and result.get("success"):
            self.checkpoint = "selftest"

    def restore_checkpoint(self):
        if not self.checkpoint:
            print("\nNOTE: no checkpoint was taken, so test edits remain on the "
                  "timeline. Undo them in Premiere (Ctrl+Z) if unwanted.")
            return
        print("\nRestoring the project to its pre-test state...")
        result = self.edit([{"op": "history.restore", "name": self.checkpoint}])
        if result.get("success"):
            self.report.add("safety", "restore", OK, "project returned to start")
        else:
            self.report.add("safety", "restore", FAIL, result.get("error", ""))
            print("WARNING: restore failed. The test edits are still on the "
                  "timeline; use Premiere's own undo.")

    # -- editing groups ------------------------------------------------

    def properties(self):
        self.check("property", "caps.params",
                   lambda: self.edit([{"op": "caps.params",
                                       "clip": self.video_clip}]))
        self.check("property", "property.get (Opacity)",
                   lambda: self.edit([{"op": "property.get",
                                       "clip": self.video_clip,
                                       "property": "opacity"}]))
        self.check("property", "property.set (Opacity)",
                   lambda: self.edit([{"op": "property.set",
                                       "clip": self.video_clip,
                                       "property": "opacity", "value": 90}]))
        self.check("property", "property.set (Scale)",
                   lambda: self.edit([{"op": "property.set",
                                       "clip": self.video_clip,
                                       "property": "scale", "value": 105}]))
        self.check("property", "property.set (Position)",
                   lambda: self.edit([{"op": "property.set",
                                       "clip": self.video_clip,
                                       "property": "position",
                                       "value": [0.5, 0.48]}]))
        self.check("property", "property.set (Rotation)",
                   lambda: self.edit([{"op": "property.set",
                                       "clip": self.video_clip,
                                       "property": "rotation", "value": 2}]))
        self.check("property", "property.set (Anchor Point)",
                   lambda: self.edit([{"op": "property.set",
                                       "clip": self.video_clip,
                                       "property": "anchor_point",
                                       "value": [0.5, 0.5]}]))

    def keyframes(self):
        self.check("keyframe", "keyframe.set",
                   lambda: self.edit([{
                       "op": "keyframe.set", "clip": self.video_clip,
                       "property": "opacity", "replace": True,
                       "keyframes": [{"time": 0.0, "value": 0},
                                     {"time": 1.0, "value": 100}],
                   }]))
        self.check("keyframe", "keyframe.list",
                   lambda: self.edit([{"op": "keyframe.list",
                                       "clip": self.video_clip,
                                       "property": "opacity"}]))
        self.check("keyframe", "keyframe.add",
                   lambda: self.edit([{"op": "keyframe.add",
                                       "clip": self.video_clip,
                                       "property": "opacity",
                                       "at": 0.5, "value": 60}]))
        self.check("keyframe", "keyframe.interpolation",
                   lambda: self.edit([{"op": "keyframe.interpolation",
                                       "clip": self.video_clip,
                                       "property": "opacity",
                                       "interp": "ease_both"}]))
        self.check("keyframe", "keyframe.move",
                   lambda: self.edit([{"op": "keyframe.move",
                                       "clip": self.video_clip,
                                       "property": "opacity", "by": 0.2}]))
        self.check("keyframe", "keyframe.remove",
                   lambda: self.edit([{"op": "keyframe.remove",
                                       "clip": self.video_clip,
                                       "property": "opacity",
                                       "range": [0.0, 0.4]}]))
        self.check("keyframe", "keyframe.clear",
                   lambda: self.edit([{"op": "keyframe.clear",
                                       "clip": self.video_clip,
                                       "property": "opacity"}]))

    def animation(self):
        self.check("animate", "native easing (2 keyframes)",
                   lambda: self.edit([{
                       "op": "animate", "clip": self.video_clip,
                       "property": "scale", "from": 100, "to": 118,
                       "duration": 1.5, "easing": "ease_out",
                   }]))
        self.check("animate", "baked curve (bounce)",
                   lambda: self.edit([{
                       "op": "animate", "clip": self.video_clip,
                       "property": "rotation", "from": 0, "to": 8,
                       "duration": 1.0, "easing": "bounce_out",
                   }]))
        self.check("animate", "custom cubic-bezier",
                   lambda: self.edit([{
                       "op": "keyframe.bake", "clip": self.video_clip,
                       "property": "opacity", "start": 0.0, "end": 1.0,
                       "from_value": 100, "to_value": 20,
                       "easing": [0.7, 0.0, 0.3, 1.0], "replace": True,
                   }]))
        self.check("animate", "vector property (Position)",
                   lambda: self.edit([{
                       "op": "animate", "clip": self.video_clip,
                       "property": "position", "from": [0.5, 0.5],
                       "to": [0.42, 0.5], "duration": 1.2,
                       "easing": "ease_in_out",
                   }]))

    def effects(self):
        applied = self.check("effect", "effect.apply (Gaussian Blur)",
                             lambda: self.edit([{
                                 "op": "effect.apply", "clip": self.video_clip,
                                 "effect": "Gaussian Blur",
                                 "params": {"Blurriness": 8},
                             }]))
        self.check("effect", "effect.list",
                   lambda: self.edit([{"op": "effect.list",
                                       "clip": self.video_clip}]))
        if applied:
            self.check("effect", "keyframe an effect parameter",
                       lambda: self.edit([{
                           "op": "animate", "clip": self.video_clip,
                           "component": "Gaussian Blur", "property": "Blurriness",
                           "from": 0, "to": 30, "duration": 1.0,
                           "easing": "ease_in",
                       }]))
            self.check("effect", "effect.remove",
                       lambda: self.edit([{
                           "op": "effect.remove", "clip": self.video_clip,
                           "effect": "Gaussian Blur",
                       }]))

    def transitions(self):
        self.check("transition", "transition.apply (Cross Dissolve)",
                   lambda: self.edit([{
                       "op": "transition.apply", "clip": self.video_clip,
                       "transition": "Cross Dissolve", "edge": "in",
                       "duration": 0.5,
                   }]))
        self.check("transition", "transition.remove",
                   lambda: self.edit([{"op": "transition.remove",
                                       "clip": self.video_clip}]),
                   expect_unsupported=True)

    def audio(self):
        if not self.audio_clip:
            self.report.add("audio", "all audio checks", SKIP,
                            "no audio clip on the timeline")
            return
        self.check("audio", "audio.gain",
                   lambda: self.edit([{"op": "audio.gain",
                                       "clip": self.audio_clip, "db": -4}]))
        self.check("audio", "audio.fade (in + out)",
                   lambda: self.edit([{"op": "audio.fade",
                                       "clip": self.audio_clip,
                                       "in": 0.5, "out": 0.8}]))
        self.check("audio", "audio.duck",
                   lambda: self.edit([{
                       "op": "audio.duck", "clip": self.audio_clip,
                       "under": [{"start": 1.0, "end": 3.0}],
                       "duck_db": -14,
                   }]))
        self.check("audio", "audio.pan",
                   lambda: self.edit([{"op": "audio.pan",
                                       "clip": self.audio_clip, "pan": -15}]))

    def speed(self):
        self.check("speed", "clip.speed (constant)",
                   lambda: self.edit([{"op": "clip.speed",
                                       "clip": self.video_clip, "rate": 1.25}]))
        self.check("speed", "clip.speed (back to 1x)",
                   lambda: self.edit([{"op": "clip.speed",
                                       "clip": self.video_clip, "rate": 1.0}]))
        self.check("speed", "clip.speed_ramp",
                   lambda: self.edit([{
                       "op": "clip.speed_ramp", "clip": self.video_clip,
                       "points": [{"time": 0.0, "rate": 1.0},
                                  {"time": 1.5, "rate": 0.4}],
                       "smooth": True,
                   }]))
        self.check("speed", "clip.freeze",
                   lambda: self.edit([{"op": "clip.freeze",
                                       "clip": self.video_clip,
                                       "at": 0.5, "duration": 1.0}]))

    def color(self):
        self.check("color", "color.grade (Lumetri)",
                   lambda: self.edit([{
                       "op": "color.grade", "clip": self.video_clip,
                       "exposure": 0.25, "contrast": 10, "saturation": 110,
                       "temperature": 8, "highlights": -12, "shadows": 8,
                       "whites": 4, "blacks": -4,
                   }]))
        self.check("color", "keyframe a Lumetri parameter",
                   lambda: self.edit([{
                       "op": "animate", "clip": self.video_clip,
                       "component": "Lumetri Color", "property": "Exposure",
                       "from": 0, "to": 0.6, "duration": 1.0,
                       "easing": "ease_in_out",
                   }]))
        self.check("color", "color.lut", lambda: self.edit([{
            "op": "color.lut", "clip": self.video_clip,
            "path": "/nonexistent/look.cube",
        }]), expect_unsupported=True)
        self.check("color", "adjustment.create",
                   lambda: self.edit([{
                       "op": "adjustment.create", "time": 0.0, "duration": 3.0,
                       "effects": [{"effect": "Lumetri Color",
                                    "params": {"Exposure": 0.1}}],
                   }]))

    def text_and_graphics(self):
        self.check("graphics", "text.create (rendered)",
                   lambda: self.edit([{
                       "op": "text.create", "text": "Nova self-test",
                       "time": 0.0, "duration": 2.0, "size": 90,
                       "color": "#FFD600", "stroke_color": "#000000",
                       "stroke_width": 4,
                   }]))
        self.check("graphics", "text.create (named style)",
                   lambda: self.edit([{
                       "op": "text.create", "text": "Styled title",
                       "style": "title", "time": 2.5, "duration": 2.0,
                   }]))
        self.check("graphics", "graphic.shape (rounded rect)",
                   lambda: self.edit([{
                       "op": "graphic.shape", "shape": "rounded_rect",
                       "size": [0.45, 0.11], "fill": "#101010CC", "radius": 14,
                       "position": [0.5, 0.84], "time": 0.0, "duration": 2.0,
                   }]))
        self.check("graphics", "graphic.shape (circle)",
                   lambda: self.edit([{
                       "op": "graphic.shape", "shape": "circle",
                       "size": [0.12, 0.12], "fill": "#FF3B30",
                       "position": [0.2, 0.2], "time": 0.0, "duration": 1.5,
                   }]))
        self.check("graphics", "animate a text overlay",
                   lambda: self.edit([{
                       "op": "animate", "clip": {"track": "V2", "index": 0},
                       "property": "opacity", "from": 0, "to": 100,
                       "duration": 0.4, "easing": "ease_out",
                   }]))

    def captions(self):
        segments = [
            {"start": 0.0, "end": 1.4, "text": "first caption line"},
            {"start": 1.4, "end": 2.8, "text": "second caption line"},
        ]
        self.check("captions", "captions.build (caption track)",
                   lambda: self.edit([{"op": "captions.build", "mode": "caption",
                                       "segments": segments}]),
                   expect_unsupported=True)
        self.check("captions", "captions.build (graphic overlays)",
                   lambda: self.edit([{"op": "captions.build", "mode": "graphic",
                                       "segments": segments}]))

    def markers(self):
        self.check("marker", "marker.add (comment)",
                   lambda: self.edit([{"op": "marker.add", "time": 1.0,
                                       "name": "selftest-marker",
                                       "comment": "placed by the self test"}]))
        self.check("marker", "marker.add (chapter)",
                   lambda: self.edit([{"op": "marker.add", "time": 2.0,
                                       "name": "Chapter 1", "type": "chapter"}]))
        found = self.check("marker", "marker.list",
                           lambda: self.edit([{"op": "marker.list"}]))
        if found:
            data = found["results"][0]["result"] or {}
            self.report.add("marker", "markers readable", OK,
                            f"{data.get('count', 0)} marker(s) on the sequence")
        self.check("marker", "marker.remove",
                   lambda: self.edit([{"op": "marker.remove",
                                       "name": "selftest-marker"}]))
        self.check("consistency", "clip.copy_attributes",
                   lambda: self.edit([{
                       "op": "clip.copy_attributes",
                       "from": self.video_clip,
                       "to": {"track": self.video_clip["track"], "all": True},
                       "include": ["effects", "transform"],
                   }]))

    def arrangement(self):
        self.check("arrange", "clip.split",
                   lambda: self.edit([{
                       "op": "clip.split", "clip": self.video_clip,
                       "at": [1.0],
                   }]))
        self.check("arrange", "clip.trim",
                   lambda: self.edit([{
                       "op": "clip.trim", "clip": self.video_clip,
                       "edge": "out", "by": 0.2,
                   }]))
        self.check("arrange", "clip.move",
                   lambda: self.edit([{
                       "op": "clip.move", "clip": self.video_clip, "by": 0.0,
                   }]))
        self.check("arrange", "clip.set (disable/enable)",
                   lambda: self.edit([{
                       "op": "clip.set", "clip": self.video_clip,
                       "enabled": True,
                   }]))
        self.check("arrange", "track.add",
                   lambda: self.edit([{"op": "track.add", "video": 1}]))
        self.check("arrange", "track.set (rename)",
                   lambda: self.edit([{"op": "track.set", "track": "V1",
                                       "name": "selftest"}]))

    def safety(self):
        self.check("safety", "dry run",
                   lambda: self.edit([{"op": "clip.remove",
                                       "clip": self.video_clip}],
                                     dry_run=True))
        stale = self.edit([{"op": "property.set", "clip": self.video_clip,
                            "property": "opacity", "value": 55}],
                          revision="definitely-stale")
        if stale.get("code") == "revision_mismatch":
            self.report.add("safety", "revision protection", OK,
                            "stale plan correctly refused")
        else:
            self.report.add("safety", "revision protection", FAIL,
                            "a stale revision was not refused")
        self.check("safety", "partial failure reporting", lambda: _expect_partial(
            self.edit([
                {"op": "property.set", "clip": self.video_clip,
                 "property": "opacity", "value": 80},
                {"op": "property.set", "clip": {"track": "V9", "index": 99},
                 "property": "opacity", "value": 80},
            ])))


def _expect_partial(result: dict) -> dict:
    """A plan with one bad op must report partial success, not blanket failure."""
    if result.get("success"):
        raise PremiereError("expected the bad operation to fail, but it did not")
    if result.get("applied") != 1:
        raise PremiereError(
            f"expected 1 applied operation, got {result.get('applied')}")
    return {"note": "first op applied, second failed, reported correctly"}


def _small(value, limit: int = 400):
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)[:limit]
    return json.loads(text) if len(text) <= limit else text[:limit] + "..."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true",
                        help="exercise editing operations (mutates, then restores)")
    parser.add_argument("--json", metavar="PATH",
                        help="write the full report as JSON")
    args = parser.parse_args(argv)

    print("Nova Premiere Bridge -- self test")
    print("=" * 62)
    tester = SelfTest(full=args.full)
    report = tester.run()

    print("=" * 62)
    print(report.summary())

    failures = [r for r in report.rows if r["status"] == FAIL]
    if failures:
        print("\nFailures:")
        for row in failures:
            print(f"  {row['group']}/{row['name']}: {row['detail']}")

    unsupported = [r for r in report.rows if r["status"] == UNSUPPORTED]
    if unsupported:
        print("\nUnsupported on this Premiere build (with fallbacks):")
        for row in unsupported:
            print(f"  {row['group']}/{row['name']}: {row['detail']}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"summary": report.counts(), "rows": report.rows},
                      handle, indent=2, default=str)
        print(f"\nFull report written to {args.json}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
