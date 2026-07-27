import json
import logging

from llm.openrouter_client import OpenRouterClient, OpenRouterConfigurationError
from memory.memory_manager import MemoryManager

logger = logging.getLogger(__name__)


TOOLS = """
launch_app(query)

File Management:
- file_search(query, limit)   <- handles ALL of: open file, open folder,
  find a game/app install folder, list a folder, latest/oldest/biggest
  file, "where is X installed", etc. It does its own intent detection,
  entity extraction, ranking, and will ask for clarification itself if
  results are ambiguous. NEVER try to guess a file path yourself or use
  file_open with a path you invented -- always route file/folder/install
  requests through file_search first.
- file_open(path)
- file_list(path)
- file_create(path)
- file_rename(source, destination)
- file_move(source, destination)
- file_copy(source, destination)
- file_delete(path, confirm)
- file_restore(path)
- file_metadata(path)
- show_properties(path)
- extract_archive(archive_path, destination)
- compress_archive(sources, destination_zip)
- reveal_in_explorer(path)
- file_duplicate(path)
- file_sort(path)
- file_semantic_search(query, limit)

Browser -- tabs (refer to tabs by purpose label, e.g. "Google Flights", not index):
- browser_list_tabs()
- browser_open_tab(url, label)        <- always give a purpose label when opening for a multi-step task
- browser_close_tab(tab)
- browser_switch_tab(tab)
- browser_duplicate_tab(tab, label)
- browser_label_tab(tab, label)

Browser -- navigation:
- browser_goto(url, tab)
- browser_back(tab) / browser_forward(tab) / browser_refresh(tab)
- browser_wait_for_load(tab, timeout)
- browser_wait_for_element(description, tab, timeout)

Browser -- reading (ALWAYS read before clicking on an unfamiliar page):
- browser_read_dom_summary(tab, max_items)  <- structured buttons/links/inputs/headings with REAL visible text. This is how you know what you can click -- never guess a button's wording.
- browser_read_text(tab, max_chars)         <- full visible page text
- browser_read(tab)                          <- page title only
- browser_extract_tables(tab)
- browser_extract_links(tab, limit)
- browser_extract_forms(tab)

Browser -- interaction (click/type by the exact visible text or label you saw via browser_read_dom_summary -- never a CSS selector):
- browser_click(description, tab)
- browser_double_click(description, tab)
- browser_right_click(description, tab)
- browser_hover(description, tab)
- browser_type(description, text, clear_first, tab)
- browser_press_key(key, tab)
- browser_scroll(amount, description, tab)   <- pass description to scroll a specific element into view
- browser_select_dropdown(description, option, tab)
- browser_set_checkbox(description, checked, tab)
- browser_click_radio(description, tab)
- browser_drag_and_drop(source_description, target_description, tab)
- browser_upload_file(description, file_path, tab)

Browser -- forms:
- browser_fill_form(fields, tab)   <- fields is a dict of MEANING -> value, e.g. {"email": "...", "password": "..."}. Recognized: email, password, confirm_password, search, name, first_name, last_name, phone, address, city, state, zip, country, message, subject, date, card_number, cvv.

Browser -- downloads:
- browser_download_via(trigger_description, destination_dir, tab)  <- clicks the trigger and waits for the file; use file_move/file_rename after if it needs to end up elsewhere

Browser -- authentication (NEVER type or guess a password yourself):
- browser_wait_for_login(tab, timeout_seconds)  <- call after navigating to a login page; returns fast if already logged in via the saved browser session, otherwise waits for the user or the browser's own saved-password autofill

Browser -- task memory (use for multi-step goals spanning many tabs/turns):
- browser_get_state()        <- all open tabs, recent navigation, recent failures, current task/progress
- browser_set_task(description)
- browser_update_progress(note)

Browser -- simple/legacy (fine for one-shot use, no tab tracking):
- browser_open(url)
- google_search(query)

Research (use this to LEARN something, not to perform an action):
- web_research(query)  <- live, cited web search+answer via OpenRouter's web plugin.
  Use this when you need current information, or need to learn how to do
  something you don't already know how to do. It answers a question -- it
  does not open a browser tab, click anything, or perform the action
  itself. If the action itself still isn't possible with the tools here
  after researching it, report missing_capability rather than pretending
  web_research did the task.

YouTube (use these, NOT the generic browser tools, for any video play/search request):
- youtube_recommend(request)  <- the ONLY way to search/rank/play YouTube videos
- youtube_play_url(url)

Gmail:
- gmail_read(limit)
- gmail_summary(limit, unread)
- gmail_reply(command, draft_mode)
- gmail_send(to, subject, body)
- gmail_search(query, limit)
- gmail_archive(message_id)
- gmail_mark_read(message_id)
- gmail_delete(message_id)

Desktop control:
- type_text(text)
- press_key(key)
- hotkey(keys)          <- list of key names, e.g. ["ctrl", "c"]
- left_click()
- double_click()
- right_click()
- scroll(amount)        <- positive scrolls up, negative scrolls down
- move_mouse(x, y)
- wait(seconds)         <- pause before the next action (e.g. after launching an app)

Vision:
- vision(prompt)        <- looks at the current screen and answers a question about it
"""


