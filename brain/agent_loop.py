import difflib
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from config.settings import settings
from skills.manager import skill_manager
from recommendation.errors import RecommendationConfigurationError
from tools.browser.browser_agent import browser_agent
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


# Ask-guard: when the goal refers to something IN the browser (an open tab,
# a page, a site), the planner must locate and read it itself with the
# browser tools. A first-turn ask_user for content it can read on its own
# ("which tab?", "what does the page say?") is rejected once and the model
# is forced to use browser_list_tabs/browser_switch_tab/browser_read.
# Ask-guard: when the goal refers to something IN the browser (an open tab,
# a page, a site), the planner must locate and read it itself with the
# browser tools. A first-turn ask_user for content it can read on its own
# ("which tab?", "what does the page say?") is rejected once and the model
# is forced to use browser_list_tabs/browser_switch_tab/browser_read.
_BROWSER_REFERENCE_PATTERN = re.compile(
    r"\b(tab|tabs|page|webpage|browser|website|web site|url)\b",
    re.IGNORECASE,
)


def _goal_references_browser(goal: str) -> bool:
    return bool(_BROWSER_REFERENCE_PATTERN.search(goal or ""))


# Raw LLM-backend failures (Ollama down, API config errors, timeouts) must
# never be narrated to the user or stored in conversation history verbatim:
# the planner has repeatedly hallucinated them as browser/internet problems
# on later turns. Terminal error decisions get a clean, neutral retry
# message instead.
_BACKEND_ERROR_PATTERN = re.compile(
    r"(could not reach|error calling llm|connection refused|failed to "
    r"establish|max retries exceeded|connectionerror|httpx|api/chat|"
    r"api\.openai|openrouter|ollama|gemini client|configuration error)",
    re.IGNORECASE,
)

_BACKEND_ERROR_RESPONSE = (
    "Sorry, my thinking engine isn't responding right now — that's the "
    "local model server, separate from your browser and internet. Give it "
    "a moment and repeat your request."
)


# --------------------------------------------------------------------- #
# Input normalization: typos + Gen-Z slang
# --------------------------------------------------------------------- #
# Users type and speak casually -- misspelled words ("reserch",
# "flgihts"), dropped letters, and Gen-Z slang ("dawg", "bro", "fr",
# "ngl", "bet"). Deterministic handlers match on exact patterns, so the
# raw text is cleaned BEFORE any pattern matching: slang is expanded to
# plain English and common misspellings are corrected. The cleaned text
# is also what the planner sees, so it never trips on "typo" either.
_GEN_Z_SLANG = {
    "dawg": "", "bro": "", "bruh": "", "broski": "", "bruv": "",
    "fr": "for real", "frfr": "for real", "ngl": "not going to lie",
    "tbh": "to be honest", "nvm": "never mind", "idk": "i don't know",
    "idc": "i don't care", "imo": "in my opinion", "imho": "in my humble opinion",
    "ikr": "i know right", "smh": "shaking my head", "bet": "okay agreed",
    "sus": "suspicious", "cap": "lying", "no cap": "not lying",
    "lit": "amazing", "fire": "amazing", "mid": "mediocre", "goated": "excellent",
    "bussin": "delicious", "vibe": "mood", "vibes": "mood",
    "lowkey": "somewhat", "highkey": "definitely", "finna": "going to",
    "gonna": "going to", "wanna": "want to", "gotta": "have to",
    "kinda": "kind of", "sorta": "sort of", "lemme": "let me",
    "gimme": "give me", "wyd": "what are you doing", "wym": "what do you mean",
    "rn": "right now", "ppl": "people", "u": "you", "ur": "your", "yall": "you all",
    "gon": "going to", "ima": "i am going to", "imma": "i am going to",
    "aight": "okay", "yo": "", "sup": "what's up", "whaddup": "what's up",
    "srsly": "seriously", "rly": "really", "obvi": "obviously",
    "def": "definitely", "defs": "definitely", "deffo": "definitely",
    "prolly": "probably", "prob": "probably", "bc": "because", "cuz": "because",
    "coz": "because", "bcos": "because", "tho": "though", "thx": "thanks",
    "ty": "thank you", "pls": "please", "plz": "please", "sry": "sorry",
    "lol": "haha", "lmao": "haha", "rofl": "haha", "ik": "i know",
    "ily": "i love you", "ily2": "i love you too", "asap": "as soon as possible",
    "btw": "by the way", "fyi": "for your information", "tldr": "too long did not read",
    "jk": "just kidding", "omg": "oh my god", "wtf": "what the heck",
    "srs": "serious", "g2g": "got to go", "ttyl": "talk to you later",
    "brb": "be right back", "afk": "away from keyboard", "wbu": "what about you",
    "hbu": "how about you", "gm": "good morning", "gn": "good night",
    "tysm": "thank you so much", "tq": "thank you", "wb": "welcome back",
}

# Common misspellings of words that matter to deterministic handlers and
# topic extraction. Normalized before any pattern matching.
_TYPO_FIXES = {
    "reserch": "research", "reseach": "research", "reasearch": "research",
    "resarch": "research", "reaserch": "research", "reserch": "research",
    "reseacrh": "research", "reaseach": "research", "rsearch": "research",
    "quatum": "quantum", "quantuum": "quantum", "quntum": "quantum",
    "computin": "computing", "computr": "computer", "computers": "computers",
    "flgiht": "flight", "flgihts": "flights", "flights": "flights",
    "flghts": "flights", "fllights": "flights", "flightes": "flights",
    "fligth": "flight", "fligths": "flights",
    "hotles": "hotels", "hotel": "hotel", "hotle": "hotel", "hotels": "hotels",
    "hotles": "hotels",
    "goole": "google", "googl": "google", "gooogle": "google", "gogle": "google",
    "goolge": "google", "googel": "google",
    "youtube": "youtube", "yutube": "youtube", "utube": "youtube",
    "youtbe": "youtube", "youtu": "youtube", "yt": "youtube",
    "sumarize": "summarize", "summrize": "summarize", "summury": "summary",
    "summery": "summary", "sammary": "summary", "sumary": "summary",
    "docuemnt": "document", "documet": "document", "document": "document",
    "docs": "docs",
    "gmail": "gmail", "gmial": "gmail", "gmil": "gmail",
    "fighs": "flights", "flites": "flights",
    "pleas": "please", "plase": "please", "plz": "please", "pls": "please",
    "thnaks": "thanks", "thankyou": "thank you",
    "wat": "what", "waht": "what", "whta": "what", "hwta": "what",
    "watz": "what's", "wats": "what's", "wht": "what", "whut": "what",
    "woudl": "would", "wouldl": "would", "couod": "could", "cna": "can",
    "nad": "and", "adn": "and", "teh": "the", "htat": "that", "taht": "that",
    "wiht": "with", "hwile": "while", "abotu": "about", "abut": "about",
    "becuase": "because", "becasue": "because", "becuse": "because",
    "bfore": "before", "b4": "before", "aftr": "after", "tommorow": "tomorrow",
    "tomorow": "tomorrow", "todya": "today", "2day": "today", "2morrow": "tomorrow",
    "wher": "where", "were": "where", "whre": "where", "were": "were",
    "hwo": "how", "hoiw": "how", "wahts": "what's", "wats": "what's",
    "wat": "what", "wats": "what's", "thers": "there's", "thier": "their",
    "hteir": "their", "yuor": "your", "yoru": "your", "yuo": "you",
    "hae": "have", "hev": "have", "hte": "the", "hting": "thing",
    "calulate": "calculate", "calcluate": "calculate", "recieve": "receive",
    "recieved": "received", "seperate": "separate", "seperately": "separately",
    "occured": "occurred", "occuring": "occurring", "definately": "definitely",
    "definetly": "definitely", "definately": "definitely", "defintly": "definitely",
    "really": "really", "reallly": "really", "realy": "really", "relly": "really",
    "becuase": "because", "cuz": "because",
    "search": "search", "seach": "search", "sreach": "search", "serach": "search",
    "srch": "search", "searh": "search", "sarch": "search",
    "weater": "weather", "weathe": "weather", "weater": "weather",
    "musci": "music", "musik": "music", "miusic": "music",
    "vedio": "video", "vido": "video", "vid": "video", "video": "video",
    "pictre": "picture", "pic": "picture", "pics": "pictures",
    "phto": "photo", "phot": "photo", "photoz": "photos",
    "messsage": "message", "mesage": "message", "msg": "message",
    "nubmer": "number", "numbr": "number", "nmber": "number",
    "wokr": "work", "wrk": "work", "wrok": "work",
    "taskk": "task", "tsk": "task", "tasls": "tasks",
    "hlep": "help", "helo": "hello", "hllo": "hello", "hii": "hi", "heyy": "hey",
    "okey": "okay", "oke": "okay", "kk": "okay", "k": "okay", "alrighty": "alright",
    "suree": "sure", "okok": "okay okay", "kewl": "cool", "cooll": "cool",
    "awsm": "awesome", "awsome": "awesome", "omw": "on my way",
    "cu": "see you", "cya": "see you", "cuz": "because",
    "recomend": "recommend", "recommend": "recommend", "recommened": "recommend",
    "shcedule": "schedule", "scheudle": "schedule", "sched": "schedule",
    "calender": "calendar", "calender": "calendar",
    "wat": "what", "wht": "what", "whut": "what",
    "dunno": "don't know", "idk": "i don't know",
}

_SLANG_RE = [
    (re.compile(rf"\b{re.escape(s)}\b", re.IGNORECASE), repl)
    for s, repl in sorted(_GEN_Z_SLANG.items(), key=lambda kv: -len(kv[0]))
]
_TYPO_RE = [
    (re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE), repl)
    for t, repl in sorted(_TYPO_FIXES.items(), key=lambda kv: -len(kv[0]))
]


def normalize_user_input(text: str) -> str:
    """Clean a raw user message: expand Gen-Z slang and fix common typos.
    Deterministic handlers and the planner both see the cleaned text, so
    'reserch quantum computin dawg' behaves exactly like 'research
    quantum computing friend'."""
    if not text:
        return text
    result = str(text)
    for pattern, repl in _SLANG_RE:
        result = pattern.sub(repl, result)
    for pattern, repl in _TYPO_RE:
        result = pattern.sub(repl, result)
    return result


