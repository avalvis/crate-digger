"""
core/mpc_export.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Digital Crate → MPC Sample Workflow

One-click path from a Digital Crate discovery straight to an MPC-ready
sample folder: reuse (or fetch) the source audio, split it into stems,
convert each stem to MPC-native PCM WAV, and file them under a single
per-track folder. This deliberately bypasses the Vault entirely —
nothing here touches vault.db or the ingestion pipeline; it's a
lighter, sample-digging-focused sibling to "Queue it".

    <destination_root>/<Artist - Title>/
        vocals.wav
        drums.wav
        bass.wav
        other.wav

Zero UI ties. Callers are expected to invoke `export_sample_to_mpc`
from a background thread, matching the rest of the app's worker-thread
conventions (see ui/tabs/digital_crate.py's _ReelCard).
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.exporter import ExportError, MPCExporter
from core.preview import PreviewError, PreviewService
from core.stems import StemSeparationError, StemSeparator
from utils.paths import sanitize_filename_component


# ─── Public types ────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class MpcSampleResult:
    track_dir: Path
    stems: dict[str, Path]  # {'vocals': Path, 'drums': Path, ...}


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
    progress_callback: Optional[Callable[[str, float], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    logger: Optional[logging.Logger] = None,
) -> MpcSampleResult:
    """
    Download (or reuse a cached preview), split into stems, convert to
    MPC-native WAV, and file under `destination_root/<Artist - Title>/`.

    `progress_callback(stage_label, percent_0_100)` reports coarse
    overall progress across the three phases (fetch / separate / convert).
    Raises MpcSampleExportError (or the Cancelled subclass) on failure;
    never leaves a partial track folder full of stray demucs scratch files
    behind — the stems staging dir is always cleaned up.
    """
    log = logger or logging.getLogger("cratedigger.mpc_export")
    started = time.monotonic()
    display_name = f"{artist} — {title}"

    def emit(label: str, pct: float) -> None:
        if progress_callback is not None:
            try:
                progress_callback(label, max(0.0, min(100.0, pct)))
            except Exception:
                pass

    def check_cancel() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise MpcSampleExportCancelledError("Cancelled by user.")

    log.info("MPC export started: %s (video_id=%s)", display_name, video_id)

    try:
        # ── 1. Source audio: reuse the preview cache if we already have it ──
        check_cancel()
        cached = preview.get_cached_path(video_id)
        if cached is not None:
            log.info("MPC export: reusing cached audio for %s (%s)",
                     display_name, cached)
            audio_path = cached
            emit("Using cached audio…", 20.0)
        else:
            log.info("MPC export: no cache hit for %s — downloading", display_name)
            emit("Downloading…", 2.0)
            try:
                data = preview.fetch(
                    video_id,
                    progress_callback=lambda pct, msg: emit(
                        msg or "Downloading…", pct * 0.2,
                    ),
                    cancel_event=cancel_event,
                )
            except PreviewError as e:
                raise MpcSampleExportError(f"Could not fetch audio: {e}") from e
            if data.source_path is None:
                raise MpcSampleExportError(
                    "Download completed but no source file was cached."
                )
            audio_path = data.source_path
            log.info(
                "MPC export: download complete for %s in %.1fs",
                display_name, time.monotonic() - started,
            )

        check_cancel()

        # ── 2. Stem separation into a scratch dir ──
        track_name = sanitize_filename_component(
            f"{artist} - {title}", max_length=150,
        )
        stage_dir = (
            Path(staging_root) / "mpc_export"
            / sanitize_filename_component(video_id, max_length=32, fallback="track")
        )
        if stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)
        stage_dir.mkdir(parents=True, exist_ok=True)

        try:
            emit("Splitting stems…", 25.0)
            stems_started = time.monotonic()
            stems_result = stem_separator.separate(
                audio_path,
                stage_dir,
                progress_callback=lambda p: emit(
                    p.message or "Splitting stems…",
                    20.0 + (p.percent / 100.0) * 55.0,
                ),
                cancel_event=cancel_event,
            )
            log.info(
                "MPC export: stems separated for %s in %.1fs (%s)",
                display_name, time.monotonic() - stems_started,
                ", ".join(sorted(stems_result.stems)),
            )

            check_cancel()

            # ── 3. Convert stems to MPC-native WAV in the final track folder ──
            track_dir = Path(destination_root) / track_name
            track_dir.mkdir(parents=True, exist_ok=True)

            # Clean up any stems from a prior export of this same track so
            # re-running doesn't pile up "vocals (2).wav" siblings.
            for existing in track_dir.glob("*.wav"):
                if existing.stem.lower() in stems_result.stems:
                    try:
                        existing.unlink()
                    except OSError:
                        pass

            emit("Converting to WAV…", 80.0)
            convert_started = time.monotonic()
            export_result = exporter.export_batch(
                sources=list(stems_result.stems.values()),
                destination_root=track_dir,
                flatten=True,
                progress_callback=lambda p: emit(
                    "Converting to WAV…", 80.0 + (p.overall_percent / 100.0) * 20.0,
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
            raise MpcSampleExportError(f"Could not convert: {names}")

        emit("Done", 100.0)
        stem_files = {
            f.destination_path.stem.lower(): f.destination_path
            for f in export_result.exported
        }
        log.info(
            "MPC export complete: %s → %s (%.1fs total)",
            display_name, track_dir, time.monotonic() - started,
        )
        return MpcSampleResult(track_dir=track_dir, stems=stem_files)

    except MpcSampleExportCancelledError:
        log.info("MPC export cancelled: %s", display_name)
        raise
    except MpcSampleExportError:
        log.exception("MPC export failed: %s", display_name)
        raise
    except Exception as e:
        log.exception("MPC export failed with an unexpected error: %s", display_name)
        raise MpcSampleExportError(str(e)) from e
