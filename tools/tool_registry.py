import copy
import inspect
import logging
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from recommendation.errors import RecommendationConfigurationError
from tools.app_launcher import launch_app
from tools.browser_tool import browser
from tools.browser.browser_agent import browser_agent
from skills.gmail import gmail
from skills.youtube import youtube
from tools.computer_tool import (
    type_text,
    press_key,
    hotkey as _hotkey_variadic,
    left_click,
    double_click,
    right_click,
    scroll,
    move_mouse,
    wait,
)
from tools.web_research import web_research
from tools.research_tool import research_topic
from tools.file_ops import (
    create_folder,
    delete_folder,
    copy_file,
    move_file,
    rename_file,
    delete_file,
    duplicate_file as file_ops_duplicate,
    get_file_metadata as file_ops_metadata,
    zip_archive,
    unzip_archive,
    tar_archive,
    untar_archive,
    extract_archive as file_ops_extract,
    search_files,
)
from tools.screenshot import capture_screenshot as capture_screenshot_tool
from premiere.tool import (
    premiere_capabilities,
    premiere_edit,
    premiere_inspect,
    premiere_status,
    premiere_undo,
)

logger = logging.getLogger(__name__)


class _LazyVision:
    _instance = None

    def __getattr__(self, name):
        if self._instance is None:
            from tools.vision import Vision
            type(self)._instance = Vision()
        return getattr(self._instance, name)


class _LazyFileManagerService:
    _instance = None

    def __getattr__(self, name):
        if self._instance is None:
            from file_manager.service import SemanticFileManagerService
            type(self)._instance = SemanticFileManagerService()
        return getattr(self._instance, name)


vision = _LazyVision()
file_manager_service = _LazyFileManagerService()


def vision_tool(prompt):
    from tools.screenshot_tool import capture_frame
    frame = capture_frame()
    return vision.look(frame, prompt)


# ---------------- Browser Wrappers ---------------- #

def _require(value, name, example=""):
    if not value:
        hint = f" (e.g. '{example}')" if example else ""
        return {"success": False, "error": f"Missing argument '{name}'. Provide a value for '{name}'{hint}."}
    return None


def browser_open(url=None):
    """Open the given URL in a NEW browser tab. Never overwrites the
    current tab. For simple 'open X' tasks this is often the only step
    needed -- after it succeeds the goal is typically complete."""
    err = _require(url, "url")
    if err:
        return err
    return browser.browser_open(url)


def youtube_recommend(request=None):
    err = _require(request, "request", "play some music")
    if err:
        return err
    return youtube.recommend(request)


def youtube_play_url(url=None):
    err = _require(url, "url", "https://youtube.com/watch?v=...")
    if err:
        return err
    return youtube.play_url(url)


def gmail_read(limit=10):
    return gmail.read(limit=limit)


def gmail_summary(limit=10, unread=False):
    if unread:
        return gmail.summarize_unread(limit=limit)
    emails = gmail.read(limit=limit)
    return gmail.summarize(emails)


def gmail_reply(command, draft_mode=True, confirm=False):
    return gmail.reply(command, draft_mode=draft_mode, confirm=confirm)


def gmail_send(to, subject, body, confirm=False):
    return gmail.send(to, subject, body, confirm=confirm)


def gmail_search(query, limit=10):
    return gmail.search(query, limit=limit)


def gmail_archive(message_id):
    return gmail.archive(message_id)


def gmail_mark_read(message_id):
    return gmail.mark_read(message_id)


def gmail_delete(message_id):
    return gmail.delete(message_id)


def google_search(query=None):
    err = _require(query, "query", "flights from Hyderabad to Delhi")
    if err:
        return err
    return browser.google_search(query)


def browser_read(tab=None, url=None, max_chars=4000):
    """Reads the CURRENT page: returns {"title", "url", "text", "truncated"}.

    `text` is the page's visible text, truncated to max_chars characters
    (default 4000; `truncated` is true when the page was longer).

    There is no 'url' argument -- the page to read is whichever tab is
    active after the last tab action. If the target page is in another
    tab, browser_switch_tab(tab) to it first.

    ('url' is accepted and ignored for compatibility with older planner
    output that wrongly passed it -- it can NOT select a page to read.)
    """
    if url is not None:
        logger.warning(
            "[Browser] browser_read() ignored an unexpected 'url' argument -- "
            "it reads the current page; switch tabs first if needed"
        )
    return browser.read_text(tab=tab, max_chars=max_chars)


def browser_read_text(tab=None, max_chars=4000):
    """Returns {"title", "url", "text", "truncated"} for the current page --
    use after browser_open/google_search when you actually need the page's
    content, not just its title."""
    return browser.read_text(tab=tab, max_chars=max_chars)


# ---------------- Browser agent: tabs ---------------- #

def browser_list_tabs():
    """Every open tab with its purpose label, url, title -- use this to see
    what's already open before deciding whether to open a new tab or switch
    to one that already serves the purpose you need."""
    return browser_agent.list_tabs()


def browser_open_tab(url=None, label=None):
    """Opens a new tab (optionally navigating it to url) and makes it
    current. Give it a purpose label (e.g. "Google Flights") so it can be
    referred to by that later instead of by index."""
    return browser_agent.open_tab(url=url, label=label)


def browser_close_tab(tab=None):
    """tab: a purpose label, tab id, or omit for the current tab."""
    return browser_agent.close_tab(tab=tab)