def _sanitize_backend_error(decision: dict) -> dict:
    """Replace raw LLM-backend error text in terminal decisions with a
    clean retry message. Leaves everything else untouched."""
    response = decision.get("response") or ""
    if (
        decision.get("step") is None
        and bool(decision.get("done") or decision.get("ask_user"))
        and _BACKEND_ERROR_PATTERN.search(response)
    ):
        logger.warning("[Agent] Sanitized raw backend error response: %r", response[:160])
        decision = dict(decision)
        decision["response"] = _BACKEND_ERROR_RESPONSE
    return decision


def _browser_content_available(observations: list) -> bool:
    """True when a successful page read is already in the observations --
    the planner has the page text in hand and cannot ask for it."""
    for obs in observations:
        tool = (obs.get("step") or {}).get("tool")
        if tool in ("browser_read", "browser_read_text", "auto_tab_select") and obs.get("success"):
            return True
    return False


# Direct-answer path (Track A): a plain CONSUME goal with the page text
# already available is answered without the planner loop at all -- the
# JSON decision format is exactly what a weak planner uses to bounce
# questions back instead of answering. These trigger words are pure
# content-consumption; interactive-ambiguous words ("check", "see",
# "look at") deliberately stay on the planner path.
_DIRECT_ANSWER_PATTERN = re.compile(
    r"\b(summar\w*|what'?s on|what is on|whats on|what'?s there|"
    r"tell me about|describe|overview|recap|translate|extract|digest|"
    r"break down)\b",
    re.IGNORECASE,
)

_DIRECT_ANSWER_EXCLUSIONS = re.compile(
    r"\b(compare|versus|vs\.?|difference|both|each|other tab|between)\b",
    re.IGNORECASE,
)

_LOCATE_PAGE_HINT = (
    "Your question to the user was rejected: the information you "
    "asked for is inside the browser and you can read it yourself "
    "with your tools. Use browser_list_tabs to find the open tab "
    "the user means, browser_switch_tab to select it, and "
    "browser_read to get the page text. NEVER ask the user to "
    "read a page to you. Return a tool step now."
)


def _goal_is_direct_answer(goal: str) -> bool:
    return bool(_DIRECT_ANSWER_PATTERN.search(goal or "")) and not bool(
        _DIRECT_ANSWER_EXCLUSIONS.search(goal or "")
    )


def _extract_read_text(observations: list) -> str:
    """Return the most recent successful browser_read page text (at least
    40 chars -- shorter than that and there is nothing to answer from)."""
    for obs in reversed(observations):
        tool = (obs.get("step") or {}).get("tool")
        if tool == "browser_read" and obs.get("success"):
            result = obs.get("result") or {}
            if isinstance(result, dict):
                text = str(result.get("text") or "").strip()
            else:
                text = str(result).strip()
            if len(text) >= 40:
                return text
    return ""


# A goal that asks the planner to CONSUME a page ("summarise the page",
# "see my chatgpt tab", "what's on this site") triggers deterministic
# page resolution -- the loop itself finds the open tab and reads it, so
# the model never has to (and can never stall asking "which tab?").
_READ_INTENT_PATTERN = re.compile(
    r"\b(summar\w*|read|reading|see|look at|look through|what'?s on|"
    r"what is on|whats on|check|tell me about|describe|review|overview|"
    r"content of|contents of|translate|recap|highlights? (?:of|from|on))\b",
    re.IGNORECASE,
)

# Words that carry no discriminating signal about WHICH page is meant.
_BROWSER_STOPWORDS = {
    "the", "a", "an", "my", "your", "our", "their", "this", "that", "these",
    "those", "and", "of", "on", "in", "to", "for", "with", "is", "are", "was",
    "be", "it", "its", "at", "me", "please", "can", "you", "what", "whats",
    "whole", "content", "contents", "currently", "tell", "about", "just",
    "want", "need", "have", "give", "get", "show", "open", "see", "read",
    "summarise", "summarize", "summary", "page", "tab", "tabs", "i", "we",
    "us", "them", "into", "from", "up", "down", "now", "also", "then",
    "there", "google", "website", "webpage", "browser", "site", "nothing",
    "specific", "short", "points", "entire", "full", "general", "basically",
}

# Content-scan limits: when no tab's title/URL names the goal, the loop
# reads a snippet of each open tab and scores the goal words against the
# page text. Caps keep the scan cheap and bounded.
_SCAN_TAB_LIMIT = 6
_SCAN_TEXT_CHARS = 1500

# Confirmation replies: "2", "the second one", "first" map to a candidate
# from the ambiguous-tabs list. Ordinals are checked before cardinals so
# "the second one" picks 2, not 1.
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6}
_DIGITS = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6}
_CARDINALS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _reply_number(text: str) -> int | None:
    for table in (_ORDINALS, _DIGITS, _CARDINALS):
        for word, idx in table.items():
            if re.search(rf"\b{re.escape(word)}\b", text):
                return idx
    return None


def _confirmation_message(candidates: list) -> str:
    """Numbered list of ambiguous candidate tabs, spoken to the user."""
    parts = ["I found several similar tabs open. Which one do you mean?"]
    for i, c in enumerate(candidates, 1):
        title = str(c.get("title") or "Untitled tab").strip()
        host = urlsplit(str(c.get("url") or "")).hostname or ""
        parts.append(f"{i}) {title} ({host})" if host else f"{i}) {title}")
    return " ".join(parts)


def _match_pending_choice(reply: str | None, candidates: list) -> dict | None:
    """Map the user's reply to a tab from the confirmation list. A
    unique title/hostname hit wins ("the chess one" must not become
    "candidate 1" because of the word "one"); a plain number reply
    ("2", "second", "2nd") is used only when no title match is unique.
    """
    if not reply or not candidates:
        return None
    text = reply.strip().lower()
    tokens = _significant_tokens(text)
    if tokens:
        hits = []
        for c in candidates:
            title = str(c.get("title") or "").lower()
            host = urlsplit(str(c.get("url") or "")).hostname or ""
            if any(t in title or t in host for t in tokens):
                hits.append(c)
        if len(hits) == 1:
            return hits[0]
    n = _reply_number(text)
    if n is not None and 1 <= n <= len(candidates):
        return candidates[n - 1]
    return None


def _goal_asks_to_read(goal: str) -> bool:
    return bool(_READ_INTENT_PATTERN.search(goal or ""))


def _significant_tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in tokens if len(t) >= 3 and t not in _BROWSER_STOPWORDS]


def _scan_tabs_for_goal(tabs: list, tokens: list) -> dict | None:
    """Read a snippet of each open tab (capped) and score the goal tokens
    against the actual page text -- catches pages whose titles and URLs
    give nothing away ("tell me about the football tab" where the tab is
    just "Untitled"). Returns a resolved tab, an ambiguity signal, or
    None when nothing matches. The user's active tab is restored after
    the scan."""
    original_active = None
    switched_any = False
    entries: list[dict] = []
    try:
        for t in tabs[:_SCAN_TAB_LIMIT]:
            tab_id = t.get("tabId")
            if bool(t.get("active")):
                original_active = tab_id
            else:
                try:
                    ok, _res = run_tool("browser_switch_tab", {"tab": tab_id})
                except Exception as exc:
                    logger.info("[Agent] Content scan: cannot switch to tab %s: %s", tab_id, exc)
                    continue
                if not ok:
                    continue
                switched_any = True
            try:
                ok, res = run_tool("browser_read", {})
            except Exception as exc:
                logger.info("[Agent] Content scan: cannot read tab %s: %s", tab_id, exc)
                continue
            if not ok:
                continue
            if isinstance(res, dict):
                text = str(res.get("text") or "")
            else:
                text = str(res)
            snippet = text[:_SCAN_TEXT_CHARS].lower()
            score = sum(1 for tok in tokens if tok in snippet)
            entries.append({
                "tabId": tab_id,
                "title": str(t.get("title") or ""),
                "url": str(t.get("url") or ""),
                "active": bool(t.get("active")),
                "score": score,
            })
    finally:
        if switched_any and original_active is not None:
            try:
                run_tool("browser_switch_tab", {"tab": original_active})
            except Exception:
                pass
    if not entries:
        return None
    best = max(entries, key=lambda e: e["score"])
    if best["score"] < 1:
        return None
    tied = [e for e in entries if e["score"] == best["score"]]
    if len(tied) > 1:
        active_first = sorted(tied, key=lambda e: (not e["active"], -e["score"]))
        logger.info(
            "[Agent] Content scan: %s tabs tied at score %s -- asking which one",
            len(tied), best["score"],
        )
        return {"ambiguous": True, "candidates": active_first}
    logger.info(
        "[Agent] Content scan resolved tab '%s' (score %s)",
        best.get("title"), best["score"],
    )
    return best


