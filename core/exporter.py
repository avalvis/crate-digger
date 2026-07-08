"""
core/exporter.py
──────────────────────────────────────────────────────────────────────
Crate Digger — MPC Export (m4a → uncompressed 16-bit / 44.1 kHz WAV)

Invoked from the Vault tab when the user selects tracks and clicks
"Export to MPC". Produces PCM WAV files on a chosen destination
(typically an SD card for the MPC One+ / Live / Key 61) that the
MPC hardware can natively sample without any further conversion.

Design contract:
  • Bit-exact PCM output: 16-bit signed little-endian, 44.1 kHz,
    stereo (downmixed from mono sources, preserved from stereo).
    This matches the MPC's native engine — any deviation forces
    on-device resampling at import time.
  • Per-file atomicity: write to a `.partial` filename first, fsync,
    then rename. Yanked SD cards and interrupted USB transfers
    never leave corrupt .wav files the MPC will choke on.
  • Subprocess isolation for ffmpeg with CREATE_NO_WINDOW on Windows
    — no console flashes during export in the frozen GUI.
  • Granular progress: per-file percent from ffmpeg's `-progress pipe:1`
    machine-readable output, parsed out of the ffmpeg stream.
  • Cancel-aware: polled threading.Event gracefully terminates the
    active ffmpeg child with SIGTERM → 3s grace → SIGKILL.

Zero UI. Zero DB writes here — the caller (pipeline / queue_manager)
logs mpc_exports rows on each successful file.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from utils.paths import sanitize_filename_component

if TYPE_CHECKING:
    from core.chopper import ChopPlan


# ─── Public types ────────────────────────────────────────────────────

class ExportStage(str, Enum):
    PREPARING = "preparing"
    EXPORTING = "exporting"
    FINALIZING = "finalizing"
    COMPLETE = "complete"


@dataclass(slots=True)
class ExportProgress:
    stage: ExportStage
    overall_percent: float         # 0..100 across the whole batch
    current_file_percent: float    # 0..100 within the current file
    current_index: int             # 1-based index of the file in progress
    total_files: int
    current_filename: str
    elapsed_seconds: float
    message: str = ""


@dataclass(slots=True, frozen=True)
class ExportedFile:
    source_path: Path
    destination_path: Path
    wav_size_bytes: int
    elapsed_seconds: float


@dataclass(slots=True, frozen=True)
class ExportResult:
    exported: tuple[ExportedFile, ...]
    failed: tuple[tuple[Path, str], ...]        # (source, error_message)
    destination_root: Path
    total_elapsed_seconds: float


# ─── Public exceptions ───────────────────────────────────────────────

class ExportError(Exception):
    """Base class for exporter failures."""


class ExportDestinationError(ExportError):
    """Destination path is invalid, unwritable, or has insufficient space."""


class FFmpegNotAvailableError(ExportError):
    """ffmpeg binary could not be found or launched."""


class ExportCancelledError(ExportError):
    """Caller cancelled via cancel_event mid-batch."""


# ─── Tunables ────────────────────────────────────────────────────────

# MPC hardware samples natively at 44.1 kHz, 16-bit. This combo is
# what maximizes compatibility across the MPC One, Live II, X SE, etc.
_TARGET_SAMPLE_RATE = 44100
_TARGET_BIT_DEPTH = 16
_TARGET_CHANNELS = 2                     # Stereo; mono sources get duplicated

# ffmpeg -progress writes a key=value block every ~500ms by default.
# Progress keys we care about:
_PROG_OUT_TIME_US = re.compile(r"out_time_us=(\d+)")
_PROG_TOTAL_SIZE  = re.compile(r"total_size=(\d+)")
_PROG_STATUS      = re.compile(r"progress=(\w+)")


# ─── The Exporter ────────────────────────────────────────────────────

class MPCExporter:
    """
    Convert .m4a files to 16-bit / 44.1 kHz PCM WAV for MPC hardware.
    Stateless across calls; safe to share across threads.
    """

    def __init__(
        self,
        ffmpeg_path: str,
        logger: Optional[logging.Logger] = None,
        *,
        target_sample_rate: int = _TARGET_SAMPLE_RATE,
        target_bit_depth: int = _TARGET_BIT_DEPTH,
        target_channels: int = _TARGET_CHANNELS,
    ) -> None:
        self._ffmpeg = ffmpeg_path
        self._log = logger or logging.getLogger("cratedigger.exporter")
        self._sr = int(target_sample_rate)
        self._bits = int(target_bit_depth)
        self._channels = int(target_channels)

        if self._bits not in (16, 24):
            raise ValueError(f"Unsupported bit depth: {self._bits}")

    def update_format(
        self,
        *,
        sample_rate: Optional[int] = None,
        bit_depth: Optional[int] = None,
    ) -> None:
        """Hot-update export format (used when Settings change at runtime)."""
        if sample_rate is not None:
            self._sr = int(sample_rate)
        if bit_depth is not None:
            bits = int(bit_depth)
            if bits not in (16, 24):
                raise ValueError(f"Unsupported bit depth: {bits}")
            self._bits = bits

    @property
    def ffmpeg_path(self) -> str:
        return self._ffmpeg

    # ── Public API ──

    def export_batch(
        self,
        sources: Iterable[Path],
        destination_root: Path,
        *,
        flatten: bool = True,
        progress_callback: Optional[Callable[[ExportProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ExportResult:
        """
        Export every file in `sources` to `destination_root` as PCM WAV.

        Args:
            sources: Paths to source .m4a files (from the Vault).
            destination_root: Target directory, usually an SD card root
                like `/Volumes/MPC_SD/`. Created if missing.
            flatten: If True (default), all WAVs land directly in
                destination_root. If False, preserves the
                [Genre]/[BPM_Key_Artist_Title]/ hierarchy from the Vault.
                MPC hardware browses flat folders more comfortably —
                hence the default.
            progress_callback: Receives granular ExportProgress events
                suitable for driving a UI progress bar directly.
            cancel_event: When set, aborts after the current file
                completes (clean) or immediately terminates the ffmpeg
                child (hard cancel).

        Returns:
            ExportResult with per-file success/failure breakdown.
            Never raises for individual file failures — those are
            captured in `.failed` so a single bad file doesn't kill
            the whole batch.
        """
        source_list = [Path(s) for s in sources]
        dest_root = Path(destination_root)

        self._validate_destination(dest_root)

        total = len(source_list)
        if total == 0:
            raise ExportError("No source files provided for export.")

        started = time.monotonic()
        exported: list[ExportedFile] = []
        failed: list[tuple[Path, str]] = []

        self._emit(progress_callback, ExportProgress(
            stage=ExportStage.PREPARING,
            overall_percent=0.0,
            current_file_percent=0.0,
            current_index=0,
            total_files=total,
            current_filename="",
            elapsed_seconds=0.0,
            message=f"Preparing {total} file(s) for export",
        ))

        for idx, src in enumerate(source_list, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelledError(
                    f"Cancelled after {len(exported)}/{total} files."
                )

            try:
                one = self._export_one(
                    src, dest_root, idx, total, flatten,
                    started, progress_callback, cancel_event,
                )
                exported.append(one)
            except ExportCancelledError:
                raise
            except Exception as e:
                # Single-file failures don't abort the batch.
                self._log.exception("Export failed for %s", src)
                failed.append((src, str(e)))

        total_elapsed = time.monotonic() - started
        self._emit(progress_callback, ExportProgress(
            stage=ExportStage.COMPLETE,
            overall_percent=100.0,
            current_file_percent=100.0,
            current_index=total,
            total_files=total,
            current_filename="",
            elapsed_seconds=total_elapsed,
            message=f"Exported {len(exported)}/{total} files "
                    f"({len(failed)} failed)",
        ))

        self._log.info(
            "Export batch complete: %d/%d succeeded in %.1fs to %s",
            len(exported), total, total_elapsed, dest_root,
        )
        return ExportResult(
            exported=tuple(exported),
            failed=tuple(failed),
            destination_root=dest_root,
            total_elapsed_seconds=total_elapsed,
        )

    def export_chop_kit(
        self,
        source: Path,
        plan: "ChopPlan",
        destination_root: Path,
        *,
        kit_name: Optional[str] = None,
        include_chops: bool = True,
        include_loops: bool = True,
        progress_callback: Optional[Callable[[ExportProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> ExportResult:
        """
        Export numbered one-shots + bar-aligned loops as an MPC-ready kit.

        Layout:
          <kit_name>/
            Chops/001.wav, 002.wav, …
            Loops/1bar.wav, 2bar.wav, …
            pad_map.json   — pad index → file mapping
        """
        source = Path(source)
        dest_root = Path(destination_root)
        self._validate_destination(dest_root)

        name = sanitize_filename_component(
            kit_name or source.stem, max_length=80,
        )
        kit_dir = dest_root / name
        kit_dir.mkdir(parents=True, exist_ok=True)
        chops_dir = kit_dir / "Chops"
        loops_dir = kit_dir / "Loops"
        if include_chops:
            chops_dir.mkdir(exist_ok=True)
        if include_loops:
            loops_dir.mkdir(exist_ok=True)

        segments: list[tuple[Path, float, float, str]] = []
        pad_entries: list[dict[str, object]] = []
        pad_index = 1

        if include_chops:
            for i, (start, end) in enumerate(plan.one_shot_regions, start=1):
                fname = f"{i:03d}.wav"
                segments.append((chops_dir / fname, start, end - start, fname))
                if pad_index <= 16:
                    pad_entries.append({
                        "pad": pad_index,
                        "file": f"Chops/{fname}",
                        "start_seconds": round(start, 4),
                        "end_seconds": round(end, 4),
                        "type": "one_shot",
                    })
                    pad_index += 1

        if include_loops:
            for loop in plan.loop_regions:
                fname = f"{loop.bars}bar_loop.wav"
                segments.append((
                    loops_dir / fname,
                    loop.start_seconds,
                    loop.end_seconds - loop.start_seconds,
                    fname,
                ))
                if pad_index <= 16:
                    pad_entries.append({
                        "pad": pad_index,
                        "file": f"Loops/{fname}",
                        "start_seconds": round(loop.start_seconds, 4),
                        "end_seconds": round(loop.end_seconds, 4),
                        "bars": loop.bars,
                        "type": "loop",
                    })
                    pad_index += 1

        if not segments:
            raise ExportError("Chop plan produced no exportable segments.")

        started = time.monotonic()
        exported: list[ExportedFile] = []
        failed: list[tuple[Path, str]] = []
        total = len(segments)

        for idx, (dest, start, duration, label) in enumerate(segments, start=1):
            if cancel_event is not None and cancel_event.is_set():
                raise ExportCancelledError(
                    f"Cancelled after {len(exported)}/{total} segments."
                )
            try:
                partial = dest.with_suffix(".wav.partial")
                self._run_ffmpeg_segment(
                    source=source,
                    dest=partial,
                    start_seconds=start,
                    duration_seconds=duration,
                    duration_hint=duration,
                    on_percent=lambda pct, i=idx, lbl=label: self._emit(
                        progress_callback,
                        ExportProgress(
                            stage=ExportStage.EXPORTING,
                            overall_percent=self._overall_pct(i, total, pct),
                            current_file_percent=pct,
                            current_index=i,
                            total_files=total,
                            current_filename=lbl,
                            elapsed_seconds=time.monotonic() - started,
                            message=f"Exporting {lbl}",
                        ),
                    ),
                    cancel_event=cancel_event,
                )
                self._finalize_partial(partial, dest)
                exported.append(ExportedFile(
                    source_path=source,
                    destination_path=dest,
                    wav_size_bytes=dest.stat().st_size,
                    elapsed_seconds=0.0,
                ))
            except ExportCancelledError:
                raise
            except Exception as e:
                self._log.exception("Segment export failed: %s", dest)
                failed.append((source, str(e)))

        # Pad map for MPC program assignment.
        pad_map = {
            "kit_name": name,
            "source": str(source),
            "bpm": plan.bpm,
            "beats_per_bar": plan.beats_per_bar,
            "pads": pad_entries,
        }
        (kit_dir / "pad_map.json").write_text(
            json.dumps(pad_map, indent=2), encoding="utf-8",
        )

        total_elapsed = time.monotonic() - started
        return ExportResult(
            exported=tuple(exported),
            failed=tuple(failed),
            destination_root=kit_dir,
            total_elapsed_seconds=total_elapsed,
        )

    # ── Destination validation ──

    def _validate_destination(self, dest: Path) -> None:
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ExportDestinationError(
                f"Cannot create destination {dest}: {e}"
            ) from e
        if not os.access(dest, os.W_OK):
            raise ExportDestinationError(
                f"Destination {dest} is not writable."
            )

    # ── Single-file export ──

    def _export_one(
        self,
        source: Path,
        dest_root: Path,
        idx: int,
        total: int,
        flatten: bool,
        batch_started: float,
        progress_cb: Optional[Callable[[ExportProgress], None]],
        cancel_event: Optional[threading.Event],
    ) -> ExportedFile:
        if not source.exists():
            raise ExportError(f"Source file missing: {source}")
        if not source.is_file():
            raise ExportError(f"Source is not a regular file: {source}")

        duration_seconds = self._probe_duration(source)

        final_path, partial_path = self._resolve_destination_paths(
            source, dest_root, flatten,
        )

        self._emit(progress_cb, ExportProgress(
            stage=ExportStage.EXPORTING,
            overall_percent=self._overall_pct(idx, total, 0.0),
            current_file_percent=0.0,
            current_index=idx,
            total_files=total,
            current_filename=final_path.name,
            elapsed_seconds=time.monotonic() - batch_started,
            message=f"Converting {source.name}",
        ))

        file_started = time.monotonic()
        self._run_ffmpeg(
            source=source,
            dest=partial_path,
            duration_seconds=duration_seconds,
            on_percent=lambda pct: self._emit(progress_cb, ExportProgress(
                stage=ExportStage.EXPORTING,
                overall_percent=self._overall_pct(idx, total, pct),
                current_file_percent=pct,
                current_index=idx,
                total_files=total,
                current_filename=final_path.name,
                elapsed_seconds=time.monotonic() - batch_started,
                message=f"Converting {source.name}",
            )),
            cancel_event=cancel_event,
        )

        # Finalize atomically: fsync + rename over target.
        self._emit(progress_cb, ExportProgress(
            stage=ExportStage.FINALIZING,
            overall_percent=self._overall_pct(idx, total, 100.0),
            current_file_percent=100.0,
            current_index=idx,
            total_files=total,
            current_filename=final_path.name,
            elapsed_seconds=time.monotonic() - batch_started,
            message="Finalizing file on destination",
        ))

        self._finalize_partial(partial_path, final_path)
        size = final_path.stat().st_size
        elapsed = time.monotonic() - file_started

        self._log.debug(
            "Exported %s → %s  (%d bytes in %.1fs)",
            source.name, final_path, size, elapsed,
        )
        return ExportedFile(
            source_path=source,
            destination_path=final_path,
            wav_size_bytes=size,
            elapsed_seconds=elapsed,
        )

    # ── ffmpeg invocation ──

    def _run_ffmpeg(
        self,
        source: Path,
        dest: Path,
        duration_seconds: Optional[float],
        on_percent: Callable[[float], None],
        cancel_event: Optional[threading.Event],
    ) -> None:
        """Spawn ffmpeg, parse -progress output, honor cancel."""
        pcm_codec = "pcm_s16le" if self._bits == 16 else "pcm_s24le"

        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",                           # overwrite .partial from prior failed run
            "-i", str(source),
            "-vn",                          # drop embedded artwork stream
            "-ar", str(self._sr),
            "-ac", str(self._channels),
            "-acodec", pcm_codec,
            "-f", "wav",
            "-progress", "pipe:1",          # machine-readable progress on stdout
            "-nostats",
            str(dest),
        ]
        self._log.debug("ffmpeg: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                **self._subprocess_platform_kwargs(with_process_group=True),
            )
        except FileNotFoundError as e:
            raise FFmpegNotAvailableError(
                f"Could not launch ffmpeg at {self._ffmpeg!r}: {e}"
            ) from e
        except OSError as e:
            raise FFmpegNotAvailableError(
                f"ffmpeg launch failed: {e}"
            ) from e

        # Background threads drain stdout (progress) and stderr (errors).
        stdout_q: "queue.Queue[Optional[str]]" = queue.Queue()
        stderr_lines: list[str] = []

        def drain_stdout() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    stdout_q.put(line)
            finally:
                stdout_q.put(None)

        def drain_stderr() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
            except Exception:
                pass

        t_out = threading.Thread(target=drain_stdout, daemon=True,
                                 name="ffmpeg-stdout")
        t_err = threading.Thread(target=drain_stderr, daemon=True,
                                 name="ffmpeg-stderr")
        t_out.start()
        t_err.start()

        duration_us = int((duration_seconds or 0.0) * 1_000_000)
        last_reported = 0.0

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._terminate_process(proc)
                    raise ExportCancelledError("Cancelled by user.")

                try:
                    line = stdout_q.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue

                if line is None:
                    break

                line = line.strip()
                if not line:
                    continue

                # Update progress when we see a fresh `out_time_us` value.
                m = _PROG_OUT_TIME_US.match(line)
                if m and duration_us > 0:
                    out_us = int(m.group(1))
                    pct = min(99.0, (out_us / duration_us) * 100.0)
                    if pct - last_reported >= 1.0:
                        last_reported = pct
                        on_percent(pct)
                    continue

                # Terminal status line from ffmpeg
                m = _PROG_STATUS.match(line)
                if m and m.group(1) == "end":
                    on_percent(100.0)

            # Drain any final stdout chunks so the thread can exit cleanly.
            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
            rc = proc.wait()

            if rc != 0:
                stderr_tail = "\n".join(stderr_lines[-10:]) or "(no stderr)"
                raise ExportError(
                    f"ffmpeg exited with code {rc}. Last errors:\n{stderr_tail}"
                )
        except ExportCancelledError:
            raise
        except ExportError:
            raise
        except Exception as e:
            self._terminate_process(proc)
            raise ExportError(f"Unexpected ffmpeg failure: {e}") from e

    def _run_ffmpeg_segment(
        self,
        source: Path,
        dest: Path,
        start_seconds: float,
        duration_seconds: float,
        duration_hint: float,
        on_percent: Callable[[float], None],
        cancel_event: Optional[threading.Event],
    ) -> None:
        """Extract a time slice from `source` into `dest` as PCM WAV."""
        pcm_codec = "pcm_s16le" if self._bits == 16 else "pcm_s24le"
        cmd = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-ss", f"{start_seconds:.6f}",
            "-i", str(source),
            "-t", f"{duration_seconds:.6f}",
            "-vn",
            "-ar", str(self._sr),
            "-ac", str(self._channels),
            "-acodec", pcm_codec,
            "-f", "wav",
            "-progress", "pipe:1",
            "-nostats",
            str(dest),
        ]
        self._log.debug("ffmpeg segment: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                **self._subprocess_platform_kwargs(with_process_group=True),
            )
        except FileNotFoundError as e:
            raise FFmpegNotAvailableError(
                f"Could not launch ffmpeg at {self._ffmpeg!r}: {e}"
            ) from e

        stdout_q: "queue.Queue[Optional[str]]" = queue.Queue()
        stderr_lines: list[str] = []

        def drain_stdout() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    stdout_q.put(line)
            finally:
                stdout_q.put(None)

        def drain_stderr() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
            except Exception:
                pass

        t_out = threading.Thread(target=drain_stdout, daemon=True)
        t_err = threading.Thread(target=drain_stderr, daemon=True)
        t_out.start()
        t_err.start()

        duration_us = int(max(duration_hint, 0.001) * 1_000_000)
        last_reported = 0.0

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._terminate_process(proc)
                    raise ExportCancelledError("Cancelled by user.")

                try:
                    line = stdout_q.get(timeout=0.25)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue

                if line is None:
                    break

                line = line.strip()
                m = _PROG_OUT_TIME_US.match(line)
                if m and duration_us > 0:
                    out_us = int(m.group(1))
                    pct = min(99.0, (out_us / duration_us) * 100.0)
                    if pct - last_reported >= 1.0:
                        last_reported = pct
                        on_percent(pct)
                    continue

                m = _PROG_STATUS.match(line)
                if m and m.group(1) == "end":
                    on_percent(100.0)

            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)
            rc = proc.wait()
            if rc != 0:
                stderr_tail = "\n".join(stderr_lines[-10:]) or "(no stderr)"
                raise ExportError(
                    f"ffmpeg exited with code {rc}. Last errors:\n{stderr_tail}"
                )
        except ExportCancelledError:
            raise
        except ExportError:
            raise
        except Exception as e:
            self._terminate_process(proc)
            raise ExportError(f"Unexpected ffmpeg failure: {e}") from e

    # ── Duration probing ──

    def _probe_duration(self, source: Path) -> Optional[float]:
        """
        Use `ffprobe` to read container duration in seconds. We derive
        ffprobe from the ffmpeg path so it honors the same binary
        location (imageio-ffmpeg ships ffprobe alongside ffmpeg).
        """
        ffprobe = self._derive_ffprobe_path()
        if ffprobe is None:
            return None

        cmd = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(source),
        ]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
                **self._subprocess_platform_kwargs(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            self._log.debug("ffprobe unavailable for %s: %s", source.name, e)
            return None

        out = (res.stdout or "").strip()
        if res.returncode != 0 or not out:
            return None
        try:
            return float(out)
        except ValueError:
            return None

    def _derive_ffprobe_path(self) -> Optional[str]:
        """Find ffprobe next to ffmpeg, or on PATH."""
        ffmpeg_p = Path(self._ffmpeg)
        candidate = ffmpeg_p.with_name(
            "ffprobe.exe" if sys.platform == "win32" else "ffprobe"
        )
        if candidate.exists():
            return str(candidate)
        return shutil.which("ffprobe")

    # ── Destination pathing ──

    @staticmethod
    def _resolve_destination_paths(
        source: Path, dest_root: Path, flatten: bool,
    ) -> tuple[Path, Path]:
        """
        Compute (final_path, partial_path). `partial_path` is what
        ffmpeg writes to; on success, we rename to `final_path`.

        When flatten=True we strip the parent directory and land every
        WAV directly in dest_root. Filename collisions get a numeric
        suffix so two tracks with the same name don't overwrite.
        """
        if flatten:
            target_dir = dest_root
        else:
            # Preserve [Genre]/[BPM_Key_Artist_Title]/ structure.
            # Assumes `source` came from the Vault tree.
            target_dir = dest_root / source.parent.name
            target_dir.mkdir(parents=True, exist_ok=True)

        base = sanitize_filename_component(source.stem, max_length=120)
        final = target_dir / f"{base}.wav"

        # Collision avoidance
        counter = 2
        while final.exists():
            final = target_dir / f"{base} ({counter}).wav"
            counter += 1
            if counter > 999:
                raise ExportError(
                    f"Too many collisions for {base}.wav in {target_dir}"
                )

        partial = final.with_suffix(".wav.partial")
        # Clean up any stale .partial from a prior aborted run.
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass

        return final, partial

    @staticmethod
    def _finalize_partial(partial: Path, final: Path) -> None:
        """fsync the .partial, then rename to .wav — atomic on POSIX."""
        try:
            fd = os.open(str(partial), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # fsync failure isn't fatal — the rename still happens and
            # the file is usually fine. Worth logging but not raising.
            pass

        try:
            # On Windows, os.replace overwrites atomically where
            # os.rename would fail on existing target.
            os.replace(str(partial), str(final))
        except OSError as e:
            raise ExportError(f"Could not finalize {final}: {e}") from e

    # ── Cancel plumbing ──

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        """Graceful SIGTERM → 3s grace → SIGKILL. Cross-platform."""
        if proc.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.terminate()
        except (ProcessLookupError, OSError):
            return

        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def _subprocess_platform_kwargs(
        with_process_group: bool = False,
    ) -> dict[str, int]:
        """CREATE_NO_WINDOW + optional process group on Windows."""
        if sys.platform != "win32":
            return {}
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = CREATE_NO_WINDOW
        if with_process_group:
            flags |= CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}

    # ── Progress helpers ──

    @staticmethod
    def _overall_pct(current_index: int, total: int, file_pct: float) -> float:
        if total <= 0:
            return 0.0
        done = (current_index - 1) / total
        in_progress = (file_pct / 100.0) / total
        return min(100.0, (done + in_progress) * 100.0)

    @staticmethod
    def _emit(
        cb: Optional[Callable[[ExportProgress], None]],
        p: ExportProgress,
    ) -> None:
        if cb is not None:
            cb(p)