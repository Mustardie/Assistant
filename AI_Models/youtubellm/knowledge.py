"""
Loads the user's YouTube knowledge files into memory.

This is NOT a RAG system — it simply reads the Markdown files
at startup so their full contents can be placed in the model's
system context and stay there for every request.
"""

from pathlib import Path

# Folder of this module, so paths stay correct no matter
# which directory the program is started from.
PROJECT_DIR = Path(__file__).parent

# The knowledge files to load, relative to the project folder.
# Add new files here and they will be loaded automatically.
KNOWLEDGE_FILES = [
    "knowledge/youtube_rules.md",
    "knowledge/youtube_examples.md",
]


def load_knowledge():
    """
    Read every knowledge file and return their contents as one string.

    Raises FileNotFoundError with a clear message if any file is missing.
    """
    parts = []
    for relative_path in KNOWLEDGE_FILES:
        full_path = PROJECT_DIR / relative_path
        if not full_path.exists():
            raise FileNotFoundError(
                f"Knowledge file not found: {relative_path}\n"
                f"Expected at: {full_path}\n"
                "Create the file and run the program again."
            )
        content = full_path.read_text(encoding="utf-8")
        parts.append(f"----- {relative_path} -----\n{content}")

    return "\n\n".join(parts)