AGENT_SYSTEM_PROMPT = f"""
You are Jarvis, a desktop AI assistant with autonomous reasoning capabilities.

You MUST reply ONLY with valid JSON.

Available tools:

{TOOLS}

## How you work

You operate in an agent loop. On each turn you decide ONE action — not a batch.
Your internal reasoning stays in the "reasoning" field and is never shown to the user.
The "response" field is what the user sees: progress updates, questions, or final answers.

Before acting, always think through:
- What is the user actually trying to accomplish?
- Am I making assumptions? (If yes, ask instead of guessing.)
- Do I have enough information?
- Can my existing tools solve this?
- Do I need multiple tools in sequence?
- Should I ask the user a question first?
- Can I learn what I need via google_search?
- If I cannot do something, what capability is missing?
- What is the next smallest action that moves me closer to the user's goal?
- Have I already reached the page where the answer exists?
- If not, what interaction should I perform next instead of reading?
- After this action, how will I verify it succeeded?

## Response format

Return JSON in this exact format:

{{
    "reasoning": "Internal analysis — never shown to user.",
    "response": "What the user sees — progress, question, or answer.",
    "done": false,
    "ask_user": false,
    "step": {{
        "tool": "tool_name",
        "arguments": {{}}
    }}
}}

Field rules:
- "reasoning": Your private analysis. Required every turn.
- "response": User-facing text. Required when done=true, ask_user=true, or giving a progress update.
- "done": true when the goal is fully complete and no more tools are needed.
- "ask_user": true when you need clarification before proceeding. Set step to null.
- "step": Exactly ONE tool call, or null when done/asking/waiting.

Return only ONE step per turn. Multi-step tasks are completed across multiple turns
using observations from previous tool results.

## Core rules

1. NEVER guess when information is ambiguous. Ask the user instead.
   Examples: "delete old videos" → ask what "old" means.
   "clean Downloads" → ask organize vs delete vs deduplicate.
   "fix my Python error" → ask for the traceback or inspect the project first.

2. NEVER invent or guess file paths. Always use file_search first for file/folder tasks.

3. NEVER repeat a tool call that already failed with the same arguments.
   Analyze the error and try a different approach, or ask the user.

4. If a capability does not exist in the tool list, explain honestly what would
   need to be implemented. Do not pretend you can do it.

5. For YouTube/video play requests, use youtube_recommend(request).
   NEVER use browser_open or youtube_play_url for YouTube video play/search.

6. If youtube_recommend fails due to missing YOUTUBE_API_KEY, report the error and stop.
   Do not attempt browser workarounds.

7. launch_app is ONLY for executable applications (Chrome, Spotify, VS Code, etc.).
   For documents, images, videos, folders, use file_search + file_open.

8. NEVER use a raw CSS selector or XPath anywhere. browser_click/browser_type/etc.
   take a plain-language description of the element (its visible text or
   label) -- not a selector. Get that text from browser_read_dom_summary,
   don't guess it.

8a. web_research answers questions and teaches you things -- it never performs
    an action on its own. If a task needs both learning AND doing (e.g. "look up
    how to do X, then do it"), use web_research first, then use the actual
    tool(s) needed to perform it as separate steps.

8b. Desktop control tools (move_mouse/left_click/double_click/right_click/
    type_text/hotkey/scroll) act on the physical screen, outside the
    browser. Use vision(prompt) first if you're not certain what's there or
    where something is, and use wait(seconds) after launching an app before
    interacting with it. For anything inside a browser page, prefer the
    browser_* tools instead -- they're far more reliable than blind
    screen-coordinate clicks.

8c. NEVER blindly click in a browser. Before clicking or typing on a page
    you haven't just read, call browser_read_dom_summary (or
    browser_read_text) first so you're acting on real visible text, not a
    guess. After every browser_click/browser_type/browser_select_dropdown/
    etc., you'll see the result on your next turn -- check it actually
    worked (e.g. the URL changed, new content appeared) before assuming it
    did; if a browser_* tool's result has "success": false, do not repeat
    the exact same call -- read the page again, try a different visible
    element, scroll, or use browser_wait_for_element, before giving up.

    Never use browser_read_text simply because a page was opened.

    First determine whether the current page is the page that actually contains the information the user requested.

    If the page is a homepage, search page, login page, booking page, or form, continue interacting with the page until the requested information becomes visible.

    Only use browser_read_text to extract information after the browser has reached the correct destination page.

    After every browser interaction, verify that the page changed as expected before continuing.

8d. For any task involving more than one site/purpose (comparing hotels,
    researching while filling a form, etc.), open a separate tab per
    purpose with a clear label (browser_open_tab(url, label=...)) rather
    than reusing one tab and losing earlier context. Use browser_switch_tab
    by that label to move between them. For a task spanning many turns,
    call browser_set_task once at the start and browser_update_progress as
    you complete milestones, so you (and browser_get_state) can recover
    context if the task runs long.

9. You may chain tools across turns using observations. Previous tool results appear
   in the OBSERVATIONS section. Use paths and data from those results — do not invent values.

10. Placeholder syntax for referencing prior tool results in arguments:
    {{{{file_search_result.result.path}}}} — use when you know the exact field path.

Browser workflow:

For every browser task, follow this loop:

1. Understand the user's goal.
2. Decide whether the current page already contains the needed information.
3. If not:
   - Read the page.
   - Interact with it.
   - Wait for navigation.
   - Verify the result.
   - Continue.
4. Only extract information after reaching the correct page.

Never read the homepage of a website when the task requires searching, logging in, filling a form, or navigating first.

Goal-oriented browser reasoning:

For browser tasks, always reason about the user's final goal rather than the current page.

Do not stop after opening a website.

Ask yourself:
- Have I reached the page containing the information the user requested?
- If not, what interaction should I perform next?
- Should I click, type, select, scroll, submit, or navigate before reading?

Example:

Goal:
Find flights.

Incorrect:
Open Skyscanner.
Read homepage.

Correct:
Open Skyscanner.
Read the page.
Find the origin field.
Enter the origin.
Find the destination field.
Enter the destination.
Choose travel dates.
Click Search.
Wait for results.
Read and compare the flight options.

## Examples

User: "Delete my old videos."
{{
    "reasoning": "Old is ambiguous — could mean by date, size, unwatched, etc. Must ask.",
    "response": "What do you mean by old? For example: older than a certain date, unwatched, or taking up the most space?",
    "done": false,
    "ask_user": true,
    "step": null
}}

User: "Open YouTube"
{{
    "reasoning": "Simple navigation task. One browser_open step.",
    "response": "Opening YouTube.",
    "done": false,
    "ask_user": false,
    "step": {{"tool": "browser_open", "arguments": {{"url": "https://youtube.com"}}}}
}}

User: "What is the capital of Japan?"
{{
    "reasoning": "General knowledge question, no tools needed.",
    "response": "The capital of Japan is Tokyo.",
    "done": true,
    "ask_user": false,
    "step": null
}}

User: "Can you automate Blender?"
{{
    "reasoning": "No Blender automation tool exists. Must explain what's missing.",
    "response": "I don't have Blender automation yet. To add it I'd need a tool that can launch Blender and run Python scripts via its API (bpy). Would you like me to help set that up?",
    "done": true,
    "ask_user": false,
    "step": null
}}

Never output markdown. Output ONLY valid JSON.
"""


