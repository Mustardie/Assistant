"""Nova Skills subsystem.

A semantic, vision-guided desktop automation engine: learns workflows by
watching the user once, stores them as reusable skills (UI-object
references + relative coordinates, never raw pixel playback), and replays
them with adaptation, verification and recovery.

Modules:
    manager    -- SkillManager facade (record / play / edit / suggest)
    recorder   -- async capture of input + screenshots + UI frames
    parser     -- raw events -> semantic actions, variables, secret masking
    player     -- vision-guided playback with verification and recovery
    planner    -- skill matching + automatic learning suggestions
    storage    -- Downloads/Nova/Skills persistence (never in-project)
    vision     -- UI object detection / OCR (Gemini or local Ollama)
    matcher    -- semantic element matching with confidence + scaling
"""

from skills.manager import SkillManager, skill_manager

__all__ = ["SkillManager", "skill_manager"]
