"""
core/queue_manager.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Background Worker Pool & Event Bus

Drives IngestionPipeline for many URLs concurrently without blocking
the UI. Every stage transition, progress update, and outcome is
emitted as a typed event on a pub/sub bus — the UI subscribes once
at startup and receives a live stream of updates. No polling of
vault.db from the UI thread.

Concurrency model:
  • N worker threads (default 2). User-configurable in Settings.
  • Jobs live in a FIFO queue.Queue backed by queue_jobs rows in
    vault.db for crash-recovery.
  • Per-job cancel via threading.Event held in the active-jobs
    registry, addressable by job_id from the UI.
  • Graceful shutdown drains in-flight jobs, marks queued ones as
    cancelled, and joins workers within a bounded timeout.

Event bus:
  • Thread-safe subscribe/unsubscribe.
  • Subscriber callbacks run ON THE EMITTING THREAD (worker).
    Subscribers are responsible for marshalling to the UI thread —
    for CustomTkinter that's `root.after(0, fn)`. This avoids
    imposing a Tk dependency on core/.
  • Dead subscribers (weakref'd callables) are auto-pruned.

Failure model:
  • Single-job failures never crash the worker — caught, logged,
    persisted to queue_jobs.status='failed', and surfaced as an event.
  • Worker-thread crashes (should never happen) are caught at the top
    of the worker loop; worker re-enters the loop after logging.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

from core.database import QueueJobRecord, VaultDatabase
from core.pipeline import (
    IngestionPipeline, PipelineCancelledError, PipelineError,
    PipelineProgress, PipelineRequest, PipelineResult, PipelineStage,
)


# ─── Event types ─────────────────────────────────────────────────────

class QueueEventType(str, Enum):
    # Lifecycle
    JOB_ENQUEUED = "job_enqueued"
    JOB_STARTED = "job_started"
    JOB_PROGRESS = "job_progress"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"
    # Batch-level
    QUEUE_DRAINED = "queue_drained"
    WORKER_ERROR = "worker_error"


@dataclass(slots=True, frozen=True)
class QueueEvent:
    """
    A single event emitted on the bus. UI tabs subscribe by filtering
    on event.type; every event includes job_id where applicable.
    """
    type: QueueEventType
    job_id: Optional[int] = None
    # Display info cached for convenience (UI rarely needs to re-query DB)
    source_url: Optional[str] = None
    display_name: Optional[str] = None
    # Progress-specific
    stage: Optional[PipelineStage] = None
    overall_percent: float = 0.0
    stage_percent: float = 0.0
    message: str = ""
    # Enrichment (populated progressively)
    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    camelot_key: Optional[str] = None
    # Outcome-specific
    track_id: Optional[int] = None
    final_path: Optional[str] = None
    error_message: Optional[str] = None
    # Timestamp (UTC ISO)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )


# ─── Event bus ───────────────────────────────────────────────────────

class EventBus:
    """
    Thread-safe pub/sub. Callbacks run on the publishing thread.
    Exceptions in subscribers are logged but never propagate back
    to the publisher — one broken UI subscriber cannot take down a worker.
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._subscribers: list[Callable[[QueueEvent], None]] = []
        # Weak refs kept for bound methods so UI teardown doesn't leak.
        self._weak_subs: list[weakref.WeakMethod] = []
        self._lock = threading.RLock()
        self._log = logger or logging.getLogger("cratedigger.events")

    def subscribe(
        self, callback: Callable[[QueueEvent], None], weak: bool = False,
    ) -> Callable[[], None]:
        """
        Register `callback`. Returns an unsubscribe fn.
        If `weak=True` and callback is a bound method, stores a WeakMethod
        so the subscriber can be garbage-collected without explicit
        unsubscription — useful for UI widgets that get destroyed.
        """
        with self._lock:
            if weak and hasattr(callback, "__self__"):
                ref = weakref.WeakMethod(callback)
                self._weak_subs.append(ref)
                def _unsub_weak() -> None:
                    with self._lock:
                        try:
                            self._weak_subs.remove(ref)
                        except ValueError:
                            pass
                return _unsub_weak
            else:
                self._subscribers.append(callback)
                def _unsub() -> None:
                    with self._lock:
                        try:
                            self._subscribers.remove(callback)
                        except ValueError:
                            pass
                return _unsub

    def publish(self, event: QueueEvent) -> None:
        """Dispatch `event` to all live subscribers."""
        with self._lock:
            strong = list(self._subscribers)
            # Prune dead weakrefs in-place
            alive_weak: list[Callable[[QueueEvent], None]] = []
            live_weakrefs: list[weakref.WeakMethod] = []
            for wm in self._weak_subs:
                cb = wm()
                if cb is not None:
                    alive_weak.append(cb)
                    live_weakrefs.append(wm)
            self._weak_subs = live_weakrefs

        # Dispatch outside the lock so subscribers don't deadlock
        # the bus if they publish further events.
        for cb in strong + alive_weak:
            try:
                cb(event)
            except Exception:
                self._log.exception(
                    "Event subscriber raised on %s", event.type,
                )

    def clear(self) -> None:
        with self._lock:
            self._subscribers.clear()
            self._weak_subs.clear()


