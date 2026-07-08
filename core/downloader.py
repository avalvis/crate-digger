"""
core/downloader.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Audio Downloader

Thin, well-typed wrapper around yt-dlp that enforces the project's
lossless ingest contract:

    • Prefer native AAC (.m4a) streams from YouTube / YouTube Music.
    • Never transcode to MP3.
    • Transcode to AAC-in-M4A *only* as a last-resort fallback when
      the source provides no native AAC track (logged loudly, and
      surfaced to the caller via `DownloadResult.was_transcoded`).
    • Emit granular progress events so the UI can show per-job bars
      without ever blocking the main thread.
    • Raise typed, UI-friendly exceptions instead of leaking raw
      yt-dlp errors.

This module is import-safe from pure workers — it has zero ties to
`ui/` and must stay that way.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError


# ─── Public types ────────────────────────────────────────────────────

class DownloadStage(str, Enum):
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    COMPLETE = "complete"


@dataclass(slots=True)
class DownloadProgress:
    """Thread-safe snapshot of a single job's progress. Emitted via callback."""
    stage: DownloadStage
    percent: float = 0.0                 # 0..100
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    speed_bps: Optional[float] = None
    eta_seconds: Optional[int] = None
    message: str = ""


@dataclass(slots=True)
class DownloadResult:
    """Everything downstream modules need. No yt-dlp types leak out of here."""
    audio_path: Path
    title: str
    uploader: str                        # YouTube channel name
    artist: Optional[str]                # Populated by YouTube Music, else None
    track: Optional[str]                 # Populated by YouTube Music, else None
    album: Optional[str]                 # Populated by YouTube Music, else None
    duration_seconds: float
    thumbnail_url: Optional[str]         # Highest-resolution thumbnail URL
    video_id: str
    source_url: str
    source_platform: str                 # 'youtube' | 'youtube_music'
    was_transcoded: bool                 # True if we had to remux/transcode
    raw_info: dict[str, Any]             # Full yt-dlp info_dict, for advanced use


# ─── Public exceptions ───────────────────────────────────────────────

class DownloaderError(Exception):
    """Base class for all errors surfaced by the downloader."""


class UnsupportedURLError(DownloaderError):
    """URL is not a supported YouTube / YouTube Music link, or is a playlist."""


class ExtractionFailedError(DownloaderError):
    """yt-dlp could not extract info or download media."""


class NoAudioStreamError(DownloaderError):
    """The URL resolved but no usable .m4a file ended up on disk."""


class DownloadCancelledError(DownloaderError):
    """The caller requested cancellation via the cancel_event."""


# ─── URL recognition ─────────────────────────────────────────────────

_YT_HOSTS = re.compile(
    r"^(?:https?://)?(?:www\.)?"
    r"(?P<host>music\.youtube\.com|youtube\.com|youtu\.be|m\.youtube\.com)/",
    re.IGNORECASE,
)


def _classify_url(url: str) -> str:
    """Return 'youtube_music' | 'youtube'. Raises UnsupportedURLError otherwise."""
    m = _YT_HOSTS.match(url.strip())
    if not m:
        raise UnsupportedURLError(f"Not a YouTube / YouTube Music URL: {url!r}")
    host = m.group("host").lower()
    return "youtube_music" if host == "music.youtube.com" else "youtube"


# ─── The Downloader ──────────────────────────────────────────────────

# Format spec per project brief: strictly prefer native m4a. The fallback
# branches are only hit when YouTube serves no AAC track — in which case
# our postprocessor transcodes to AAC-in-M4A and we flag the result.
_FORMAT_SPEC = "bestaudio[ext=m4a]/bestaudio/best"

# Fallback transcode bitrate for the rare non-AAC case. 256 kbit/s VBR
# is transparent for the overwhelming majority of source material and
# well above the typical 128 kbit/s AAC YouTube actually serves.
_FALLBACK_AAC_QUALITY = "256"

