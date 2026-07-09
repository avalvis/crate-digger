"""
core/mpc_export_manager.py
──────────────────────────────────────────────────────────────────────
Bounded FIFO queue for Digital Crate MPC exports.

One global manager replaces per-card dialogs: jobs survive reel refresh,
concurrency is capped (demucs is heavy), and events drive the UI manager.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from core.discovery import DiscoverySuggestion
from core.exporter import MPCExporter
from core.mpc_export import (
    MpcExportMode,
    MpcSampleExportCancelledError,
    MpcSampleExportError,
    MpcSampleResult,
    export_sample_to_mpc,
)
from core.preview import PreviewService
from core.stems import StemSeparator


class MpcExportEventType(str, Enum):
    ENQUEUED = "enqueued"
    STARTED = "started"
    PROGRESS = "progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DRAINED = "drained"


@dataclass(slots=True, frozen=True)
class MpcExportEvent:
    type: MpcExportEventType
    job_id: str
    video_id: str
    display_name: str = ""
    mode: MpcExportMode = MpcExportMode.STEMS
    message: str = ""
    percent: float = 0.0
    error_message: Optional[str] = None
    track_dir: Optional[str] = None


@dataclass(slots=True)
class _MpcJob:
    job_id: str
    suggestion: DiscoverySuggestion
    mode: MpcExportMode
    cancel_event: threading.Event = field(default_factory=threading.Event)
    state: str = "queued"  # queued | running | completed | failed | cancelled
    message: str = ""
    percent: float = 0.0
    error_message: Optional[str] = None
    track_dir: Optional[str] = None


class MpcExportManager:
    """FIFO MPC export pool with bounded worker threads."""

    def __init__(
        self,
        *,
        preview: PreviewService,
        stem_separator: StemSeparator,
        exporter: MPCExporter,
        destination_root: Callable[[], Path],
        staging_root: Callable[[], Path],
        max_workers: int = 1,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._preview = preview
        self._stem_separator = stem_separator
        self._exporter = exporter
        self._destination_root = destination_root
        self._staging_root = staging_root
        self._max_workers = max(1, int(max_workers))
        self._log = logger or logging.getLogger("cratedigger.mpc_export_manager")

        self._lock = threading.RLock()
        self._jobs: dict[str, _MpcJob] = {}
        self._video_active: dict[str, str] = {}  # video_id -> job_id
        self._queue: queue.Queue[str] = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._running = False
        self._shutdown = threading.Event()

        self._subscribers: list[Callable[[MpcExportEvent], None]] = []
        self._weak_subs: list[weakref.WeakMethod] = []
        self._progress_throttle: dict[str, float] = {}
        self._PROGRESS_MIN_INTERVAL = 0.25

    # ── Lifecycle ──

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._shutdown.clear()
            for i in range(self._max_workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"mpc-export-worker-{i}",
                    daemon=True,
                )
                self._workers.append(t)
                t.start()

    def shutdown(self, *, cancel_pending: bool = True) -> None:
        with self._lock:
            self._running = False
            self._shutdown.set()
            if cancel_pending:
                for job in self._jobs.values():
                    if job.state in ("queued", "running"):
                        job.cancel_event.set()
                        job.state = "cancelled"
        for _ in self._workers:
            try:
                self._queue.put_nowait("")
            except queue.Full:
                pass
        for t in self._workers:
            t.join(timeout=2.0)
        self._workers.clear()

    def set_max_workers(self, n: int) -> None:
        """Adjust pool size (takes effect on next start if not running)."""
        with self._lock:
            self._max_workers = max(1, int(n))

    def subscribe(
        self, callback: Callable[[MpcExportEvent], None], *, weak: bool = True,
    ) -> Callable[[], None]:
        with self._lock:
            if weak and hasattr(callback, "__self__"):
                ref = weakref.WeakMethod(callback)  # type: ignore[arg-type]
                self._weak_subs.append(ref)

                def _unsub() -> None:
                    with self._lock:
                        try:
                            self._weak_subs.remove(ref)
                        except ValueError:
                            pass
                return _unsub
            self._subscribers.append(callback)

            def _unsub_strong() -> None:
                with self._lock:
                    try:
                        self._subscribers.remove(callback)
                    except ValueError:
                        pass
            return _unsub_strong

    # ── Public API ──

    def enqueue(self, suggestion: DiscoverySuggestion, mode: MpcExportMode) -> str:
        """Queue an export. Dedupes in-flight jobs for the same video_id."""
        vid = (suggestion.youtube_video_id or "").strip()
        if not vid:
            raise ValueError("Suggestion has no YouTube video id.")

        pending: list[MpcExportEvent] = []
        job_id: str
        with self._lock:
            existing_id = self._video_active.get(vid)
            if existing_id:
                job = self._jobs.get(existing_id)
                if job and job.state in ("queued", "running"):
                    pending.append(
                        MpcExportEvent(
                            MpcExportEventType.ENQUEUED,
                            existing_id,
                            vid,
                            display_name=suggestion.display_name,
                            mode=job.mode,
                        ),
                    )
                    job_id = existing_id
                else:
                    existing_id = None
            if not existing_id:
                job_id = uuid.uuid4().hex[:12]
                job = _MpcJob(
                    job_id=job_id,
                    suggestion=suggestion,
                    mode=mode,
                )
                self._jobs[job_id] = job
                self._video_active[vid] = job_id
                self._queue.put(job_id)
                pending.append(
                    MpcExportEvent(
                        MpcExportEventType.ENQUEUED,
                        job_id,
                        vid,
                        display_name=suggestion.display_name,
                        mode=mode,
                    ),
                )
        for event in pending:
            self._publish(event)
        return job_id

    def cancel_job(self, job_id: str) -> None:
        pending: Optional[MpcExportEvent] = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.cancel_event.set()
            if job.state == "queued":
                job.state = "cancelled"
                pending = MpcExportEvent(
                    MpcExportEventType.CANCELLED,
                    job_id,
                    job.suggestion.youtube_video_id,
                    display_name=job.suggestion.display_name,
                    mode=job.mode,
                )
        if pending is not None:
            self._publish(pending)

    def get_job_for_video(self, video_id: str) -> Optional[_MpcJob]:
        vid = (video_id or "").strip()
        with self._lock:
            job_id = self._video_active.get(vid)
            if job_id is None:
                return None
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[_MpcJob]:
        with self._lock:
            return list(self._jobs.values())

    def clear_finished(self) -> None:
        with self._lock:
            finished = [
                jid for jid, j in self._jobs.items()
                if j.state in ("completed", "failed", "cancelled")
            ]
            for jid in finished:
                job = self._jobs.pop(jid, None)
                if job is not None:
                    vid = job.suggestion.youtube_video_id
                    if self._video_active.get(vid) == jid:
                        del self._video_active[vid]

    def counts(self) -> tuple[int, int]:
        """Return (running, queued) counts."""
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.state == "running")
            queued = sum(1 for j in self._jobs.values() if j.state == "queued")
            return running, queued

    # ── Workers ──

    def _worker_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not job_id or self._shutdown.is_set():
                continue
            self._run_job(job_id)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.cancel_event.is_set():
                return
            job.state = "running"
            job.message = "Starting…"
            suggestion = job.suggestion
            mode = job.mode

        self._publish(
            MpcExportEvent(
                MpcExportEventType.STARTED,
                job_id,
                suggestion.youtube_video_id,
                display_name=suggestion.display_name,
                mode=mode,
                message=job.message,
            ),
        )

        def on_progress(label: str, pct: float) -> None:
            if job.cancel_event.is_set():
                return
            job.message = label
            job.percent = pct
            self._publish(
                MpcExportEvent(
                    MpcExportEventType.PROGRESS,
                    job_id,
                    suggestion.youtube_video_id,
                    display_name=suggestion.display_name,
                    mode=mode,
                    message=label,
                    percent=pct,
                ),
            )

        try:
            dest = self._destination_root()
            stage = self._staging_root()
            result: MpcSampleResult = export_sample_to_mpc(
                video_id=suggestion.youtube_video_id,
                artist=suggestion.artist,
                title=suggestion.title,
                destination_root=dest,
                staging_root=stage,
                preview=self._preview,
                stem_separator=self._stem_separator,
                exporter=self._exporter,
                mode=mode,
                progress_callback=on_progress,
                cancel_event=job.cancel_event,
                logger=self._log,
            )
            job.state = "completed"
            job.percent = 100.0
            job.message = "Complete"
            job.track_dir = str(result.track_dir)
            self._publish(
                MpcExportEvent(
                    MpcExportEventType.COMPLETED,
                    job_id,
                    suggestion.youtube_video_id,
                    display_name=suggestion.display_name,
                    mode=mode,
                    message="Complete",
                    percent=100.0,
                    track_dir=job.track_dir,
                ),
            )
        except MpcSampleExportCancelledError:
            job.state = "cancelled"
            self._publish(
                MpcExportEvent(
                    MpcExportEventType.CANCELLED,
                    job_id,
                    suggestion.youtube_video_id,
                    display_name=suggestion.display_name,
                    mode=mode,
                ),
            )
        except MpcSampleExportError as e:
            job.state = "failed"
            job.error_message = str(e)
            self._publish(
                MpcExportEvent(
                    MpcExportEventType.FAILED,
                    job_id,
                    suggestion.youtube_video_id,
                    display_name=suggestion.display_name,
                    mode=mode,
                    error_message=str(e),
                    message=str(e),
                ),
            )
            self._log.warning("MPC export failed for %s: %s", job_id, e)
        except Exception as e:  # noqa: BLE001
            job.state = "failed"
            job.error_message = str(e)
            self._publish(
                MpcExportEvent(
                    MpcExportEventType.FAILED,
                    job_id,
                    suggestion.youtube_video_id,
                    display_name=suggestion.display_name,
                    mode=mode,
                    error_message=str(e),
                ),
            )
            self._log.exception("Unexpected MPC export failure for %s", job_id)
        finally:
            self._maybe_emit_drained()

    def _maybe_emit_drained(self) -> None:
        with self._lock:
            active = any(
                j.state in ("queued", "running") for j in self._jobs.values()
            )
            if not active:
                self._publish(
                    MpcExportEvent(
                        MpcExportEventType.DRAINED,
                        "",
                        "",
                    ),
                )

    def _publish(self, event: MpcExportEvent) -> None:
        if event.type == MpcExportEventType.PROGRESS:
            now = time.monotonic()
            last = self._progress_throttle.get(event.job_id, 0.0)
            if now - last < self._PROGRESS_MIN_INTERVAL:
                return
            self._progress_throttle[event.job_id] = now
        elif event.type in (
            MpcExportEventType.COMPLETED,
            MpcExportEventType.FAILED,
            MpcExportEventType.CANCELLED,
        ):
            self._progress_throttle.pop(event.job_id, None)

        with self._lock:
            strong = list(self._subscribers)
            alive: list[Callable[[MpcExportEvent], None]] = []
            live_refs: list[weakref.WeakMethod] = []
            for ref in self._weak_subs:
                cb = ref()
                if cb is not None:
                    alive.append(cb)
                    live_refs.append(ref)
            self._weak_subs = live_refs
        for cb in strong + alive:
            try:
                cb(event)
            except Exception:
                self._log.exception("MPC export subscriber failed")
