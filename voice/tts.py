"""Nova TTS engine layer.

Two engines are supported, selected ONLY via TTS_ENGINE in the .env file:
  - "piper" (default): Rhasspy Piper, more natural/realistic voices.
    Voice chosen via PIPER_VOICE (e.g. en_US-ryan-high).
  - "kokoro": Kokoro-82M. Voice chosen via KOKORO_VOICE.

Both expose the same TTS.synthesize(text) -> (float32 array, sample_rate)
interface so the VoiceManager/Player never changes."""

import difflib
import logging
from pathlib import Path

import numpy as np

from config.settings import settings

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Piper (default engine)
# --------------------------------------------------------------------- #

# Canonical US English voices from rhasspy/piper-voices (offline fallback
# when voices.json cannot be fetched). The repo also carries many more
# languages/qualities; the network list is authoritative when reachable.
_PIPER_KNOWN_VOICES = (
    "en_US-amy-low", "en_US-amy-medium", "en_US-arctic-medium",
    "en_US-bryce-medium", "en_US-danny-low", "en_US-hfc_female-medium",
    "en_US-hfc_male-medium", "en_US-joe-medium", "en_US-john-medium",
    "en_US-kathleen-low", "en_US-kristin-medium", "en_US-kusal-medium",
    "en_US-l2arctic-medium", "en_US-lessac-high", "en_US-lessac-low",
    "en_US-lessac-medium", "en_US-libritts-high", "en_US-libritts_r-medium",
    "en_US-ljspeech-high", "en_US-ljspeech-medium", "en_US-mike-medium",
    "en_US-norman-medium", "en_US-reza_ibrahim-medium", "en_US-ryan-high",
    "en_US-ryan-low", "en_US-ryan-medium", "en_US-sam-medium",
    "en_GB-alan-medium", "en_GB-alba-medium", "en_GB-aru-medium",
    "en_GB-cori-high", "en_GB-cori-medium", "en_GB-emi-medium",
    "en_GB-iona-medium", "en_GB-jenny_dioco-medium", "en_GB-libritts_r-medium",
    "en_GB-northern_english_male-medium", "en_GB-semaine-medium",
    "en_GB-southern_english_female-low", "en_GB-vctk-medium",
    "en_GB-whisper-medium", "en_GB-whisper-female-medium",
)

_piper_voices_cache: list[str] | None = None


def _piper_voices_from_network() -> list[str]:
    try:
        from urllib.request import urlopen

        with urlopen(
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json?download=true",
            timeout=15,
        ) as response:
            import json

            return sorted(json.load(response).keys())
    except Exception as exc:
        logger.warning("Could not fetch Piper voice list (%s)", exc)
        return []


def piper_available_voices() -> list[str]:
    """All valid Piper voices: network voices.json, else bundled fallback."""
    global _piper_voices_cache
    if _piper_voices_cache is not None:
        return _piper_voices_cache
    voices = _piper_voices_from_network()
    if not voices:
        voices = list(_PIPER_KNOWN_VOICES)
    _piper_voices_cache = voices
    return voices


def resolve_piper_voice(requested: str) -> str:
    """Map a configured PIPER_VOICE to an actual Piper voice name.

    Piper names are case-sensitive (en_US-ryan-high). Exact match wins;
    then a case-insensitive exact match (so PIPER_VOICE=En_Us-Ryan-High
    still resolves cleanly, no warning); then a fuzzy suggestion; then an
    error listing every valid voice (never a silent fallback).
    """
    requested = (requested or "").strip()
    voices = piper_available_voices()
    if requested in voices:
        return requested
    lower_lookup = {v.lower(): v for v in voices}
    ci = lower_lookup.get(requested.lower())
    if ci:
        logger.warning(
            "PIPER_VOICE=%r differs only in case from the canonical '%s'; "
            "using '%s'.", requested, ci, ci,
        )
        return ci
    matches = difflib.get_close_matches(requested, voices, n=1, cutoff=0.6)
    if matches:
        logger.warning(
            "PIPER_VOICE=%r is not a valid Piper voice; using closest valid "
            "voice '%s' instead.", requested, matches[0],
        )
        return matches[0]
    raise ValueError(
        f"PIPER_VOICE={requested!r} is not a valid Piper voice. "
        f"Available voices: {', '.join(voices)}"
    )


# --------------------------------------------------------------------- #
# Kokoro (legacy engine)
# --------------------------------------------------------------------- #

_KOKORO_KNOWN_VOICES = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa", "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)

_kokoro_voices_cache: list[str] | None = None


def _kokoro_repo_cache_dir() -> Path:
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    return hub / f"models--{settings.kokoro_repo_id.replace('/', '--')}"


def _kokoro_voices_from_cache() -> list[str]:
    voices: list[str] = []
    for pt in _kokoro_repo_cache_dir().glob("snapshots/*/voices/*.pt"):
        voices.append(pt.stem)
    return sorted(set(voices))


def _kokoro_voices_from_repo() -> list[str]:
    try:
        from huggingface_hub import list_repo_files

        files = list_repo_files(settings.kokoro_repo_id)
    except Exception as exc:
        logger.warning("Could not query Kokoro repo voices (%s)", exc)
        return []
    voices = [f.rsplit("/", 1)[-1][:-3] for f in files if f.startswith("voices/") and f.endswith(".pt")]
    return sorted(set(voices))