def browser_switch_tab(tab):
    """tab: a purpose label (e.g. "Booking.com") or tab id."""
    return browser_agent.switch_tab(tab)


def browser_duplicate_tab(tab=None, label=None):
    return browser_agent.duplicate_tab(tab=tab, label=label)


def browser_label_tab(tab, label):
    """Rename a tab's purpose label, e.g. after navigating somewhere new."""
    return browser_agent.label_tab(tab, label)


# ---------------- Browser agent: navigation ---------------- #

def browser_goto(url=None, tab=None):
    """Open URL in a NEW tab. Always creates a new tab -- never
    overwrites the current page. Prefer this over browser_navigate
    unless you explicitly need to replace the current tab."""
    if not url:
        return {"success": False, "error": "Missing argument 'url'. Provide the URL to navigate to (e.g. 'https://flights.google.com')."}
    return browser_agent.goto(url, tab=tab)


def browser_navigate(url=None, tab=None):
    """Navigate the CURRENT tab to the given URL (replaces the current
    page). Unlike browser_goto, this does NOT create a new tab. Use
    only when you explicitly want to replace what's in the current tab
    (e.g. after browser_back, to navigate somewhere new from the
    current page)."""
    if not url:
        return {"success": False, "error": "Missing argument 'url'. Provide the URL to navigate to (e.g. 'https://flights.google.com')."}
    return browser_agent.navigate(url, tab=tab)


def browser_get_browser_state():
    """Lightweight snapshot of the current browser: active tab URL/title,
    all open tabs, focused window, loading status. Uses the browser's
    tab/window API directly (no page DOM injection). Call this to
    quickly check what's open without reading a page."""
    return browser_agent.get_browser_state()


def browser_back(tab=None):
    return browser_agent.back(tab=tab)


def browser_forward(tab=None):
    return browser_agent.forward(tab=tab)


def browser_refresh(tab=None):
    return browser_agent.refresh(tab=tab)


def browser_wait_for_load(tab=None, timeout=15000):
    return browser_agent.wait_for_load(tab=tab, timeout=timeout)


def browser_wait_for_element(description, tab=None, timeout=15000):
    return browser_agent.wait_for_element(description, tab=tab, timeout=timeout)


# ---------------- Browser agent: reading ---------------- #

def browser_read_dom_summary(tab=None, max_items=40):
    """Structured list of what's actually on the page right now: headings,
    buttons, links, inputs, dropdowns, with their real visible text/labels.
    ALWAYS call this (or browser_read_text) before clicking or typing on an
    unfamiliar page -- click/type by the exact text you see here, don't
    guess a description."""
    return browser_agent.read_dom_summary(tab=tab, max_items=max_items)


def browser_extract_tables(tab=None):
    return browser_agent.extract_tables(tab=tab)


def browser_extract_links(tab=None, limit=50):
    return browser_agent.extract_links(tab=tab, limit=limit)


def browser_extract_forms(tab=None):
    return browser_agent.extract_forms(tab=tab)


# ---------------- Browser agent: interaction ---------------- #

def _require_description(description, action):
    if not description:
        return {"success": False, "error": f"Missing argument 'description' for {action}. Provide the visible text label of the element (e.g. 'Search button')."}
    return None


def browser_click(description=None, tab=None):
    err = _require_description(description, "browser_click")
    if err:
        return err
    return browser_agent.click(description, tab=tab)


def browser_double_click(description=None, tab=None):
    err = _require_description(description, "browser_double_click")
    if err:
        return err
    return browser_agent.double_click(description, tab=tab)


def browser_right_click(description=None, tab=None):
    err = _require_description(description, "browser_right_click")
    if err:
        return err
    return browser_agent.right_click(description, tab=tab)


def browser_hover(description=None, tab=None):
    err = _require_description(description, "browser_hover")
    if err:
        return err
    return browser_agent.hover(description, tab=tab)


def browser_type(description=None, text=None, clear_first=True, press_enter=False, tab=None):
    if not description:
        return {"success": False, "error": "Missing argument 'description'. Provide the visible text label of the input field (e.g. 'Where from?')."}
    if not text:
        return {"success": False, "error": "Missing argument 'text'. Provide the text to type into the field."}
    return browser_agent.type_text(description, text, clear_first=clear_first, press_enter=press_enter, tab=tab)


def browser_press_key(key, tab=None):
    return browser_agent.press_key(key, tab=tab)


def browser_scroll(amount=600, description=None, tab=None):
    """Positive amount scrolls down. If description is given, scrolls that
    element into view instead of scrolling by a fixed amount."""
    return browser_agent.scroll(amount=amount, description=description, tab=tab)


def browser_select_dropdown(description, option, tab=None):
    return browser_agent.select_dropdown(description, option, tab=tab)


def browser_set_checkbox(description, checked=True, tab=None):
    return browser_agent.set_checkbox(description, checked=checked, tab=tab)


def browser_click_radio(description, tab=None):
    return browser_agent.click_radio(description, tab=tab)


def browser_drag_and_drop(source_description, target_description, tab=None):
    return browser_agent.drag_and_drop(source_description, target_description, tab=tab)


def browser_upload_file(description, file_path, tab=None):
    return browser_agent.upload_file(description, file_path, tab=tab)


# ---------------- Browser agent: forms ---------------- #

def browser_fill_form(fields, tab=None):
    """fields: a dict of semantic field name -> value, e.g.
    {"email": "me@example.com", "password": "..."}. Recognized semantic
    names: email, password, confirm_password, search, name, first_name,
    last_name, phone, address, city, state, zip, country, message, subject,
    date, card_number, cvv. Any other key is tried as literal label text."""
    return browser_agent.fill_form(fields, tab=tab)


