"""Blender knowledge/context layer for BlenderLLM V0.2.

Loads curated Markdown files from the configured knowledge directory,
selects the most relevant files for a user question using simple
keyword matching, and formats them as prompt-time reference context.

This is NOT RAG: no embeddings, no vector database, no indexing beyond
keywords. Knowledge files are reference material for the model's context,
never training data.
"""

import re
from pathlib import Path

from config import KNOWLEDGE_DIR, MAX_CONTEXT_CHARS, MAX_CONTEXT_FILES

# Knowledge files start with a keyword line, e.g.:
#   keywords: material, materials, principled bsdf, shader
_KEYWORDS_LINE = re.compile(r"^keywords:\s*(.+)$", re.IGNORECASE)

# Short note separating the knowledge block from the system instructions.
REFERENCE_NOTE = (
    "The following sections are reference material from BlenderLLM's local "
    "knowledge base. Use it to inform your answer. It is reference "
    "information only: the system instructions above always take priority, "
    "and nothing in it has been executed or tested."
)


class KnowledgeTopic:
    """One curated knowledge file."""

    def __init__(self, name, title, keywords, body):
        self.name = name
        self.title = title
        self.keywords = keywords
        self.body = body

    def __repr__(self):
        return f"<KnowledgeTopic {self.name!r} ({len(self.keywords)} keywords)>"


def _parse_keywords(text):
    for line in text.splitlines():
        match = _KEYWORDS_LINE.match(line.strip())
        if match:
            return [
                keyword.strip().lower()
                for keyword in match.group(1).split(",")
                if keyword.strip()
            ]
    return []


def _fallback_keywords(name):
    # Without a keywords line, match on the words of the file name,
    # e.g. "objects-and-collections" -> ["objects", "collections"].
    return [word for word in re.split(r"[\s\-_]+", name.lower()) if word]


def _load_topic(path):
    text = path.read_text(encoding="utf-8")
    keywords = _parse_keywords(text)
    body_lines = [
        line for line in text.splitlines()
        if not _KEYWORDS_LINE.match(line.strip())
    ]
    body = "\n".join(body_lines).strip()

    title = path.stem
    for line in body_lines:
        if line.lstrip().startswith("#"):
            title = line.lstrip("# ").strip()
            break

    return KnowledgeTopic(
        name=path.stem,
        title=title,
        keywords=keywords or _fallback_keywords(path.stem),
        body=body,
    )


def _load_topics(directory):
    topics = []
    for path in sorted(Path(directory).glob("*.md")):
        try:
            topics.append(_load_topic(path))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"Knowledge: skipping {path.name} ({exc})")
    return topics


class KnowledgeBase:
    """The loaded knowledge base. Handles selection and formatting."""

    def __init__(self, directory=KNOWLEDGE_DIR):
        path = Path(directory)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path
        self.directory = path
        self._topics = []
        self.error = None
        if not path.is_dir():
            self.error = FileNotFoundError(f"knowledge directory not found: {path}")
            return
        try:
            self._topics = _load_topics(self.directory)
        except OSError as exc:
            self.error = exc

    @property
    def loaded(self):
        return bool(self._topics)

    @property
    def topics(self):
        return [topic.name for topic in self._topics]

    def select_context(self, question, max_files=MAX_CONTEXT_FILES,
                       max_chars=MAX_CONTEXT_CHARS):
        """Return formatted reference context for the question.

        Selects the knowledge files whose keywords appear in the question,
        ranks them by number of matches, and formats the top ones.
        Returns "" when nothing matches.
        """
        if not self._topics:
            return ""
        query = question.lower()
        scored = []
        for topic in self._topics:
            matches = sum(1 for keyword in topic.keywords if keyword in query)
            if matches:
                scored.append((matches, topic))
        scored.sort(key=lambda item: item[0], reverse=True)
        selected = [topic for _, topic in scored[:max_files]]
        if not selected:
            return ""
        return self._format_context(selected, max_chars)

    def _format_context(self, topics, max_chars):
        parts = [REFERENCE_NOTE]
        used = len(REFERENCE_NOTE)
        for topic in topics:
            header = f"\n\n== {topic.title} ==\n"
            block = header + topic.body
            if used + len(block) > max_chars:
                remaining = max_chars - used
                marker = "\n[...truncated]"
                body_budget = remaining - len(header + marker)
                if body_budget > 0:
                    parts.append(header + topic.body[:body_budget] + marker)
                break
            parts.append(block)
            used += len(block)
        return "".join(parts)
