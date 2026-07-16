"""
core/stems.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Stem Separation via Demucs

Subprocess-based wrapper around the demucs CLI. Produces 4 stems
(vocals, drums, bass, other) per track by default, or 6 stems
(+piano, +guitar) when `htdemucs_6s` is selected.

Why subprocess instead of the demucs Python API:
  • Process isolation. Torch retains substantial resident memory after
    a separation; keeping it in a child process means that memory is
    fully reclaimed as soon as the job finishes — important for a
    long-running desktop app.
  • Clean mid-run cancellation. SIGTERM on the child works reliably;
    the Python API exposes no cancel hook and would leave torch ops
    running to completion even after a user-initiated cancel.
  • CLI stability. demucs.separate CLI flags have been stable across
    4.x releases; the Python API has shifted multiple times.

Default model: htdemucs (balanced quality and speed).
The separator accepts a per-call model override so the pipeline can
honor the user's Settings-tab choice without instance rebuild.
"""

from __future__ import annotations

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
from typing import Callable, Optional, Union

# ─── Models ──────────────────────────────────────────────────────────


class StemModel(str, Enum):
    """
    Demucs models. Listed in quality-descending / speed-ascending order
    so the Settings dropdown can render them in a sensible default order.
    """

    HTDEMUCS_FT = "htdemucs_ft"  # Fine-tuned 4-model ensemble; slowest
    HTDEMUCS = "htdemucs"  # Demucs v4 balanced default
    HTDEMUCS_6S = "htdemucs_6s"  # 6-stem variant (+piano, +guitar)
    MDX_EXTRA = "mdx_extra"  # MDX architecture; ~2x faster on CPU
    MDX_EXTRA_Q = "mdx_extra_q"  # Quantized MDX; smallest memory, fastest

    @property
    def produces_6_stems(self) -> bool:
        return self is StemModel.HTDEMUCS_6S


# Core 4 stems every model produces. htdemucs_6s produces these plus
# 'piano' and 'guitar', which we pick up generically via glob.
_CORE_STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")


# ─── Public types ────────────────────────────────────────────────────


class SeparationStage(str, Enum):
    PREPARING = "preparing"
    LOADING_MODEL = "loading_model"
    SEPARATING = "separating"
    WRITING = "writing"
    COMPLETE = "complete"


@dataclass(slots=True)
class SeparationProgress:
    stage: SeparationStage
    percent: float = 0.0
    elapsed_seconds: float = 0.0
    message: str = ""


@dataclass(slots=True, frozen=True)
class StemsResult:
    model_used: str
    device_used: str
    stems: dict[str, Path]  # {'vocals': Path, 'drums': Path, ...}
    output_dir: Path
    elapsed_seconds: float


# ─── Public exceptions ───────────────────────────────────────────────


class StemSeparationError(Exception):
    """Base class for stem-separation failures."""


class DemucsNotAvailableError(StemSeparationError):
    """demucs or torch cannot be imported in the worker interpreter."""


class StemSeparationFailedError(StemSeparationError):
    """demucs ran but did not produce the expected outputs."""


class StemSeparationCancelledError(StemSeparationError):
    """Caller cancelled via cancel_event; child process was terminated."""


# tqdm progress line, e.g. " 43%|████▎     | 86/200 [00:12<00:16,  6.92it/s]"
_PROGRESS_RE = re.compile(r"\b(\d{1,3})%\|")
_BAG_MODELS_RE = re.compile(r"\bbag of (\d+) models?\b", re.IGNORECASE)

# Injected as `python -c _DEMUCS_RUNNER` so we can patch torchaudio.save
# before demucs imports it.  torchaudio 2.8+ delegates save() to torchcodec
# whose native DLL may fail to load on CPU-only or certain Windows builds.
# soundfile writes 32-bit float WAV with zero native-DLL dependencies.
# sys.argv layout when called via `python -c CODE arg1 arg2 …`:
#   sys.argv == ['-c', arg1, arg2, …]
# demucs.separate.main() reads sys.argv[1:], so the args land correctly.
_DEMUCS_RUNNER = (
    "import sys, torchaudio\n"
    "def _sf_save(uri, src, sample_rate, channels_first=True,\n"
    "             format=None, encoding=None, bits_per_sample=None,\n"
    "             compression=None, backend=None):\n"
    "    import soundfile as sf\n"
    "    wav = src.numpy()\n"
    "    if channels_first:\n"
    "        wav = wav.T\n"
    "    sf.write(str(uri), wav, sample_rate, subtype='FLOAT')\n"
    "torchaudio.save = _sf_save\n"
    "from demucs.__main__ import main; main()\n"
)


