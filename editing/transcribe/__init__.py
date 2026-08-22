"""Local speech-to-text, so the editing brain can hear the episode.

Sessions 1-9 could read a transcript from five formats and could not make one.
That mattered more than it looked: objectives, open loops, callbacks, setup and
payoff, and half the retention risks are all read off the transcript, so a
folder with no SRT beside it left the entire story layer quiet.

```
media file → faster-whisper (local, CPU or CUDA)
                    ↓
        TranscriptionResult   segments, word timings, probabilities
                    ↓
   transcripts/<job_id>/      transcript.json / .srt / .txt / metadata
                    ↓
   transcripts/<asset_id>.json   ← the durable copy every pass already reads
```

**Nothing leaves the machine.** No API, no key, no upload. The model runs
locally through CTranslate2.

**Nothing is faked silently.** The mock backend exists for tests and for
machines with no model, and everything it produces is stamped ``mock=True`` --
on the result, on every file, and in the transcript's own note.

**Nothing overwrites your footage.** Audio extraction, when it is needed at
all, writes a 16 kHz WAV into the cache directory and never beside the source.
"""
from editing.transcribe.schema import (  # noqa: F401
    BACKENDS, INSTALL_HINT, KNOWN_MODELS, MEDIA_EXTENSIONS,
    TranscriptSegment, TranscriptWord, TranscriptionBatch,
    TranscriptionCacheEntry, TranscriptionConfig, TranscriptionFailure,
    TranscriptionJob, TranscriptionResult,
)
from editing.transcribe.backends import (  # noqa: F401
    FasterWhisperBackend, MockBackend, build_backend, check,
)
from editing.transcribe.run import (  # noqa: F401
    missing_transcripts, transcribe_file, transcribe_folder,
)

__all__ = [
    "BACKENDS", "FasterWhisperBackend", "INSTALL_HINT", "KNOWN_MODELS",
    "MEDIA_EXTENSIONS", "MockBackend", "TranscriptSegment",
    "TranscriptWord", "TranscriptionBatch", "TranscriptionCacheEntry",
    "TranscriptionConfig", "TranscriptionFailure", "TranscriptionJob",
    "TranscriptionResult", "build_backend", "check", "missing_transcripts",
    "transcribe_file", "transcribe_folder",
]