# ---------------- Browser agent: downloads ---------------- #

def browser_download_via(trigger_description, destination_dir=None, tab=None):
    """Clicks the given element (e.g. "Download" button) and waits for the
    resulting download to finish. Returns the saved file path. Use
    file_move/file_rename afterward if it needs to end up somewhere else."""
    return browser_agent.download_via(trigger_description, destination_dir=destination_dir, tab=tab)


# ---------------- Browser agent: authentication ---------------- #

def browser_wait_for_login(tab=None, timeout_seconds=120):
    """Call this right after navigating to a login page. If the site's
    saved session/cookies in the browser profile are still valid, this
    returns almost immediately. Otherwise it waits (up to timeout_seconds)
    for the user or the browser's own saved-password autofill to complete
    the login, then continues automatically. Never enters credentials
    itself."""
    return browser_agent.wait_for_login(tab=tab, timeout_seconds=timeout_seconds)


# ---------------- Browser agent: page understanding ---------------- #

def browser_read_page(tab=None):
    """Comprehensive structured snapshot of the current page: URL, title,
    page type (search, form, article, error, etc.), all interactive elements
    (buttons, links, inputs, selects, checkboxes), headings, visible text,
    forms, tables, lists, scroll position. Use THIS instead of read_dom_summary
    + read_text + extract_links + extract_forms + extract_tables separately.
    Call this BEFORE every interaction so you act on real page state."""
    return browser_agent.read_page(tab=tab)


def browser_read_page_light(tab=None):
    """Quick page check: URL, title, page type, buttons, links, inputs.
    Lighter than read_page for fast verification loops after an action."""
    return browser_agent.read_page_light(tab=tab)


# ---------------- Browser agent: action verification ---------------- #

def browser_verify_navigation(tab=None, before_url="", before_title=""):
    """Check if the page changed (URL, title) since a previous state.
    Call before an action to capture state, then after to verify the
    action actually had an effect (e.g. after clicking a link or
    submitting a form)."""
    return browser_agent.verify_navigation(tab=tab, before_url=before_url, before_title=before_title)


def browser_verify_element(description, tab=None, timeout=5000):
    """Check if an element matching the description is visible now.
    Returns confidence score. Use after an action to confirm the
    expected result appeared."""
    return browser_agent.verify_element_appeared(description, tab=tab, timeout=timeout)


def browser_vision(question="What do you see on this page?", tab=None):
    """Take a screenshot of the current page and ask the vision model a
    question. Use when text-based snapshot isn't enough — dropdown popups,
    date picker calendars, captchas, custom widgets."""
    return browser_agent.vision(question=question, tab=tab)


# ---------------- Browser agent: task memory ---------------- #

def browser_get_state():
    """Every open tab, recent navigation, recent failures, extracted data,
    completed steps, and the current task/progress notes. Call this if
    you've lost track of what's open, what data you've collected, or
    what's already been tried."""
    return browser_agent.get_state()


def browser_set_task(description, total_steps=0):
    """Record the overall goal and step count for a multi-step browser
    task (e.g. "Plan a trip: flights, hotel, itinerary doc"). Provide
    total_steps so the agent can track completion percentage."""
    return browser_agent.set_task(description, total_steps=total_steps)


def browser_update_progress(note):
    """Append a short progress note (e.g. "Booked flight, now comparing
    hotels") to the current task's memory."""
    return browser_agent.update_progress(note)


def browser_complete_step(step_description):
    """Mark one step of the current task as completed. Updates step
    counter and appends to completed_steps list."""
    return browser_agent.complete_step(step_description)


def browser_record_data(key, data, tab=None):
    """Store extracted information (prices, search results, product names,
    etc.) in browser memory so it can be referenced later without re-reading
    the same pages."""
    return browser_agent.record_data(key=key, data=data, tab=tab)


def browser_plan(goal=None, steps=None, plan=None):
    """Define an explicit ordered plan for a multi-tab browser workflow.
    Steps is a list of strings like ['Search flights to Tokyo',
    'Compare hotel options', 'Create itinerary doc'].
    Call this ONCE at the start of any multi-step task spanning >1 tab.
    Internally also calls browser_set_task."""
    steps = steps or plan or []
    return browser_agent.plan(goal=goal or "", steps=steps)


def browser_note(key, value):
    """Store a session-level note (cross-tab working memory).
    Unlike browser_record_data which is per-tab, notes are global —
    use for things like cheapest_flight, best_price, summary strings."""
    return browser_agent.note(key=key, value=value)


def browser_summarize_session(format="text"):
    """Collect all extracted data, notes, page summaries, and progress
    into a final structured digest. Call at the END of a multi-tab
    workflow to present the full result."""
    return browser_agent.summarize_session(fmt=format)


def recipe_save(goal, tags, steps):
    """Save the current successful workflow as a reusable recipe.
    Call this after completing a multi-step task that you might want
    to repeat later. Provide tags for matching and the list of
    {tool, arguments} steps that worked."""
    from tools.recipe_manager import recipe_manager
    recipe_manager.save_recipe(goal=goal, tags=tags, steps=steps)
    return {"success": True, "goal": goal, "tags": tags, "steps": len(steps)}


