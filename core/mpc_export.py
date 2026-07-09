"""
core/mpc_export.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Digital Crate → MPC Sample Workflow

One-click path from a Digital Crate discovery straight to an MPC-ready
sample folder: reuse (or fetch) the source audio, optionally split into
stems, and convert to MPC-native PCM WAV under a per-track folder.

    <destination_root>/<Artist - Title>/
        original.wav          (when song and/or both)
        vocals.wav            (when stems and/or both)
        drums.wav
        bass.wav
        other.wav

Zero UI ties. Callers invoke `export_sample_to_mpc` from a background
thread (see ui/tabs/digital_crate.py).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from core.exporter import ExportError, MPCExporter
from core.preview import PreviewError, PreviewService
from core.stems import StemModel, StemSeparationError, StemSeparator
from utils.paths import sanitize_filename_component


# MPC Workflow is a quick sample-dig path — use the single-model demucs
# preset and cap length so a 5-minute album track doesn't block for 20+ min
# (htdemucs_ft runs four models back-to-back on CPU).
MPC_WORKFLOW_STEM_MODEL = StemModel.HTDEMUCS
MPC_WORKFLOW_MAX_SECONDS = 120.0
_ORIGINAL_WAV_STEM = "original"


class MpcExportMode(str, Enum):
  """What to write into the MPC sample folder."""

  SONG = "song"    # full track as original.wav
  STEMS = "stems"  # trimmed stem split only
  BOTH = "both"    # full original.wav + trimmed stems


# ─── Public types ────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MpcSampleResult:
    track_dir: Path
    stems: dict[str, Path]           # stem name → wav path
    original: Optional[Path] = None  # full-track original.wav, if exported


# ─── Public exceptions ───────────────────────────────────────────────


class MpcSampleExportError(Exception):
    """Base class for MPC sample workflow failures."""


class MpcSampleExportCancelledError(MpcSampleExportError):
    """Caller cancelled via cancel_event."""


# ─── Public API ──────────────────────────────────────────────────────


def export_sample_to_mpc(
    *,
    video_id: str,
    artist: str,
    title: str,
    destination_root: Path,
    staging_root: Path,
    preview: PreviewService,
    stem_separator: StemSeparator,
    exporter: MPCExporter,
    mode: MpcExportMode = MpcExportMode.STEMS,
    ffmpeg_path: Optional[str] = None,
    stem_model: StemModel = MPC_WORKFLOW_STEM_MODEL,
    max_duration_seconds: float = MPC_WORKFLOW_MAX_SECONDS,
    progress_callback: Optional[Callable[[str, float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    logger: Optional[logging.Logger] = None,
) -> MpcSampleResult:
    """
    Download (or reuse cached audio), then export per `mode`:

    • SONG  — full track as ``original.wav`` (no stem separation)
    • STEMS — opening slice split into stem WAVs (fast CPU path)
    • BOTH  — full ``original.wav`` plus trimmed stem WAVs
    """
    log = logger or logging.getLogger("cratedigger.mpc_export")
    started = time.monotonic()
    display_name = f"{artist} — {title}"
    want_song = mode in (MpcExportMode.SONG, MpcExportMode.BOTH)
    want_stems = mode in (MpcExportMode.STEMS, MpcExportMode.BOTH)

    def emit(label: str, pct: float) -> None:
        if progress_callback is not None:
            try:
                progress_callback(label, max(0.0, min(100.0, pct)))
            except Exception:
                pass

    def check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise MpcSampleExportCancelledError("Cancelled by user.")

    log.info(
        "MPC export started: %s (video_id=%s, mode=%s)",
        display_name, video_id, mode.value,
    )

    track_name = sanitize_filename_component(
        f"{artist} - {title}", max_length=150,
    )
    track_dir = Path(destination_root) / track_name
    stage_dir = (
        Path(staging_root) / "mpc_export"
        / sanitize_filename_component(video_id, max_length=32, fallback="track")
    )
    ffmpeg = ffmpeg_path or exporter.ffmpeg_path
    original_path: Optional[Path] = None
    stem_files: dict[str, Path] = {}

    try:
        check_cancel()
        emit("Locating audio…", 2.0)
        audio_path = _resolve_audio(
            video_id,
            preview,
            emit,
            check_cancel,
            cancel_event,
            log,
            display_name,
        )

        track_dir.mkdir(parents=True, exist_ok=True)

        if want_song:
            emit("Converting original…", 15.0 if want_stems else 40.0)
            try:
                original_path = _export_original_wav(
                    audio_path,
                    track_dir,
                    stage_dir,
                    exporter,
                    emit,
                    check_cancel,
                    cancel_event,
                    progress_end=30.0 if want_stems else 95.0,
                )
            finally:
                if want_stems:
                    pass  # stage_dir reused for stems below
                elif stage_dir.exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
            log.info("MPC export: original saved → %s", original_path)

        if want_stems:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            stage_dir.mkdir(parents=True, exist_ok=True)

            stem_input = _maybe_trim_audio(
                audio_path,
                max_duration_seconds=max_duration_seconds,
                ffmpeg_path=ffmpeg,
                work_dir=stage_dir,
                logger=log,
            )
            if stem_input != audio_path:
                emit("Trimmed for stem split…", 35.0 if want_song else 22.0)

            try:
                emit("Splitting stems…", 40.0 if want_song else 25.0)
                stems_started = time.monotonic()
                stems_result = stem_separator.separate(
                    stem_input,
                    stage_dir,
                    progress_callback=lambda p: emit(
                        p.message or "Splitting stems…",
                        (40.0 if want_song else 25.0)
                        + (p.percent / 100.0) * (45.0 if want_song else 55.0),
                    ),
                    cancel_event=cancel_event,
                    model=stem_model,
                )
                log.info(
                    "MPC export: stems separated for %s in %.1fs (%s)",
                    display_name, time.monotonic() - stems_started,
                    ", ".join(sorted(stems_result.stems)),
                )

                check_cancel()

                for existing in track_dir.glob("*.wav"):
                    if existing.stem.lower() in stems_result.stems:
                        try:
                            existing.unlink()
                        except OSError:
                            pass

                emit("Converting stems…", 88.0)
                convert_started = time.monotonic()
                export_result = exporter.export_batch(
                    sources=list(stems_result.stems.values()),
                    destination_root=track_dir,
                    flatten=True,
                    progress_callback=lambda p: emit(
                        "Converting stems…",
                        88.0 + (p.overall_percent / 100.0) * 12.0,
                    ),
                    cancel_event=cancel_event,
                )
                log.info(
                    "MPC export: converted %d/%d stem(s) for %s in %.1fs",
                    len(export_result.exported),
                    len(export_result.exported) + len(export_result.failed),
                    display_name, time.monotonic() - convert_started,
                )
            except (StemSeparationError, ExportError) as e:
                raise MpcSampleExportError(str(e)) from e
            finally:
                shutil.rmtree(stage_dir, ignore_errors=True)

            if export_result.failed:
                names = ", ".join(p.name for p, _ in export_result.failed)
                raise MpcSampleExportError(f"Could not convert stems: {names}")

            stem_files = {
                f.destination_path.stem.lower(): f.destination_path
                for f in export_result.exported
            }

        emit("Done", 100.0)
        log.info(
            "MPC export complete: %s → %s (%.1fs, mode=%s)",
            display_name, track_dir, time.monotonic() - started, mode.value,
        )
        return MpcSampleResult(
            track_dir=track_dir,
            stems=stem_files,
            original=original_path,
        )

    except MpcSampleExportCancelledError:
        log.info("MPC export cancelled: %s", display_name)
        raise
    except MpcSampleExportError:
        log.exception("MPC export failed: %s", display_name)
        raise
    except Exception as e:
        log.exception("MPC export failed with an unexpected error: %s", display_name)
        raise MpcSampleExportError(str(e)) from e


def _resolve_audio(
    video_id: str,
    preview: PreviewService,
    emit: Callable[[str, float], None],
    check_cancel: Callable[[], None],
    cancel_event: Optional[threading.Event],
    log: logging.Logger,
    display_name: str,
) -> Path:
    cached = preview.get_cached_path(video_id)
    if cached is not None:
        log.info("MPC export: reusing cached audio for %s (%s)", display_name, cached)
        emit("Using cached audio…", 12.0)
        return cached

    log.info("MPC export: no cache hit for %s — downloading", display_name)
    emit("Downloading…", 5.0)
    try:
        data = preview.fetch(
            video_id,
            progress_callback=lambda pct, msg: emit(
                msg or "Downloading…", 5.0 + pct * 0.12,
            ),
            cancel_event=cancel_event,
        )
    except PreviewError as e:
        raise MpcSampleExportError(f"Could not fetch audio: {e}") from e
    check_cancel()
    if data.source_path is None:
        raise MpcSampleExportError(
            "Download completed but no source file was cached."
        )
    return data.source_path


def _export_original_wav(
    audio_path: Path,
    track_dir: Path,
    stage_dir: Path,
    exporter: MPCExporter,
    emit: Callable[[str, float], None],
    check_cancel: Callable[[], None],
    cancel_event: Optional[threading.Event],
    *,
    progress_end: float,
) -> Path:
    """Convert the full source file to ``original.wav`` in ``track_dir``."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    staging_copy = stage_dir / f"{_ORIGINAL_WAV_STEM}{audio_path.suffix}"
    if staging_copy.exists():
        staging_copy.unlink()
    shutil.copy2(audio_path, staging_copy)

    final = track_dir / f"{_ORIGINAL_WAV_STEM}.wav"
    if final.exists():
        try:
            final.unlink()
        except OSError:
            pass

    check_cancel()
    export_result = exporter.export_batch(
        sources=[staging_copy],
        destination_root=track_dir,
        flatten=True,
        progress_callback=lambda p: emit(
            "Converting original…",
            15.0 + (p.overall_percent / 100.0) * (progress_end - 15.0),
        ),
        cancel_event=cancel_event,
    )
    if export_result.failed:
        names = ", ".join(p.name for p, _ in export_result.failed)
        raise MpcSampleExportError(f"Could not convert original: {names}")
    if not export_result.exported:
        raise MpcSampleExportError("Original conversion produced no output.")

    produced = export_result.exported[0].destination_path
    if produced.name != final.name:
        if final.exists():
            final.unlink()
        produced.rename(final)
    return final


