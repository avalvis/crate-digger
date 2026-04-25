"""
core/analyzer.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Audio Analysis (BPM + Musical Key)

BPM via librosa's beat tracker; musical key via a hand-rolled,
vectorized Krumhansl-Schmuckler implementation.

Performance contract:
  • Single audio load per analysis — waveform is reused across stages.
  • Mono 22.05 kHz — enough resolution for BPM and chromagram accuracy,
    ~4x faster than full-rate stereo on the typical 4-minute track.
  • Pre-computed 24×12 profile matrix for vectorized Pearson correlation
    (twelve rotations × major/minor) as a single matrix-vector product.
    No Python-level loops in the hot path.
  • Typical wall-clock: 1.5–3.5s per 4-minute track on a modern CPU,
    dominated by HPSS. Disable HPSS (hpss_margin=None) for ~1s analyses
    at a modest accuracy cost on drum-heavy material.

Thread safety: the pre-computed profile matrix is read-only, librosa
calls are thread-safe, and the analyzer holds no mutable per-call state
— one instance can service many concurrent workers.

Zero ties to ui/ or mutagen. Takes a path, returns an AnalysisResult.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# Formats that soundfile (libsndfile) cannot decode; we pipe these through
# ffmpeg to raw PCM before handing to librosa. Without this, librosa falls
# back to the deprecated audioread backend.
_FFMPEG_DECODE_EXTS = frozenset({".m4a", ".mp3", ".aac", ".ogg", ".opus", ".wma"})


# ─── Public types ────────────────────────────────────────────────────

class AnalysisStage(str, Enum):
    LOADING = "loading"
    BPM = "bpm"
    HARMONIC_ISOLATION = "harmonic_isolation"
    KEY = "key"
    COMPLETE = "complete"


@dataclass(slots=True)
class AnalysisProgress:
    stage: AnalysisStage
    percent: float = 0.0
    message: str = ""


@dataclass(slots=True, frozen=True)
class AnalysisResult:
    bpm: float                     # rounded to 2dp
    bpm_confidence: float          # 0..1 (inter-beat interval tightness)
    musical_key: str               # e.g. "Am", "F#", "C"
    camelot_key: str               # e.g. "8A", "11B"
    key_confidence: float          # 0..1 (margin between top-2 correlations)
    duration_seconds: float
    sample_rate: int


# ─── Public exceptions ───────────────────────────────────────────────

class AnalyzerError(Exception):
    """Base class for analyzer failures."""


class AnalysisLoadError(AnalyzerError):
    """Could not load or decode the audio file."""


class AnalysisCancelledError(AnalyzerError):
    """Caller cancelled via cancel_event."""


# ─── Krumhansl-Kessler key profiles ──────────────────────────────────
# Source: Krumhansl, C. L. (1990). Cognitive Foundations of Musical Pitch.
# These are the empirically derived probe-tone profiles — the de-facto
# baseline for MIR key detection before deep-learning methods.

_KK_MAJOR = np.array([
    6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
    2.52, 5.19, 2.39, 3.66, 2.29, 2.88,
], dtype=np.float64)

_KK_MINOR = np.array([
    6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
    2.54, 4.75, 3.98, 2.69, 3.34, 3.17,
], dtype=np.float64)

# Pitch-class names (indexed from C, sharp spelling — matches librosa's chroma output)
_PC_SHARP: tuple[str, ...] = (
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
)

# Camelot Wheel — the notation Serato/Rekordbox/Mixed In Key use
# for harmonic mixing. Major keys on the B ring, minor on the A ring.
_CAMELOT_MAJOR: dict[str, str] = {
    "B":  "1B",  "F#": "2B",  "C#": "3B",  "G#": "4B",
    "D#": "5B",  "A#": "6B",  "F":  "7B",  "C":  "8B",
    "G":  "9B",  "D":  "10B", "A":  "11B", "E":  "12B",
}
_CAMELOT_MINOR: dict[str, str] = {
    "G#": "1A",  "D#": "2A",  "A#": "3A",  "F":  "4A",
    "C":  "5A",  "G":  "6A",  "D":  "7A",  "A":  "8A",
    "E":  "9A",  "B":  "10A", "F#": "11A", "C#": "12A",
}


# ─── The Analyzer ────────────────────────────────────────────────────

class AudioAnalyzer:
    """
    BPM and musical key detector for a single audio file.
    Stateless across calls; safe to share across threads.
    """

    # Tracks shorter than this aren't long enough to yield a reliable
    # tempo (need ~4 bars of signal) or stable chromagram.
    MIN_DURATION_SECONDS = 5.0

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        *,
        sample_rate: int = 22050,
        hpss_margin: Optional[float] = 4.0,
        chroma_bins_per_octave: int = 36,
        ffmpeg_path: Optional[str] = None,
    ) -> None:
        """
        Args:
            sample_rate: Analysis rate. 22050 is the librosa default and
                retains full musical pitch info (Nyquist ≈ 11 kHz covers
                every fundamental + most useful harmonics for chroma).
            hpss_margin: HPSS separation margin for key detection.
                Higher = cleaner harmonic content but more loss. 4.0 is
                a good middle ground for drum-heavy material. Set to
                None to skip HPSS entirely (~2x faster, less accurate
                on percussion-dense genres).
            chroma_bins_per_octave: CQT resolution. 36 (three bins per
                semitone) is markedly more accurate than 12 for key
                detection at negligible extra cost.
            ffmpeg_path: Path to the ffmpeg binary. When provided, formats
                that soundfile can't decode natively (e.g. .m4a, .mp3) are
                piped through ffmpeg to raw PCM before librosa sees them.
                This avoids the deprecated audioread fallback.
        """
        self._log = logger or logging.getLogger("cratedigger.analyzer")
        self._sr = int(sample_rate)
        self._hpss_margin = hpss_margin
        self._chroma_bpo = int(chroma_bins_per_octave)
        self._ffmpeg_path = ffmpeg_path

        # Pre-compute the 24×12 profile matrix. Row layout:
        #   rows  0..11  → major key rooted at pitch class i
        #   rows 12..23  → minor key rooted at pitch class (i - 12)
        # We also pre-center each row (subtract its mean) and cache the
        # L2 norm, so Pearson correlation at analysis time is a single
        # matrix-vector product + elementwise divide.
        major_rotations = np.stack([np.roll(_KK_MAJOR, i) for i in range(12)])
        minor_rotations = np.stack([np.roll(_KK_MINOR, i) for i in range(12)])
        profiles = np.vstack([major_rotations, minor_rotations])        # (24, 12)

        self._profiles_centered = profiles - profiles.mean(axis=1, keepdims=True)
        self._profiles_norm = np.linalg.norm(self._profiles_centered, axis=1)

    # ── Public API ──

    def analyze(
        self,
        audio_path: Path,
        progress_callback: Optional[Callable[[AnalysisProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> AnalysisResult:
        """
        Analyze `audio_path` and return BPM + musical key.

        Raises AnalysisLoadError if the file can't be decoded,
        AnalysisCancelledError if `cancel_event` fires mid-analysis.
        """
        # Deferred import — librosa's import is heavy (~1s) and we want
        # cold-start cost confined to the first analysis call, not app boot.
        import librosa

        path = Path(audio_path)
        if not path.exists():
            raise AnalysisLoadError(f"File does not exist: {path}")

        self._check_cancel(cancel_event)
        self._emit(progress_callback, AnalysisStage.LOADING, 0.0, "Loading audio")

        try:
            y, sr = self._load_audio(path)
        except AnalysisLoadError:
            raise
        except Exception as e:
            raise AnalysisLoadError(f"Could not load audio: {e}") from e

        if y.size == 0:
            raise AnalysisLoadError("Loaded audio is empty.")

        duration = float(len(y) / sr)
        if duration < self.MIN_DURATION_SECONDS:
            raise AnalysisLoadError(
                f"Audio too short for analysis: {duration:.1f}s "
                f"(minimum {self.MIN_DURATION_SECONDS}s)"
            )

        # ── BPM ──
        self._check_cancel(cancel_event)
        self._emit(progress_callback, AnalysisStage.BPM, 25.0, "Detecting BPM")
        bpm, bpm_conf = self._detect_bpm(y, sr, librosa)

        # ── Harmonic isolation (optional) ──
        self._check_cancel(cancel_event)
        y_for_key = y
        if self._hpss_margin is not None:
            self._emit(progress_callback, AnalysisStage.HARMONIC_ISOLATION,
                       55.0, "Isolating harmonic content")
            y_for_key = self._extract_harmonic(y, librosa)

        # ── Key ──
        self._check_cancel(cancel_event)
        self._emit(progress_callback, AnalysisStage.KEY, 80.0, "Detecting musical key")
        key_name, camelot, key_conf = self._detect_key(y_for_key, sr, librosa)

        self._emit(progress_callback, AnalysisStage.COMPLETE, 100.0, "Analysis complete")

        self._log.debug(
            "Analyzed %s: BPM=%.2f (conf=%.2f)  key=%s/%s (conf=%.2f)  dur=%.1fs",
            path.name, bpm, bpm_conf, key_name, camelot, key_conf, duration,
        )

        return AnalysisResult(
            bpm=round(bpm, 2),
            bpm_confidence=bpm_conf,
            musical_key=key_name,
            camelot_key=camelot,
            key_confidence=key_conf,
            duration_seconds=duration,
            sample_rate=sr,
        )

    # ── BPM ──

    def _detect_bpm(self, y: np.ndarray, sr: int, librosa) -> tuple[float, float]:
        # Median-aggregated onset strength is noticeably more robust on
        # drum-heavy material than the default max aggregation.
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, aggregate=np.median)

        tempo_raw, beat_times = librosa.beat.beat_track(
            onset_envelope=onset_env, sr=sr, units="time",
        )

        # librosa's return type for tempo has shifted between versions
        # (scalar vs 1-element array). Normalize defensively.
        tempo = float(np.atleast_1d(tempo_raw).flat[0])

        if not np.isfinite(tempo) or tempo <= 0:
            return 0.0, 0.0

        # Confidence proxy: how tight are the inter-beat intervals?
        # A metronomic drum loop will have ~100% of IBIs within 5% of the
        # median; a rubato jazz performance will have <50%.
        if len(beat_times) < 4:
            return tempo, 0.0

        ibis = np.diff(beat_times)
        if ibis.size == 0:
            return tempo, 0.0

        median_ibi = float(np.median(ibis))
        if median_ibi <= 0:
            return tempo, 0.0

        tight_fraction = float(
            np.mean(np.abs(ibis - median_ibi) / median_ibi < 0.05)
        )
        return tempo, tight_fraction

    # ── Key ──

    def _extract_harmonic(self, y: np.ndarray, librosa) -> np.ndarray:
        # `librosa.effects.harmonic` is a HPSS wrapper. Higher margin
        # biases the filter toward rejecting percussive content more
        # aggressively, which is exactly what chromagram accuracy needs.
        # margin=4 is a commonly cited sweet spot in the MIR literature.
        return librosa.effects.harmonic(y, margin=self._hpss_margin)

    def _detect_key(
        self, y: np.ndarray, sr: int, librosa,
    ) -> tuple[str, str, float]:
        # Constant-Q chromagram: log-frequency bins align with the
        # equal-tempered scale, giving far cleaner pitch-class energy
        # than STFT chroma for harmonic content.
        chroma = librosa.feature.chroma_cqt(
            y=y, sr=sr, bins_per_octave=self._chroma_bpo,
        )

        # Median across time is more robust than mean:
        #   – loud outlier chords don't dominate
        #   – transient drum bleed (post-HPSS residual) gets filtered
        chroma_profile = np.median(chroma, axis=1)                 # (12,)

        if not np.any(chroma_profile > 0):
            self._log.warning("Zero chromagram — falling back to C major.")
            return "C", _CAMELOT_MAJOR["C"], 0.0

        # Vectorized Pearson correlation: centre input, dot with
        # pre-centered profile matrix, divide by product of L2 norms.
        c_centered = chroma_profile - chroma_profile.mean()
        c_norm = float(np.linalg.norm(c_centered)) + 1e-12

        correlations = (
            self._profiles_centered @ c_centered
        ) / (self._profiles_norm * c_norm)                         # (24,)

        best_idx = int(np.argmax(correlations))
        is_major = best_idx < 12
        tonic_name = _PC_SHARP[best_idx % 12]
        key_name = tonic_name if is_major else f"{tonic_name}m"

        cam_map = _CAMELOT_MAJOR if is_major else _CAMELOT_MINOR
        camelot = cam_map.get(tonic_name, "?")

        # Confidence: margin between winner and runner-up. Typical
        # observed margins in testing: 0.05–0.30, so we scale by 3×
        # and clip to keep the number roughly in the user's mental
        # model of "0.7 = confident, 0.3 = unsure".
        sorted_corr = np.sort(correlations)[::-1]
        margin = float(sorted_corr[0] - sorted_corr[1])
        confidence = float(np.clip(margin * 3.0, 0.0, 1.0))

        # Top-3 candidates logged for debugging misdetections
        if self._log.isEnabledFor(logging.DEBUG):
            top3 = np.argsort(correlations)[-3:][::-1]
            readable = [
                (_PC_SHARP[i % 12] + ("" if i < 12 else "m"),
                 round(float(correlations[i]), 3))
                for i in top3
            ]
            self._log.debug("Key candidates (top-3): %s", readable)

        return key_name, camelot, confidence

    # ── Utilities ──

    def _load_audio(self, path: Path) -> tuple[np.ndarray, int]:
        """
        Load audio as float32 mono at self._sr.

        Uses ffmpeg → raw PCM pipe for compressed formats (.m4a, .mp3,
        etc.) when an ffmpeg binary is available — this avoids librosa's
        deprecated audioread fallback and is faster. Falls back to
        librosa.load for WAV/FLAC or when ffmpeg is not configured.
        """
        import librosa

        use_ffmpeg = (
            self._ffmpeg_path is not None
            and path.suffix.lower() in _FFMPEG_DECODE_EXTS
        )
        if use_ffmpeg:
            cmd = [
                self._ffmpeg_path,
                "-i", str(path),
                "-f", "f32le",      # raw 32-bit float LE
                "-ac", "1",         # mono
                "-ar", str(self._sr),
                "-",                # stdout
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=120,
                )
                if result.returncode == 0 and result.stdout:
                    y = np.frombuffer(result.stdout, dtype=np.float32).copy()
                    if y.size > 0:
                        return y, self._sr
            except Exception as exc:
                self._log.debug("ffmpeg decode failed (%s); falling back to librosa", exc)

        y, sr = librosa.load(str(path), sr=self._sr, mono=True)
        return y, sr

    @staticmethod
    def _check_cancel(event: Optional[threading.Event]) -> None:
        if event is not None and event.is_set():
            raise AnalysisCancelledError("Cancelled by user.")

    @staticmethod
    def _emit(
        cb: Optional[Callable[[AnalysisProgress], None]],
        stage: AnalysisStage,
        percent: float,
        message: str,
    ) -> None:
        if cb is not None:
            cb(AnalysisProgress(stage=stage, percent=percent, message=message))