def file_search(query, limit=10, action=None, filters=None, sort=None):
    """
    Returns a structured dict: {"status": "ok"|"clarify"|"no_match", ...}.
    Intent detection, entity extraction, ranking, and disambiguation all
    happen inside file_manager_service.search -- callers (agent.py) just
    interpret the status field.
    """
    from tools.file_intelligence import enrich_search_response

    if isinstance(query, dict):
        response = file_manager_service.search(query=query, limit=limit)
        query_text = str(query.get("query") or query.get("text") or "")
    else:
        response = file_manager_service.search(query=query, limit=limit, action=action, filters=filters, sort=sort)
        query_text = str(query)
    try:
        records = file_manager_service.indexer.get_raw_records(limit=5000)
    except Exception as exc:
        logger.warning("Could not load file index records for intent enrichment: %s", exc)
        records = None
    return enrich_search_response(query_text, response, records, limit=limit)


def file_profile(path, inspect_content=True):
    """Return bounded purpose/risk evidence for one local file."""
    from tools.file_intelligence import profile_file

    return {"success": True, "profile": profile_file(path, inspect_content=bool(inspect_content)).to_dict()}


def file_intent_search(query, limit=10):
    """Search the local index by likely purpose instead of filename alone."""
    from tools.file_intelligence import search_file_intent

    try:
        records = file_manager_service.indexer.get_raw_records(limit=10000)
    except Exception as exc:
        return {"success": False, "query": str(query), "results": [], "error": f"File index unavailable: {exc}"}
    results = search_file_intent(str(query), records, limit=max(1, min(int(limit), 50)))
    return {
        "success": True,
        "query": str(query),
        "results": results,
        "count": len(results),
        "content_policy": "bounded samples and metadata only",
    }


def file_git_summary(path=None):
    from tools.file_git import summarize_git_status

    return summarize_git_status(path).to_dict()


def file_safe_stage(path=None):
    """Build a reviewable staging plan. This function never runs git add."""
    from tools.file_git import safe_stage_plan

    return safe_stage_plan(path)


def file_open(path):
    target = Path(path).expanduser().resolve()
    logger.info("Attempting to open file: %s", str(target))

    try:
        if not target.exists():
            raise FileNotFoundError(f"File does not exist: {target}")

        if platform.system().lower() == "windows":
            try:
                os.startfile(str(target))
                logger.info("os.startfile succeeded for: %s", str(target))
                return {"success": True, "path": str(target), "launched": True}
            except Exception as exc:
                logger.exception("os.startfile failed for: %s", str(target))
                raise RuntimeError(f"Failed to open file with Windows shell: {exc}") from exc

        if sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
            logger.info("Opened file with macOS open: %s", str(target))
            return {"success": True, "path": str(target), "launched": True}

        subprocess.Popen(["xdg-open", str(target)])
        logger.info("Opened file with xdg-open: %s", str(target))
        return {"success": True, "path": str(target), "launched": True}
    except Exception as exc:
        logger.exception("Failed to open file: %s", str(target))
        return {"success": False, "path": str(target), "error": str(exc), "launched": False}


def file_create(path):
    return file_manager_service.create_folder(path)


def file_list(path):
    return file_manager_service.list_folder(path)


def file_move(source, destination, confirm=False):
    from tools.file_intelligence import assess_file_action

    assessment = assess_file_action(source, "move")
    if assessment["risk"] in {"high", "critical"} and not confirm:
        return {
            "success": False,
            "requires_confirmation": True,
            "source": str(source),
            "destination": str(destination),
            "error": assessment["reason"],
            "file_safety": assessment,
        }
    from file_manager.operations import FileOperations
    return FileOperations(file_manager_service).move(source, destination)


def file_copy(source, destination):
    from file_manager.operations import FileOperations
    return FileOperations(file_manager_service).copy(source, destination)


def file_rename(source, destination):
    """Rename a file/folder in place.

    Renaming must NEVER change which folder or drive the item lives in --
    that's what file_move is for. If `destination` is a bare name (the
    normal case, e.g. "12345" or "12345.pdf"), we rebuild the target path
    inside the *source's own parent directory*. Even if the caller passes
    a full path with different directories, we still take only the name
    portion, so a rename can never turn into a cross-folder/cross-drive
    move by accident.
    """
    src = Path(source).expanduser().resolve()
    logger.info("Attempting to rename: %s -> %s", str(src), destination)

    if not src.exists():
        return {"success": False, "path": str(src), "error": f"Source does not exist: {src}", "renamed": False}

    new_name = Path(str(destination).strip()).name
    if not new_name:
        return {"success": False, "path": str(src), "error": "No destination name provided.", "renamed": False}

    new_path = src.parent / new_name

    # Preserve the original extension unless the caller explicitly supplied one.
    if src.is_file() and not new_path.suffix:
        new_path = new_path.with_suffix(src.suffix)

    if new_path == src:
        return {"success": True, "path": str(src), "old_path": str(src), "renamed": False}

    if new_path.exists():
        return {
            "success": False,
            "path": str(src),
            "error": f"A file already exists at {new_path}",
            "renamed": False,
        }

    try:
        src.rename(new_path)
        logger.info("Renamed %s -> %s", str(src), str(new_path))
        return {"success": True, "path": str(new_path), "old_path": str(src), "renamed": True}
    except Exception as exc:
        logger.exception("Failed to rename %s -> %s", str(src), str(new_path))
        return {"success": False, "path": str(src), "error": str(exc), "renamed": False}