# ─── The Separator ──────────────────────────────────────────────────


class StemSeparator:
    """
    Invokes demucs as a child process. One instance can service many
    jobs; each `separate()` call spawns a fresh subprocess.
    """

    def __init__(
        self,
        model: Union[StemModel, str] = StemModel.HTDEMUCS,
        device: str = "auto",
        ffmpeg_path: Optional[str] = None,
        python_executable: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        """
        Args:
            model: Default model; can be overridden per `separate()` call.
            device: 'auto' | 'cpu' | 'cuda' | 'mps'. 'auto' prefers CUDA,
                then MPS (Apple Silicon), then CPU.
            ffmpeg_path: Optional ffmpeg binary path. When set, the stem
                subprocess gets this path prepended to PATH so demucs can
                reliably decode audio on systems without global ffmpeg.
            python_executable: Python interpreter used to run demucs. In
                a PyInstaller freeze this will need to point at the
                bundled torch-enabled interpreter; defaults to sys.executable.
        """
        self._model = StemModel(model) if isinstance(model, str) else model
        self._device_pref = device
        self._ffmpeg_path = ffmpeg_path
        self._python = python_executable or sys.executable
        self._log = logger or logging.getLogger("cratedigger.stems")
        # Demucs saturates the CPU and consumes substantial RAM. Running two
        # instances together makes both dramatically slower and can make the
        # desktop UI feel hung. The app shares one separator, so this lock
        # keeps the expensive portion single-file while downloads/analysis
        # remain concurrent.
        self._run_lock = threading.Lock()

    # ── Availability probe ──

    def probe_availability(self, timeout: float = 15.0) -> tuple[bool, str]:
        """Verify stems runtime health. Delegates to probe_runtime."""
        return self.probe_runtime(timeout=timeout)

    def probe_runtime(self, timeout: float = 20.0) -> tuple[bool, str]:
        """
        Probe whether torch, torchaudio, and demucs are importable.

        Returns:
            (ok, details)
            ok=True  -> details has version info.
            ok=False -> details has first actionable failure line.

        Intentionally does NOT import torchcodec — its DLL may fail to load
        on machines without the right CUDA runtime even when demucs works
        fine (demucs decodes audio via ffmpeg, not torchcodec).
        """
        script = (
            "import json, sys\n"
            "import torch, torchaudio, demucs\n"
            "print(json.dumps({"
            "'torch': torch.__version__, "
            "'torchaudio': torchaudio.__version__, "
            "'demucs': demucs.__version__, "
            "'python': sys.version.split()[0]"
            "}))\n"
        )

        try:
            result = subprocess.run(
                [self._python, "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._subprocess_env(),
                **self._subprocess_platform_kwargs(),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return False, f"Interpreter probe failed: {e}"

        if result.returncode != 0:
            raw = (result.stderr or result.stdout or "unknown error").strip()
            return False, self._summarize_subprocess_error(raw)
        return True, result.stdout.strip()

    # ── Core API ──

    def separate(
        self,
        audio_path: Path,
        output_dir: Path,
        progress_callback: Optional[Callable[[SeparationProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        model: Optional[Union[StemModel, str]] = None,
    ) -> StemsResult:
        """
        Separate `audio_path` into stems inside `output_dir`.

        Args:
            audio_path: Path to input .m4a (or anything ffmpeg can decode).
            output_dir: Directory where final stem .wav files will land.
                Created if missing. A `_demucs_staging/` subdirectory is
                created during the run and removed on completion/failure.
            model: Per-call override of the instance default. Accepts
                StemModel or the raw string (for easy Settings-tab wiring).

        Raises:
            StemSeparationError subclasses for typed failures.
        """
        audio_path = Path(audio_path).resolve()
        if not audio_path.exists():
            raise StemSeparationError(f"File does not exist: {audio_path}")

        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve per-call overrides
        if model is None:
            active_model = self._model
        elif isinstance(model, str):
            active_model = StemModel(model)
        else:
            active_model = model

        active_model = self._prepare_runtime(active_model, progress_callback)

        device = self._resolve_device()

        # Staging root isolates demucs's nested output structure from
        # the caller's flat `output_dir`. Cleaned up after files move.
        staging_root = output_dir / "_demucs_staging"
        acquired = False
        try:
            self._acquire_run_slot(progress_callback, cancel_event)
            acquired = True

            started = time.monotonic()
            self._emit(
                progress_callback,
                SeparationStage.PREPARING,
                0.0,
                0.0,
                f"Preparing (model={active_model.value}, device={device})",
            )

            if staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            staging_root.mkdir(parents=True, exist_ok=True)

            cmd = [
                self._python,
                "-c", _DEMUCS_RUNNER,
                "-n", active_model.value,
                "-d", device,
                "-o", str(staging_root),
                str(audio_path),
            ]
            self._log.debug("demucs args: -n %s -d %s -o %s %s",
                            active_model.value, device, staging_root, audio_path)

            self._run_demucs(cmd, progress_callback, cancel_event, started)

            # demucs output layout: {staging_root}/{model_name}/{track_stem}/*.wav
            stems = self._collect_stems(
                staging_root=staging_root,
                model_name=active_model.value,
                source_stem=audio_path.stem,
                output_dir=output_dir,
                expect_6=active_model.produces_6_stems,
                progress_callback=progress_callback,
                started=started,
            )

            elapsed = time.monotonic() - started
            self._emit(
                progress_callback,
                SeparationStage.COMPLETE,
                100.0,
                elapsed,
                f"Separation complete ({elapsed:.1f}s)",
            )

            self._log.debug(
                "Stems complete: model=%s device=%s stems=%s elapsed=%.1fs",
                active_model.value,
                device,
                list(stems.keys()),
                elapsed,
            )

            return StemsResult(
                model_used=active_model.value,
                device_used=device,
                stems=stems,
                output_dir=output_dir,
                elapsed_seconds=elapsed,
            )
        except StemSeparationCancelledError:
            raise
        except StemSeparationError:
            raise
        except Exception as e:
            raise StemSeparationFailedError(f"Unexpected error: {e}") from e
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            if acquired:
                self._run_lock.release()

    # ── Subprocess management ──

    def _acquire_run_slot(
        self,
        progress_callback: Optional[Callable[[SeparationProgress], None]],
        cancel_event: Optional[threading.Event],
    ) -> None:
        """Wait cancellably for the single heavy Demucs execution slot."""
        waiting_since = time.monotonic()
        last_emit = waiting_since - 2.0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise StemSeparationCancelledError("Cancelled while waiting for stems.")
            if self._run_lock.acquire(timeout=0.25):
                return
            now = time.monotonic()
            if now - last_emit >= 2.0:
                last_emit = now
                self._emit(
                    progress_callback,
                    SeparationStage.PREPARING,
                    0.0,
                    now - waiting_since,
                    "Waiting for the active stem separation to finish",
                )

    def _prepare_runtime(
        self,
        model: StemModel,
        progress_callback: Optional[Callable[[SeparationProgress], None]],
    ) -> StemModel:
        """
        Validate required runtime deps before launching demucs.

        - `mdx_extra_q` requires `diffq`; automatically fall back to
          `mdx_extra` when unavailable.
        - torchaudio 2.9+ relies on `torchcodec`; fail early with an
          actionable error when the import/runtime is broken.
        """
        active_model = model

        if active_model is StemModel.MDX_EXTRA_Q:
            ok, _ = self._python_module_check("diffq")
            if not ok:
                self._log.warning(
                    "Model %s requested but 'diffq' is unavailable; "
                    "falling back to %s.",
                    StemModel.MDX_EXTRA_Q.value,
                    StemModel.MDX_EXTRA.value,
                )
                active_model = StemModel.MDX_EXTRA
                self._emit(
                    progress_callback,
                    SeparationStage.PREPARING,
                    0.0,
                    0.0,
                    "Model mdx_extra_q requires diffq; using mdx_extra instead",
                )

        # torchcodec is an optional torchaudio 2.9+ backend; demucs itself
        # decodes audio via ffmpeg, so missing torchcodec is non-fatal.
        ok, details = self._python_module_check("torchcodec")
        if not ok:
            self._log.debug(
                "torchcodec not available (%s); demucs will use ffmpeg backend.",
                details,
            )

        return active_model

    def _python_module_check(self, module_name: str) -> tuple[bool, str]:
        """Check if a module can be imported by the demucs interpreter."""
        try:
            result = subprocess.run(
                [self._python, "-c", f"import {module_name}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env=self._subprocess_env(),
                **self._subprocess_platform_kwargs(),
            )
        except Exception as e:
            return False, str(e)

        if result.returncode == 0:
            return True, ""

        details = (result.stderr or result.stdout or "import failed").strip()
        return False, self._summarize_subprocess_error(details)[:400]

    @staticmethod
    def _summarize_subprocess_error(raw: str) -> str:
        """Return the most actionable line from a Python traceback-like output."""
        if not raw:
            return "unknown error"

        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            return "unknown error"

        for line in reversed(lines):
            if line.startswith("Traceback"):
                continue
            if ":" in line:
                return line
        return lines[-1]

    def _run_demucs(
        self,
        cmd: list[str],
        progress_callback: Optional[Callable[[SeparationProgress], None]],
        cancel_event: Optional[threading.Event],
        started: float,
    ) -> None:
        """Spawn demucs, parse progress from its stderr, honor cancellation."""
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                # Must match PYTHONUTF8=1 set in _subprocess_env() so the
                # parent reads the same UTF-8 bytes demucs writes. Without
                # this, Python defaults to CP1252 on Windows and crashes on
                # any non-ASCII character in paths or tqdm progress output.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=self._subprocess_env(),
                **self._subprocess_platform_kwargs(with_process_group=True),
            )
        except FileNotFoundError as e:
            raise DemucsNotAvailableError(
                f"Could not launch Python interpreter: {e}"
            ) from e

        # Drain child output in a background thread so the main thread
        # can watch cancel_event without blocking on readline().
        output_q: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=512)

        def _drain() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    output_q.put(line)
            finally:
                output_q.put(None)

        drain_thread = threading.Thread(
            target=_drain,
            name="demucs-drain",
            daemon=True,
        )
        drain_thread.start()

        last_reported_pct = 0.0
        saw_model_load = False
        saw_separating_track = False
        model_count = 1
        current_model = 0
        previous_demucs_pct: Optional[int] = None
        output_lines: list[str] = []  # full buffer for error reporting
        last_emit_time = time.monotonic()
        # Between demucs's tqdm bar hitting 100% and the process actually
        # exiting, it's still flushing stem WAVs to disk with zero output —
        # can take real time on a full song. Without a heartbeat here the
        # UI just sits frozen on the last percent, indistinguishable from
        # having actually hung.
        _HEARTBEAT_SECONDS = 4.0

        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_process(proc)
                drain_thread.join(timeout=2.0)
                raise StemSeparationCancelledError("Cancelled by user.")

            try:
                line = output_q.get(timeout=0.25)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                if time.monotonic() - last_emit_time > _HEARTBEAT_SECONDS:
                    last_emit_time = time.monotonic()
                    elapsed = time.monotonic() - started
                    self._emit(
                        progress_callback,
                        SeparationStage.SEPARATING,
                        last_reported_pct,
                        elapsed,
                        (
                            f"Separating model {current_model + 1}/{model_count}… "
                            f"({elapsed:.0f}s elapsed)"
                            if model_count > 1
                            else f"Separating… ({elapsed:.0f}s elapsed)"
                        ),
                    )
                continue

            if line is None:
                break

            line = line.rstrip()
            if not line:
                continue

            output_lines.append(line)
            self._log.debug("demucs: %s", line)

            bag_match = _BAG_MODELS_RE.search(line)
            if bag_match:
                model_count = max(1, int(bag_match.group(1)))

            if "separating track" in line.lower():
                saw_separating_track = True

            # Stage heuristics. First "loading"/"downloading" line fires
            # the LOADING_MODEL event — useful because model download on
            # first run can be slow (50–200MB) and users need to know
            # the app hasn't frozen.
            if not saw_model_load:
                lowered = line.lower()
                if "download" in lowered or "loading" in lowered:
                    saw_model_load = True
                    last_emit_time = time.monotonic()
                    self._emit(
                        progress_callback,
                        SeparationStage.LOADING_MODEL,
                        5.0,
                        time.monotonic() - started,
                        "Loading model weights",
                    )

            # Parse tqdm progress. Ensemble models (notably htdemucs_ft)
            # print one independent 0-100 bar per model. Treating the first
            # bar as the whole run is what made the UI sit at 97% for most of
            # the actual work. Aggregate all model bars before mapping the
            # result onto our 10-95 range.
            m = _PROGRESS_RE.search(line)
            if m and saw_separating_track:
                demucs_pct = int(m.group(1))
                if (
                    previous_demucs_pct is not None
                    and demucs_pct < previous_demucs_pct
                    and previous_demucs_pct >= 90
                ):
                    current_model = min(current_model + 1, model_count - 1)
                previous_demucs_pct = demucs_pct

                aggregate_pct = (
                    (current_model + demucs_pct / 100.0) / model_count * 100.0
                )
                mapped = 10.0 + (aggregate_pct / 100.0) * 85.0
                if mapped > last_reported_pct + 0.5:  # throttle UI spam
                    last_reported_pct = mapped
                    last_emit_time = time.monotonic()
                    if model_count > 1:
                        message = (
                            f"Separating model {current_model + 1}/{model_count} "
                            f"({demucs_pct}%)"
                        )
                    else:
                        message = f"Separating ({demucs_pct}%)"
                    self._emit(
                        progress_callback,
                        SeparationStage.SEPARATING,
                        mapped,
                        time.monotonic() - started,
                        message,
                    )

        drain_thread.join(timeout=2.0)
        rc = proc.wait()
        if rc != 0:
            tail = "\n".join(output_lines[-30:]) if output_lines else "(no output)"
            raise StemSeparationFailedError(f"demucs exited with code {rc}.\n{tail}")

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        """Graceful terminate → 3s grace → kill. Cross-platform."""
        try:
            if sys.platform == "win32":
                # With CREATE_NEW_PROCESS_GROUP set at spawn time,
                # CTRL_BREAK_EVENT is the cooperative shutdown signal.
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
        """Windows-specific Popen flags: hide console, optionally new group."""
        if sys.platform != "win32":
            return {}
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        flags = CREATE_NO_WINDOW
        if with_process_group:
            flags |= CREATE_NEW_PROCESS_GROUP
        return {"creationflags": flags}

    def _subprocess_env(self) -> dict[str, str]:
        """Environment for demucs subprocesses with optional ffmpeg wiring."""
        env = dict(os.environ)

        # Force UTF-8 I/O so demucs's print() calls don't crash on
        # non-ASCII characters in file paths (e.g. Greek, CJK track names)
        # when the Windows console codec is CP1252.
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        if not self._ffmpeg_path:
            return env

        ffmpeg = Path(self._ffmpeg_path)
        ffmpeg_dir = str(ffmpeg.parent)
        existing = env.get("PATH", "")
        if ffmpeg_dir and ffmpeg_dir not in existing.split(os.pathsep):
            env["PATH"] = ffmpeg_dir + os.pathsep + existing

        # Common env hints consumed by audio stacks and wrappers.
        env.setdefault("FFMPEG_BINARY", str(ffmpeg))
        env.setdefault("IMAGEIO_FFMPEG_EXE", str(ffmpeg))
        return env

    # ── Output collection ──

    def _collect_stems(
        self,
        staging_root: Path,
        model_name: str,
        source_stem: str,
        output_dir: Path,
        expect_6: bool,
        progress_callback: Optional[Callable[[SeparationProgress], None]],
        started: float,
    ) -> dict[str, Path]:
        """Locate demucs output, verify completeness, move into final layout."""
        self._emit(
            progress_callback,
            SeparationStage.WRITING,
            97.0,
            time.monotonic() - started,
            "Finalizing stems",
        )

        expected_dir = staging_root / model_name / source_stem
        if not expected_dir.exists():
            # Fallback: demucs occasionally renames based on title metadata;
            # scan the staging tree for any produced stem files.
            found = list(staging_root.rglob("vocals.wav"))
            if not found:
                raise StemSeparationFailedError(
                    f"No stems found under {staging_root} after demucs run"
                )
            expected_dir = found[0].parent

        # Collect every .wav produced — works for both 4-stem and 6-stem models.
        stems: dict[str, Path] = {}
        for wav in sorted(expected_dir.glob("*.wav")):
            stem_name = wav.stem.lower()
            dst = output_dir / f"{stem_name}.wav"
            if dst.exists():
                dst.unlink()
            shutil.move(str(wav), str(dst))
            stems[stem_name] = dst

        # Verify at minimum the four core stems every model should produce.
        missing = set(_CORE_STEMS) - set(stems.keys())
        if missing:
            raise StemSeparationFailedError(
                f"Missing expected stems: {sorted(missing)}"
            )
        if expect_6 and len(stems) < 6:
            self._log.warning(
                "htdemucs_6s produced only %d stems (expected 6): %s",
                len(stems),
                list(stems.keys()),
            )
        return stems

    # ── Device resolution ──

    def _resolve_device(self) -> str:
        """
        Map 'auto' → best available torch device. Order: CUDA > MPS > CPU.
        MPS (Apple Silicon) is typically 3–6× faster than CPU on M1/M2/M3.
        """
        if self._device_pref != "auto":
            return self._device_pref
        try:
            import torch

            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    # ── Progress plumbing ──

    @staticmethod
    def _emit(
        cb: Optional[Callable[[SeparationProgress], None]],
        stage: SeparationStage,
        percent: float,
        elapsed: float,
        message: str,
    ) -> None:
        if cb is not None:
            cb(
                SeparationProgress(
                    stage=stage,
                    percent=percent,
                    elapsed_seconds=elapsed,
                    message=message,
                )
            )
