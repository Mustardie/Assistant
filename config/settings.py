import os
from dataclasses import dataclass

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

    # Reasoning / planning model (cloud). This is what brain.py and the
    # long-term memory classifier use for all "thinking" now.
    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_model: str = os.getenv("OPENROUTER_MODEL", "openrouter/free")

    # No longer used by brain.py/long_memory.py (replaced by OpenRouter
    # above) -- left here in case you want to switch back later.
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Local Ollama is kept only because tools/vision.py talks to a local
    # multimodal model (qwen2.5vl) directly for screen understanding. It is
    # no longer used for the main reasoning loop.
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "gemma3:12b")

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

    # Voice settings
    voice_sample_rate: int = int(os.getenv("VOICE_SAMPLE_RATE", "16000"))
    voice_model: str = os.getenv("VOICE_MODEL", "base")
    voice_language: str = os.getenv("VOICE_LANGUAGE", "en")
    voice_silence_seconds: float = float(os.getenv("VOICE_SILENCE_SECONDS", "1.5"))
    voice_device: str = os.getenv("VOICE_DEVICE", "")
    tts_voice: str = os.getenv("TTS_VOICE", "af_heart")
    tts_speed: float = float(os.getenv("TTS_SPEED", "1.0"))


settings = Settings()