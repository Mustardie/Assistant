import json
import logging
import re

from config.settings import settings
from recommendation.errors import RecommendationConfigurationError
from tools.tool_registry import clear_tool_context, run_tool

logger = logging.getLogger(__name__)


# Root-cause guard for the "keeps saying it's about to do something and
# never does it" failure mode.
#
# Nothing in the JSON contract stops the LLM from returning
# done=true/ask_user=true, step=null, and a "response" that narrates an
# action in first-person future tense ("I will now open Google Flights...")
# instead of actually returning that action as `step`. Once the harness
# sees done/ask_user=true it has always trusted it unconditionally and
# returned control to the user immediately -- so the narrated action never
# runs, and because the very next user turn (e.g. "yes") starts a brand
# new, independent call with no shared plan, the same non-answer can come
# back out of the model again, forever.
#
# This is a generic, domain-agnostic linguistic safety net (it does not
# reference flights, browsers, or any specific tool) that catches exactly
# that self-contradiction -- "done/ask_user" plus "no step" plus "I'm
# about to do X" phrasing -- and refuses to accept it as a valid turn.
# The primary fix is the system-prompt rule (see brain.py) telling the
# model to act instead of narrating; this is the enforcement backstop for
# when it doesn't listen.
_UNEXECUTED_ACTION_PATTERN = re.compile(
    r"\bi(?:'ll| will)\b[^.!?]{0,60}\b(now|proceed|go ahead|open|start|search|"
    r"navigate|book|check|launch|fill|submit)\b"
    r"|\bi(?:'m| am) going to\b"
    r"|\blet me (?:now )?(?:go ahead and |proceed to )?"
    r"(open|search|navigate|book|check|launch|fill|submit|proceed)\b"
    r"|\bi will now\b|\bi'll now\b|\babout to\b|\bgoing to now\b",
    re.IGNORECASE,
)


