"""Blender Python generation support for BlenderLLM V0.3.

Two small, model-free pieces of logic:

1. is_code_request() - lightweight detection of requests that want
   Blender Python. No ML, just signal words and question-style openers.
2. extract_python_code() - pulls the python code block out of a model
   response so a future execution layer (V0.4) can consume it cleanly,
   separated from the surrounding explanation and notes.

This module never executes anything.
"""

import re

# Strong signals: almost always mean "give me Blender Python".
_STRONG_SIGNALS = (
    "script", "code", "blender python", "python code", "write a", "write an",
    "generate", "automate", "procedural", "debug", "fix", "failing",
    "error in",
)

# Weak signals: usually mean "do/build this in code", not always.
_WEAK_SIGNALS = (
    "create a", "create an", "make a", "make an", "build a", "build an",
    "add a", "add an", "set up", "modify", "nodes", "bmesh", "from_pydata",
)

# Question-style openings: conceptual questions, not code requests.
_QUESTION_OPENERS = (
    "what is", "what are", "what's", "whats", "how do i", "how does",
    "how can i", "why", "when", "where", "can i", "is it", "explain",
    "difference", "vs",
)


def is_code_request(text):
    """Return True when the user message asks for Blender Python.

    Strong signals win (even inside a question). Otherwise question-style
    openings are treated as conceptual questions, and a weak signal alone
    marks a code request.
    """
    query = text.lower()
    if any(signal in query for signal in _STRONG_SIGNALS):
        return True
    if any(opener in query for opener in _QUESTION_OPENERS):
        return False
    return any(signal in query for signal in _WEAK_SIGNALS)


_FENCED_BLOCK = re.compile(r"```([a-zA-Z0-9_+-]*)\s*\n(.*?)```", re.DOTALL)


def extract_python_code(response):
    """Return the first python code block in a model response, or None.

    Prefers blocks explicitly tagged ```python (or ```py); falls back to
    the first untagged fenced block so code inside a plain ``` block is
    still usable.
    """
    blocks = _FENCED_BLOCK.findall(response or "")
    tagged = [(lang.strip().lower(), code) for lang, code in blocks]
    for lang, code in tagged:
        if lang in ("python", "py"):
            return code.strip()
    for lang, code in tagged:
        if not lang:
            return code.strip()
    return None