# ─── Public exceptions ───────────────────────────────────────────────

class QueueManagerError(Exception):
    """Base class for queue manager failures."""


class QueueShutdownError(QueueManagerError):
    """Operation attempted after shutdown."""


# ─── Internal types ──────────────────────────────────────────────────

@dataclass(slots=True)
class _InFlightJob:
    """Per-job runtime state held in the active-jobs registry."""
    job_id: int
    request: PipelineRequest
    cancel_event: threading.Event
    display_name: Optional[str] = None
    started_at: float = 0.0


# Sentinel for worker shutdown
_SHUTDOWN_SENTINEL = object()


# ─── The Queue Manager ───────────────────────────────────────────────

class QueueManager:
    """
    Background worker pool that runs IngestionPipeline jobs. One
    instance per app; started at app boot, shut down at app exit.
    """

    def __init__(
        self,
        *,
        pipeline: IngestionPipeline,
        database: VaultDatabase,
        event_bus: Optional[EventBus] = None,
        num_workers: int = 2,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if num_workers < 1:
            raise ValueError("num_workers must be >= 1")

        self._pipeline = pipeline
        self._db = database
        self._bus = event_bus or EventBus(logger=logger)
        self._num_workers = num_workers
        self._log = logger or logging.getLogger("cratedigger.queue")

        # Work queue. `Any` because we also push the shutdown sentinel.
        self._work_q: "queue.Queue[Any]" = queue.Queue()

        # Active jobs registry: job_id → _InFlightJob
        self._active: dict[int, _InFlightJob] = {}
        self._active_lock = threading.RLock()

        self._workers: list[threading.Thread] = []
        self._state_lock = threading.RLock()
        self._running = False
        self._shutting_down = False

    # ── Public API: subscription ──

    @property
    def events(self) -> EventBus:
        """Event bus handle for UI subscribers."""
        return self._bus

    # ── Public API: lifecycle ──

    def start(self) -> None:
        """
        Start the worker pool. Must be called exactly once.
        Also resets any crash-stuck jobs in the DB on first start.
        """
        with self._state_lock:
            if self._running:
                raise QueueManagerError("QueueManager already started")
            if self._shutting_down:
                raise QueueShutdownError("QueueManager is shutting down")

            # Recover from prior crash — any row marked 'in-flight' is
            # stuck because the worker that owned it died.
            reset = self._db.reset_stuck_jobs()
            if reset:
                self._log.warning(
                    "Reset %d crash-stuck job(s) to 'failed' on startup.", reset,
                )

            for i in range(self._num_workers):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"cratedigger-worker-{i + 1}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)

            self._running = True
            self._log.info("QueueManager started with %d worker(s)", self._num_workers)

    def shutdown(self, timeout: float = 10.0, cancel_in_flight: bool = True) -> None:
        """
        Stop accepting new jobs. Optionally cancel in-flight, then join
        workers within `timeout` seconds. Safe to call multiple times.
        """
        with self._state_lock:
            if not self._running or self._shutting_down:
                return
            self._shutting_down = True

        self._log.info("QueueManager shutdown requested (cancel_in_flight=%s)",
                       cancel_in_flight)

        if cancel_in_flight:
            with self._active_lock:
                for job in self._active.values():
                    job.cancel_event.set()

        # Push one shutdown sentinel per worker
        for _ in range(self._num_workers):
            self._work_q.put(_SHUTDOWN_SENTINEL)

        deadline = time.monotonic() + timeout
        for w in self._workers:
            remaining = max(0.0, deadline - time.monotonic())
            w.join(timeout=remaining)
            if w.is_alive():
                self._log.warning("Worker %s did not shut down in time", w.name)

        with self._state_lock:
            self._running = False

        self._log.info("QueueManager shut down.")

    # ── Public API: enqueue / cancel ──

    def enqueue(self, request: PipelineRequest) -> int:
        """
        Enqueue a pipeline run. Returns the queue_jobs.id so the UI
        can track the specific job via events.
        """
        with self._state_lock:
            if not self._running:
                raise QueueManagerError("QueueManager not started")
            if self._shutting_down:
                raise QueueShutdownError("Cannot enqueue during shutdown")

        # Persist job row first so UI can render it immediately via events
        # and so crash-recovery catches jobs queued but never run.
        job_id = self._db.create_queue_job(QueueJobRecord(
            source_url=request.source_url,
            status="pending",
            enable_stems=request.enable_stems,
        ))

        self._work_q.put((job_id, request))

        self._bus.publish(QueueEvent(
            type=QueueEventType.JOB_ENQUEUED,
            job_id=job_id,
            source_url=request.source_url,
            message="Enqueued",
        ))
        self._log.info("Enqueued job %d: %s", job_id, request.source_url)
        return job_id

    def cancel(self, job_id: int) -> bool:
        """
        Signal cancellation for `job_id`. Returns True if the job was
        in-flight and received the cancel, False if it wasn't found
        (already completed, failed, or never started).

        For pending (not-yet-picked-up) jobs, cancellation happens when
        the worker pulls it — we update the DB status immediately so
        the UI reflects the intent, and the worker skips it on pickup.
        """
        with self._active_lock:
            job = self._active.get(job_id)
        if job is not None:
            job.cancel_event.set()
            self._log.info("Cancel signal sent to in-flight job %d", job_id)
            return True

        # Not active — might be pending. Mark in DB so worker skips on pickup.
        rec = self._db.list_queue_jobs()
        for r in rec:
            if r.id == job_id and r.status == "pending":
                self._db.update_queue_job(
                    job_id, status="cancelled",
                    completed_at=_utc_now_iso(),
                    error_message="Cancelled before start",
                )
                self._bus.publish(QueueEvent(
                    type=QueueEventType.JOB_CANCELLED,
                    job_id=job_id,
                    source_url=r.source_url,
                    message="Cancelled before start",
                ))
                return True
        return False

    def cancel_all(self) -> int:
        """
        Cancel every active and pending job. Returns count cancelled.
        Typically wired to a "Stop All" button.
        """
        count = 0
        with self._active_lock:
            for job in self._active.values():
                job.cancel_event.set()
                count += 1
        for r in self._db.list_queue_jobs(statuses=("pending",)):
            if r.id is None:
                continue
            self._db.update_queue_job(
                r.id, status="cancelled",
                completed_at=_utc_now_iso(),
                error_message="Cancelled by 'Cancel All'",
            )
            self._bus.publish(QueueEvent(
                type=QueueEventType.JOB_CANCELLED,
                job_id=r.id,
                source_url=r.source_url,
                message="Cancelled",
            ))
            count += 1
        return count

    # ── Public API: introspection ──

    def active_job_ids(self) -> list[int]:
        with self._active_lock:
            return list(self._active.keys())

    def list_jobs(self, statuses=None, limit: int = 100) -> list[QueueJobRecord]:
        """Passthrough to the DB — convenience for the queue UI."""
        return self._db.list_queue_jobs(statuses=statuses, limit=limit)

    # ── Worker loop ──

    def _worker_loop(self) -> None:
        """Top-level loop of every worker thread. Never raises."""
        thread_name = threading.current_thread().name
        self._log.debug("%s started", thread_name)

        while True:
            try:
                item = self._work_q.get()
            except Exception:
                self._log.exception("%s: queue.get raised", thread_name)
                continue

            if item is _SHUTDOWN_SENTINEL:
                self._log.debug("%s received shutdown sentinel", thread_name)
                return

            try:
                job_id, request = item
                self._run_job(job_id, request)
            except Exception as e:
                # Absolute safety net — worker must never die.
                self._log.exception("%s: unhandled worker error", thread_name)
                self._bus.publish(QueueEvent(
                    type=QueueEventType.WORKER_ERROR,
                    error_message=f"{thread_name}: {e}",
                ))
            finally:
                # Check for idle → emit QUEUE_DRAINED
                with self._active_lock:
                    if not self._active and self._work_q.empty():
                        self._bus.publish(QueueEvent(
                            type=QueueEventType.QUEUE_DRAINED,
                            message="Queue idle",
                        ))
                self._work_q.task_done()

    # ── Per-job execution ──

    def _run_job(self, job_id: int, request: PipelineRequest) -> None:
        """Run one pipeline job end-to-end. Catches all pipeline errors."""
        # Check if the job was cancelled while waiting in queue
        db_rec = self._db_job_status(job_id)
        if db_rec == "cancelled":
            self._log.info("Skipping job %d — already cancelled", job_id)
            return

        cancel_event = threading.Event()
        in_flight = _InFlightJob(
            job_id=job_id,
            request=request,
            cancel_event=cancel_event,
            started_at=time.monotonic(),
        )
        with self._active_lock:
            self._active[job_id] = in_flight

        now = _utc_now_iso()
        self._db.update_queue_job(
            job_id, status="downloading", started_at=now,
            progress_pct=0.0, current_stage="downloading",
        )
        self._bus.publish(QueueEvent(
            type=QueueEventType.JOB_STARTED,
            job_id=job_id,
            source_url=request.source_url,
            stage=PipelineStage.DOWNLOADING,
            message="Job started",
        ))

        try:
            # Wire pipeline progress → DB + event bus.
            def on_progress(p: PipelineProgress) -> None:
                # Capture display_name into in_flight as it becomes known,
                # so subsequent events carry it without a DB round-trip.
                if p.display_name and not in_flight.display_name:
                    in_flight.display_name = p.display_name

                # Persist progress for crash-recovery / UI state on boot.
                self._db.update_queue_job(
                    job_id,
                    status=_stage_to_db_status(p.stage),
                    progress_pct=p.overall_percent,
                    current_stage=p.stage.value,
                )

                self._bus.publish(QueueEvent(
                    type=QueueEventType.JOB_PROGRESS,
                    job_id=job_id,
                    source_url=request.source_url,
                    display_name=in_flight.display_name,
                    stage=p.stage,
                    overall_percent=p.overall_percent,
                    stage_percent=p.stage_percent,
                    message=p.message,
                    bpm=p.bpm,
                    musical_key=p.musical_key,
                    camelot_key=p.camelot_key,
                ))

            result: PipelineResult = self._pipeline.run(
                request,
                progress_callback=on_progress,
                cancel_event=cancel_event,
            )

            completed_at = _utc_now_iso()
            self._db.update_queue_job(
                job_id,
                status="complete",
                progress_pct=100.0,
                current_stage="complete",
                track_id=result.track_id,
                completed_at=completed_at,
            )
            self._bus.publish(QueueEvent(
                type=QueueEventType.JOB_COMPLETED,
                job_id=job_id,
                source_url=request.source_url,
                display_name=in_flight.display_name,
                track_id=result.track_id,
                final_path=str(result.final_audio_path),
                bpm=result.analysis.bpm,
                musical_key=result.analysis.musical_key,
                camelot_key=result.analysis.camelot_key,
                overall_percent=100.0,
                message=f"Complete in {result.total_elapsed_seconds:.1f}s",
            ))
            self._log.info(
                "Job %d complete: track_id=%d in %.1fs",
                job_id, result.track_id, result.total_elapsed_seconds,
            )

        except PipelineCancelledError as e:
            self._db.update_queue_job(
                job_id, status="cancelled",
                error_message=str(e),
                completed_at=_utc_now_iso(),
            )
            self._bus.publish(QueueEvent(
                type=QueueEventType.JOB_CANCELLED,
                job_id=job_id,
                source_url=request.source_url,
                display_name=in_flight.display_name,
                message=str(e),
            ))

        except PipelineError as e:
            self._db.update_queue_job(
                job_id, status="failed",
                error_message=str(e),
                completed_at=_utc_now_iso(),
            )
            self._bus.publish(QueueEvent(
                type=QueueEventType.JOB_FAILED,
                job_id=job_id,
                source_url=request.source_url,
                display_name=in_flight.display_name,
                error_message=str(e),
                message="Failed",
            ))
            self._log.warning("Job %d failed: %s", job_id, e)

        except Exception as e:
            # Defensive — pipeline.run shouldn't reach here, but if it
            # does, convert to a failure event and keep the worker alive.
            self._log.exception("Job %d: unexpected error", job_id)
            self._db.update_queue_job(
                job_id, status="failed",
                error_message=f"Unexpected: {e}",
                completed_at=_utc_now_iso(),
            )
            self._bus.publish(QueueEvent(
                type=QueueEventType.JOB_FAILED,
                job_id=job_id,
                source_url=request.source_url,
                error_message=str(e),
                message="Unexpected error",
            ))

        finally:
            with self._active_lock:
                self._active.pop(job_id, None)

    def _db_job_status(self, job_id: int) -> Optional[str]:
        for r in self._db.list_queue_jobs():
            if r.id == job_id:
                return r.status
        return None


# ─── Helpers ─────────────────────────────────────────────────────────

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stage_to_db_status(stage: PipelineStage) -> str:
    """
    Map pipeline stages to the constrained `queue_jobs.status` values
    defined in the schema. Stages not represented (e.g. artwork, indexing)
    fold into the nearest durable status.
    """
    mapping = {
        PipelineStage.DOWNLOADING:        "downloading",
        PipelineStage.ANALYZING:          "analyzing",
        PipelineStage.FETCHING_ARTWORK:   "tagging",
        PipelineStage.TAGGING:            "tagging",
        PipelineStage.RELOCATING:         "tagging",
        PipelineStage.INDEXING:           "tagging",
        PipelineStage.SEPARATING_STEMS:   "separating_stems",
        PipelineStage.COMPLETE:           "complete",
        PipelineStage.FAILED:             "failed",
        PipelineStage.CANCELLED:          "cancelled",
    }
    return mapping.get(stage, "pending")