def _probe_duration(ffmpeg_path: str, source: Path) -> Optional[float]:
    """Return container duration in seconds via ffprobe."""
    ffmpeg_p = Path(ffmpeg_path)
    ffprobe = ffmpeg_p.with_name(
        "ffprobe.exe" if sys.platform == "win32" else "ffprobe",
    )
    if not ffprobe.exists():
        ffprobe_path = shutil.which("ffprobe")
        if ffprobe_path is None:
            return None
        ffprobe = Path(ffprobe_path)

    cmd = [
        str(ffprobe),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    try:
        return float((res.stdout or "").strip())
    except ValueError:
        return None


def _maybe_trim_audio(
    audio_path: Path,
    *,
    max_duration_seconds: Optional[float],
    ffmpeg_path: str,
    work_dir: Path,
    logger: logging.Logger,
) -> Path:
    """Shorten long sources so MPC stem separation stays responsive on CPU."""
    if max_duration_seconds is None or max_duration_seconds <= 0:
        return audio_path

    duration = _probe_duration(ffmpeg_path, audio_path)
    if duration is None or duration <= max_duration_seconds + 0.5:
        return audio_path

    trimmed = work_dir / f"{audio_path.stem}_mpc_trim.m4a"
    cmd = [
        ffmpeg_path,
        "-y",
        "-i", str(audio_path),
        "-t", str(max_duration_seconds),
        "-vn",
        "-acodec", "copy",
        str(trimmed),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise MpcSampleExportError(f"Could not trim audio: {e}") from e

    if res.returncode != 0 or not trimmed.exists():
        cmd = [
            ffmpeg_path,
            "-y",
            "-i", str(audio_path),
            "-t", str(max_duration_seconds),
            "-vn",
            "-c:a", "aac",
            "-b:a", "192k",
            str(trimmed),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            raise MpcSampleExportError(f"Could not trim audio: {e}") from e
        if res.returncode != 0 or not trimmed.exists():
            tail = (res.stderr or res.stdout or "unknown error")[-400:]
            raise MpcSampleExportError(f"Could not trim audio: {tail}")

    logger.info(
        "MPC export: trimmed %.1fs → %.1fs for faster stem separation",
        duration,
        max_duration_seconds,
    )
    return trimmed