def _resolve_browser_page(goal: str) -> dict | None:
    """Deterministically find the open tab the user means.

    Every significant goal word scores against each tab's title (2
    points) and URL/host (2 points); the active tab gets +1. Returns the
    best tab with score >= 2, an ambiguity signal when several tabs tie
    or share the same site (e.g. many Wikipedia tabs -- the goal cannot
    pick one page), or None when nothing matches (or the tab list cannot
    be queried). With no significant tokens at all (e.g. "summarise the
    page") the active tab is the best possible referent. When metadata
    matching finds nothing, the loop falls back to reading a snippet of
    each tab and scoring the goal words against the page text.
    """
    try:
        listing = browser_agent.list_tabs()
    except Exception as exc:
        logger.info("[Agent] Auto page resolution skipped (tab list unavailable): %s", exc)
        return None
    tabs = (listing or {}).get("tabs") or []
    if not tabs:
        return None

    tokens = _significant_tokens(goal)
    if not tokens:
        for t in tabs:
            if t.get("active"):
                return {
                    "tabId": t.get("tabId"),
                    "title": str(t.get("title") or ""),
                    "url": str(t.get("url") or ""),
                    "active": True,
                    "score": 1,
                }
        return None

    scored: list[dict] = []
    for t in tabs:
        title = str(t.get("title") or "").lower()
        url = str(t.get("url") or "")
        host = urlsplit(url).hostname or ""
        hay_url = f"{url} {host}".lower()
        score = 0
        for tok in tokens:
            if tok in title:
                score += 2
            elif tok in hay_url:
                # A hostname/URL match is as distinctive as a title match
                # (titles are often generic: "PC control from phone" while
                # the URL says chatgpt.com). A token found in either is a
                # strong signal.
                score += 2
        if t.get("active"):
            score += 1
        if score >= 2:
            scored.append({
                "tabId": t.get("tabId"),
                "title": str(t.get("title") or ""),
                "url": url,
                "active": bool(t.get("active")),
                "score": score,
            })
    if scored:
        scored.sort(key=lambda s: (s["score"], s["active"]), reverse=True)
        best = scored[0]
        host = urlsplit(best["url"]).hostname or ""
        candidates_by_id: dict = {}
        for s in scored:
            same_host = host and urlsplit(s["url"]).hostname == host
            if s["score"] == best["score"] or same_host:
                candidates_by_id[s["tabId"]] = s
        if len(candidates_by_id) > 1:
            active_first = sorted(
                candidates_by_id.values(),
                key=lambda s: (not s["active"], -s["score"]),
            )
            logger.info(
                "[Agent] Auto page resolution: %s tabs ambiguous (tie or same "
                "site) -- asking the user to pick one",
                len(candidates_by_id),
            )
            return {"ambiguous": True, "candidates": active_first}
        return best

    # No tab named the goal in its title/URL -- scan page content.
    logger.info("[Agent] Auto page resolution: no tab matched the goal tokens %s -- scanning page content", tokens)
    return _scan_tabs_for_goal(tabs, tokens)


_GDOCS_WRITE_PATTERN = re.compile(
    r"(?:google docs|docs\.google\.com|docs\.new).{0,40}\b(write|type|create|put|add|start)\b"
    r"|\b(write|type|create|put|add|start)\b.{0,40}(?:google docs|docs\.google\.com|docs\.new)",
    re.IGNORECASE,
)
_DOC_TO_FOLDER_PATTERN = re.compile(
    r"\b(save|write|create|make|draft|store|put|export|add|keep)\b"
    r".{0,80}\b(?:documents?|reports?|essays?|articles?|notes?|files?)\b"
    r".{0,80}(?:\b(save|store|put|keep)\b|\b(folder|directory)\b)",
    re.IGNORECASE,
)
_DOC_WRITE_PATTERN = re.compile(
    r"\b(write|create|make|draft)\b.{0,40}\b(document|report|essay|article|note)\b",
    re.IGNORECASE,
)
_FLIGHT_SEARCH_PATTERN = re.compile(
    r"\b(book|find|search|look for|plan|look up)\b.{0,20}\bflights?\b"
    r"|\bflights?\b.{0,40}\b(?:to|from|for|departing|returning)\b",
    re.IGNORECASE,
)
_HOTEL_SEARCH_PATTERN = re.compile(
    r"\b(book|find|search|look for|look up)\b.{0,20}\bhotels?\b",
    re.IGNORECASE,
)
# "research X" / "do research on X" / "deep dive into X" routes to the
# deterministic research pipeline (research_topic), never to the weak
# planner -- the planner has repeatedly picked browser_open for these.
_RESEARCH_PATTERN = re.compile(
    r"\b(?:do|conduct|perform|run|start)\b.{0,20}\b(?:a\s+)?(?:deep\s+)?research\b"
    r"|\b(?:research|deep dive)\b.{0,20}\b(?:on|about|into|of)\b"
    r"|\b(?:research|deep dive)\s+(?!on|about|into|of|papers?|methods?|"
    r"methodology|results?|findings?)\S",
    re.IGNORECASE,
)
# Blender requests route to the BlenderLLM specialist deterministically --
# the weak planner must never be asked to chain "generate code, then run it
# in Blender" on its own (and the old prompt told the model Blender was
# impossible). "research ... blender ..." still goes to the research
# pipeline (the blender check runs after the research check).
_BLENDER_PATTERN = re.compile(r"\bblender\b|\bbpy\b", re.IGNORECASE)
_DATE_HINT_PATTERN = re.compile(
    r"\b(?:on|for|until)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|"
    r"sunday|tomorrow|today|this\s+\w+|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|"
    r"apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|\d{1,2}[-/]\d{1,2}(?:[-/]\d{2,4})?)\b",
    re.IGNORECASE,
)
_TOPIC_STRIP_PHRASES = (
    "save it in a new folder named", "save it in the folder named",
    "save it in a folder named", "in a new folder named", "in the folder named",
    "in a folder named", "and save it in", "and save it as", "save it in",
    "save it as", "then save it", "and store it in", "and put it in",
    "google docs", "a new document", "a document", "an essay", "an article",
    "a report", "a note", "write a document about", "write a document on",
    "write an essay about", "write an essay on", "write an article about",
    "write an article on", "write a report about", "write a report on",
    "write a note about", "write a note on", "write about", "write on",
    "create a document about", "create a document on", "create a report about",
    "create an article about", "draft a document about", "draft a document on",
    "do a deep research on", "do a deep research about", "do deep research on",
    "do deep research about", "conduct research on", "conduct research about",
    "perform research on", "perform research about", "do research on",
    "do research about", "run research on", "run research about",
    "deep dive into", "deep dive on", "research report on", "research report about",
    "research on", "research about", "research into", "research",
    "and save it", "then save it", "and save", "save it", "about", "on",
    "please", "for me", "the topic of", "topic",
    "can you", "could you", "do you", "would you", "can i get",
    "for real", "right now", "i want", "i need", "i would like",
)


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip())
    return name or "document"


