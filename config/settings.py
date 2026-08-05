import os
from dataclasses import dataclass
from pathlib import Path

from .paths import get_nova_memory_dir


def _load_dotenv():
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        ".env",
    )

    if not os.path.exists(env_path):
        return

    with open(env_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            os.environ.setdefault(key, value)


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    youtube_api_key: str = os.getenv("YOUTUBE_API_KEY", "")
    assistant_name: str = os.getenv("ASSISTANT_NAME", "Nova")

    # LLM provider: "openrouter" or "gemini"
    llm_provider: str = os.getenv("LLM_PROVIDER", "ollama")

    # Reasoning / planning model (cloud). Any of the providers below can be
    # selected as the active llm_provider from Settings -> Provider; each
    # keeps its own API key and model so switching providers never clobbers
    # another provider's saved key.
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")

    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")

    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # Local Ollama is kept only because tools/vision.py talks to a local
    # multimodal model (qwen2.5vl) directly for screen understanding. It is
    # no longer used for the main reasoning loop.
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma3:12b")

    # Research pipeline (Tavily). research_dir defaults to ~/Downloads/Nova Research
    # on Windows (Path.home() / "Downloads"). Empty env value falls back to default.
    research_provider: str = os.getenv("RESEARCH_PROVIDER", "tavily") or "tavily"
    tavily_api_key: str = os.getenv("TAVILY_API_KEY", "") or ""
    research_dir: str = os.getenv("RESEARCH_DIR", "") or str(
        Path.home() / "Downloads" / "Nova Research"
    )
    research_max_sources: int = int(os.getenv("RESEARCH_MAX_SOURCES", "12"))
    research_max_images: int = int(os.getenv("RESEARCH_MAX_IMAGES", "6"))
    research_max_papers: int = int(os.getenv("RESEARCH_MAX_PAPERS", "2"))

    # Role-split: a dedicated, stronger model runs the planner (decisions,
    # tool selection, recovery) while the main model above handles cheap
    # work (page summaries, document drafts). Falls back to the main model
    # automatically if the planner model is unavailable (e.g. not pulled).
    planner_provider: str = os.getenv("PLANNER_PROVIDER", "ollama")
    planner_model: str = os.getenv("PLANNER_MODEL", "qwen3:14b")

    memory_dir: str = os.getenv("MEMORY_DIR", str(get_nova_memory_dir()))
    recommendation_pool_size: int = int(os.getenv("RECOMMENDATION_POOL_SIZE", "80"))
    recommendation_top_n: int = int(os.getenv("RECOMMENDATION_TOP_N", "10"))
    recommendation_queries_min: int = int(os.getenv("RECOMMENDATION_QUERIES_MIN", "5"))
    recommendation_queries_max: int = int(os.getenv("RECOMMENDATION_QUERIES_MAX", "8"))
    youtube_allow_browser_fallback: bool = (
        os.getenv("YOUTUBE_ALLOW_BROWSER_FALLBACK", "false").lower() == "true"
    )
    agent_max_iterations: int = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))
    agent_max_recovery_attempts: int = int(os.getenv("AGENT_MAX_RECOVERY_ATTEMPTS", "3"))

    # Skills subsystem (semantic desktop automation). Skills are always
    # stored OUTSIDE the project folder -- Downloads/Nova/Skills by default.
    skills_dir: str = os.getenv("SKILLS_DIR", "") or str(
        Path.home() / "Downloads" / "Nova" / "Skills"
    )
    skills_capture_interval_ms: int = int(os.getenv("SKILLS_CAPTURE_INTERVAL_MS", "400"))
    skills_screenshot_quality: int = int(os.getenv("SKILLS_SCREENSHOT_QUALITY", "70"))
    skills_min_suggest_frequency: int = int(
        os.getenv("SKILLS_MIN_SUGGEST_FREQUENCY", "3")
    )
    skills_suggest_window_days: int = int(os.getenv("SKILLS_SUGGEST_WINDOW_DAYS", "14"))
    skills_auto_play_threshold: float = float(
        os.getenv("SKILLS_AUTO_PLAY_THRESHOLD", "0.7")
    )
    skills_mouse_poll_hz: int = int(os.getenv("SKILLS_MOUSE_POLL_HZ", "20"))

    # Voice settings
    voice_sample_rate: int = int(os.getenv("VOICE_SAMPLE_RATE", "16000"))
    voice_model: str = os.getenv("VOICE_MODEL", "base")
    voice_language: str = os.getenv("VOICE_LANGUAGE", "en")
    voice_silence_seconds: float = float(os.getenv("VOICE_SILENCE_SECONDS", "1.5"))
    voice_device: str = os.getenv("VOICE_DEVICE", "")
    # TTS engine: "piper" (default, more natural voices) or "kokoro".
    tts_engine: str = os.getenv("TTS_ENGINE", "piper")
    piper_voice: str = os.getenv("PIPER_VOICE", "en_US-ryan-high")
    piper_voice_dir: str = os.getenv("PIPER_VOICE_DIR", "") or str(
        Path.home() / ".cache" / "piper-voices"
    )
    kokoro_repo_id: str = os.getenv("KOKORO_REPO_ID", "hexgrad/Kokoro-82M")
    kokoro_voice: str = os.getenv("KOKORO_VOICE", "us_puck")
    tts_speed: float = float(os.getenv("TTS_SPEED", "1.0"))


settings = Settings()


# --------------------------------------------------------------------------- #
# Provider metadata -- maps a provider key (lowercase, e.g. "openrouter") to
# the Settings field that holds its model / API key. Used by main.py and the
# Settings UI so applying a provider change is a generic lookup instead of a
# hardcoded if/elif chain per provider.
# --------------------------------------------------------------------------- #
PROVIDER_MODEL_FIELD = {
    "ollama": "ollama_model",
    "gemini": "gemini_model",
    "openrouter": "openrouter_model",
    "openai": "openai_model",
    "anthropic": "anthropic_model",
    "groq": "groq_model",
    "deepseek": "deepseek_model",
}

# Ollama is local and needs no API key.
PROVIDER_API_KEY_FIELD = {
    "ollama": None,
    "gemini": "gemini_api_key",
    "openrouter": "openrouter_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "groq": "groq_api_key",
    "deepseek": "deepseek_api_key",
}


def get_provider_default_model(provider_key: str) -> str:
    """Return the currently configured default model for a provider key."""
    field = PROVIDER_MODEL_FIELD.get((provider_key or "").strip().lower())
    return getattr(settings, field, "") if field else ""


def apply_runtime_overrides(**overrides: str) -> None:
    """Apply settings changes at runtime without a restart.

    ``Settings`` is frozen and instantiated once at import; every consumer
    (LLM clients, Brain, TTS, skills, ...) reads from that same shared
    instance. Mutating it in place via ``object.__setattr__`` (bypassing
    the frozen guard) plus setting the matching environment variable
    (``OPENROUTER_API_KEY`` -> ``openrouter_api_key``, etc.) makes the new
    value visible to code that is rebuilt after the change.
    """
    for key, value in overrides.items():
        if value is None:
            continue
        if hasattr(settings, key):
            object.__setattr__(settings, key, value)
        os.environ[key.upper()] = str(value)