def kokoro_available_voices() -> list[str]:
    global _kokoro_voices_cache
    if _kokoro_voices_cache is not None:
        return _kokoro_voices_cache
    voices = sorted(
        set(_kokoro_voices_from_repo()) | set(_kokoro_voices_from_cache())
    )
    if not voices:
        voices = list(_KOKORO_KNOWN_VOICES)
    _kokoro_voices_cache = voices
    return voices


def resolve_kokoro_voice(requested: str) -> str:
    requested = (requested or "").strip().lower()
    voices = kokoro_available_voices()
    if requested in voices:
        return requested
    if requested.startswith("us_"):
        rest = requested[3:]
        for candidate in (f"am_{rest}", f"af_{rest}"):
            if candidate in voices:
                logger.warning(
                    "KOKORO_VOICE=%r is not a valid Kokoro v1.0 voice name "
                    "(US voices use am_/af_ prefixes); using closest valid "
                    "voice '%s' instead.", requested, candidate,
                )
                return candidate
    matches = difflib.get_close_matches(requested, voices, n=1, cutoff=0.6)
    if matches:
        logger.warning(
            "KOKORO_VOICE=%r is not a valid Kokoro voice name; using closest "
            "valid voice '%s' instead.", requested, matches[0],
        )
        return matches[0]
    raise ValueError(
        f"KOKORO_VOICE={requested!r} is not a valid Kokoro voice. "
        f"Available voices: {', '.join(voices)}"
    )


# --------------------------------------------------------------------- #
# Unified TTS
# --------------------------------------------------------------------- #


class TTS:
    _pipeline = None
    _engine = None

    def __init__(self, voice=None, speed=None, lang_code="a"):
        engine = (settings.tts_engine or "piper").strip().lower()
        if engine not in ("piper", "kokoro"):
            raise ValueError(
                f"TTS_ENGINE={engine!r} is not supported (choose 'piper' or 'kokoro')."
            )
        if engine == "piper":
            requested = (voice or settings.piper_voice or "").strip()
            self.voice = resolve_piper_voice(requested)
        else:
            requested = (voice or settings.kokoro_voice or "").strip().lower()
            self.voice = resolve_kokoro_voice(requested)
        self.requested_voice = requested
        self.speed = speed or settings.tts_speed
        self.lang_code = lang_code
        logger.info("TTS Engine: %s", "Piper" if engine == "piper" else "Kokoro")
        if self.voice != self.requested_voice:
            logger.info("Voice: %s (configured %s=%s)",
                        self.voice,
                        "PIPER_VOICE" if engine == "piper" else "KOKORO_VOICE",
                        self.requested_voice)
        else:
            logger.info("Voice: %s", self.voice)

    def load_pipeline(self):
        if TTS._pipeline is not None and TTS._engine == self.engine_name():
            return
        if self.engine_name() == "piper":
            try:
                self._load_piper()
                return
            except ImportError:
                # piper-tts isn't installed -- fall back to the kokoro
                # engine (also bundled in requirements.txt) instead of
                # failing startup or breaking voice output.
                logger.warning(
                    "piper-tts is not installed; falling back to the "
                    "kokoro TTS engine for this session."
                )
                from config.settings import apply_runtime_overrides
                apply_runtime_overrides(tts_engine="kokoro")
        self._load_kokoro()

    @staticmethod
    def engine_name() -> str:
        return (settings.tts_engine or "piper").strip().lower()

    def _load_piper(self):
        from piper import PiperVoice
        from piper.download_voices import download_voice

        voice_dir = Path(settings.piper_voice_dir)
        voice_dir.mkdir(parents=True, exist_ok=True)
        model_path = voice_dir / f"{self.voice}.onnx"
        config_path = voice_dir / f"{self.voice}.onnx.json"
        if not model_path.exists() or model_path.stat().st_size == 0:
            download_voice(self.voice, voice_dir)
        TTS._pipeline = PiperVoice.load(
            model_path,
            config_path=config_path,
        )
        TTS._engine = "piper"

    def _load_kokoro(self):
        from kokoro import KPipeline

        TTS._pipeline = KPipeline(
            lang_code=self.lang_code,
            repo_id=settings.kokoro_repo_id,
        )
        TTS._engine = "kokoro"

    def synthesize(self, text: str):
        self.load_pipeline()
        if not text.strip():
            return np.zeros(0, dtype=np.float32), 24000
        if self.engine_name() == "piper":
            return self._synthesize_piper(text)
        return self._synthesize_kokoro(text)

    def _synthesize_piper(self, text: str):
        from piper.config import SynthesisConfig

        syn_config = SynthesisConfig(length_scale=1.0 / max(self.speed, 0.1))
        parts: list[np.ndarray] = []
        sample_rate = 22050
        for chunk in TTS._pipeline.synthesize(text, syn_config=syn_config):
            parts.append(chunk.audio_float_array)
            sample_rate = chunk.sample_rate
        if not parts:
            return np.zeros(0, dtype=np.float32), sample_rate
        return np.concatenate(parts).astype(np.float32), sample_rate

    def _synthesize_kokoro(self, text: str):
        gen = TTS._pipeline(text, voice=self.voice, speed=self.speed)
        audio_parts = []
        for result in gen:
            audio_parts.append(result[-1])
        if not audio_parts:
            return np.zeros(0, dtype=np.float32), 24000
        sample_rate = getattr(TTS._pipeline, "sample_rate", 24000)
        return np.concatenate(audio_parts), sample_rate