def file_delete(path, confirm=False):
    from tools.file_intelligence import assess_file_action

    assessment = assess_file_action(path, "delete")
    if not confirm:
        return {
            "success": False,
            "requires_confirmation": True,
            "path": str(path),
            "error": assessment["reason"],
            "file_safety": assessment,
        }
    from file_manager.operations import FileOperations
    return FileOperations(file_manager_service).delete(path, confirm=confirm)


def file_restore(path):
    from file_manager.trash import TrashManager
    return TrashManager().restore(path)


def file_metadata(path):
    from file_manager.metadata import FileMetadata
    from tools.file_intelligence import profile_file

    value = FileMetadata(file_manager_service).metadata(path)
    if isinstance(value, dict):
        return {**value, "file_profile": profile_file(path).to_dict()}
    return {"metadata": value, "file_profile": profile_file(path).to_dict()}


def show_properties(path):
    return file_manager_service.show_properties(path)


def extract_archive(archive_path, destination=None):
    return file_manager_service.extract_archive(archive_path, destination)


def compress_archive(sources, destination_zip):
    return file_manager_service.compress_archive(sources, destination_zip)


def reveal_in_explorer(path):
    return file_manager_service.reveal_in_explorer(path)


def file_duplicate(path):
    return file_ops_duplicate(path)


def file_sort(path):
    return {"success": True, "path": str(path), "organized": True}


def file_semantic_search(query, limit=10):
    return file_search(query=query, limit=limit)


def rebuild_file_index():
    return file_manager_service.rebuild_index()


# ---------------- New Folder Management ---------------- #


def file_ops_create_folder(path):
    return create_folder(path)


def file_ops_delete_folder(path, confirm=False):
    return delete_folder(path, confirm=confirm)


# ---------------- New File Management ---------------- #


def file_ops_copy_file(source, destination):
    return copy_file(source, destination)


def file_ops_move_file(source, destination, confirm=False):
    from tools.file_intelligence import assess_file_action

    assessment = assess_file_action(source, "move")
    if assessment["risk"] in {"high", "critical"} and not confirm:
        return {
            "success": False,
            "requires_confirmation": True,
            "source": str(source),
            "destination": str(destination),
            "error": assessment["reason"],
            "file_safety": assessment,
        }
    return move_file(source, destination)


def file_ops_rename_file(source, destination):
    return rename_file(source, destination)


def file_ops_delete_file(path, confirm=False):
    return delete_file(path, confirm=confirm)


def file_ops_duplicate_file(path):
    return file_ops_duplicate(path)


def file_ops_get_file_metadata(path):
    return file_ops_metadata(path)


# ---------------- New Archive Management ---------------- #


def file_ops_zip_archive(sources, destination):
    return zip_archive(sources, destination)


def file_ops_unzip_archive(archive_path, destination=None):
    return unzip_archive(archive_path, destination)


def file_ops_tar_archive(sources, destination, compression=None):
    return tar_archive(sources, destination, compression=compression)


def file_ops_untar_archive(archive_path, destination=None):
    return untar_archive(archive_path, destination)


# ---------------- File Searching ---------------- #


def file_ops_search_files(query=None, extension=None, folder=None, recursive=True, limit=50):
    return search_files(query=query, extension=extension, folder=folder, recursive=recursive, limit=limit)


# ---------------- Screenshot ---------------- #


def file_ops_capture_screenshot(directory=None):
    return capture_screenshot_tool(directory=directory)


# ---------------- Skills wrappers ---------------- #
# Thin facade over the Skills subsystem. Skill playback ("do the X skill",
# "watch me") is handled deterministically by the agent loop before the
# planner runs -- these tools exist so the planner can also manage skills.

def skill_record_start(name=None):
    from skills.manager import skill_manager

    return skill_manager.start_recording(name)


def skill_record_stop(name=None):
    from skills.manager import skill_manager

    return skill_manager.stop_recording(name)


def skill_list():
    from skills.manager import skill_manager

    return skill_manager.list_skills()


def skill_rename(name, new_name):
    from skills.manager import skill_manager

    return skill_manager.rename_skill(name, new_name)


def skill_delete(name):
    from skills.manager import skill_manager

    return skill_manager.delete_skill(name)


def skill_export(name, destination=None):
    from skills.manager import skill_manager

    return skill_manager.export_skill(name, destination)


def skill_duplicate(name, new_name=None):
    from skills.manager import skill_manager

    return skill_manager.duplicate_skill(name, new_name)


def skill_test(name):
    from skills.manager import skill_manager

    return skill_manager.test_skill(name)


# ---------------- Connector wrappers ---------------- #

def connector_list():
    from connectors.defaults import default_registry

    registry = default_registry()
    return {
        "success": True,
        "connectors": [registry.describe(name) for name in registry.names()],
    }


def connector_status(name):
    from connectors.defaults import default_registry

    description = default_registry().describe(name)
    return {"success": True, "connector": name, "status": description["status"], "details": description}


def connector_capabilities(name):
    from connectors.defaults import default_registry

    capabilities = default_registry().capabilities(name)
    if not capabilities:
        return {"success": False, "error": f"Connector '{name}' is unavailable or has no capabilities"}
    return {"success": True, "connector": name, "capabilities": capabilities}


def connector_execute(name, capability, arguments=None, confirm=False):
    from connectors.defaults import default_registry

    return default_registry().execute(
        name,
        capability,
        arguments or {},
        confirmed=confirm,
    ).to_dict()


def connector_test(name):
    from connectors.defaults import default_registry

    return default_registry().test(name)


