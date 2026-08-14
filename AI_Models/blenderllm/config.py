"""Central configuration for BlenderLLM V0.1-V0.4.

Everything that might change from machine to machine lives here.
Change a value here; do not hard-code these values anywhere else.
"""

# Address of the local Ollama server.
# The default is the standard local Ollama server.
OLLAMA_HOST = "http://localhost:11434"

# The model used for chat.
MODEL = "qwen2.5-coder:14b"

# --- Blender knowledge/context layer (V0.2) --------------------------------

# Directory containing the curated Markdown knowledge files.
# Relative paths are resolved from the project folder.
KNOWLEDGE_DIR = "knowledge"

# Master switch for the knowledge layer.
KNOWLEDGE_ENABLED = True

# How many knowledge files may be included in one request...
MAX_CONTEXT_FILES = 3

# ...and how much context text in total (characters).
# Goal: relevant information, not maximum information.
MAX_CONTEXT_CHARS = 4000

# --- Blender execution bridge (V0.4) ---------------------------------------

# The Blender-side add-on (blender_bridge_addon.py) listens on 127.0.0.1.
# Keep this local: never expose the bridge to a remote network.
BLENDER_BRIDGE_HOST = "127.0.0.1"
BLENDER_BRIDGE_PORT = 41987

# Seconds to wait for Blender to answer before giving up.
BLENDER_BRIDGE_TIMEOUT = 15