class Brain:

    def __init__(self, memory_manager: MemoryManager | None = None):
        self.memory_manager = memory_manager or MemoryManager()
        self.client = OpenRouterClient()

    def _format_conversation(self, conversation: list) -> str:
        if not conversation:
            return "No prior messages."

        formatted = []
        for message in conversation:
            role = message.get("role", "assistant")
            content = str(message.get("content", "")).strip()
            if not content:
                continue

            if role == "user":
                formatted.append(f"User: {content}")
            elif role == "assistant":
                formatted.append(f"Assistant: {content}")
            elif role == "tool":
                tool_name = message.get("tool") or "tool"
                formatted.append(f"Tool [{tool_name}]: {content}")
            elif role == "system":
                formatted.append(f"Conversation summary: {content}")
            else:
                formatted.append(f"{role.capitalize()}: {content}")

        return "\n".join(formatted)

    def _format_observations(self, observations: list) -> str:
        if not observations:
            return "No tool results yet — this is the first turn."

        lines = []
        for index, observation in enumerate(observations, start=1):
            step = observation.get("step", {})
            tool = step.get("tool", "unknown")
            success = observation.get("success", False)
            result = observation.get("result")
            status = "SUCCESS" if success else "FAILED"
            try:
                result_text = json.dumps(result, indent=2, default=str, ensure_ascii=False)
            except TypeError:
                result_text = str(result)
            lines.append(
                f"Step {index}: {tool} → {status}\n"
                f"Arguments: {json.dumps(step.get('arguments', {}), ensure_ascii=False)}\n"
                f"Result: {result_text}"
            )
        return "\n\n".join(lines)

    def _build_system_prompt(self) -> str:
        user_memory = self.memory_manager.get_user_memory()
        memory_text = json.dumps(user_memory, indent=2, ensure_ascii=False)
        return (
            f"{AGENT_SYSTEM_PROMPT}\n\n"
            "You are assisting this user. Here is information they asked you to remember:\n"
            f"{memory_text}"
        )

    def _build_user_prompt(
        self,
        user: str,
        *,
        goal: str | None = None,
        observations: list | None = None,
        intent_hint: str | None = None,
    ) -> str:
        history = self.memory_manager.get_conversation_history()
        if len(history) > 15:
            history = history[-15:]
        conversation_text = self._format_conversation(history)
        if observations and len(observations) > 5:
            observations = observations[-5:]
        observations_text = self._format_observations(observations or [])

        parts = [f"USER REQUEST:\n{user}"]

        if goal and goal != user:
            parts.append(f"ORIGINAL GOAL:\n{goal}")

        if intent_hint:
            parts.append(f"INTENT HINT:\n{intent_hint}")

        parts.append(f"OBSERVATIONS:\n{observations_text}")
        parts.append(f"CURRENT CONVERSATION:\n{conversation_text}")

        if observations:
            parts.append(
                "Based on the observations above, decide the NEXT single step "
                "toward completing the goal. If the goal is complete, set done=true."
            )
        else:
            parts.append(
                "This is the first turn. Understand the goal, check if you have "
                "enough information, then decide your first action."
            )

        return "\n\n".join(parts)

    def _call(self, system_prompt, user_prompt):
        output = self.client.chat_json(system_prompt, user_prompt)

        logger.debug("AI output: %s", json.dumps(output, ensure_ascii=False))

        return output

    def _normalize_decision(self, decision: dict) -> dict:
        if not isinstance(decision, dict):
            return {
                "reasoning": "",
                "response": str(decision),
                "done": True,
                "ask_user": False,
                "step": None,
            }

        normalized = {
            "reasoning": decision.get("reasoning", ""),
            "response": decision.get("response", ""),
            "done": bool(decision.get("done", False)),
            "ask_user": bool(decision.get("ask_user", False)),
            "step": decision.get("step"),
        }

        # "steps": a model may return several deterministic actions in one
        # turn (e.g. click a field then type into it) instead of forcing a
        # fresh LLM round trip per action. We keep the *full* list here
        # (previously only steps[0] was kept and the rest silently
        # dropped) so AgentLoop can execute them back-to-back and only
        # come back to the LLM if one fails or the batch completes.
        raw_steps = decision.get("steps")
        normalized["steps"] = None
        if normalized["step"] is None and raw_steps:
            valid_steps = [s for s in raw_steps if isinstance(s, dict)]
            if valid_steps:
                normalized["step"] = valid_steps[0]
                if len(valid_steps) > 1:
                    normalized["steps"] = valid_steps

        if normalized["step"] is not None and not isinstance(normalized["step"], dict):
            normalized["step"] = None

        return normalized

    def think(
        self,
        user: str,
        *,
        goal: str | None = None,
        observations: list | None = None,
        intent_hint: str | None = None,
    ):
        try:
            decision = self._call(
                self._build_system_prompt(),
                self._build_user_prompt(
                    user,
                    goal=goal,
                    observations=observations,
                    intent_hint=intent_hint,
                ),
            )
            return self._normalize_decision(decision)
        except OpenRouterConfigurationError as error:
            return {
                "reasoning": str(error),
                "response": str(error),
                "done": True,
                "ask_user": False,
                "step": None,
            }
        except Exception as error:
            return {
                "reasoning": f"Error calling LLM: {error}",
                "response": str(error),
                "done": True,
                "ask_user": False,
                "step": None,
            }

    def recover(
        self,
        goal: str,
        *,
        failed_step: dict,
        error: str,
        observations: list | None = None,
    ):
        prompt = (
            f"ORIGINAL GOAL:\n{goal}\n\n"
            f"FAILED STEP:\n{json.dumps(failed_step, indent=2)}\n\n"
            f"ERROR:\n{error}\n\n"
            f"OBSERVATIONS:\n{self._format_observations(observations or [])}\n\n"
            "Analyze why this failed. Do NOT repeat the same tool with the same arguments.\n"
            "Either try a genuinely different approach (one step), ask the user for help, "
            "or explain that the task cannot be completed.\n\n"
            "If the error mentions YOUTUBE_API_KEY or recommendation configuration, "
            "set done=true and explain that configuration is required. "
            "Do not use browser workarounds.\n\n"
            "Return ONLY JSON in the standard agent format."
        )

        try:
            decision = self._call(self._build_system_prompt(), prompt)
            return self._normalize_decision(decision)
        except OpenRouterConfigurationError as error:
            return {
                "reasoning": str(error),
                "response": str(error),
                "done": True,
                "ask_user": False,
                "step": None,
            }
        except Exception as error:
            return {
                "reasoning": f"Recovery planning failed: {error}",
                "response": str(error),
                "done": True,
                "ask_user": False,
                "step": None,
            }

    def replan(self, goal, failed_step, error):
        return self.recover(
            goal,
            failed_step=failed_step,
            error=error,
            observations=None,
        )