def connector_plan(name, capability, arguments=None, confirm=False):
    from dataclasses import asdict

    from connectors.base import ConnectorRequest
    from connectors.defaults import default_registry

    request = ConnectorRequest(name, capability, arguments or {}, bool(confirm))
    return asdict(default_registry().plan(request))


def connector_action_plan(request):
    from connectors.planner import ConnectorActionPlanner

    return ConnectorActionPlanner().choose(str(request)).to_dict()


_inbox_service_instance = None


def _inbox_service():
    global _inbox_service_instance
    if _inbox_service_instance is None:
        from tools.inbox_intelligence import InboxIntelligenceService

        _inbox_service_instance = InboxIntelligenceService()
    return _inbox_service_instance


def inbox_scan_downloads(query="assignment worksheet homework", days=3, limit=12, source="downloads"):
    from tools.inbox_models import InboxSource

    if str(source).lower() in {InboxSource.WHATSAPP.value, InboxSource.DISCORD.value}:
        return _inbox_service().scan_limited_source(str(source).lower(), query=str(query), days=float(days), limit=int(limit))
    return _inbox_service().scan_downloads(query=str(query), days=float(days), limit=int(limit))


def inbox_ingest_file(path, source="manual_import", message="", sender="", channel="", timestamp=""):
    return _inbox_service().ingest_file(
        path,
        source=source,
        message=message,
        sender=sender,
        channel=channel,
        timestamp=timestamp,
    ).to_dict()


def inbox_ingest_folder(path, source="folder_watch", message="", limit=25):
    return _inbox_service().ingest_folder(path, source=source, message=message, limit=int(limit))


def inbox_analyze(path, source="manual_import"):
    from tools.inbox_models import InboxSource

    source_value = InboxSource(str(source))
    return {"success": True, "analysis": _inbox_service().analyze_attachment(path, source=source_value).to_dict()}


def assignment_extract(assignment_id):
    return _inbox_service().extract_tasks(str(assignment_id))


def assignment_plan(assignment_id):
    return {"success": True, "plan": _inbox_service().create_plan(str(assignment_id)).to_dict()}


def assignment_draft(assignment_id, response_text=""):
    return {"success": True, "output": asdict(_inbox_service().generate_draft(str(assignment_id), response_text=response_text))}


def assignment_export(assignment_id, output_format="docx"):
    return _inbox_service().export(str(assignment_id), output_format=str(output_format))


def assignment_report(assignment_id):
    return _inbox_service().generate_report(str(assignment_id))


def assignment_submission_draft(assignment_id, connector, recipient, message, attachment_paths=None):
    return {
        "success": True,
        "submission": asdict(_inbox_service().submission_draft(
            str(assignment_id),
            connector=str(connector),
            recipient=str(recipient),
            message=str(message),
            attachment_paths=attachment_paths,
        )),
        "sent": False,
        "requires_confirmation": True,
    }


def pause_indexing():
    return file_manager_service.pause_indexing()


def resume_indexing():
    return file_manager_service.resume_indexing()


def show_indexing_status():
    return file_manager_service.show_status()


def index_folder(path):
    return file_manager_service.index_folder(path)


def exclude_folder(path):
    return file_manager_service.exclude_folder(path)


def remove_from_index(path):
    return file_manager_service.remove_from_index(path)


# ---------------- Desktop control wrappers ---------------- #

def hotkey(keys):
    """Accepts a list of key names, e.g. ["ctrl", "c"], matching the tool
    schema documented to the LLM (hotkey(keys)). The underlying pyautogui
    helper is variadic (*keys), so it's unpacked here rather than being
    registered directly -- registering it directly means any real call
    with a `keys` keyword argument raises TypeError."""
    if isinstance(keys, str):
        keys = [keys]
    return _hotkey_variadic(*keys)


# ---------------- Tool Registry ---------------- #

