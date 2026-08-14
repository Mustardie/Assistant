"""SkillManager -- the single facade for the Skills subsystem.

Everything the rest of Nova needs is here:

    recording   start_recording / pause / resume / cancel / stop
    playback    play / continue_playback / abort_playback / apply_improvement
    editing     list_skills / rename_skill / delete_skill / duplicate_skill /
                export_skill / import_skill
    planning    find_skill / auto_play_candidate / observe_request /
                suggest_skills_for

No skill logic lives in the agent loop; the loop only calls this facade.
The recorder/player imports are lazy so importing this module stays cheap.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from config.settings import settings
from skills import parser as parser_module
from skills import planner, storage

logger = logging.getLogger(__name__)


class SkillManager:
    def __init__(self):
        self._recorder = None
        self._draft_name = None
        self._session = None
        self._last_result = None
        self._pending_finalize = None  # holds a stopped-but-unnamed recording

    # ------------------------------------------------------------------ #
    # Recording (Phase 1)
    # ------------------------------------------------------------------ #

    @property
    def is_recording(self) -> bool:
        return self._recorder is not None and self._recorder.is_recording

    @property
    def is_paused(self) -> bool:
        return self._recorder is not None and self._recorder.is_paused

    @property
    def awaiting_skill_name(self) -> bool:
        return self._pending_finalize is not None

    def start_recording(self, name: str | None = None) -> dict:
        if self.is_recording:
            return {"success": False,
                    "speak": "I'm already watching. Say 'stop recording' when you're done."}
        if self._pending_finalize is not None:
            return {"success": False,
                    "speak": "I still need a name for the last skill you recorded -- "
                             "what should I call it?"}
        if self._session is not None:
            return {"success": False,
                    "speak": "Finish the current skill playback before recording."}

        from skills.recorder import Recorder

        draft = storage.sanitize_name(name) if name else (
            f"Recording {datetime.now().strftime('%H%M%S')}"
        )
        storage.ensure_layout(draft)
        self._recorder = Recorder(storage.skill_dir(draft))
        self._recorder.start()
        self._draft_name = draft
        logger.info("[Skills] Recording started as draft '%s'", draft)
        return {
            "success": True,
            "name": draft,
            "speak": "I'm watching. Go ahead and perform the task. "
                     "Say 'stop recording' when you're done.",
        }

    def pause_recording(self) -> dict:
        if not self.is_recording:
            return {"success": False, "speak": "I'm not recording right now."}
        self._recorder.pause()
        return {"success": True, "speak": "Recording paused. Say 'resume recording' to continue."}

    def resume_recording(self) -> dict:
        if not self.is_recording:
            return {"success": False, "speak": "I'm not recording right now."}
        self._recorder.resume()
        return {"success": True, "speak": "Recording resumed."}

    def add_narration(self, text: str) -> dict:
        """Capture a spoken instruction while a recording is in progress.
        Returns a dict with 'captured' so the agent loop knows the text was
        consumed as narration (it must NOT also be dispatched to the
        planner)."""
        if not self.is_recording:
            return {"success": False, "captured": False}
        self._recorder.add_narration(text)
        return {"success": True, "captured": True,
                "speak": "Got it, I'll remember that instruction."}

    def cancel_recording(self) -> dict:
        if not self.is_recording:
            return {"success": False, "speak": "I'm not recording right now."}
        self._recorder.cancel()
        storage.delete_skill(self._draft_name)
        logger.info("[Skills] Recording cancelled (draft '%s' removed)", self._draft_name)
        self._recorder = None
        self._draft_name = None
        return {"success": True, "speak": "Recording cancelled. I didn't save anything."}

    def stop_recording(self, final_name: str | None = None) -> dict:
        if not self.is_recording:
            return {"success": False, "speak": "I'm not recording right now."}
        recorder = self._recorder
        draft = self._draft_name
        self._recorder = None
        self._draft_name = None

        capture = recorder.stop()
        if not capture.get("events"):
            storage.delete_skill(draft)
            return {"success": False,
                    "speak": "I didn't capture anything. Want to try again? Say 'watch me'."}

        try:
            llm = self._make_llm()
            parsed = parser_module.parse_recording(capture, llm=llm)
            profile = parser_module.llm_skill_profile(
                llm, parsed["semantic"], fallback_name=draft,
                instructions=parsed.get("instructions"),
            )
        except Exception as exc:
            logger.exception("[Skills] Parsing recording failed")
            storage.delete_skill(draft)
            return {"success": False,
                    "speak": f"I had a problem understanding what I recorded: {exc}."}

        if not final_name:
            # Don't silently guess the name -- hold the recording and ask.
            # This doubles as a natural opening for the user to add any
            # extra requirements they couldn't narrate while their hands
            # were busy (e.g. "read the title before downloading").
            self._pending_finalize = {
                "draft": draft, "capture": capture, "parsed": parsed, "profile": profile,
            }
            suggested = profile.get("name") or draft
            return {
                "success": True,
                "need_name": True,
                "speak": (
                    f"Got it, recording stopped. What should I call this skill? "
                    f"(I'd suggest '{suggested}'.) Let me know now if there's anything "
                    "else I should do each time, like reading a title before downloading."
                ),
            }

        return self._finalize(draft, capture, parsed, profile, final_name)

    def finish_recording(self, reply: str) -> dict:
        """Complete a stop_recording() that was left waiting on a name.

        Accepts a free-form reply: the part before the first comma/'and'
        is treated as the skill name, anything after that is folded in as
        extra playback instructions -- e.g. "school notes, read the title
        before downloading" -> name="school notes", instructions += [...].
        """
        pending = self._pending_finalize
        if pending is None:
            return {"success": False, "speak": "I wasn't waiting on a name for a skill."}
        self._pending_finalize = None

        reply = (reply or "").strip()
        fallback_name = pending["profile"].get("name") or pending["draft"]
        if not reply:
            name = fallback_name
            extra_instructions: list[str] = []
        else:
            parts = re.split(r"[,;]|\band\b", reply, maxsplit=1)
            name = parts[0].strip().strip('"\'') or fallback_name
            extra_instructions = [p.strip(" .,!?") for p in parts[1:] if p.strip(" .,!?")]

        if extra_instructions:
            pending["parsed"]["instructions"] = list(
                pending["parsed"].get("instructions") or []
            ) + extra_instructions

        return self._finalize(
            pending["draft"], pending["capture"], pending["parsed"],
            pending["profile"], name,
        )

    def _finalize(self, draft: str, capture: dict, parsed: dict, profile: dict,
                   final_name: str | None) -> dict:
        final = storage.sanitize_name(final_name or profile.get("name") or draft)
        if final.lower() != draft.lower():
            storage.rename_skill(draft, final)

        timeline = {
            "semantic": parsed["semantic"],
            "raw": parsed["raw"],
            "variables": parsed["variables"],
            "instructions": parsed.get("instructions") or [],
            "screenshots": capture.get("screenshots") or [],
            "screen": capture.get("screen") or {},
        }
        storage.save_timeline(final, timeline)

        meta = dict(parsed.get("meta") or {})
        meta.update({
            "created": storage.now_iso(),
            "recording": {
                "draft": draft,
                "screen": capture.get("screen") or {},
                "event_count": len(capture.get("events") or []),
                "screenshot_count": len(capture.get("screenshots") or []),
            },
            "detected_variables": parsed["variables"],
            "improvement_history": [],
        })
        if parsed.get("instructions"):
            meta["instructions"] = parsed["instructions"]
        storage.save_metadata(final, meta)

        record = {
            "id": storage.new_skill_id(),
            "name": final,
            "description": profile.get("description", ""),
            "created": storage.now_iso(),
            "last_used": None,
            "applications": meta.get("applications", []),
            "difficulty": meta.get("difficulty", "medium"),
            "estimated_duration_s": meta.get("estimated_duration_s", 0),
            "required_windows": meta.get("windows", []),
            "required_permissions": meta.get("permissions", []),
            "tags": profile.get("tags", []),
            "version": 1,
        }
        storage.save_skill(record)

        step_count = len(parsed["semantic"])
        variable_count = len(parsed["variables"])
        self._last_result = {"record": record, "timeline": timeline}
        logger.info("[Skills] Saved skill '%s' (%d steps, %d variables)",
                    final, step_count, variable_count)
        return {
            "success": True,
            "name": final,
            "steps": step_count,
            "variables": variable_count,
            "record": record,
            "speak": (
                f"Done! Saved skill '{final}' with {step_count} steps"
                + (f" and {variable_count} variable{'s' if variable_count != 1 else ''}"
                   if variable_count else "")
                + ". You can run it anytime by saying 'do the "
                + final + " skill'."
            ),
        }

    # ------------------------------------------------------------------ #
    # Playback (Phase 2/3/4)
    # ------------------------------------------------------------------ #

    @property
    def active_session(self):
        return self._session

    @property
    def pending_question(self) -> str | None:
        if self._session and self._session.status == "need_input":
            return self._session.pending_question
        return None

    def play(self, name: str) -> dict:
        if self.is_recording:
            return {"status": "failed",
                    "message": "Finish the current recording first."}
        if self._session is not None:
            return {"status": "failed",
                    "message": "Another skill playback is still in progress."}

        record = storage.load_skill(name)
        if record is None:
            close = planner.suggest_skills_for(name)
            if close:
                listing = ", ".join(f"'{s['name']}'" for s in close[:3])
                return {"status": "failed",
                        "message": f"I don't have a skill called '{name}'. "
                                   f"Closest I have: {listing}. Say 'do the <name> skill'."}
            return {"status": "failed",
                    "message": f"I don't have a skill called '{name}'. "
                               "Say 'show my skills' to see what I can do, "
                               "or 'watch me' to teach me a new one."}

        from skills.player import PlaybackSession

        self._session = PlaybackSession(name)
        return self._advance_playback(self._session, answer=None)

    def continue_playback(self, session, answer: str) -> dict:
        if self._session is not session or session is None:
            return {"status": "failed", "message": "That playback is no longer active."}
        return self._advance_playback(session, answer=answer)

    def abort_playback(self, session=None) -> dict:
        target = session or self._session
        if target is None:
            return {"status": "failed", "message": "No playback is running."}
        target.cancel()
        if self._session is target:
            self._session = None
        return {"status": "cancelled", "message": "Stopped the skill playback."}

    def _advance_playback(self, session, answer: str | None) -> dict:
        try:
            if answer is not None:
                session.answer(answer)
            status = session.step_until_blocked()
        except Exception as exc:
            logger.exception("[Skills] Playback engine error")
            self._session = None
            return {"status": "failed",
                    "message": f"Something went wrong during playback: {exc}"}

        if status == "need_input":
            return {"status": "need_input", "message": session.pending_question,
                    "session": session}

        self._session = None
        if status == "done":
            message = (
                f"Done! The {session.name} skill completed in {session.step_index} steps."
            )
            if session.improvement_offer:
                message += (
                    " I noticed a faster way this time (fewer steps than the "
                    "recorded path). Would you like me to improve this skill?"
                )
            return {"status": "done", "message": message, "session": session,
                    "improvement_offer": session.improvement_offer}
        if status == "cancelled":
            return {"status": "cancelled", "message": "Stopped the skill playback.",
                    "session": session}
        return {"status": "failed", "message": session.failure_reason or
                "The skill playback failed.", "session": session}

    def apply_improvement(self, session, accept: bool) -> dict:
        """Phase 4: after a successful playback that found a shorter path,
        rewrite the skill's timeline with the executed sequence."""
        if session is None or not accept:
            return {"success": not accept,
                    "speak": "I'll keep the original skill as is."}
        actual = getattr(session, "_actual_steps", None)
        if not actual or not session.timeline.get("semantic"):
            return {"success": False, "speak": "There was no shorter path to save."}
        storage.save_timeline(session.name, {
            "semantic": [dict(step) for step in actual],
            "raw": session.timeline.get("raw", []),
            "variables": session.timeline.get("variables", []),
            "screen": session.timeline.get("screen", {}),
        })
        meta = storage.load_metadata(session.name)
        history = meta.setdefault("improvement_history", [])
        history.append({
            "at": storage.now_iso(),
            "from_steps": len(session.timeline["semantic"]),
            "to_steps": len(actual),
        })
        meta["improved_at"] = storage.now_iso()
        storage.save_metadata(session.name, meta)
        record = storage.load_skill(session.name)
        if record:
            record["version"] = int(record.get("version", 1)) + 1
            storage.save_skill(record)
        logger.info("[Skills] Improved skill '%s': %d -> %d steps",
                    session.name, len(session.timeline["semantic"]), len(actual))
        return {"success": True,
                "speak": f"Updated the '{session.name}' skill with the faster "
                         f"sequence ({len(actual)} steps instead of "
                         f"{len(session.timeline['semantic'])})."}

    # ------------------------------------------------------------------ #
    # Editing (Phase 2)
    # ------------------------------------------------------------------ #

    def list_skills(self) -> list[dict]:
        return storage.list_skills()

    def rename_skill(self, old_name: str, new_name: str) -> dict:
        if not storage.skill_exists(old_name):
            return {"success": False, "speak": f"I don't have a skill called '{old_name}'."}
        record = storage.rename_skill(old_name, new_name)
        if record is None:
            return {"success": False, "speak": "Could not rename the skill."}
        return {"success": True, "speak": f"Renamed skill '{old_name}' to '{new_name}'."}

    def delete_skill(self, name: str) -> dict:
        if not storage.skill_exists(name):
            return {"success": False, "speak": f"I don't have a skill called '{name}'."}
        if self._session is not None and self._session.name == name:
            self.abort_playback()
        storage.delete_skill(name)
        return {"success": True, "speak": f"Deleted skill '{name}'."}

    def duplicate_skill(self, name: str, new_name: str | None = None) -> dict:
        if not storage.skill_exists(name):
            return {"success": False, "speak": f"I don't have a skill called '{name}'."}
        target = new_name or f"{name} copy"
        record = storage.duplicate_skill(name, target)
        if record is None:
            return {"success": False, "speak": "Could not duplicate the skill."}
        return {"success": True, "speak": f"Duplicated skill '{name}' as '{target}'."}

    def export_skill(self, name: str, destination: str | None = None) -> dict:
        if not storage.skill_exists(name):
            return {"success": False, "speak": f"I don't have a skill called '{name}'."}
        path = storage.export_skill(name, destination)
        if path is None:
            return {"success": False, "speak": "Could not export the skill."}
        return {"success": True, "speak": f"Exported skill '{name}' to {path}.", "path": str(path)}

    def import_skill(self, path: str) -> dict:
        imported = storage.import_skill(path)
        if imported is None:
            return {"success": False, "speak": "Could not import that skill file."}
        return {"success": True, "speak": f"Imported skill '{imported}'."}

    # ------------------------------------------------------------------ #
    # Planning integration
    # ------------------------------------------------------------------ #

    def find_skill(self, request: str) -> dict | None:
        return planner.find_skill(request)

    def auto_play_candidate(self, request: str) -> dict | None:
        """Skill that should run automatically instead of the planner
        rebuilding the workflow ('generate my monthly report')."""
        return planner.auto_play_skill(request)

    def observe_request(self, request: str) -> str | None:
        """Track user requests; returns a learning suggestion when the same
        task repeats often enough (Phase 5)."""
        return planner.observe_request(request)

    def suggest_skills_for(self, request: str, limit: int = 3) -> list[dict]:
        return planner.suggest_skills_for(request, limit)

    # ------------------------------------------------------------------ #
    # LLM plumbing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _make_llm():
        """A chat_json(system, user) callable for the parser, or None.
        Never hits the network under pytest -- unit tests stay offline."""
        import os

        if "PYTEST_CURRENT_TEST" in os.environ:
            return None
        try:
            provider = settings.llm_provider or "ollama"
            if provider == "gemini":
                from llm.gemini_client import GeminiClient

                return GeminiClient().chat_json
            if provider == "openrouter":
                from llm.openrouter_client import OpenRouterClient

                return OpenRouterClient().chat_json
            from llm.ollama_client import OllamaClient

            return OllamaClient(settings.ollama_model).chat_json
        except Exception as exc:
            logger.warning("[Skills] LLM unavailable for parsing: %s", exc)
            return None


skill_manager = SkillManager()