def _extract_topic(goal: str) -> str | None:
    """The subject of a document task: the quoted text first, then a
    targeted 'write ... about/on X' capture, then the goal with all
    known framing phrases stripped (longest phrases first)."""
    m = re.search(r"['\"]([^'\"]+)['\"]", goal or "")
    if m:
        return m.group(1).strip()
    m = re.search(
        r"\b(?:write|create|make|draft|type|save)\b.{0,15}\b(?:about|on|document about|document on)\s+(.+)",
        goal or "", re.IGNORECASE,
    )
    if m:
        topic = m.group(1).strip(" .,;:!?")
        # Cut at the save/folder part ("... and save it in a new folder ...",
        # "... to a folder called ...")
        topic = re.split(
            r"\b(?:and\s+)?(?:then\s+)?(?:save|store|put)\b|"
            r"\b(?:to|into|in|inside)\s+(?:a|my|our|the|your)\s+(?:new\s+)?(?:folder|directory)\b",
            topic, maxsplit=1, flags=re.IGNORECASE,
        )[0].strip(" .,;:!?")
        return topic or None
    text = goal or ""
    for phrase in sorted(_TOPIC_STRIP_PHRASES, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,;:!?")
    return text or None


def _extract_folder(goal: str, topic: str | None) -> str | None:
    m = re.search(
        r"\b(?:folder|directory)\s+(?:named\s+|called\s+)?['\"]?([^'\".,;]{2,60})['\"]?",
        goal or "", re.IGNORECASE,
    )
    if m:
        name = m.group(1).strip()
        if name and not name.lower().startswith(("named", "called")):
            return name
    if topic:
        return f"research of {topic}"
    return None


_TRAILING_STOP = r"(?=\s+(?:from|on|for|under|between|around|until|in|departing|returning|with|near|next|this|budget)|[,.;]|$)"


def _flight_search_url(goal: str) -> str | None:
    m = re.search(
        r"\bfrom\s+([^,.;]{2,40}?)\s+to\s+([^,.;]{2,40}?)" + _TRAILING_STOP,
        goal or "", re.IGNORECASE,
    )
    if m:
        query = f"flights from {m.group(1).strip()} to {m.group(2).strip()}"
    else:
        m = re.search(
            r"\bflights?\s+(?:to|towards)\s+([^,.;]{2,40}?)" + _TRAILING_STOP,
            goal or "", re.IGNORECASE,
        )
        if not m:
            return None
        query = f"flights to {m.group(1).strip()}"
    date = _DATE_HINT_PATTERN.search(goal or "")
    if date:
        query += " " + re.sub(r"\s+", " ", date.group(0).strip())
    return f"https://www.google.com/travel/flights?q={quote(query)}"


def _hotel_search_url(goal: str) -> str | None:
    m = re.search(
        r"\b(?:hotels?\s+)?(?:in|at|near)\s+([^,.;]{2,40}?)" + _TRAILING_STOP,
        goal or "", re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"\bhotels?\s+in\s+([^,.;]{2,40}?)" + _TRAILING_STOP,
            goal or "", re.IGNORECASE,
        )
    if not m:
        return None
    query = f"hotels in {m.group(1).strip()}"
    date = _DATE_HINT_PATTERN.search(goal or "")
    if date:
        query += " " + re.sub(r"\s+", " ", date.group(0).strip())
    return f"https://www.google.com/travel/hotels?q={quote(query)}"


def _detect_complex_task(goal: str) -> str | None:
    """Classify a fresh goal into a deterministic multi-step task, or None
    when the normal planner loop should handle it."""
    goal = goal or ""
    if _RESEARCH_PATTERN.search(goal):
        return "research"
    if _BLENDER_PATTERN.search(goal):
        return "blender"
    if _GDOCS_WRITE_PATTERN.search(goal):
        return "gdocs"
    if _DOC_TO_FOLDER_PATTERN.search(goal):
        return "document_to_folder"
    if _DOC_WRITE_PATTERN.search(goal):
        return "gdocs"
    if _FLIGHT_SEARCH_PATTERN.search(goal):
        return "flights"
    if _HOTEL_SEARCH_PATTERN.search(goal):
        return "hotels"
    return None


# ---------------------------------------------------------------------------
# Skills subsystem: deterministic command detection (watch me / do the X
# skill / show my skills / ...). Runs before the planner so skill commands
# never depend on the LLM; playback then drives itself with its own
# vision loop and only pauses when the user's input is genuinely needed.
# ---------------------------------------------------------------------------

_SKILL_LIST_PATTERN = re.compile(
    r"\b(?:show|list|see|what)\b[^.!?]{0,40}\bskills?\b", re.IGNORECASE
)

_SKILL_RECORD_STOP_PATTERN = re.compile(
    r"\b(?:stop|end|finish|done|save)\s+(?:the\s+|this\s+|my\s+)?recording\b"
    r"|\bsave\s+the\s+skill\b"
    r"|\b(?:i'?m|im)\s+done\b",
    re.IGNORECASE,
)

_SKILL_RECORD_PAUSE_PATTERN = re.compile(r"\bpause\s+(?:the\s+)?recording\b", re.IGNORECASE)
_SKILL_RECORD_RESUME_PATTERN = re.compile(
    r"\b(?:resume|continue)\s+(?:the\s+)?recording\b", re.IGNORECASE
)
_SKILL_RECORD_CANCEL_PATTERN = re.compile(
    r"\b(?:cancel|discard|abandon|forget)\s+(?:the\s+)?recording\b", re.IGNORECASE
)

_SKILL_RECORD_START_PATTERN = re.compile(
    r"\bwatch me\b"
    r"|\blearn this\b"
    r"|\b(?:start|begin)\s+recording\b"
    r"|\brecord\s+(?:a\s+|this\s+|my\s+|the\s+|a\s+new\s+)?"
    r"(?:skill|workflow|routine|macro|this|it)\b"
    r"|\b(?:teach|train)\s+me\b[^.!?]{0,30}\bskill\b",
    re.IGNORECASE,
)

_SKILL_RECORD_NAME_PATTERN = re.compile(
    r"\b(?:skill|workflow|routine|macro)\s+(?:called|named)\s+[\"']?([^\"'.!?]{2,60})[\"']?",
    re.IGNORECASE,
)

_SKILL_SAVE_AS_NAME_PATTERN = re.compile(
    # Allow any short run of filler words between "save" and "as" (e.g.
    # "save it", "save this skill", "save my skill", "save that", "save
    # the recording") instead of a fixed list -- the fixed list used to
    # miss common phrasings like "save this skill as X" or "save my
    # skill as X", silently falling through to an auto-generated name.
    r"\bsave\b[^,.!?]{0,30}?\bas\s+"
    r"[\"']?([^\"'.!?]+?)[\"']?"
    r"(?=\s*(?:,|\.|!|\?| and | then | after |,|\bplease\b|$))",
    re.IGNORECASE,
)

_STOP_INSTRUCTION_LEADERS = (
    r"^(?:and|then|also|after|please|finally|next|now|remember to|also to)\b\s*"
)


def _record_stop_instructions(goal: str) -> list[str]:
    """Extract written instructions the user typed alongside the stop
    command, e.g. "stop recording, save the skill as school files, read the
    title before downloading and navigate through the pages" ->
    ["read the title before downloading", "navigate through the pages"].
    These are captured as narration so the model understands them even when
    the user could not speak during the demo."""
    text = goal or ""
    text = _SKILL_SAVE_AS_NAME_PATTERN.sub(" ", text)
    text = _SKILL_RECORD_STOP_PATTERN.sub(" ", text)
    instructions = []
    for part in re.split(r"[,;]", text):
        part = re.sub(_STOP_INSTRUCTION_LEADERS, "", part.strip(), flags=re.IGNORECASE)
        part = re.sub(r"\s{2,}", " ", part).strip(" .,!?")
        if part:
            instructions.append(part)
    return instructions

_SKILL_PLAY_PATTERN = re.compile(
    r"\b(?:do|run|play|execute|start|perform|repeat|use)\b"
    r"\s+(?:the\s+|your\s+|my\s+|our\s+|a\s+)?"
    r"([\w][\w .\-']{1,60}?)"
    r"(?=\s+(?:skill|workflow|routine|macro)\b|[.!?]|$)",
    re.IGNORECASE,
)

_SKILL_RENAME_PATTERN = re.compile(
    r"\b(?:rename|re-title|change)\s+"
    r"(?:the\s+|this\s+|that\s+|it\s+|its\s+)?(?:skill\s+)?"
    r"(?:[\"']?([^\"']{2,60}?)[\"']?(?:\s+(?:skill|workflow|routine))?\s+)?"
    r"(?:to|as)\s+[\"']?([^\"',.!?]{2,60})(?:\s+(?:skill|workflow|routine))?[\"']?",
    re.IGNORECASE,
)

_SKILL_DELETE_PATTERN = re.compile(
    r"\b(?:delete|remove|erase)\s+(?:the\s+)?(?:skill\s+)?[\"']?([^\"']{2,60})[\"']?\b",
    re.IGNORECASE,
)

_SKILL_EXPORT_PATTERN = re.compile(
    r"\bexport\s+(?:the\s+)?(?:skill\s+)?[\"']?([^\"']{2,60})[\"']?\b",
    re.IGNORECASE,
)

_SKILL_DUPLICATE_PATTERN = re.compile(
    r"\b(?:duplicate|copy)\s+(?:the\s+)?(?:skill\s+)?[\"']?([^\"']{2,60}?)[\"']?"
    r"(?:\s+skill)?(?:\s+(?:as|to)\s+[\"']?([^\"']{2,60})[\"']?)?\b",
    re.IGNORECASE,
)

_SKILL_IMPORT_PATTERN = re.compile(
    r"\bimport\s+(?:a\s+|the\s+)?skill\s+(?:from\s+)?[\"']?([^\"']{2,120})[\"']?\b",
    re.IGNORECASE,
)

# Goals that should never auto-run a skill even on a high match: file
# operations and media playback go through their own deterministic paths.
_SKILL_AUTO_PLAY_BLOCKED = re.compile(
    r"\b(file|folder|directory|download|pdf|docx?|xlsx?|pptx?|zip|rar|tar|"
    r"video|music|song|playlist|watch)\b",
    re.IGNORECASE,
)


def _skill_edit_cue(goal: str) -> bool:
    """Editing commands (rename/delete/export/duplicate) only count as
    skill commands when the word 'skill' appears or the name is quoted --
    otherwise 'delete my old videos' would be swallowed as a skill
    command and never reach the file/planner paths."""
    return bool(re.search(r"\bskill\b", goal, re.IGNORECASE)) \
        or '"' in goal or "'" in goal


def _fuzzy_skill_name(name: str, floor: float = 0.55) -> str | None:
    """Resolve a (possibly vague) spoken name to a saved skill using
    token similarity, so 'do the save school' finds 'save school
    document'. Returns the canonical skill name or None."""
    needle = (name or "").strip().lower()
    if not needle:
        return None
    best, best_score = None, 0.0
    try:
        from rapidfuzz import fuzz
    except Exception:
        fuzz = None
    for record in skill_manager.list_skills():
        candidate = (record.get("name") or "").strip()
        if not candidate:
            continue
        if needle == candidate.lower():
            return candidate
        try:
            if fuzz is not None:
                score = float(fuzz.token_set_ratio(needle, candidate.lower()))
            else:
                import difflib
                score = difflib.SequenceMatcher(None, needle, candidate.lower()).ratio() * 100
        except Exception:
            continue
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= floor * 100 else None


def _detect_skill_request(goal: str) -> dict | None:
    """Detect an explicit skill command. Returns {"intent", "args"} or
    None when the goal is not a skill command."""
    goal = goal or ""

    if _SKILL_LIST_PATTERN.search(goal):
        return {"intent": "list", "args": {}}
    if _SKILL_RECORD_STOP_PATTERN.search(goal):
        save_match = _SKILL_SAVE_AS_NAME_PATTERN.search(goal)
        return {"intent": "record_stop",
                "args": {"name": save_match.group(1) if save_match else None,
                         "instructions": _record_stop_instructions(goal)}}
    if _SKILL_RECORD_PAUSE_PATTERN.search(goal):
        return {"intent": "record_pause", "args": {}}
    if _SKILL_RECORD_RESUME_PATTERN.search(goal):
        return {"intent": "record_resume", "args": {}}
    if _SKILL_RECORD_CANCEL_PATTERN.search(goal):
        return {"intent": "record_cancel", "args": {}}
    if _SKILL_RECORD_START_PATTERN.search(goal):
        name_match = _SKILL_RECORD_NAME_PATTERN.search(goal)
        return {"intent": "record_start", "args": {"name": name_match.group(1) if name_match else None}}

    if _SKILL_RENAME_PATTERN.search(goal) and _skill_edit_cue(goal):
        match = _SKILL_RENAME_PATTERN.search(goal)
        new_name = re.sub(
            r"\s+(?:skill|workflow|routine|macro)$", "", match.group(2), flags=re.IGNORECASE
        ).strip()
        return {"intent": "rename", "args": {"name": match.group(1), "new_name": new_name}}
    if _SKILL_DELETE_PATTERN.search(goal) and _skill_edit_cue(goal):
        match = _SKILL_DELETE_PATTERN.search(goal)
        return {"intent": "delete", "args": {"name": match.group(1)}}
    if _SKILL_EXPORT_PATTERN.search(goal) and _skill_edit_cue(goal):
        match = _SKILL_EXPORT_PATTERN.search(goal)
        return {"intent": "export", "args": {"name": match.group(1)}}
    if _SKILL_DUPLICATE_PATTERN.search(goal) and _skill_edit_cue(goal):
        match = _SKILL_DUPLICATE_PATTERN.search(goal)
        return {"intent": "duplicate",
                "args": {"name": match.group(1), "new_name": match.group(2)}}
    if _SKILL_IMPORT_PATTERN.search(goal):
        match = _SKILL_IMPORT_PATTERN.search(goal)
        return {"intent": "import", "args": {"path": match.group(1)}}

    play_match = _SKILL_PLAY_PATTERN.search(goal)
    if play_match:
        name = play_match.group(1).strip()
        explicit_skill = bool(re.search(r"\bskill\b", goal, re.IGNORECASE))
        # Without the literal word "skill" ("do the save school"), only
        # accept the play intent when the spoken name actually matches a
        # saved skill -- otherwise "do the dishes" would be hijacked.
        if explicit_skill or _fuzzy_skill_name(name):
            return {"intent": "play", "args": {"name": name}}

    return None


def _user_accepted(text: str) -> bool:
    """Did the user say yes to an improvement offer?"""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    if re.search(
        r"\b(no|nope|nah|don'?t|do not|never|keep it|leave it|as is)\b", lowered
    ):
        return False
    return bool(
        re.search(
            r"\b(yes|yeah|yep|sure|ok|okay|improve|update|go ahead|do it|please|y)\b",
            lowered,
        )
    )


def _skills_runtime_active() -> bool:
    """Unit tests must never touch the real skills directory."""
    import os

    return "PYTEST_CURRENT_TEST" not in os.environ


def _goal_looks_like_file_request(goal: str) -> bool:
    """Guard against auto-running a skill for goals that should go through
    the file manager's deterministic intent detection instead."""
    goal = goal or ""
    if not re.search(
        r"\b(open|find|search|show|list|rename|move|copy|delete|extract|unzip|"
        r"compress|zip|reveal|properties|where is|look for)\b", goal, re.IGNORECASE
    ):
        return False
    return bool(re.search(
        r"\b(pdf|docx?|xlsx?|pptx?|file|folder|directory|archive|zip|rar|tar|"
        r"downloads?|screenshot|notes|document|project|code|image|photo|picture)\b",
        goal, re.IGNORECASE,
    ))


def _read_resolved_page(page: dict) -> list[dict]:
    """Switch to the resolved tab and read it, returning observation dicts.

    Runs deterministically through run_tool so argument aliases apply;
    failures become FAILED observations (the planner sees them and can
    adapt) instead of aborting the task.
    """
    obs: list[dict] = []

    def _obs(tool: str, arguments: dict, success: bool, result) -> dict:
        return {
            "step": {"tool": tool, "arguments": arguments},
            "success": success,
            "result": result,
        }

    tab_id = page.get("tabId")
    switch_ok = True
    if not page.get("active") and tab_id is not None:
        try:
            switch_ok, switch_result = run_tool("browser_switch_tab", {"tab": tab_id})
            if not switch_ok:
                obs.append(_obs(
                    "browser_switch_tab",
                    {"tab": tab_id},
                    False,
                    switch_result,
                ))
                return obs
        except Exception as exc:
            obs.append(_obs("browser_switch_tab", {"tab": tab_id}, False, str(exc)))
            return obs

    try:
        read_ok, read_result = run_tool("browser_read", {})
    except Exception as exc:
        read_ok, read_result = False, str(exc)

    obs.append(_obs(
        "auto_tab_select",
        {"tabId": tab_id, "title": page.get("title"), "url": page.get("url")},
        True,
        {
            "resolved_page": page.get("title"),
            "url": page.get("url"),
            "tabId": tab_id,
            "note": (
                "This is the page the user meant. Read its content from the "
                "next observation; you do not need more tabs."
            ),
        },
    ))
    obs.append(_obs("browser_read", {}, read_ok, read_result))
    return obs


def _describes_unexecuted_action(response: str | None) -> bool:
    if not response:
        return False
    return bool(_UNEXECUTED_ACTION_PATTERN.search(response))


def _short_error(result) -> str:
    if isinstance(result, dict):
        return str(result.get("error") or result)[:120]
    return str(result)[:120]


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


# ---------------------------------------------------------------------------
# Loop guard: repeated identical tool calls
#
# Guards against the "planner keeps calling the same tool with the same
# arguments forever" failure mode (observed with browser_read: the call
# succeeded and returned a result, and the planner just kept repeating the
# identical call instead of acting on what it got). Within one task the
# loop tracks every tool call (tool + normalized arguments + result hash)
# and throttles consecutive identical proposals:
#   1st consecutive identical call  -> executed normally
#   2nd consecutive identical call  -> NOT re-executed; the identical
#                                      cached result is served instead
#   3rd consecutive identical call  -> BLOCKED; the tool is marked
#                                      unproductive for this goal and the
#                                      planner is forced to do something
#                                      different
#   4th consecutive identical call  -> AUTO-STOP: the loop ends the task
#                                      and asks the user for help
# The counts reset as soon as the tool or its arguments differ, so a
# legitimate second use of a tool (after a different action changed the
# page state) is never throttled. This is the enforcement backstop for the
# system-prompt rule (see brain.py) telling the model to act on tool
# results instead of re-issuing the same call.
TOOL_REPEAT_LIMIT = 2  # identical executions allowed in a row before reuse/block
IDENTICAL_BLOCK_AT = TOOL_REPEAT_LIMIT + 1  # 3rd consecutive proposal -> block
IDENTICAL_AUTO_STOP_AT = TOOL_REPEAT_LIMIT + 2  # 4th consecutive proposal -> stop


def _normalize_signature_arguments(arguments) -> dict:
    """Drop None-valued keys so {'tab': None} and {} count as the same call."""
    if not isinstance(arguments, dict):
        return {}
    return {key: value for key, value in arguments.items() if value is not None}


def _step_signature(tool, arguments) -> tuple:
    return (tool, json.dumps(_normalize_signature_arguments(arguments), sort_keys=True, default=str))


def _hash_result(result) -> str:
    try:
        blob = json.dumps(result, sort_keys=True, default=str)
    except Exception:
        blob = repr(result)
    return hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:12]


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
        self._consecutive_guard_firings = 0
        self._no_step_streak = 0
        self._tool_history: list[dict] = []
        self._result_cache: dict[tuple, dict] = {}
        self._unproductive_tools: set[str] = set()
        self._consecutive_signature: tuple | None = None
        self._consecutive_count = 0
        self._loop_guard_blocks = 0
        self._browser_ask_guard_fired = False
        self._pending_tab_choice = None
        # Risk-confirmation state: a medium/high-risk tool call needs the
        # user's OK before it actually runs. Survives turns (like
        # _pending_tab_choice) so the next reply ('yes'/'no') completes or
        # cancels it.
        self._pending_confirmation = None
        # Skills subsystem state (persists across turns like _pending_tab_choice):
        # a playback paused on a user question, a completed playback waiting
        # on the improvement offer, and suggestions shown this session.
        self._pending_skill_session = None
        self._pending_improvement_session = None
        # Set when stop_recording() couldn't get a name inline and is
        # waiting on the user's very next reply to name (and optionally
        # add instructions to) the skill just recorded.
        self._pending_skill_finalize = False
        self._suggested_phrases: set[str] = set()

    # ------------------------------------------------------------------ #
    # Deterministic complex-task handlers (pre-planner)
    # ------------------------------------------------------------------ #

    def _draft_content(self, topic: str | None) -> str:
        if not topic:
            return ""
        try:
            raw = self.brain.draft_document(topic)
        except Exception as exc:
            logger.error("[ComplexTask] Document draft failed: %s", exc)
            return ""
        return str(raw or "").strip()

    def _task_gdocs(self, user: str, goal: str) -> bool:
        """Open a new Google Doc and write the requested content into it."""
        topic = _extract_topic(goal)
        content = self._draft_content(topic) if topic else ""
        try:
            ok, res = run_tool("browser_open", {"url": "https://docs.new"})
        except Exception as exc:
            logger.error("[ComplexTask] gdocs open failed: %s", exc)
            return False
        if not ok:
            self.speak(f"I couldn't open Google Docs: {_short_error(res)}")
            return True
        try:
            run_tool("browser_wait_for_load", {"timeout": 20000})
        except Exception:
            pass
        try:
            run_tool("browser_wait_for_element", {"description": "[contenteditable]", "timeout": 20000})
        except Exception:
            pass
        if not content:
            self.speak("Google Docs is open with a new blank document. What should I write?")
            return True
        try:
            run_tool("browser_click", {"description": "Untitled document"})
            run_tool("browser_type", {"description": "Untitled document", "text": topic, "clear_first": True})
            run_tool("browser_press_key", {"key": "Enter"})
        except Exception as exc:
            logger.info("[ComplexTask] gdocs title rename failed (non-fatal): %s", exc)
        chunks = [content[i:i + 1500] for i in range(0, len(content), 1500)] or [""]
        for i, chunk in enumerate(chunks):
            try:
                ok, res = run_tool("browser_type", {
                    "description": "[contenteditable]",
                    "text": chunk,
                    "clear_first": i == 0,
                })
            except Exception as exc:
                logger.error("[ComplexTask] gdocs typing failed: %s", exc)
                self.speak("I opened Google Docs but had trouble writing the content.")
                return True
            if not ok and i == 0:
                try:
                    ok, res = run_tool("browser_type", {
                        "description": "Body",
                        "text": chunk,
                        "clear_first": True,
                    })
                except Exception as exc:
                    logger.error("[ComplexTask] gdocs typing fallback failed: %s", exc)
            if not ok:
                self.speak("I opened Google Docs but the editor wasn't ready for typing.")
                return True
        self.speak(f"Done — I've written a document on {topic} in a new Google Doc, open in your browser.")
        return True

    def _task_document_to_folder(self, user: str, goal: str) -> bool:
        """Create 'research of X' (or a user-named folder) and save a
        drafted document about the topic as a file in it."""
        topic = _extract_topic(goal)
        folder = _extract_folder(goal, topic)
        if not folder:
            return False
        folder_name = _sanitize_filename(folder)
        try:
            ok, res = run_tool("create_folder", {"path": folder_name})
        except Exception as exc:
            logger.error("[ComplexTask] folder create failed: %s", exc)
            return False
        if not ok:
            self.speak(f"I couldn't create the folder: {_short_error(res)}")
            return True
        content = self._draft_content(topic)
        if not content:
            self.speak(f"The folder '{folder_name}' is created, but I couldn't draft the document content.")
            return True
        filename = _sanitize_filename(topic or "document") + ".md"
        path = Path(folder_name) / filename
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.error("[ComplexTask] document save failed: %s", exc)
            self.speak("The folder was created, but saving the document failed.")
            return True
        logger.info("[ComplexTask] Saved document: %s", path)
        self.speak(f"Done. I created the folder '{folder_name}' and saved the document on {topic} there: {path}")
        return True

    def _task_blender(self, user: str, goal: str) -> bool:
        """Generate Blender Python via the BlenderLLM specialist.

        Generation only -- the specialist never executes anything, and the
        planner prompt forbids claiming it did. Execution is a separate
        follow-up the user can request ("run it in Blender"), which then
        goes through blender_execute's explicit confirmation flow.
        """
        try:
            ok, res = run_tool("blender_generate", {"request": goal})
        except Exception as exc:
            logger.error("[ComplexTask] blender_generate failed: %s", exc)
            self.speak("The Blender specialist hit an error and didn't generate a script.")
            return True
        if not ok:
            self.speak(f"I couldn't generate Blender code: {_short_error(res)}")
            return True
        reply = (res.get("reply") if isinstance(res, dict) else None) or str(res)
        if reply:
            self.speak(reply)
        if isinstance(res, dict) and res.get("code_ready"):
            self.speak(
                "The script is ready. I can run it in Blender when you want — "
                "just say so and I'll confirm with you first."
            )
        return True

    def _task_research(self, user: str, goal: str) -> bool:
        """Run the full research pipeline (research_topic) directly --
        the planner is never asked to pick a tool for these."""
        topic = _extract_topic(goal)
        if not topic:
            return False
        self.speak(f"Starting in-depth research on {topic}. This takes a couple of minutes.")
        try:
            ok, res = run_tool("research_topic", {"topic": topic})
        except Exception as exc:
            logger.error("[ComplexTask] research_topic failed: %s", exc)
            self.speak("The research pipeline hit an error and didn't complete.")
            return True
        if not ok:
            self.speak(f"I couldn't complete the research: {_short_error(res)}")
            return True
        folder = (res or {}).get("folder") if isinstance(res, dict) else None
        self.speak(
            f"Research on {topic} is done. The report and sources are saved in {folder}."
            if folder
            else f"Research on {topic} is done."
        )
        return True

    def _task_flights(self, user: str, goal: str) -> bool:
        """Open a pre-built Google Flights search. Returns False so the
        planner continues reading/interacting with the results."""
        url = _flight_search_url(goal)
        if not url:
            return False
        try:
            ok, res = run_tool("browser_open", {"url": url})
            if ok:
                run_tool("browser_wait_for_load", {"timeout": 20000})
        except Exception as exc:
            logger.error("[ComplexTask] flights open failed: %s", exc)
        return False

    def _task_hotels(self, user: str, goal: str) -> bool:
        url = _hotel_search_url(goal)
        if not url:
            return False
        try:
            ok, res = run_tool("browser_open", {"url": url})
            if ok:
                run_tool("browser_wait_for_load", {"timeout": 20000})
        except Exception as exc:
            logger.error("[ComplexTask] hotels open failed: %s", exc)
        return False

    def _handle_complex_task(self, user: str, goal: str, task: str) -> bool:
        handler = {
            "gdocs": self._task_gdocs,
            "document_to_folder": self._task_document_to_folder,
            "flights": self._task_flights,
            "hotels": self._task_hotels,
            "research": self._task_research,
            "blender": self._task_blender,
        }.get(task)
        if handler is None:
            return False
        return handler(user, goal)

    # ------------------------------------------------------------------ #
    # Skills subsystem handlers (pre-planner, deterministic)
    # ------------------------------------------------------------------ #

    def _handle_skill_request(self, user: str, goal: str, parsed: dict) -> str:
        """Handle an explicit skill command. Returns "done" (fully
        handled), "paused" (playback paused on a user question), or None
        (not handled -- let the planner try)."""
        intent = parsed.get("intent")
        args = parsed.get("args") or {}
        try:
            if intent == "record_start":
                result = skill_manager.start_recording(args.get("name"))
                self.speak(result["speak"])
                return "done"
            if intent == "record_stop":
                for instruction in (args.get("instructions") or []):
                    skill_manager.add_narration(instruction)
                result = skill_manager.stop_recording(args.get("name"))
                self.speak(result["speak"])
                if result.get("need_name"):
                    self._pending_skill_finalize = True
                    return "paused"
                return "done"
            if intent == "record_pause":
                result = skill_manager.pause_recording()
                self.speak(result["speak"])
                return "done"
            if intent == "record_resume":
                result = skill_manager.resume_recording()
                self.speak(result["speak"])
                return "done"
            if intent == "record_cancel":
                result = skill_manager.cancel_recording()
                self.speak(result["speak"])
                return "done"

            if intent == "list":
                self._speak_skill_list()
                return "done"

            if intent == "rename":
                old_name = args.get("name")
                new_name = args.get("new_name")
                result = skill_manager.rename_skill(
                    self._editable_skill_name(old_name), new_name
                )
                self.speak(result["speak"])
                return "done"
            if intent == "delete":
                result = skill_manager.delete_skill(args.get("name"))
                self.speak(result["speak"])
                return "done"
            if intent == "export":
                result = skill_manager.export_skill(args.get("name"))
                self.speak(result["speak"])
                return "done"
            if intent == "duplicate":
                result = skill_manager.duplicate_skill(
                    args.get("name"), args.get("new_name")
                )
                self.speak(result["speak"])
                return "done"
            if intent == "import":
                result = skill_manager.import_skill(args.get("path"))
                self.speak(result["speak"])
                return "done"

            if intent == "play":
                name = args.get("name")
                if not name:
                    self.speak("Which skill should I run? Say 'do the <skill name> skill'.")
                    return "done"
                return self._start_skill_playback(name)
        except Exception as exc:
            logger.exception("[Skills] Skill request '%s' failed", intent)
            self.speak(f"Something went wrong with that skill request: {exc}")
            return "done"
        return None

    def _editable_skill_name(self, raw: str | None) -> str | None:
        """Resolve a name for rename/delete/duplicate. Pronouns like
        'it'/'this'/'that'/'the last one' (or a missing name, as in
        'change it to X skill') resolve to the most recently recorded
        skill. Returns None only when nothing can be resolved."""
        if not raw:
            return None
        cleaned = raw.strip().strip('"\'')
        if cleaned.lower() in ("it", "this", "that", "this one", "the last one",
                                "the previous one", "the skill"):
            return self._most_recent_skill_name()
        return raw.strip()

    def _most_recent_skill_name(self) -> str | None:
        skills = skill_manager.list_skills()
        if not skills:
            return None

        def sort_key(record):
            return record.get("last_used") or record.get("created") or ""
        return max(skills, key=sort_key).get("name")

    def _speak_skill_list(self) -> None:
        skills = skill_manager.list_skills()
        if not skills:
            self.speak(
                "You don't have any skills yet. Say 'watch me' and perform a task "
                "once -- I'll learn it as a reusable skill."
            )
            return
        lines = []
        for skill in skills:
            name = skill.get("name", "")
            description = skill.get("description", "")
            used = skill.get("last_used")
            suffix = f" (last used {used[:10]})" if used else ""
            lines.append(f"{name} -- {description or 'no description'}{suffix}")
        summary = "; ".join(lines)
        self.speak(f"You have {len(skills)} skill{'s' if len(skills) != 1 else ''}: {summary}")

    def _start_skill_playback(self, name: str) -> str:
        result = skill_manager.play(name)
        return self._consume_playback_result(result)

    def _continue_skill_playback(self, session, user: str) -> str:
        result = skill_manager.continue_playback(session, user)
        return self._consume_playback_result(result)

    def _consume_playback_result(self, result: dict) -> str:
        """Drive one step of playback. Returns "paused" when the user's
        input is needed (the loop pauses and resumes next turn), else
        "done" (playback finished or failed)."""
        status = result.get("status")
        if status == "need_input":
            self._pending_skill_session = result.get("session")
            self.speak(result.get("message") or "")
            return "paused"
        self._pending_skill_session = None
        if status == "done":
            self.speak(result.get("message") or "The skill playback finished.")
            if result.get("improvement_offer"):
                self._pending_improvement_session = result.get("session")
            return "done"
        self.speak(result.get("message") or "The skill playback stopped.")
        return "done"

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
        mid-task (a genuine clarifying question, a recovery dead-end, the
        planner repeatedly failing to produce a step or answer, or the
        iteration budget ran out) -- the caller should pass this
        straight back in as resume_goal/resume_observations on the next
        call so the user's reply continues the SAME task instead of
        starting an unrelated new one.
        """
        clear_tool_context()

        logger.info("[Agent] Received user request: %s", user[:200])

        user = normalize_user_input(user)
        goal = normalize_user_input(resume_goal or user)
        observations: list[dict] = list(resume_observations) if resume_observations else []
        recovery_attempts = 0
        last_failed_signature: tuple | None = None
        repeat_failures = 0
        self._last_response = ""
        self._consecutive_guard_firings = 0
        self._no_step_streak = 0
        self._consecutive_signature = None
        self._consecutive_count = 0
        self._loop_guard_blocks = 0
        self._browser_ask_guard_fired = False

        if resume_goal:
            logger.info(
                "[Agent] Resuming pending task (%d prior observation(s)): %s",
                len(observations), goal[:120],
            )
        else:
            # A fresh task gets a fresh loop-guard memory: previous calls,
            # cached results and unproductive-tool marks all belong to the
            # prior goal. A resumed task keeps them so the same repeated
            # call cannot restart after a user reply.
            logger.info("[LoopGuard] New task -- tool history and result cache reset")
            self._tool_history = []
            self._result_cache = {}
            self._unproductive_tools = set()
            self._pending_tab_choice = None

        # Skills subsystem resume: a playback paused on a user question, or
        # a finished playback waiting on the improvement offer. These states
        # live on the loop (like _pending_tab_choice) so they survive turns.
        if resume_goal and self._pending_improvement_session is not None:
            session = self._pending_improvement_session
            self._pending_improvement_session = None
            try:
                result = skill_manager.apply_improvement(session, accept=_user_accepted(user))
                self.speak(result["speak"])
            except Exception as exc:
                logger.exception("[Skills] Improvement decision failed")
                self.speak(f"I couldn't apply that change to the skill: {exc}")
            return None

        if resume_goal and self._pending_skill_session is not None:
            outcome = self._continue_skill_playback(self._pending_skill_session, user)
            if outcome == "paused":
                return {"goal": goal, "observations": list(observations)}
            return None

        if resume_goal and self._pending_skill_finalize:
            self._pending_skill_finalize = False
            try:
                result = skill_manager.finish_recording(user)
                self.speak(result["speak"])
            except Exception as exc:
                logger.exception("[Skills] Finalizing recording failed")
                self.speak(f"I couldn't finish saving that skill: {exc}")
            return None

        # Risk confirmation resume: a previous turn paused because a
        # medium/high-risk tool needed approval. The user's reply decides:
        # 'yes' -> re-run the exact step with confirm=True; 'no' -> mark it
        # declined and let the planner pick a different path.
        if resume_goal and self._pending_confirmation is not None:
            pending = self._pending_confirmation
            self._pending_confirmation = None
            if _user_accepted(user):
                logger.info(
                    "[Confirm] User confirmed -- re-running %s with confirm=True",
                    pending["tool"],
                )
                step = {"tool": pending["tool"],
                        "arguments": dict(pending["arguments"], confirm=True)}
                try:
                    outcome = self._execute_tool_step(user, step)
                except RecommendationConfigurationError as error:
                    self.speak(str(error))
                    return None
                if outcome["stop"]:
                    return None
                observations.append({
                    "step": step,
                    "success": outcome["success"],
                    "result": outcome["result"],
                    "confirmed_by_user": True,
                })
                logger.info("[Confirm] Confirmed step executed (success=%s)", outcome["success"])
            else:
                logger.info("[Confirm] User declined confirmation for %s", pending["tool"])
                self.speak("Understood — I'll skip that action.")
                observations.append({
                    "step": {"tool": pending["tool"], "arguments": pending["arguments"]},
                    "success": False,
                    "result": "User declined confirmation.",
                    "declined_by_user": True,
                })

        # Deterministic complex-task handling: fresh goals that match a
        # known multi-step workflow (Google Docs writing, document saved
        # into a new folder, flight/hotel search) run through a
        # deterministic executor instead of relying on the weak planner
        # to chain 3+ tool calls correctly. Flight/hotel handlers return
        # False (they only pre-open the search) so the planner still
        # drives the interactive part.
        if not resume_goal and not observations:
            task = _detect_complex_task(goal)
            if task:
                logger.info("[ComplexTask] Detected task '%s' for goal: %s", task, goal[:120])
                handled = self._handle_complex_task(user, goal, task)
                if handled:
                    return None

            # Skills subsystem -- explicit skill commands ("watch me",
            # "do the X skill", "show my skills", ...) run deterministically
            # before the planner ever sees them.
            skill_request = _detect_skill_request(goal)
            if skill_request:
                logger.info("[Skills] Detected request '%s' for goal: %s",
                            skill_request["intent"], goal[:120])
                outcome = self._handle_skill_request(user, goal, skill_request)
                if outcome == "paused":
                    return {"goal": goal, "observations": list(observations)}
                if outcome == "done":
                    return None
                logger.info("[Skills] Request '%s' unhandled -- falling through to planner",
                            skill_request["intent"])

            # While a recording is active, any speech that is NOT a skill
            # command is narration: the user is explaining the task as they
            # demonstrate it ("read the title of the document before
            # downloading..."). Capture it into the recording instead of
            # dispatching it to the planner as a brand-new task. This never
            # touches the skills directory -- it only appends to the active
            # recorder's in-memory event log -- so it is safe even when the
            # manager is mocked or a recording is inactive (add_narration
            # returns captured=False in those cases).
            if skill_manager.is_recording:
                if not skill_request:
                    note = skill_manager.add_narration(goal)
                    if note.get("captured"):
                        logger.info("[Skills] Captured narration while recording: %s",
                                    goal[:120])
                        self.speak(note.get("speak", ""))
                        return None

            # Skills subsystem -- implicit integration: track repeated
            # requests for learning suggestions (Phase 5) and auto-run a
            # matching skill instead of rebuilding the workflow.
            if _skills_runtime_active():
                try:
                    suggestion = skill_manager.observe_request(goal)
                    if suggestion:
                        key = suggestion[:30]
                        if key not in self._suggested_phrases:
                            self._suggested_phrases.add(key)
                            self.speak(suggestion)
                    if (
                        not self._pending_skill_session
                        and not _SKILL_AUTO_PLAY_BLOCKED.search(goal)
                        and not _goal_looks_like_file_request(goal)
                    ):
                        auto = skill_manager.auto_play_candidate(goal)
                        if auto:
                            name = auto.get("name")
                            logger.info("[Skills] Auto-running matching skill '%s'", name)
                            self.speak(
                                f"I have a skill for that -- '{name}'. Running it now."
                            )
                            outcome = self._start_skill_playback(name)
                            if outcome == "paused":
                                return {"goal": goal, "observations": list(observations)}
                            return None
                except Exception:
                    logger.exception("[Skills] Automatic matching failed -- continuing normally")

        # Deterministic page resolution: when the goal asks to CONSUME a
        # page ("summarise my chatgpt tab", "what's on the page") and no
        # tool has run yet, the loop itself finds the open tab the user
        # means, switches to it and reads it -- handing the page text to
        # the planner as observations. The model cannot stall asking
        # "which tab?" or "what does it say?" because the answer is
        # already in front of it.
        if not observations and _goal_references_browser(goal) and _goal_asks_to_read(goal):
            # A confirmation question was pending from the previous turn:
            # map the user's reply ("2", "the second one", a title) to a
            # tab from the list and read it.
            if self._pending_tab_choice:
                picked = _match_pending_choice(user, self._pending_tab_choice)
                if picked:
                    auto_obs = _read_resolved_page(picked)
                    if auto_obs:
                        observations.extend(auto_obs)
                        self._pending_tab_choice = None
                        logger.info(
                            "[Agent] Read page chosen from confirmation list: '%s'",
                            picked.get("title"),
                        )
                else:
                    logger.info(
                        "[Agent] Reply did not match any candidate tab -- re-running resolution"
                    )
                    self._pending_tab_choice = None
            if not observations:
                resolved = _resolve_browser_page(goal)
                if resolved and resolved.get("ambiguous"):
                    candidates = resolved.get("candidates") or []
                    self._pending_tab_choice = candidates
                    logger.info(
                        "[Agent] Multiple similar tabs (%s) -- asking the user to pick one",
                        len(candidates),
                    )
                    self.speak(_confirmation_message(candidates))
                    return {"goal": goal, "observations": []}
                if resolved:
                    auto_obs = _read_resolved_page(resolved)
                    if auto_obs:
                        observations.extend(auto_obs)
                        logger.info(
                            "[Agent] Auto-resolved page '%s' (%s) and read it before planning",
                            resolved.get("title"), resolved.get("url"),
                        )

        # Track A shortcut: a plain consume goal ("summarise the page",
        # "what's on my tab") with the page text already in hand (just
        # auto-read, or carried over from a pending task) is answered
        # DIRECTLY -- the planner's JSON decision loop has nothing to add,
        # and a weak planner uses it to bounce questions back instead of
        # answering. Interactive words ("check", "see") and comparisons
        # stay on the planner path. A failed or empty direct answer falls
        # through to normal planning.
        page_text = _extract_read_text(observations)
        if (
            page_text
            and _goal_references_browser(goal)
            and _goal_asks_to_read(goal)
            and _goal_is_direct_answer(goal)
        ):
            try:
                answer = self.brain.answer_from_page(user, goal=goal, page_text=page_text)
            except Exception as exc:
                logger.error("[Agent] Direct page answer failed: %s", exc)
                self.speak(_BACKEND_ERROR_RESPONSE)
                return None
            answer_text = (answer or "").strip()
            if answer_text:
                logger.info("[Agent] Answering directly from resolved page text (planner bypassed)")
                self._last_response = answer_text
                self.speak(answer_text)
                return None
            logger.info("[Agent] Direct page answer came back empty -- falling back to the planner")

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

            decision = _sanitize_backend_error(decision)

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
                self._consecutive_guard_firings += 1
                if response:
                    self._last_response = response
                if self._consecutive_guard_firings > 3:
                    logger.warning(
                        "[Planner] Guard fired %s times consecutively; breaking out of loop",
                        self._consecutive_guard_firings,
                    )
                    self.speak(
                        "I seem to be going in circles. Let me pause and ask — "
                        "could you rephrase what you need or give me more specific instructions?"
                    )
                    return _pending_result()
                continue

            # Reject an ask_user when the goal points at something IN the
            # browser (an open tab, a page) and the planner is trying to
            # make the user do its own job -- either no tool has run yet
            # (it must locate the page itself) or the page was ALREADY
            # read and its text is in the observations (it must answer
            # from that text). The question is not spoken, and the model
            # is forced to act.
            browser_ask_rejected = False
            if (
                decision.get("ask_user")
                and not self._browser_ask_guard_fired
                and _goal_references_browser(goal)
                and _goal_asks_to_read(goal)
                and (not observations or _browser_content_available(observations))
            ):
                self._browser_ask_guard_fired = True
                browser_ask_rejected = True
                hint = None
                if _browser_content_available(observations):
                    hint = (
                        "Your question to the user was rejected: the page was already "
                        "read and its text is in the observations above. Write the answer "
                        "from that text now and set done=true. If the text is empty, set "
                        "done=true and say the page appears to have no readable content. "
                        "NEVER ask the user to provide the page's content."
                    )
                else:
                    # The initial auto-resolution found nothing. The planner
                    # is refusing to look -- retry the deterministic
                    # resolution right here before even hinting.
                    resolved = _resolve_browser_page(goal)
                    if resolved and not resolved.get("ambiguous"):
                        auto_obs = _read_resolved_page(resolved)
                        if auto_obs:
                            observations.extend(auto_obs)
                            logger.info(
                                "[Agent] Re-resolved page '%s' after rejected ask_user",
                                resolved.get("title"),
                            )
                        else:
                            hint = _LOCATE_PAGE_HINT
                    else:
                        hint = _LOCATE_PAGE_HINT
                if hint:
                    observations.append({
                        "step": {"tool": "_browser_ask_guard", "arguments": {}},
                        "success": False,
                        "result": hint,
                    })
                logger.info(
                    "[Agent] Rejected ask_user on iteration %s: goal references the "
                    "browser -- forced planner action",
                    iteration,
                )

            # Speak the response ONLY if it is a real message for the user.
            # "I will now..."-style narration (even on a turn that has a
            # step) is the model's running commentary, not an answer --
            # repeating it aloud every iteration is what made multi-step
            # tasks sound like an endless stalling loop. Also skip speaking
            # if this response is identical to the last one spoken.
            if browser_ask_rejected:
                logger.info("[TTS] Suppressed rejected ask_user response: %s", (response or "")[:80])
            elif response and not _describes_unexecuted_action(response):
                if response == self._last_response:
                    logger.info("[TTS] Duplicate response prevented at loop level: %s", response[:60])
                else:
                    self._last_response = response
                    logger.info("[Agent] Responding to user")
                    self.speak(response)
            elif response:
                logger.info("[TTS] Suppressed narrated-action response: %s", response[:80])

            if decision.get("done"):
                self._consecutive_guard_firings = 0
                logger.info("[Agent] Goal marked complete on iteration %s", iteration)
                return None

            if decision.get("ask_user"):
                self._consecutive_guard_firings = 0
                if browser_ask_rejected:
                    continue
                logger.info("[Agent] Awaiting user clarification on iteration %s; task pending", iteration)
                return _pending_result()

            if not step:
                # LLM returned no tool step — if it's the first turn and
                # the fallback catches it (youtube/file), dispatch there.
                if not observations and self.fallback and self.fallback(user, intent_hint):
                    return None

                # A weak planner that has already gathered the data it
                # needs will often keep returning narration ("I will now
                # summarize...") or nothing instead of the final answer.
                # Count consecutive no-step turns: on the first one tell it
                # to ANSWER (not call another tool), and after three give
                # up instead of looping forever.
                self._no_step_streak += 1
                logger.warning(
                    "LLM returned no step on iteration %s (no-step streak %d)",
                    iteration, self._no_step_streak,
                )

                if self._no_step_streak >= 3:
                    logger.warning("LLM returned no step 3 times in a row; ending loop")
                    if response and not _describes_unexecuted_action(response):
                        self.speak(response)
                    else:
                        self.speak(
                            "I've gathered some information, but I'm having trouble "
                            "putting together the final answer. Could you rephrase "
                            "what you need?"
                        )
                    return _pending_result()

                if observations:
                    # Data is already gathered from earlier tool calls --
                    # the planner should FINISH, not call yet another tool.
                    observations.append({
                        "step": {"tool": "_finish_hint", "arguments": {}},
                        "success": False,
                        "result": (
                            "Do NOT call another tool -- you already have the "
                            "information needed from the previous tool results. "
                            "Finish the task NOW: return done=true and write the "
                            "complete final answer in `response`."
                        ),
                    })
                    continue

                if not response:
                    continue

                # Nothing has been done yet and the planner just talked
                # instead of acting: force a real tool step.
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
            self._consecutive_guard_firings = 0
            self._no_step_streak = 0  # a step was returned -- progress
            batch = decision.get("steps") or [step]

            tool = step.get("tool")
            arguments = step.get("arguments", {})
            step_signature = _step_signature(tool, arguments)
            success = True
            result = None
            guard_blocked = False

            logger.info(
                "[Planner] Selected tool: %s%s",
                tool, " (+%d more queued)" % (len(batch) - 1) if len(batch) > 1 else "",
            )

            for batch_step in batch:
                tool = batch_step.get("tool")
                arguments = batch_step.get("arguments", {})
                step_signature = _step_signature(tool, arguments)

                guard_action = self._guard_identical_call(tool, step_signature)

                if guard_action == "reuse":
                    cached = self._result_cache.get(step_signature)
                    if cached is None:
                        guard_action = "execute"
                    else:
                        logger.info(
                            "[LoopGuard] Serving cached result for identical consecutive "
                            "call: %s args=%s (consecutive x%d)",
                            tool, arguments, self._consecutive_count,
                        )
                        success = cached["success"]
                        result = cached["result"]
                        observations.append({
                            "step": batch_step,
                            "success": success,
                            "result": result,
                            "reused_from_cache": True,
                        })
                        safe_print(f"\n[{tool}]")
                        safe_print(result)
                        continue

                if guard_action in ("block", "auto_stop"):
                    self._loop_guard_blocks += 1
                    self._unproductive_tools.add(tool)
                    cached = self._result_cache.get(step_signature, {})
                    hash_note = ""
                    if cached.get("result_hash"):
                        hash_note = f" (result hash {cached['result_hash']} every time)"
                    message = (
                        f"Loop guard: {tool} was called with identical arguments "
                        f"{self._consecutive_count} times in a row{hash_note} and the "
                        f"result never changed. It is marked UNPRODUCTIVE for this goal. "
                        f"Repeating it cannot succeed. Choose a DIFFERENT tool or "
                        f"different arguments, explain why the goal cannot be completed, "
                        f"or ask the user for clarification."
                    )
                    if guard_action == "auto_stop":
                        logger.error(
                            "[LoopGuard] Auto-stopping: identical tool call %s args=%s "
                            "repeated %d consecutive times",
                            tool, arguments, self._consecutive_count,
                        )
                        observations.append({
                            "step": batch_step,
                            "success": False,
                            "result": message,
                            "blocked_by_loop_guard": True,
                            "auto_stopped": True,
                        })
                        self.speak(
                            f"I keep trying the same action ({tool}) with identical inputs "
                            f"and it is not making progress, so I'm stopping. Could you "
                            f"rephrase what you need or give me more details?"
                        )
                        return _pending_result()
                    logger.warning(
                        "[LoopGuard] Blocked repeated identical call: %s args=%s "
                        "(consecutive x%d) -- tool marked unproductive",
                        tool, arguments, self._consecutive_count,
                    )
                    observations.append({
                        "step": batch_step,
                        "success": False,
                        "result": message,
                        "blocked_by_loop_guard": True,
                    })
                    guard_blocked = True
                    break

                try:
                    outcome = self._execute_tool_step(user, batch_step)
                except RecommendationConfigurationError as error:
                    self.speak(str(error))
                    safe_print(f"\n[{tool}]")
                    safe_print(error)
                    return None

                # A medium/high-risk tool asked for user approval: pause the
                # loop, remember the step, and end this turn asking the user.
                # Their next reply ('yes'/'no') re-runs or cancels the step.
                if outcome.get("needs_confirmation"):
                    self._pending_confirmation = outcome.get("pending_confirmation")
                    self.speak(
                        outcome["result"].get(
                            "message", "This action needs your confirmation."
                        )
                    )
                    return _pending_result()

                if outcome["stop"]:
                    return None

                success = outcome["success"]
                result = outcome["result"]

                self._record_tool_call(batch_step, step_signature, success, result)

                observations.append({
                    "step": batch_step,
                    "success": success,
                    "result": result,
                })

                if not success:
                    step = batch_step
                    break

            if guard_blocked:
                # The planner was told to do something different; skip the
                # normal success/failure handling and give it another turn.
                self._consecutive_guard_firings = 0
                continue

            if success:
                self._consecutive_guard_firings = 0
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
            recovery = _sanitize_backend_error(recovery)
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
            recovery_signature = _step_signature(recovery_tool, recovery_args)

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
                self._consecutive_guard_firings = 0
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

        # Risk gate: a tool returned requires_confirmation (medium/high risk).
        # Do NOT treat this as a failure for recovery -- pause and ask the
        # user; their next reply re-runs the step with confirm=True.
        if isinstance(result, dict) and result.get("requires_confirmation"):
            logger.info("[Confirm] %s needs user confirmation before running", tool)
            pending = {
                "tool": tool,
                "arguments": arguments,
                "message": result.get("message", f"{tool} needs your confirmation."),
            }
            self._pending_confirmation = pending
            return {"success": False, "result": result, "stop": False,
                    "needs_confirmation": True,
                    "pending_confirmation": pending}

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
        elif tool == "browser_open" and success:
            # browser_open always opens a new tab and is typically the
            # only step for simple "open X" tasks. Returning stop=True
            # prevents the LLM from calling it again on the next turn
            # and forces it to either set done=true or proceed to the
            # next logical step (reading, interacting, etc.).
            logger.info("browser_open succeeded; returning control for next decision")
            stop = True

        return {"success": success, "result": result, "stop": stop}

    def _guard_identical_call(self, tool: str, signature: tuple) -> str:
        """Track consecutive identical (tool, normalized arguments) proposals.

        Returns one of:
          "execute"   - first identical proposal; execute the tool
          "reuse"     - second identical proposal; serve the cached result
          "block"     - third identical proposal; do not execute, force a
                        change (tool gets marked unproductive for the task)
          "auto_stop" - fourth+ identical proposal; end the task and ask
                        the user for help
        """
        if signature == self._consecutive_signature:
            self._consecutive_count += 1
        else:
            self._consecutive_signature = signature
            self._consecutive_count = 1

        if self._consecutive_count >= IDENTICAL_AUTO_STOP_AT:
            return "auto_stop"
        if self._consecutive_count >= IDENTICAL_BLOCK_AT:
            return "block"
        if self._consecutive_count > 1:
            return "reuse"
        return "execute"

    def _record_tool_call(self, step: dict, signature: tuple, success: bool, result) -> None:
        """Remember every executed tool call (tool + normalized arguments +
        result hash) so the guard can detect identical repeats and serve
        cached results without re-executing."""
        entry = {
            "tool": step.get("tool"),
            "arguments": step.get("arguments", {}),
            "signature": signature,
            "success": success,
            "result": result,
            "result_hash": _hash_result(result),
        }
        self._tool_history.append(entry)
        self._result_cache[signature] = {
            "result": result,
            "success": success,
            "result_hash": entry["result_hash"],
        }

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