def _describes_unexecuted_action(response: str | None) -> bool:
    if not response:
        return False
    return bool(_UNEXECUTED_ACTION_PATTERN.search(response))


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
        self._last_response = ""

    def run(
        self,
        user: str,
        intent_hint: str | None = None,
        *,
        resume_goal: str | None = None,
        resume_observations: list[dict] | None = None,
    ) -> dict | None:
        """Runs the plan -> execute -> observe loop for one user turn.

        Returns None when the task reached a true terminal state (goal
        completed, or the caller should treat this as fully finished).
        Returns {"goal": ..., "observations": [...]} when the loop paused
        mid-task (a genuine clarifying question, a recovery dead-end, or
        the iteration budget ran out) -- the caller should pass this
        straight back in as resume_goal/resume_observations on the next
        call so the user's reply continues the SAME task instead of
        starting an unrelated new one.
        """
        clear_tool_context()

        logger.info("[Agent] Received user request: %s", user[:200])

        goal = resume_goal or user
        observations: list[dict] = list(resume_observations) if resume_observations else []
        recovery_attempts = 0
        last_failed_signature: tuple | None = None
        repeat_failures = 0
        self._last_response = ""

        if resume_goal:
            logger.info(
                "[Agent] Resuming pending task (%d prior observation(s)): %s",
                len(observations), goal[:120],
            )

        def _pending_result() -> dict:
            return {"goal": goal, "observations": list(observations)}

        from tools.recipe_manager import recipe_manager
        matched_recipe = recipe_manager.find_match(goal)
        if matched_recipe:
            logger.info("Found matching recipe: %s", matched_recipe.get("goal"))

        for iteration in range(1, settings.agent_max_iterations + 1):
            logger.info("Agent loop iteration %s for goal: %s", iteration, goal[:80])

            try:
                decision = self.brain.think(
                    user,
                    goal=goal,
                    observations=observations,
                    intent_hint=intent_hint,
                    recipe=matched_recipe if iteration == 1 else None,
                )
            except json.JSONDecodeError:
                logger.warning("LLM returned invalid JSON on iteration %s; treating as empty decision", iteration)
                decision = {"reasoning": "", "response": None, "done": False, "step": None}
            except Exception as llm_err:
                err_msg = str(llm_err)
                logger.error("LLM call failed on iteration %s: %s", iteration, err_msg)
                decision = {
                    "reasoning": err_msg,
                    "response": f"The language model is taking too long. Let me try again.",
                    "done": False,
                    "step": None,
                }

            self._log_reasoning(decision.get("reasoning"))
            logger.info("[Planner] Intent detected for iteration %s", iteration)

            response = decision.get("response")
            step = decision.get("step")
            claims_terminal = bool(decision.get("done") or decision.get("ask_user"))

            # Guard against the "I will now proceed..." trap: the model
            # claims the turn is over (done or ask_user) but returned no
            # step and its own response describes an action it hasn't
            # taken. That combination is never valid -- if it were really
            # done, there'd be nothing left to narrate; if it really needed
            # to ask the user something, it wouldn't be pre-announcing an
            # action. Reject it and force a real decision instead of
            # handing this non-answer to the user.
            if claims_terminal and not step and _describes_unexecuted_action(response):
                logger.warning(
                    "[Planner] Rejected decision on iteration %s: claimed %s with no "
                    "step, but response narrates an unexecuted action: %r",
                    iteration,
                    "done" if decision.get("done") else "ask_user",
                    (response or "")[:160],
                )
                observations.append({
                    "step": {"tool": "_planner_guard", "arguments": {}},
                    "success": False,
                    "result": (
                        "You said you would perform an action but did not include it as "
                        "`step`, and you marked the turn done/ask_user instead of acting. "
                        "Ordinary, non-destructive actions (opening a site, searching, "
                        "navigating, filling a form, etc.) do NOT need user confirmation -- "
                        "the original request already is the permission. Return the actual "
                        "tool call in `step` now. Only use ask_user if information genuinely "
                        "required to proceed is missing or ambiguous, and only require "
                        "confirmation for destructive/sensitive actions (deleting data, "
                        "sending messages/emails, purchases, shutdown/restart)."
                    ),
                })
                continue

            # Deduplicate: skip speaking if this response is identical to the last one spoken.
            if response:
                if response == self._last_response:
                    logger.info("[TTS] Duplicate response prevented at loop level: %s", response[:60])
                else:
                    self._last_response = response
                    logger.info("[Agent] Responding to user")
                    self.speak(response)

            if decision.get("done"):
                logger.info("[Agent] Goal marked complete on iteration %s", iteration)
                return None

            if decision.get("ask_user"):
                logger.info("[Agent] Awaiting user clarification on iteration %s; task pending", iteration)
                return _pending_result()

            if not step:
                # LLM returned no tool step — if it's the first turn and
                # the fallback catches it (youtube/file), dispatch there.
                if not observations and self.fallback and self.fallback(user, intent_hint):
                    return None

                # If we have observations (tools were called) but the LLM
                # still returned no step and no response, retry once.
                if observations and not response:
                    logger.warning("LLM returned no step on iteration %s; retrying", iteration)
                    continue

                # LLM returned a chat response but no tool step.
                # Instead of silently returning, retry with stronger
                # prompting so it actually does something.
                if response:
                    logger.warning(
                        "LLM returned response without step on iteration %s; "
                        "retrying with stronger instruction", iteration,
                    )
                    observations.append({
                        "step": {"tool": "_llm_hint", "arguments": {}},
                        "success": False,
                        "result": (
                            "You returned a response without a tool step. "
                            "You MUST return a valid step with tool + arguments. "
                            "Do NOT ask the user for information already in the goal."
                        ),
                    })
                    continue

                logger.info("[Agent] Responding to user")
                self.speak(
                    "I wasn't able to figure out a next step for that. "
                    "Could you rephrase or give me a bit more detail?"
                )
                return _pending_result()

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

            logger.info(
                "[Planner] Selected tool: %s%s",
                tool, " (+%d more queued)" % (len(batch) - 1) if len(batch) > 1 else "",
            )

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
                    return None

                if outcome["stop"]:
                    return None

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
                return _pending_result()

            recovery_attempts += 1
            if recovery_attempts > settings.agent_max_recovery_attempts:
                self.speak(
                    "I've tried several approaches without success. "
                    "Could you provide more information or rephrase what you need?"
                )
                return _pending_result()

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
                return None

            if recovery.get("ask_user"):
                return _pending_result()

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
                return _pending_result()

            try:
                raw_recovery_success, recovery_result = run_tool(recovery_tool, recovery_args)
            except RecommendationConfigurationError as error:
                self.speak(str(error))
                safe_print(f"\n[{recovery_tool}]")
                safe_print(error)
                return None

            recovery_success = _tool_reported_success(raw_recovery_success, recovery_result)

            if recovery_success and self.track_file_action and recovery_tool in {"file_rename", "file_move"}:
                self.track_file_action(recovery_tool, recovery_args)

            self.record_tool(recovery_tool, recovery_result)
            safe_print(f"\n[{recovery_tool}]")
            safe_print(recovery_result)

            if recovery_tool == "file_search" and isinstance(recovery_result, dict):
                handled = self._handle_file_search(user, recovery_result)
                if handled:
                    return None

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

        logger.info("[Agent] Step limit (%s) reached without completing the goal", settings.agent_max_iterations)
        self.speak(
            "I've reached my step limit for this task. "
            "Let me know if you'd like me to continue."
        )
        return _pending_result()

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
        elif tool == "youtube_recommend" and success:
            logger.info("youtube_recommend succeeded; terminating loop (video is playing)")
            stop = True

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