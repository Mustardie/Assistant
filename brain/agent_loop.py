import json
import logging

from config.settings import settings
from recommendation.errors import RecommendationConfigurationError
from tools.tool_registry import clear_tool_context, run_tool

logger = logging.getLogger(__name__)


def safe_print(value):
    try:
        print(value)
    except UnicodeEncodeError:
        print(str(value).encode("utf-8", errors="replace").decode("utf-8"))


def _tool_reported_success(raw_success: bool, result) -> bool:
    """run_tool()'s own bool only reflects whether the tool function raised.
    Most tools in this codebase (file ops, browser interactions, etc.)
    instead report failure by returning {"success": False, "error": ...}
    without raising -- if that's ignored, the loop thinks every such step
    worked, and recovery/repeat-failure detection never fires. Check the
    tool's own reported outcome too."""
    if not raw_success:
        return False
    if isinstance(result, dict) and "success" in result:
        return bool(result["success"])
    return True


class AgentLoop:
    """Iterative agent loop: plan → execute one step → observe → reflect → repeat."""

    def __init__(
        self,
        brain,
        *,
        speak,
        record_tool,
        on_file_search_result=None,
        track_file_action=None,
        fallback=None,
    ):
        self.brain = brain
        self.speak = speak
        self.record_tool = record_tool
        self.on_file_search_result = on_file_search_result
        self.track_file_action = track_file_action
        self.fallback = fallback

    def run(self, user: str, intent_hint: str | None = None) -> None:
        clear_tool_context()

        goal = user
        observations: list[dict] = []
        recovery_attempts = 0
        last_failed_signature: tuple | None = None
        repeat_failures = 0

        for iteration in range(1, settings.agent_max_iterations + 1):
            logger.info("Agent loop iteration %s for goal: %s", iteration, goal[:80])

            decision = self.brain.think(
                user,
                goal=goal,
                observations=observations,
                intent_hint=intent_hint,
            )

            self._log_reasoning(decision.get("reasoning"))

            response = decision.get("response")
            if response:
                self.speak(response)

            if decision.get("done"):
                return

            if decision.get("ask_user"):
                return

            step = decision.get("step")

            if not step:
                if not observations and self.fallback and self.fallback(user, intent_hint):
                    return

                if observations:
                    if response:
                        continue

                    self.speak("I'm not sure what to do next. Could you clarify what you'd like?")
                    return

                if not response:
                    self.speak(
                        "I wasn't able to figure out a next step for that. "
                        "Could you rephrase or give me a bit more detail?"
                    )

                return

            # A decision may bundle several deterministic actions into one
            # turn (decision["steps"], e.g. "click field" + "type value")
            # instead of forcing a fresh LLM call per action. Run them
            # back-to-back here; stop at the first failure (later queued
            # steps assumed the prior one would succeed) and fall through
            # to the existing single-step failure/recovery handling below,
            # acting on whichever step actually failed -- for a single
            # step turn this is exactly the old behavior.
            batch = decision.get("steps") or [step]

            tool = step.get("tool")
            arguments = step.get("arguments", {})
            step_signature = (tool, json.dumps(arguments, sort_keys=True, default=str))
            success = True
            result = None

            for batch_step in batch:
                tool = batch_step.get("tool")
                arguments = batch_step.get("arguments", {})
                step_signature = (tool, json.dumps(arguments, sort_keys=True, default=str))

                try:
                    outcome = self._execute_tool_step(user, batch_step)
                except RecommendationConfigurationError as error:
                    self.speak(str(error))
                    safe_print(f"\n[{tool}]")
                    safe_print(error)
                    return

                if outcome["stop"]:
                    return

                success = outcome["success"]
                result = outcome["result"]

                observations.append({
                    "step": batch_step,
                    "success": success,
                    "result": result,
                })

                if not success:
                    step = batch_step
                    break

            if success:
                recovery_attempts = 0
                repeat_failures = 0
                last_failed_signature = None
                continue

            if step_signature == last_failed_signature:
                repeat_failures += 1
            else:
                repeat_failures = 1
                last_failed_signature = step_signature

            if repeat_failures >= 2:
                self.speak(
                    "That approach isn't working. I'll stop repeating it — "
                    "can you give me more details or suggest a different approach?"
                )
                return

            recovery_attempts += 1
            if recovery_attempts > settings.agent_max_recovery_attempts:
                self.speak(
                    "I've tried several approaches without success. "
                    "Could you provide more information or rephrase what you need?"
                )
                return

            recovery = self.brain.recover(
                goal,
                failed_step=step,
                error=str(result),
                observations=observations,
            )
            self._log_reasoning(recovery.get("reasoning"))

            if recovery.get("response"):
                self.speak(recovery["response"])

            if recovery.get("done"):
                return

            if recovery.get("ask_user"):
                return

            recovery_step = recovery.get("step")
            if not recovery_step:
                continue

            recovery_tool = recovery_step.get("tool")
            recovery_args = recovery_step.get("arguments", {})
            recovery_signature = (recovery_tool, json.dumps(recovery_args, sort_keys=True, default=str))

            if recovery_signature == step_signature:
                self.speak(
                    "The recovery plan would repeat the same failing action. "
                    "I need a different approach or more information from you."
                )
                return

            try:
                raw_recovery_success, recovery_result = run_tool(recovery_tool, recovery_args)
            except RecommendationConfigurationError as error:
                self.speak(str(error))
                safe_print(f"\n[{recovery_tool}]")
                safe_print(error)
                return

            recovery_success = _tool_reported_success(raw_recovery_success, recovery_result)

            if recovery_success and self.track_file_action and recovery_tool in {"file_rename", "file_move"}:
                self.track_file_action(recovery_tool, recovery_args)

            self.record_tool(recovery_tool, recovery_result)
            safe_print(f"\n[{recovery_tool}]")
            safe_print(recovery_result)

            if recovery_tool == "file_search" and isinstance(recovery_result, dict):
                handled = self._handle_file_search(user, recovery_result)
                if handled:
                    return

            observations.append({
                "step": recovery_step,
                "success": recovery_success,
                "result": recovery_result,
            })

            if recovery_success:
                recovery_attempts = 0
                repeat_failures = 0
                last_failed_signature = None
            else:
                last_failed_signature = recovery_signature
                repeat_failures = 1

        self.speak(
            "I've reached my step limit for this task. "
            "Let me know if you'd like me to continue."
        )

    def _execute_tool_step(self, user: str, step: dict) -> dict:
        """Runs one tool step and does the same bookkeeping every step in
        this codebase already relied on (file-action tracking, recording,
        printing, file_search dispatch) -- factored out so a batch of
        steps and a single step go through identical logic. Returns
        {"success", "result", "stop"}; stop=True means run() should end
        immediately (e.g. file_search was fully handled), matching the
        original single-step behavior. May raise
        RecommendationConfigurationError; callers handle it the same way
        the old single-step code did."""
        tool = step.get("tool")
        arguments = step.get("arguments", {})

        raw_success, result = run_tool(tool, arguments)
        success = _tool_reported_success(raw_success, result)

        if success and self.track_file_action and tool in {"file_rename", "file_move"}:
            self.track_file_action(tool, arguments)

        self.record_tool(tool, result)
        safe_print(f"\n[{tool}]")
        safe_print(result)

        stop = False
        if tool == "file_search" and isinstance(result, dict):
            stop = bool(self._handle_file_search(user, result))

        return {"success": success, "result": result, "stop": stop}

    def _handle_file_search(self, user: str, result: dict) -> bool:
        if not self.on_file_search_result:
            return False
        return self.on_file_search_result(user, result)

    @staticmethod
    def _log_reasoning(reasoning: str | None) -> None:
        if not reasoning:
            return
        logger.info("Agent reasoning: %s", reasoning[:500])
        safe_print("\n[agent reasoning]")
        safe_print(reasoning)