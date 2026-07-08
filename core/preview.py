"""
core/preview.py
──────────────────────────────────────────────────────────────────────
Crate Digger — In-App Preview Service

Fetches playable audio for a YouTube / YouTube Music video id and decodes
it into an in-memory PCM buffer plus a downsampled peak array, ready for
the `WaveformPlayer` to draw and scrub.

Pipeline:
    1. yt-dlp downloads `bestaudio` into a cache dir keyed by video id
       (reused across re-previews — and a head start for later ingestion).
    2. ffmpeg decodes the file to interleaved float32 stereo PCM at a
       fixed playback sample rate (mirrors AudioAnalyzer's ffmpeg→PCM
       path in core/analyzer.py).
    3. A max-abs peak array is computed from the mono downmix for the
       waveform.

Zero UI ties. Runs on a worker thread; progress + cancel are cooperative
via callback / threading.Event, matching the rest of the engine.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

# Playback engine target: 44.1 kHz stereo float32 is universally safe for
# sounddevice/PortAudio and matches the MPC's native rate.
_PLAYBACK_SR = 44100
_PLAYBACK_CHANNELS = 2

# Default number of peak buckets across the whole track. ~2000 gives a
# crisp waveform on a wide card without ballooning memory.
_DEFAULT_PEAK_BUCKETS = 2000

# YouTube's per-format stream URLs are short-lived and occasionally get
# rejected with a 403 even though the video itself is perfectly
# downloadable — re-extracting fresh info (a new signed URL) and trying
# again almost always clears it.
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_DELAY_SECONDS = 2.0


# ─── Public types ────────────────────────────────────────────────────


@dataclass(slots=True)
class PreviewData:
    """A decoded, playable preview with a precomputed waveform."""

    video_id: str
    samplerate: int
    channels: int
    samples: np.ndarray          # shape (frames, channels), float32, [-1, 1]
    peaks: np.ndarray            # shape (buckets,), float32 in [0, 1]
    duration_seconds: float
    source_path: Optional[Path]  # cached compressed file on disk (reusable)

    @property
    def frame_count(self) -> int:
        return int(self.samples.shape[0])


# ─── Exceptions ──────────────────────────────────────────────────────


class PreviewError(Exception):
    """Base class for preview failures."""


class PreviewCancelledError(PreviewError):
    """Caller cancelled via cancel_event."""


class PreviewFetchError(PreviewError):
    """yt-dlp could not fetch the audio stream."""


class PreviewDecodeError(PreviewError):
    """ffmpeg could not decode the fetched file."""


# ─── The service ─────────────────────────────────────────────────────


class PreviewService:
    """
    Downloads + decodes audio for in-app preview. One instance is shared
    across the app; each `fetch()` is self-contained and thread-safe.
    """

    _YT_WATCH = "https://music.youtube.com/watch?v={vid}"

    def __init__(
        self,
        ffmpeg_path: str,
        cache_dir: Path,
        logger: Optional[logging.Logger] = None,
        *,
        target_sr: int = _PLAYBACK_SR,
        peak_buckets: int = _DEFAULT_PEAK_BUCKETS,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._log = logger or logging.getLogger("cratedigger.preview")
        self._sr = int(target_sr)
        self._peak_buckets = int(peak_buckets)

    # ── Public API ──

    def fetch(
        self,
        video_id: str,
        *,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> PreviewData:
        """
        Return a decoded, playable PreviewData for `video_id`.

        `progress_callback(percent, message)` reports 0..100 across the
        fetch+decode. Raises PreviewCancelledError when cancelled.
        """
        vid = self._normalize_video_id(video_id)
        self._emit(progress_callback, 2.0, "Locating stream…")

        source = self.get_cached_path(vid)
        if source is None:
            source = self._download(vid, progress_callback, cancel_event)
        else:
            self._log.debug("Preview cache hit for %s: %s", vid, source)
            self._emit(progress_callback, 55.0, "Using cached audio")

        self._check_cancel(cancel_event)
        self._emit(progress_callback, 65.0, "Decoding audio…")
        samples = self._decode_to_pcm(source)

        self._check_cancel(cancel_event)
        self._emit(progress_callback, 92.0, "Building waveform…")
        peaks = self._compute_peaks(samples)
        duration = samples.shape[0] / float(self._sr)

        self._emit(progress_callback, 100.0, "Ready")
        return PreviewData(
            video_id=vid,
            samplerate=self._sr,
            channels=_PLAYBACK_CHANNELS,
            samples=samples,
            peaks=peaks,
            duration_seconds=duration,
            source_path=source,
        )

    def load_file(
        self,
        path: Path,
        *,
        progress_callback: Optional[Callable[[float, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> PreviewData:
        """
        Decode a local audio file into a playable PreviewData. Used by the
        Vault to preview already-ingested tracks without any download.
        """
        path = Path(path)
        if not path.exists():
            raise PreviewFetchError(f"File not found: {path}")

        self._emit(progress_callback, 30.0, "Decoding audio…")
        self._check_cancel(cancel_event)
        samples = self._decode_to_pcm(path)

        self._check_cancel(cancel_event)
        self._emit(progress_callback, 90.0, "Building waveform…")
        peaks = self._compute_peaks(samples)
        duration = samples.shape[0] / float(self._sr)

        self._emit(progress_callback, 100.0, "Ready")
        return PreviewData(
            video_id=path.stem,
            samplerate=self._sr,
            channels=_PLAYBACK_CHANNELS,
            samples=samples,
            peaks=peaks,
            duration_seconds=duration,
            source_path=path,
        )

    def get_cached_path(self, video_id: str) -> Optional[Path]:
        """Return a previously-downloaded file for this id, if present."""
        vid = self._normalize_video_id(video_id)
        for p in self._cache_dir.glob(f"{vid}.*"):
            if p.is_file() and p.suffix.lower() != ".part":
                return p
        return None

    def clear_cache(self) -> int:
        """Delete all cached preview files. Returns count removed."""
        removed = 0
        for p in self._cache_dir.glob("*"):
            try:
                if p.is_file():
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

    def clear_stale_cache(self, max_age_days: float = 14.0) -> int:
        """
        Remove cache entries not worth keeping: leftover `.part` files
        from downloads interrupted by a crash or force-quit (unusable
        regardless of age) and complete files older than `max_age_days`.
        Meant to be called once at app startup. Returns count removed.
        """
        removed = 0
        cutoff = time.time() - (max_age_days * 86400.0)
        for p in self._cache_dir.glob("*"):
            if not p.is_file():
                continue
            try:
                is_partial = p.suffix.lower() == ".part"
                if is_partial or p.stat().st_mtime < cutoff:
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

    # ── Download ──

    def _download(
        self,
        vid: str,
        progress_callback: Optional[Callable[[float, str], None]],
        cancel_event: Optional[threading.Event],
    ) -> Path:
        import yt_dlp
        from yt_dlp.utils import DownloadError, ExtractorError

        def hook(d: dict[str, Any]) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise PreviewCancelledError("Cancelled by user.")
            if progress_callback is None:
                return
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                got = int(d.get("downloaded_bytes") or 0)
                frac = (got / total) if total else 0.0
                # Map download onto 5..55% of the overall preview progress.
                self._emit(progress_callback, 5.0 + frac * 50.0,
                           "Fetching audio…")

        opts: dict[str, Any] = {
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "ffmpeg_location": self._ffmpeg,
            "quiet": True,
            "no_warnings": True,
            "no_color": True,        # otherwise raw ANSI codes leak into
                                      # logged/surfaced error text
            "noprogress": True,
            "noplaylist": True,
            "overwrites": True,
            "retries": 3,
            "outtmpl": {"default": str(self._cache_dir / "%(id)s.%(ext)s")},
            "paths": {"home": str(self._cache_dir)},
            "progress_hooks": [hook],
        }

        url = self._YT_WATCH.format(vid=vid)
        info = None
        last_error: Optional[Exception] = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            self._check_cancel(cancel_event)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                last_error = None
                break
            except PreviewCancelledError:
                raise
            except (DownloadError, ExtractorError) as e:
                if cancel_event is not None and cancel_event.is_set():
                    raise PreviewCancelledError("Cancelled by user.") from e
                last_error = e
                if attempt < _DOWNLOAD_ATTEMPTS:
                    self._log.warning(
                        "yt-dlp attempt %d/%d failed for %s (%s); "
                        "retrying with a fresh stream URL in %.0fs",
                        attempt, _DOWNLOAD_ATTEMPTS, vid, e,
                        _DOWNLOAD_RETRY_DELAY_SECONDS,
                    )
                    time.sleep(_DOWNLOAD_RETRY_DELAY_SECONDS)
            except Exception as e:  # defensive
                raise PreviewFetchError(f"Unexpected fetch error: {e}") from e

        if last_error is not None:
            raise PreviewFetchError(
                f"Could not fetch preview audio: {last_error}"
            ) from last_error

        path = self._locate_download(info, vid)
        if path is None:
            raise PreviewFetchError("Download completed but no audio file found.")
        return path

    def _locate_download(
        self, info: Optional[dict[str, Any]], vid: str,
    ) -> Optional[Path]:
        candidates: list[Path] = []
        if info:
            for rd in info.get("requested_downloads", []) or []:
                fp = rd.get("filepath") or rd.get("_filename")
                if fp:
                    candidates.append(Path(fp))
            fp = info.get("filepath")
            if fp:
                candidates.append(Path(fp))
        candidates.extend(self._cache_dir.glob(f"{vid}.*"))
        for p in candidates:
            if p.is_file() and p.suffix.lower() != ".part":
                return p
        return None

    # ── Decode ──

    def _decode_to_pcm(self, source: Path) -> np.ndarray:
        """ffmpeg → interleaved float32 stereo at the playback rate."""
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-i", str(source),
            "-f", "f32le",
            "-ac", str(_PLAYBACK_CHANNELS),
            "-ar", str(self._sr),
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=180,
                **self._subprocess_kwargs(),
            )
        except FileNotFoundError as e:
            raise PreviewDecodeError(f"ffmpeg not found: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise PreviewDecodeError("ffmpeg decode timed out.") from e

        if result.returncode != 0 or not result.stdout:
            tail = (result.stderr or b"").decode("utf-8", "replace")[-400:]
            raise PreviewDecodeError(f"ffmpeg decode failed: {tail}")

        flat = np.frombuffer(result.stdout, dtype=np.float32)
        # Trim any partial trailing frame, then reshape to (frames, ch).
        usable = (flat.size // _PLAYBACK_CHANNELS) * _PLAYBACK_CHANNELS
        flat = flat[:usable]
        samples = flat.reshape(-1, _PLAYBACK_CHANNELS).copy()
        if samples.size == 0:
            raise PreviewDecodeError("Decoded audio was empty.")
        return samples

    # ── Peaks ──

    def _compute_peaks(self, samples: np.ndarray) -> np.ndarray:
        """Downsample the mono mix into max-abs buckets, normalized to 1."""
        mono = samples.mean(axis=1)
        n = mono.shape[0]
        buckets = max(1, min(self._peak_buckets, n))
        # Pad so the reshape is exact, then take max-abs per bucket.
        per = int(np.ceil(n / buckets))
        pad = per * buckets - n
        if pad > 0:
            mono = np.concatenate([mono, np.zeros(pad, dtype=mono.dtype)])
        reshaped = np.abs(mono).reshape(buckets, per)
        peaks = reshaped.max(axis=1)
        peak_max = float(peaks.max()) if peaks.size else 0.0
        if peak_max > 1e-6:
            peaks = peaks / peak_max
        return peaks.astype(np.float32)

    # ── Helpers ──

    @staticmethod
    def _normalize_video_id(value: str) -> str:
        """Accept a bare id or a watch URL; return the 11-char id."""
        v = (value or "").strip()
        if "watch?v=" in v:
            v = v.split("watch?v=", 1)[1]
        if "youtu.be/" in v:
            v = v.split("youtu.be/", 1)[1]
        # Strip any trailing query params.
        for sep in ("&", "?", "/"):
            if sep in v:
                v = v.split(sep, 1)[0]
        return v

    @staticmethod
    def _subprocess_kwargs() -> dict[str, int]:
        if sys.platform != "win32":
            return {}
        CREATE_NO_WINDOW = 0x08000000
        return {"creationflags": CREATE_NO_WINDOW}

    @staticmethod
    def _check_cancel(event: Optional[threading.Event]) -> None:
        if event is not None and event.is_set():
            raise PreviewCancelledError("Cancelled by user.")

    @staticmethod
    def _emit(
        cb: Optional[Callable[[float, str], None]],
        percent: float,
        message: str,
    ) -> None:
        if cb is not None:
            try:
                cb(max(0.0, min(100.0, percent)), message)
            except Exception:
                pass
