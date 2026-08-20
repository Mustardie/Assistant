"""The visual critic: review frames in, findings out.

The same ``Qwen3-VL-8B-Instruct`` server the analysis pass uses, asked a
different question. Reusing the backend is deliberate -- one model to serve,
one health check, one set of retry semantics -- and the critic differs only in
its prompt and in what it does with the answer.

Four rules this module holds to:

**One frame per call.** Batching frames into one request is cheaper and much
worse: a small VLM shown six stills reliably attributes a problem in frame four
to frame one, and a finding pointing at the wrong moment is worse than no
finding. One frame also makes the cache entry mean exactly one thing.

**Nothing uncoerced escapes.** Whatever the model answers goes through
``parse_response``, which is total: any input produces a list of findings or an
empty one. A response the parser cannot make sense of becomes a
``needs_human_review`` finding rather than an exception, because "the critic
said something I could not read" is information a person should see.

**A failed frame is recorded, not fatal.** One unreachable call in a
sixty-frame pass costs that frame and a warning, not the pass. Failed frames
are never cached.

**Mock answers are marked, everywhere.** ``MockCritic`` sets ``mock: true`` on
every finding and the report carries it too, so a mock pass can never be read
as a real one -- the same rule the analysis layer follows.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from editing.cache import Cache
from editing.config import EditingConfig
from editing.critic import prompt as prompt_module
from editing.critic.schema import CriticFinding, CriticReport, finding_id_for
from editing.errors import ModelError
from editing.roughcut.review import ReviewFrame, ReviewSet

logger = logging.getLogger("nova.editing.critic")

#: Called as ``progress(done, total, frame)`` after each frame resolves.
ProgressHook = Callable[[int, int, ReviewFrame], None]


class CriticModel(Protocol):
    """What the critic requires of a backend. Same shape as ``VisionModel``."""

    name: str

    def analyze(
        self, frames: Sequence[Path], *, system: str, user: str
    ) -> dict: ...


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def parse_response(payload: Any, frame: ReviewFrame) -> list[CriticFinding]:
    """Coerce one model answer into findings. Total: never raises.

    The shapes seen in practice, all accepted:

    * ``{"looks_ok": true, "issues": []}`` -- the common case, no findings
    * ``{"issues": [{...}]}`` / ``{"findings": [{...}]}``
    * ``{"issue": "too_dark", "confidence": 0.8}`` -- a bare single finding
    * ``{"issues": ["too_dark", "hud_hidden"]}`` -- issue names as strings
    * ``{"looks_ok": false}`` with no issues -- a contradiction, resolved below

    That last case is the interesting one. A model that says something is wrong
    but names nothing is not saying "fine", so it produces one
    ``needs_human_review`` finding carrying whatever notes it did give. Reading
    it as "no findings" would silently discard a real signal.
    """
    if not isinstance(payload, dict):
        return [_unreadable(frame, f"the critic returned {type(payload).__name__}, "
                                   "not an object")]

    raw = payload.get("issues")
    if raw is None:
        raw = payload.get("findings")
    if raw is None:
        raw = payload.get("problems")

    entries: list[dict] = []
    if isinstance(raw, dict):
        entries = [raw]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entries.append(item)
            elif isinstance(item, str) and item.strip():
                entries.append({"issue": item})
    elif isinstance(raw, str) and raw.strip():
        entries = [{"issue": raw}]

    # A bare finding at the top level, with no wrapper.
    if not entries and any(key in payload for key in ("issue", "type", "problem")):
        entries = [payload]

    looks_ok = payload.get("looks_ok")
    if looks_ok is None:
        looks_ok = payload.get("ok")
    notes = str(payload.get("notes") or payload.get("summary") or "")[:600]

    findings: list[CriticFinding] = []
    for entry in entries:
        data = dict(entry)
        data.setdefault("frame_id", frame.frame_id)
        finding = CriticFinding.from_dict(data)
        finding.frame_id = frame.frame_id
        finding.sequence_time = frame.sequence_time
        finding.placement_id = frame.placement_id
        if not finding.evidence:
            finding.evidence = notes
        if not finding.finding_id:
            finding.finding_id = finding_id_for(
                frame.frame_id, finding.issue, finding.evidence
            )
        findings.append(finding)

    if not findings and looks_ok is False:
        findings.append(_unreadable(
            frame,
            notes or "the critic said the frame is not ok but named no issue",
            confidence=0.35,
        ))

    return findings


def _unreadable(
    frame: ReviewFrame, evidence: str, *, confidence: float = 0.3
) -> CriticFinding:
    """The finding produced when the critic said something unusable."""
    return CriticFinding(
        finding_id=finding_id_for(frame.frame_id, "needs_human_review", evidence),
        frame_id=frame.frame_id,
        issue="needs_human_review",
        severity="low",
        confidence=confidence,
        evidence=evidence[:600],
        suggested_fix="review_marker",
        sequence_time=frame.sequence_time,
        placement_id=frame.placement_id,
    )


# ---------------------------------------------------------------------------
# The mock critic
# ---------------------------------------------------------------------------

class MockCritic:
    """A deterministic stand-in, for tests and for GPU-less dry runs.

    It reads the frame's *metadata*, never the picture -- so it cannot detect
    anything real, and says so: every finding is marked ``mock`` and its
    evidence names the metadata field it fired on. Its purpose is to exercise
    the whole path (parse, revise, plan, dry run) with a plausible spread of
    issue types and confidences, including some deliberately below the
    automatic-fix threshold so the "stays a recommendation" branch is reachable
    without a model server.
    """

    name = "mock-critic"

    def __init__(self, responses: Optional[list] = None):
        #: Canned answers, consumed in order, before the derived ones.
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def analyze(self, frames, *, system: str, user: str) -> dict:
        self.calls.append({
            "frames": [str(frame) for frame in frames],
            "system": system,
            "user": user,
        })
        if self.responses:
            answer = self.responses.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)
        return {"looks_ok": True, "issues": [],
                "notes": "mock: true -- no picture was examined"}

    def critique_frame(self, frame: ReviewFrame) -> dict:
        """The derived answer for one frame, from its metadata alone."""
        issues: list[dict] = []
        zoom = next(
            (edit for edit in frame.applied_edits if edit.get("kind") == "zoom"),
            None,
        )
        if zoom is not None:
            scale = float(zoom.get("to") or 100.0)
            if frame.ui_flags:
                issues.append({
                    "issue": "hud_hidden",
                    "severity": "high",
                    "confidence": 0.72,
                    "evidence": (
                        f"mock: true -- a zoom to {scale:g}% lands where the "
                        f"analysis pass recorded {', '.join(frame.ui_flags)}"
                    ),
                    "suggested_fix": "remove_zoom",
                })
            elif scale >= 112.0:
                issues.append({
                    "issue": "zoom_too_strong",
                    "severity": "medium",
                    "confidence": 0.68,
                    "evidence": f"mock: true -- the plan zooms to {scale:g}%",
                    "suggested_fix": "reduce_zoom",
                })

        if frame.has_text:
            issues.append({
                "issue": "text_placed_badly",
                "severity": "low",
                "confidence": 0.62,
                "evidence": "mock: true -- a text placeholder is planned here "
                            "with no position chosen",
                "suggested_fix": "move_text_placeholder",
            })

        if frame.frame_kind == "clip_end" and frame.importance in (
            "danger", "payoff", "reveal"
        ):
            issues.append({
                "issue": "cut_too_early",
                "severity": "medium",
                "confidence": 0.61,
                "evidence": (
                    f"mock: true -- the clip ends on a {frame.importance} beat"
                ),
                "suggested_fix": "extend_hold",
            })

        if frame.keep_reason == "filler" and frame.clip_duration >= 8.0:
            issues.append({
                "issue": "boring_too_long",
                "severity": "low",
                "confidence": 0.55,
                "evidence": (
                    f"mock: true -- {frame.clip_duration:.0f}s of filler"
                ),
                "suggested_fix": "shorten_section",
            })

        if frame.environment in ("cave", "nether") and not issues:
            # Deliberately under the automatic-fix threshold: this is the path
            # where a real-looking finding correctly stays a recommendation.
            issues.append({
                "issue": "too_dark",
                "severity": "low",
                "confidence": 0.35,
                "evidence": (
                    f"mock: true -- guessed from environment={frame.environment}, "
                    "not from the picture"
                ),
                "suggested_fix": "color_marker",
            })

        return {
            "looks_ok": not issues,
            "issues": issues,
            "notes": "mock: true -- derived from frame metadata, no picture "
                     "was examined",
        }

    def health(self) -> dict:
        return {"reachable": True, "backend": "mock", "model_served": True,
                "note": "Mock critic: no real analysis is performed."}


# ---------------------------------------------------------------------------
# The critic
# ---------------------------------------------------------------------------

class VisualCritic:
    """Runs the critic over a set of exported review frames."""

    def __init__(
        self,
        config: EditingConfig,
        *,
        model: Optional[CriticModel] = None,
        cache: Optional[Cache] = None,
    ):
        self.config = config
        self.cache = cache
        self._model = model

    @property
    def model(self) -> CriticModel:
        if self._model is None:
            from editing.visual.qwen import build_model
            backend = (self.config.vision_backend or "openai").strip().lower()
            self._model = (
                MockCritic() if backend == "mock" else build_model(self.config)
            )
        return self._model

    @property
    def model_name(self) -> str:
        """The name in cache keys and provenance.

        Taken from the model object, not the config, so a mock pass can never
        share a cache entry with a real one.
        """
        return getattr(self.model, "name", None) or self.config.vision_model

    @property
    def is_mock(self) -> bool:
        return isinstance(self.model, MockCritic) or str(
            self.model_name
        ).startswith("mock")

    # -- one frame ------------------------------------------------------

    def critique_frame(self, frame: ReviewFrame) -> tuple[list[CriticFinding], bool]:
        """Findings for one frame, and whether they came from the cache.

        Raises ``ModelError`` when the backend could not be reached at all --
        the caller decides whether one unreachable frame ends the pass.
        """
        key = self._cache_key(frame)
        if self.cache is not None and key:
            cached = self.cache.get("critic", key)
            if cached is not None:
                findings = [
                    CriticFinding.from_dict(entry)
                    for entry in (cached.get("findings") or [])
                ]
                return findings, True

        model = self.model
        if isinstance(model, MockCritic) and not model.responses:
            payload = model.critique_frame(frame)
            model.calls.append({"frames": [frame.path], "derived": True})
        else:
            payload = model.analyze(
                [Path(frame.path)] if frame.path else [],
                system=prompt_module.SYSTEM_PROMPT,
                user=prompt_module.build_user_prompt(
                    frame, sequence_name=frame.sequence_name
                ),
            )

        findings = parse_response(payload, frame)
        if self.is_mock:
            for finding in findings:
                finding.mock = True

        if self.cache is not None and key:
            self.cache.put("critic", key, {
                "frame_id": frame.frame_id,
                "model": self.model_name,
                "findings": [finding.to_dict() for finding in findings],
            })
        return findings, False

    def _cache_key(self, frame: ReviewFrame) -> str:
        """Keyed on the picture *and* the context it was judged against.

        Both matter. A re-exported frame at a different width is a different
        picture; the same picture with a zoom now planned over it is a
        different question. Either one changing must miss the cache.
        """
        if self.cache is None or not frame.path:
            return ""
        try:
            stat = Path(frame.path).stat()
            fingerprint = {"size": stat.st_size, "mtime": int(stat.st_mtime)}
        except OSError:
            fingerprint = {"size": 0, "mtime": 0}
        return self.cache.key(
            "critic",
            path=str(frame.path),
            file=fingerprint,
            model=self.model_name,
            prompt_version=prompt_module.PROMPT_VERSION,
            context=prompt_module.context_lines(frame),
        )

    # -- a whole review set ---------------------------------------------

    def critique(
        self,
        review: ReviewSet,
        *,
        progress: Optional[ProgressHook] = None,
        limit: int = 0,
    ) -> CriticReport:
        """Critique every exported frame in ``review``."""
        started = time.time()
        frames = [frame for frame in review.frames if frame.path]
        if limit and limit > 0:
            frames = frames[:limit]

        report = CriticReport(
            sequence_name=review.sequence_name,
            model=self.model_name,
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            frames_examined=len(frames),
            mock=self.is_mock,
        )
        if len(frames) < len(review.frames):
            report.warnings.append(
                f"{len(review.frames) - len(frames)} frame(s) in the manifest "
                "have no exported image and were skipped. Re-run "
                "`review export-frames`."
            )
        if not frames:
            report.warnings.append(
                "There are no exported review frames to critique."
            )
            report.elapsed = time.time() - started
            return report
        if report.mock:
            report.warnings.append(
                "The mock critic was used: findings are derived from frame "
                "metadata, not from the pictures. Nothing here is a real "
                "visual judgement."
            )

        results = self._run(frames)

        for index, (frame, findings, hit, error) in enumerate(results, start=1):
            if error is not None:
                report.frames_failed += 1
                report.warnings.append(
                    f"{frame.frame_id} at {frame.sequence_time:.2f}s: {error}"
                )
            else:
                if hit:
                    report.cache_hits += 1
                else:
                    report.cache_misses += 1
                if findings:
                    report.findings.extend(findings)
                else:
                    report.frames_clean += 1
            if progress is not None:
                progress(index, len(frames), frame)

        report.findings.sort(key=lambda f: (f.sequence_time, f.issue))
        report.elapsed = time.time() - started
        return report

    def _run(self, frames: Sequence[ReviewFrame]) -> list[tuple]:
        """Critique each frame, keeping results in frame order."""
        concurrency = max(1, int(self.config.vision_concurrency))

        def one(frame: ReviewFrame) -> tuple:
            try:
                findings, hit = self.critique_frame(frame)
            except ModelError as exc:
                return (frame, [], False, exc.message)
            except Exception as exc:  # noqa: BLE001 - one frame is not the pass
                logger.debug("Critic failed on %s: %s", frame.frame_id, exc)
                return (frame, [], False, str(exc))
            return (frame, findings, hit, None)

        if concurrency == 1 or len(frames) == 1:
            return [one(frame) for frame in frames]
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            return list(pool.map(one, frames))


def build_critic(
    config: EditingConfig,
    *,
    cache: Optional[Cache] = None,
    model: Optional[CriticModel] = None,
) -> VisualCritic:
    return VisualCritic(config, model=model, cache=cache)


def health(config: EditingConfig) -> dict:
    """Reachability of the critic backend, without raising."""
    from editing.visual import qwen

    backend = (config.vision_backend or "openai").strip().lower()
    if backend == "mock":
        return MockCritic().health()
    return qwen.health(config)