TOOLS = {
    "launch_app": launch_app,

    "browser_open": browser_open,
    "youtube_recommend": youtube_recommend,
    "youtube_play_url": youtube_play_url,
    "gmail_read": gmail_read,
    "gmail_summary": gmail_summary,
    "gmail_reply": gmail_reply,
    "gmail_send": gmail_send,
    "gmail_search": gmail_search,
    "gmail_archive": gmail_archive,
    "gmail_mark_read": gmail_mark_read,
    "gmail_delete": gmail_delete,
    "google_search": google_search,
    "browser_read": browser_read,
    "browser_read_text": browser_read_text,
    "web_research": web_research,
    "research_topic": research_topic,

    "browser_list_tabs": browser_list_tabs,
    "browser_open_tab": browser_open_tab,
    "browser_close_tab": browser_close_tab,
    "browser_switch_tab": browser_switch_tab,
    "browser_duplicate_tab": browser_duplicate_tab,
    "browser_label_tab": browser_label_tab,

    "browser_goto": browser_goto,
    "browser_navigate": browser_navigate,
    "browser_get_browser_state": browser_get_browser_state,
    "browser_back": browser_back,
    "browser_forward": browser_forward,
    "browser_refresh": browser_refresh,
    "browser_wait_for_load": browser_wait_for_load,
    "browser_wait_for_element": browser_wait_for_element,

    "browser_read_dom_summary": browser_read_dom_summary,
    "browser_extract_tables": browser_extract_tables,
    "browser_extract_links": browser_extract_links,
    "browser_extract_forms": browser_extract_forms,

    "browser_click": browser_click,
    "browser_double_click": browser_double_click,
    "browser_right_click": browser_right_click,
    "browser_hover": browser_hover,
    "browser_type": browser_type,
    "browser_press_key": browser_press_key,
    "browser_scroll": browser_scroll,
    "browser_select_dropdown": browser_select_dropdown,
    "browser_set_checkbox": browser_set_checkbox,
    "browser_click_radio": browser_click_radio,
    "browser_drag_and_drop": browser_drag_and_drop,
    "browser_upload_file": browser_upload_file,

    "browser_fill_form": browser_fill_form,

    "browser_download_via": browser_download_via,

    "browser_wait_for_login": browser_wait_for_login,

    "browser_read_page": browser_read_page,
    "browser_read_page_light": browser_read_page_light,

    "browser_verify_navigation": browser_verify_navigation,
    "browser_verify_element": browser_verify_element,
    "browser_vision": browser_vision,

    "browser_get_state": browser_get_state,
    "browser_set_task": browser_set_task,
    "browser_update_progress": browser_update_progress,
    "browser_complete_step": browser_complete_step,
    "browser_record_data": browser_record_data,
    "browser_plan": browser_plan,
    "browser_note": browser_note,
    "browser_summarize_session": browser_summarize_session,
    "recipe_save": recipe_save,

    # Adobe Premiere Pro editing. Five tools, not fifty: the expressive power
    # lives in the EditPlan passed to premiere_edit (see premiere/catalog.py
    # for the ~75 operations it accepts), so new creative capabilities do not
    # mean new tool names for the planner to learn.
    "premiere_status": premiere_status,
    "premiere_capabilities": premiere_capabilities,
    "premiere_inspect": premiere_inspect,
    "premiere_edit": premiere_edit,
    "premiere_undo": premiere_undo,

    "file_search": file_search,
    "file_profile": file_profile,
    "file_intent_search": file_intent_search,
    "file_git_summary": file_git_summary,
    "file_safe_stage": file_safe_stage,
    "file_open": file_open,
    "file_create": file_create,
    "file_list": file_list,
    "file_move": file_move,
    "file_copy": file_copy,
    "file_rename": file_rename,
    "file_delete": file_delete,
    "file_restore": file_restore,
    "file_metadata": file_metadata,
    "show_properties": show_properties,
    "extract_archive": extract_archive,
    "compress_archive": compress_archive,
    "reveal_in_explorer": reveal_in_explorer,
    "file_duplicate": file_duplicate,
    "file_sort": file_sort,
    "file_semantic_search": file_semantic_search,

    # New folder management
    "create_folder": file_ops_create_folder,
    "delete_folder": file_ops_delete_folder,

    # New file management (improved implementations)
    "copy_file": file_ops_copy_file,
    "move_file": file_ops_move_file,
    "rename_file": file_ops_rename_file,
    "delete_file": file_ops_delete_file,
    "duplicate_file": file_ops_duplicate_file,
    "get_file_metadata": file_ops_get_file_metadata,

    # New archive management
    "zip_archive": file_ops_zip_archive,
    "unzip_archive": file_ops_unzip_archive,
    "tar_archive": file_ops_tar_archive,
    "untar_archive": file_ops_untar_archive,

    # File searching
    "search_files": file_ops_search_files,

    # Screenshot
    "capture_screenshot": file_ops_capture_screenshot,
    "rebuild_file_index": rebuild_file_index,
    "pause_indexing": pause_indexing,
    "resume_indexing": resume_indexing,
    "show_indexing_status": show_indexing_status,
    "index_folder": index_folder,
    "exclude_folder": exclude_folder,
    "remove_from_index": remove_from_index,

    "connector_list": connector_list,
    "connector_status": connector_status,
    "connector_capabilities": connector_capabilities,
    "connector_plan": connector_plan,
    "connector_action_plan": connector_action_plan,
    "connector_execute": connector_execute,
    "connector_test": connector_test,

    "inbox_scan_downloads": inbox_scan_downloads,
    "inbox_ingest_file": inbox_ingest_file,
    "inbox_ingest_folder": inbox_ingest_folder,
    "inbox_analyze": inbox_analyze,
    "assignment_extract": assignment_extract,
    "assignment_plan": assignment_plan,
    "assignment_draft": assignment_draft,
    "assignment_export": assignment_export,
    "assignment_report": assignment_report,
    "assignment_submission_draft": assignment_submission_draft,

    "type_text": type_text,
    "press_key": press_key,
    "hotkey": hotkey,
    "left_click": left_click,
    "double_click": double_click,
    "right_click": right_click,
    "scroll": scroll,
    "move_mouse": move_mouse,
    "wait": wait,

    "vision": vision_tool,

    # Skills subsystem
    "skill_record_start": skill_record_start,
    "skill_record_stop": skill_record_stop,
    "skill_list": skill_list,
    "skill_rename": skill_rename,
    "skill_delete": skill_delete,
    "skill_export": skill_export,
    "skill_duplicate": skill_duplicate,
    "skill_test": skill_test,

}

# Context for sequential tool executions. Each tool result is stored under
# '<tool_name>_result' so later tool arguments can reference previous outputs.
tool_context = {}

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def clear_tool_context():
    """Reset the placeholder-resolution context. Called by AgentLoop at the
    start of every new user request so a fresh task never accidentally
    resolves a {{...}} placeholder against a previous, unrelated task's
    tool results."""
    tool_context.clear()