# YouTube's per-format stream URLs are short-lived and occasionally get
# rejected with a 403 even though the video itself is perfectly
# downloadable — re-extracting fresh info (a new signed URL) and trying
# again almost always clears it. Not a network-retry (yt-dlp's own
# `retries`/`fragment_retries` handle those); this re-runs extraction
# from scratch.
_DOWNLOAD_ATTEMPTS = 3
_DOWNLOAD_RETRY_DELAY_SECONDS = 2.0


class Downloader:
    """
    Facade over yt-dlp. One instance can service many downloads — each
    `download()` call is a self-contained yt-dlp invocation and is
    expected to run on a worker thread managed by core.queue_manager.
    """

    def __init__(
        self,
        ffmpeg_path: str,
        logger: Optional[logging.Logger] = None,
        *,
        retries: int = 5,
        fragment_retries: int = 5,
        concurrent_fragments: int = 4,
    ) -> None:
        self._ffmpeg_path = ffmpeg_path
        self._log = logger or logging.getLogger("cratedigger.downloader")
        self._retries = retries
        self._fragment_retries = fragment_retries
        self._concurrent_fragments = concurrent_fragments

    # ── Public API ──

    def probe(self, url: str) -> DownloadResult:
        """
        Resolve metadata for a URL *without* downloading. Used by the
        Manual Rip tab to show 'Artist — Title' before the user commits
        to queueing. `audio_path` on the result is a placeholder; callers
        must not assume a file exists on disk.
        """
        platform = _classify_url(url)
        opts = self._base_opts(staging_dir=None)
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except (DownloadError, ExtractorError) as e:
            raise ExtractionFailedError(f"Could not probe URL: {e}") from e

        if info is None:
            raise ExtractionFailedError("yt-dlp returned no info for URL.")
        if info.get("_type") == "playlist":
            raise UnsupportedURLError(
                "Playlists are not supported; paste a single-track URL."
            )

        return self._build_result(
            info=info,
            audio_path=Path(),           # placeholder — probe only
            source_url=url,
            source_platform=platform,
            was_transcoded=False,
        )

    def download(
        self,
        url: str,
        staging_dir: Path,
        progress_callback: Optional[Callable[[DownloadProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> DownloadResult:
        """
        Download a single URL as .m4a into `staging_dir`. Returns a fully
        populated DownloadResult. The pipeline orchestrator is responsible
        for moving the file to its final vault location once BPM/Key are
        known — this module never writes outside `staging_dir`.
        """
        platform = _classify_url(url)
        staging_dir = Path(staging_dir)
        staging_dir.mkdir(parents=True, exist_ok=True)

        self._emit(progress_callback, DownloadStage.PREPARING,
                   message="Resolving stream…")

        opts = self._base_opts(staging_dir=staging_dir)
        opts["progress_hooks"] = [
            self._make_progress_hook(progress_callback, cancel_event)
        ]
        opts["postprocessor_hooks"] = [
            self._make_postprocessor_hook(progress_callback, cancel_event)
        ]

        info: dict[str, Any] | None = None
        last_error: Optional[Exception] = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("Cancelled by user.")
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                last_error = None
                break
            except DownloadCancelledError:
                raise
            except (DownloadError, ExtractorError) as e:
                # yt-dlp sometimes wraps hook exceptions — double-check the
                # event so cancellation isn't misreported as a network failure.
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelledError("Cancelled by user.") from e
                last_error = e
                if attempt < _DOWNLOAD_ATTEMPTS:
                    self._log.warning(
                        "yt-dlp attempt %d/%d failed for %s (%s); "
                        "retrying with a fresh stream URL in %.0fs",
                        attempt, _DOWNLOAD_ATTEMPTS, url, e,
                        _DOWNLOAD_RETRY_DELAY_SECONDS,
                    )
                    time.sleep(_DOWNLOAD_RETRY_DELAY_SECONDS)
            except Exception as e:           # defensive: never leak raw tracebacks
                self._log.exception("Unexpected downloader failure for %s", url)
                raise ExtractionFailedError(f"Unexpected error: {e}") from e

        if last_error is not None:
            self._log.error(
                "yt-dlp failed for %s after %d attempt(s): %s",
                url, _DOWNLOAD_ATTEMPTS, last_error,
            )
            raise ExtractionFailedError(str(last_error)) from last_error

        if info is None:
            raise ExtractionFailedError("yt-dlp returned no info.")
        if info.get("_type") == "playlist":
            raise UnsupportedURLError(
                "Playlists are not supported; paste a single-track URL."
            )

        audio_path, was_transcoded = self._locate_output(info, staging_dir)
        if audio_path is None:
            raise NoAudioStreamError(
                "Download completed but no .m4a file was produced."
            )

        self._emit(progress_callback, DownloadStage.COMPLETE,
                   percent=100.0, message="Download complete")

        return self._build_result(
            info=info,
            audio_path=audio_path,
            source_url=url,
            source_platform=platform,
            was_transcoded=was_transcoded,
        )

    # ── yt-dlp option builder ──

    def _base_opts(self, staging_dir: Optional[Path]) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "format": _FORMAT_SPEC,
            "ffmpeg_location": self._ffmpeg_path,
            "quiet": True,
            "no_warnings": False,
            "no_color": True,                # otherwise raw ANSI codes leak
                                              # into logged/surfaced error text
            "noprogress": True,              # we report via progress_hooks
            "logger": _YDLLoggerAdapter(self._log),
            "noplaylist": True,
            "retries": self._retries,
            "fragment_retries": self._fragment_retries,
            "concurrent_fragment_downloads": self._concurrent_fragments,
            "overwrites": True,
            # Safety net: when `bestaudio[ext=m4a]` is unavailable and we
            # fall back to opus/webm, this transcodes to AAC-in-M4A. When
            # the source *is* already m4a/aac, yt-dlp skips re-encoding.
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": _FALLBACK_AAC_QUALITY,
                },
            ],
        }
        if staging_dir is not None:
            # %(id)s.%(ext)s guarantees collision-free filenames. The
            # pipeline orchestrator renames post-analysis once BPM/Key
            # are known and the final vault path can be constructed.
            opts["outtmpl"] = {"default": str(staging_dir / "%(id)s.%(ext)s")}
            opts["paths"] = {"home": str(staging_dir)}
        return opts

    # ── Progress plumbing ──

    def _make_progress_hook(
        self,
        cb: Optional[Callable[[DownloadProgress], None]],
        cancel: Optional[threading.Event],
    ) -> Callable[[dict[str, Any]], None]:
        def hook(d: dict[str, Any]) -> None:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelledError("Cancelled by user.")
            if cb is None:
                return

            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = int(d.get("downloaded_bytes") or 0)
                percent = (downloaded / total * 100.0) if total else 0.0
                cb(DownloadProgress(
                    stage=DownloadStage.DOWNLOADING,
                    percent=min(percent, 99.0),   # reserve 100% for COMPLETE
                    downloaded_bytes=downloaded,
                    total_bytes=int(total) if total else None,
                    speed_bps=float(d["speed"]) if d.get("speed") else None,
                    eta_seconds=int(d["eta"]) if d.get("eta") else None,
                    message="Downloading audio stream",
                ))
            elif status == "finished":
                cb(DownloadProgress(
                    stage=DownloadStage.POSTPROCESSING,
                    percent=99.0,
                    downloaded_bytes=int(d.get("downloaded_bytes") or 0),
                    total_bytes=int(d["total_bytes"]) if d.get("total_bytes") else None,
                    message="Finalizing file",
                ))
        return hook

    def _make_postprocessor_hook(
        self,
        cb: Optional[Callable[[DownloadProgress], None]],
        cancel: Optional[threading.Event],
    ) -> Callable[[dict[str, Any]], None]:
        def hook(d: dict[str, Any]) -> None:
            if cancel is not None and cancel.is_set():
                raise DownloadCancelledError("Cancelled by user.")
            if cb is None:
                return
            if d.get("status") == "started":
                cb(DownloadProgress(
                    stage=DownloadStage.POSTPROCESSING,
                    percent=99.0,
                    message=f"Post-processing ({d.get('postprocessor', '')})",
                ))
        return hook

    # ── Output inspection ──

    def _locate_output(
        self,
        info: dict[str, Any],
        staging_dir: Path,
    ) -> tuple[Optional[Path], bool]:
        """
        Find the .m4a yt-dlp produced and determine whether transcoding
        was required. yt-dlp surfaces the final path in several places
        depending on whether postprocessors ran — check them in order.
        """
        candidates: list[Path] = []

        # 1. `requested_downloads` — most reliable on yt-dlp 2023+.
        for rd in info.get("requested_downloads", []) or []:
            fp = rd.get("filepath") or rd.get("_filename")
            if fp:
                candidates.append(Path(fp))

        # 2. Top-level `filepath` (legacy).
        fp = info.get("filepath")
        if fp:
            candidates.append(Path(fp))

        # 3. Derive from id (fallback when neither field is populated).
        vid = info.get("id")
        if vid:
            for ext in ("m4a", "mp4"):
                candidates.append(staging_dir / f"{vid}.{ext}")

        m4a_paths = [p for p in candidates
                     if p.exists() and p.suffix.lower() == ".m4a"]
        if not m4a_paths:
            return None, False

        final_path = m4a_paths[0]
        # `ext` in info is the *source* extension before postprocessing.
        original_ext = (info.get("ext") or "").lower()
        was_transcoded = original_ext not in ("m4a", "mp4", "")
        if was_transcoded:
            self._log.warning(
                "Source had no native AAC stream (%s); transcoded to m4a.",
                original_ext or "unknown",
            )
        return final_path, was_transcoded

    # ── Result assembly ──

    def _build_result(
        self,
        info: dict[str, Any],
        audio_path: Path,
        source_url: str,
        source_platform: str,
        was_transcoded: bool,
    ) -> DownloadResult:
        return DownloadResult(
            audio_path=audio_path,
            title=str(info.get("title") or "").strip(),
            uploader=str(info.get("uploader") or info.get("channel") or "").strip(),
            artist=(info.get("artist") or None),
            track=(info.get("track") or None),
            album=(info.get("album") or None),
            duration_seconds=float(info.get("duration") or 0.0),
            thumbnail_url=_pick_best_thumbnail(info),
            video_id=str(info.get("id") or ""),
            source_url=source_url,
            source_platform=source_platform,
            was_transcoded=was_transcoded,
            raw_info=info,
        )

    # ── Misc ──

    @staticmethod
    def _emit(
        cb: Optional[Callable[[DownloadProgress], None]],
        stage: DownloadStage,
        *,
        percent: float = 0.0,
        message: str = "",
    ) -> None:
        if cb is None:
            return
        cb(DownloadProgress(stage=stage, percent=percent, message=message))


# ─── Helpers ─────────────────────────────────────────────────────────

def _pick_best_thumbnail(info: dict[str, Any]) -> Optional[str]:
    """Return the URL of the highest-resolution thumbnail, or None."""
    thumbs = info.get("thumbnails") or []
    if not thumbs:
        return info.get("thumbnail")

    def score(t: dict[str, Any]) -> tuple[int, int]:
        w = int(t.get("width") or 0)
        h = int(t.get("height") or 0)
        pref = int(t.get("preference") or 0)
        return (w * h, pref)

    best = max(thumbs, key=score)
    return best.get("url")


class _YDLLoggerAdapter:
    """
    Adapts our stdlib logger to the shape yt-dlp expects. yt-dlp's
    INFO channel is extremely chatty ("[youtube] extracting URL…",
    "[info] downloading 1 format…", etc.) — we demote those to DEBUG so
    `cratedigger.log` stays signal-rich. True warnings/errors are kept
    at their original level.
    """

    __slots__ = ("_log",)

    def __init__(self, log: logging.Logger) -> None:
        self._log = log

    def debug(self, msg: str) -> None:
        # yt-dlp emits both real-debug and info-as-debug through this
        # method; the "[debug] " prefix differentiates them.
        if msg.startswith("[debug] "):
            self._log.debug(msg[8:])
        else:
            self._log.debug(msg)              # intentionally demoted

    def info(self, msg: str) -> None:
        self._log.debug(msg)                   # intentionally demoted

    def warning(self, msg: str) -> None:
        self._log.warning(msg)

    def error(self, msg: str) -> None:
        self._log.error(msg)