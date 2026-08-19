"""Audio event layer.

    loudness envelope + silence ranges + transcript markers -> AudioEvent

Transcript alone misses a lot: laughter, screams, dead air, the moment the
player goes quiet because something is about to go wrong. This layer adds a
cheap, honest reading of the audio track alongside it.

    signal.py        the pure detectors (no ffmpeg, no files)
    ffmpeg_audio.py  the two FFmpeg passes, with testable parsers
    markers.py       [laughs] / [music] written into the transcript
    analyzer.py      orchestration and caching

What it claims, and what it does not: silence, clipping and loudness spikes are
*measured*. Laughter, screaming and music are *inferred* from the shape of a
loudness curve, are named ``possible_*`` where that applies, and are capped in
confidence by ``AudioConfig.max_inferred_confidence`` so no downstream layer can
treat a guess as a measurement.
"""
from editing.audio.analyzer import (
    AudioAnalyzer, AudioResult, AudioSource, FFmpegAudioSource, build_analyzer,
)
from editing.audio.ffmpeg_audio import (
    loudness_envelope, parse_astats_output, parse_silencedetect_output,
    silence_ranges,
)
from editing.audio.markers import detect_markers, find_annotations, marker_type
from editing.audio.signal import (
    LoudnessSample, SILENT_DB, Span, analyse, baseline_db, detect_clipping,
    detect_laughter_clusters, detect_low_energy, detect_music_regions,
    detect_pauses, detect_silence, detect_speech_density_changes, detect_spikes,
    is_sudden_reaction, merge_adjacent, speech_density, summarise,
)

__all__ = [
    # signal
    "LoudnessSample", "Span", "SILENT_DB", "baseline_db", "analyse",
    "detect_silence", "detect_low_energy", "detect_spikes", "detect_clipping",
    "detect_pauses", "detect_speech_density_changes", "detect_laughter_clusters",
    "detect_music_regions", "is_sudden_reaction", "speech_density",
    "merge_adjacent", "summarise",
    # ffmpeg
    "loudness_envelope", "silence_ranges", "parse_astats_output",
    "parse_silencedetect_output",
    # markers
    "detect_markers", "find_annotations", "marker_type",
    # orchestration
    "AudioAnalyzer", "AudioResult", "AudioSource", "FFmpegAudioSource",
    "build_analyzer",
]