def _resolve_placeholder_expression(expression):
    path = expression.strip()
    if not path:
        raise ValueError("Empty placeholder expression")

    parts = [part.strip() for part in path.split(".") if part.strip()]
    current = tool_context
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            try:
                current = current[index]
            except IndexError as exc:
                raise KeyError(f"Placeholder '{expression}' refers to out-of-range index {index}") from exc
        elif hasattr(current, part):
            current = getattr(current, part)
        else:
            raise KeyError(f"Unresolved placeholder: {{{{{expression}}}}}")
    return current


def _resolve_placeholders(value):
    if isinstance(value, dict):
        resolved = {}
        for key, item in value.items():
            resolved_key = _resolve_placeholders(key) if isinstance(key, str) else key
            resolved[resolved_key] = _resolve_placeholders(item)
        return resolved

    if isinstance(value, (list, tuple)):
        resolved_items = [_resolve_placeholders(item) for item in value]
        return type(value)(resolved_items)

    if isinstance(value, str):
        matches = list(PLACEHOLDER_PATTERN.finditer(value))
        if not matches:
            return value

        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return _resolve_placeholder_expression(matches[0].group(1))

        def replacement(match):
            resolved = _resolve_placeholder_expression(match.group(1))
            return str(resolved)

        return PLACEHOLDER_PATTERN.sub(replacement, value)

    return value


def _resolve_tool_arguments(arguments):
    if arguments is None:
        return None
    if not isinstance(arguments, dict):
        return _resolve_placeholders(arguments)
    return _resolve_placeholders(copy.deepcopy(arguments))


# Argument-name aliases the LLM is known to use for the same parameter.
# Chrome/extension tool results report tab ids as "tabId"; weak models
# copy that name straight into the next tool call, but every browser tool
# names the parameter "tab". Accept both spellings so a copied "tabId"
# can never TypeError a tool again. Keyed by the canonical parameter name.
_ARGUMENT_ALIASES = {
    "tab": ("tabId", "tab_id"),
}


def _apply_argument_aliases(tool_name, arguments, function) -> dict:
    """Rename LLM-ism argument keys (e.g. tabId -> tab) before the call,
    but only for tools that actually accept the canonical name, so a
    legitimate 'tabId'-accepting tool is never disturbed."""
    if not isinstance(arguments, dict) or not arguments:
        return arguments

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return arguments

    renamed = dict(arguments)
    changed = False
    for canonical, aliases in _ARGUMENT_ALIASES.items():
        if canonical not in parameters or canonical in renamed:
            continue
        for alias in aliases:
            if alias in renamed:
                renamed[canonical] = renamed.pop(alias)
                changed = True
                break
    if changed:
        logger.info(
            "[Executor] %s: mapped argument alias -> %s",
            tool_name, {k: (renamed[k] if k in renamed else None) for k in sorted(set(arguments) | set(renamed))},
        )
    return renamed


def run_tool(tool_name, arguments):
    if tool_name not in TOOLS:
        logger.error("[Executor] Unknown tool requested: %s", tool_name)
        return False, f"Unknown tool: {tool_name}"

    try:
        resolved_arguments = _resolve_tool_arguments(arguments)
    except Exception as e:
        logger.error("[Executor] Failed to resolve arguments for %s: %s", tool_name, e)
        return False, str(e)

    logger.info("[Executor] Executing %s(%s)", tool_name, resolved_arguments or "")

    try:
        function = TOOLS[tool_name]

        if resolved_arguments:
            if not isinstance(resolved_arguments, dict):
                logger.error(
                    "[Executor] %s received non-dict arguments (%s) -- bad JSON from the LLM",
                    tool_name, type(resolved_arguments).__name__,
                )
                return False, (
                    f"Tool '{tool_name}' was called with {type(resolved_arguments).__name__} "
                    f"arguments (expected dict). The LLM generated bad JSON for this step."
                )
            resolved_arguments = _apply_argument_aliases(tool_name, resolved_arguments, function)
            result = function(**resolved_arguments)
        else:
            result = function()

        tool_context[f"{tool_name}_result"] = result

        result_status = str(result.get("status") or "").lower() if isinstance(result, dict) else ""
        error_shaped = bool(
            isinstance(result, dict)
            and (result.get("error") or result.get("error_message"))
            and result.get("success") is not True
        )
        partial = bool(isinstance(result, dict) and (result.get("partial") or result_status == "partial"))
        if isinstance(result, dict) and (
            result.get("success") is False
            or result_status in {"error", "failed", "failure"}
            or error_shaped
            or partial
        ):
            logger.warning("[Executor] %s reported failure: %s", tool_name, result.get("error", result))
            # The legacy contract used to return True here merely because
            # the Python function did not raise.  That made
            # (True, {"success": False}) a normal outcome and allowed callers
            # to claim success after a tool explicitly failed.
            return False, result
        else:
            logger.info("[Executor] Tool completed successfully: %s", tool_name)

        return True, result

    except RecommendationConfigurationError:
        raise
    except Exception as e:
        # Previously this exception was swallowed into a bare string with no
        # trace of where/why it happened -- any bug inside a tool (browser
        # automation included) would silently look like an ordinary "tool
        # returned an error" to the agent loop, with nothing in the logs to
        # debug it from. Log the full traceback, then still return the same
        # (False, str(e)) contract callers already depend on.
        logger.exception("[Executor] %s raised an unhandled exception", tool_name)
        return False, str(e)


def list_tools():
    return list(TOOLS.keys())
