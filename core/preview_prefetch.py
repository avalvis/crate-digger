"""
core/preview_prefetch.py
──────────────────────────────────────────────────────────────────────
Background preview warmup for Digital Crate reels.

After a dig completes, video IDs are queued here so yt-dlp + decode work
happens before the user clicks Preview. Bounded concurrency, deduped
per video_id, optional in-memory LRU of decoded PreviewData.
"""
from __future__ import annotations

import logging
import threading
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from core.preview import PreviewCancelledError, PreviewData, PreviewError, PreviewService


class PrefetchState(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    DECODING = "decoding"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PrefetchEventType(str, Enum):
    STATE_CHANGED = "state_changed"
    PROGRESS = "progress"
    BATCH_DRAINED = "batch_drained"


@dataclass(slots=True, frozen=True)
class PrefetchEvent:
    type: PrefetchEventType
    video_id: str
    state: PrefetchState = PrefetchState.PENDING
    message: str = ""
    percent: float = 0.0
    error_message: Optional[str] = None
    data: Optional[PreviewData] = None


@dataclass(slots=True)
class _PrefetchEntry:
    video_id: str
    state: PrefetchState = PrefetchState.PENDING
    message: str = ""
    percent: float = 0.0
    error_message: Optional[str] = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = 0.0


class PreviewPrefetchService:
    """Bounded background prefetch pool for reel previews."""

    _DECODED_LRU_MAX = 8
    _JOB_TIMEOUT_S = 90.0

    def __init__(
        self,
        preview: PreviewService,
        *,
        max_workers: int = 2,
        keep_decoded: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._preview = preview
        self._max_workers = max(1, int(max_workers))
        self._keep_decoded = bool(keep_decoded)
        self._log = logger or logging.getLogger("cratedigger.preview_prefetch")

        self._lock = threading.RLock()
        self._entries: dict[str, _PrefetchEntry] = {}
        self._queue: deque[str] = deque()
        self._decoded_lru: OrderedDict[str, PreviewData] = OrderedDict()
        self._active = 0
        self._batch_total = 0
        self._batch_done = 0
        self._batch_ids: list[str] = []
        self._batch_drained = False
        self._running = False
        self._shutdown = threading.Event()

        self._subscribers: list[Callable[[PrefetchEvent], None]] = []
        self._weak_subs: list[weakref.WeakMethod] = []

    # ── Lifecycle ──

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._shutdown.clear()

    def shutdown(self, *, cancel_pending: bool = True) -> None:
        with self._lock:
            self._running = False
            self._shutdown.set()
            if cancel_pending:
                for entry in self._entries.values():
                    entry.cancel_event.set()
                self._queue.clear()

    def subscribe(
        self, callback: Callable[[PrefetchEvent], None], *, weak: bool = True,
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

    def enqueue_batch(self, video_ids: list[str]) -> None:
        """Queue reel previews top-to-bottom; skips already-ready IDs."""
        ids = [self._preview.normalize_video_id(v) for v in video_ids if v]
        pending: list[PrefetchEvent] = []
        with self._lock:
            self._batch_ids = list(ids)
            self._batch_drained = False
            self._batch_total = len(ids)
            self._batch_done = 0
            for vid in ids:
                if vid in self._decoded_lru:
                    self._ensure_entry(vid).state = PrefetchState.READY
                    pending.append(
                        PrefetchEvent(
                            PrefetchEventType.STATE_CHANGED,
                            vid,
                            state=PrefetchState.READY,
                            message="Ready",
                            percent=100.0,
                            data=self._decoded_lru[vid],
                        ),
                    )
                    continue
                entry = self._entries.get(vid)
                if entry and entry.state in (
                    PrefetchState.PENDING,
                    PrefetchState.DOWNLOADING,
                    PrefetchState.DECODING,
                    PrefetchState.READY,
                ):
                    continue
                if entry and entry.state == PrefetchState.FAILED:
                    entry.cancel_event = threading.Event()
                    entry.state = PrefetchState.PENDING
                    entry.error_message = None
                elif entry is None:
                    entry = _PrefetchEntry(video_id=vid)
                    self._entries[vid] = entry
                else:
                    entry.cancel_event = threading.Event()
                    entry.state = PrefetchState.PENDING
                self._queue.append(vid)
                pending.append(
                    PrefetchEvent(
                        PrefetchEventType.STATE_CHANGED,
                        vid,
                        state=PrefetchState.PENDING,
                        message="Queued",
                    ),
                )
        for event in pending:
            self._publish(event)
        self._pump()
        self._emit_drain_check()

    def cancel_batch(self, video_ids: Optional[list[str]] = None) -> None:
        with self._lock:
            targets = (
                {self._preview.normalize_video_id(v) for v in video_ids}
                if video_ids
                else set(self._entries)
            )
            for vid in targets:
                entry = self._entries.get(vid)
                if entry is None:
                    continue
                entry.cancel_event.set()
                if entry.state not in (PrefetchState.READY, PrefetchState.FAILED):
                    entry.state = PrefetchState.CANCELLED
                try:
                    while vid in self._queue:
                        self._queue.remove(vid)
                except ValueError:
                    pass
        self._pump()
        self._emit_drain_check()

    def get_state(self, video_id: str) -> PrefetchState:
        vid = self._preview.normalize_video_id(video_id)
        with self._lock:
            entry = self._entries.get(vid)
            if entry is None:
                return PrefetchState.PENDING
            return entry.state

    def get_decoded(self, video_id: str) -> Optional[PreviewData]:
        vid = self._preview.normalize_video_id(video_id)
        with self._lock:
            if vid in self._decoded_lru:
                self._decoded_lru.move_to_end(vid)
                return self._decoded_lru[vid]
        return None

    def wait_ready(
        self,
        video_id: str,
        *,
        timeout: float = 120.0,
        poll: float = 0.15,
    ) -> Optional[PreviewData]:
        vid = self._preview.normalize_video_id(video_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.get_decoded(vid)
            if data is not None:
                return data
            state = self.get_state(vid)
            if state == PrefetchState.FAILED:
                return None
            if state == PrefetchState.READY:
                return self.get_decoded(vid)
            time.sleep(poll)
        return self.get_decoded(vid)

    def batch_progress(self) -> tuple[int, int]:
        """Return (finished, total) where finished = ready, failed, or cancelled."""
        with self._lock:
            total = len(self._batch_ids)
            if total == 0:
                return 0, 0
            terminal = (
                PrefetchState.READY,
                PrefetchState.FAILED,
                PrefetchState.CANCELLED,
            )
            done = sum(
                1 for vid in self._batch_ids
                if (entry := self._entries.get(vid)) is not None
                and entry.state in terminal
            )
            return done, total

    def reap_stale_jobs(self, *, timeout_s: Optional[float] = None) -> int:
        """Fail in-flight jobs that exceeded the warmup timeout."""
        limit = float(timeout_s if timeout_s is not None else self._JOB_TIMEOUT_S)
        now = time.monotonic()
        reaped = 0
        events: list[PrefetchEvent] = []
        with self._lock:
            for vid in list(self._batch_ids):
                entry = self._entries.get(vid)
                if entry is None:
                    continue
                if entry.state in (
                    PrefetchState.READY,
                    PrefetchState.FAILED,
                    PrefetchState.CANCELLED,
                ):
                    continue
                if entry.started_at <= 0:
                    continue
                if now - entry.started_at < limit:
                    continue
                entry.cancel_event.set()
                entry.state = PrefetchState.FAILED
                entry.error_message = "Preview warmup timed out"
                entry.message = "Timed out"
                reaped += 1
                events.append(
                    PrefetchEvent(
                        PrefetchEventType.STATE_CHANGED,
                        vid,
                        state=PrefetchState.FAILED,
                        message="Timed out",
                        error_message=entry.error_message,
                    ),
                )
        for event in events:
            self._publish(event)
        if reaped:
            self._emit_drain_check()
            self._pump()
        return reaped

    def is_batch_drained(self) -> bool:
        with self._lock:
            return self._batch_drained

    def is_batch_idle(self) -> bool:
        """True when every ID in the current batch reached a terminal state."""
        with self._lock:
            return self._batch_terminal_locked()

    def _batch_terminal_locked(self) -> bool:
        if not self._batch_ids:
            return True
        terminal = (
            PrefetchState.READY,
            PrefetchState.FAILED,
            PrefetchState.CANCELLED,
        )
        for vid in self._batch_ids:
            entry = self._entries.get(vid)
            if entry is None or entry.state not in terminal:
                return False
        return True

    def _check_batch_drained_locked(self) -> Optional[PrefetchEvent]:
        if self._batch_drained or not self._batch_ids:
            return None
        if not self._batch_terminal_locked():
            return None
        if self._queue or self._active > 0:
            return None
        self._batch_drained = True
        last_id = self._batch_ids[-1] if self._batch_ids else ""
        return PrefetchEvent(
            PrefetchEventType.BATCH_DRAINED,
            last_id,
        )

    def _emit_drain_check(self) -> None:
        with self._lock:
            event = self._check_batch_drained_locked()
        if event is not None:
            self._publish(event)

    def configure(
        self,
        *,
        max_workers: Optional[int] = None,
        keep_decoded: Optional[bool] = None,
    ) -> None:
        with self._lock:
            if max_workers is not None:
                self._max_workers = max(1, int(max_workers))
            if keep_decoded is not None:
                self._keep_decoded = bool(keep_decoded)

    # ── Workers ──

    def _pump(self) -> None:
        with self._lock:
            if not self._running or self._shutdown.is_set():
                return
            while self._active < self._max_workers and self._queue:
                vid = self._queue.popleft()
                entry = self._entries.get(vid)
                if entry is None or entry.cancel_event.is_set():
                    continue
                if entry.state == PrefetchState.READY:
                    continue
                self._active += 1
                threading.Thread(
                    target=self._worker,
                    args=(vid,),
                    name=f"preview-prefetch-{vid}",
                    daemon=True,
                ).start()
        self._emit_drain_check()

    def _worker(self, video_id: str) -> None:
        try:
            self._run_prefetch(video_id)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)
            self._emit_drain_check()
            self._pump()

    def _run_prefetch(self, video_id: str) -> None:
        entry = self._entries.get(video_id)
        if entry is None or entry.cancel_event.is_set():
            return
        entry.started_at = time.monotonic()
        last_progress_pub = 0.0

        def on_progress(pct: float, msg: str) -> None:
            nonlocal last_progress_pub
            if entry.cancel_event.is_set():
                return
            now = time.monotonic()
            if pct < 99.0 and now - last_progress_pub < 0.25:
                return
            last_progress_pub = now
            state = (
                PrefetchState.DOWNLOADING
                if pct < 65.0
                else PrefetchState.DECODING
            )
            entry.state = state
            entry.message = msg
            entry.percent = pct
            self._publish(
                PrefetchEvent(
                    PrefetchEventType.PROGRESS,
                    video_id,
                    state=state,
                    message=msg,
                    percent=pct,
                ),
            )

        try:
            entry.state = PrefetchState.DOWNLOADING
            entry.message = "Warming preview…"
            self._publish(
                PrefetchEvent(
                    PrefetchEventType.STATE_CHANGED,
                    video_id,
                    state=PrefetchState.DOWNLOADING,
                    message=entry.message,
                ),
            )

            quick_path = self._preview.get_quick_cached_path(video_id)
            full_path = self._preview.get_cached_path(video_id)
            if quick_path is None and full_path is None:
                self._preview.warm_cache(
                    video_id,
                    progress_callback=on_progress,
                    cancel_event=entry.cancel_event,
                )

            if entry.cancel_event.is_set():
                entry.state = PrefetchState.CANCELLED
                return

            if self._keep_decoded:
                data = self._preview.fetch_quick(
                    video_id,
                    progress_callback=on_progress,
                    cancel_event=entry.cancel_event,
                )
                with self._lock:
                    self._decoded_lru[video_id] = data
                    self._decoded_lru.move_to_end(video_id)
                    while len(self._decoded_lru) > self._DECODED_LRU_MAX:
                        self._decoded_lru.popitem(last=False)

            entry.state = PrefetchState.READY
            entry.message = "Ready"
            entry.percent = 100.0
            data = self.get_decoded(video_id)
            self._publish(
                PrefetchEvent(
                    PrefetchEventType.STATE_CHANGED,
                    video_id,
                    state=PrefetchState.READY,
                    message="Ready",
                    percent=100.0,
                    data=data,
                ),
            )
            self._emit_drain_check()
        except PreviewCancelledError:
            entry.state = PrefetchState.CANCELLED
            self._publish(
                PrefetchEvent(
                    PrefetchEventType.STATE_CHANGED,
                    video_id,
                    state=PrefetchState.CANCELLED,
                ),
            )
            self._emit_drain_check()
        except PreviewError as e:
            entry.state = PrefetchState.FAILED
            entry.error_message = str(e)
            self._publish(
                PrefetchEvent(
                    PrefetchEventType.STATE_CHANGED,
                    video_id,
                    state=PrefetchState.FAILED,
                    message=str(e),
                    error_message=str(e),
                ),
            )
            self._log.warning("Prefetch failed for %s: %s", video_id, e)
            self._emit_drain_check()
        except Exception as e:  # noqa: BLE001
            entry.state = PrefetchState.FAILED
            entry.error_message = str(e)
            self._publish(
                PrefetchEvent(
                    PrefetchEventType.STATE_CHANGED,
                    video_id,
                    state=PrefetchState.FAILED,
                    error_message=str(e),
                ),
            )
            self._log.exception("Unexpected prefetch failure for %s", video_id)
            self._emit_drain_check()

    def _ensure_entry(self, video_id: str) -> _PrefetchEntry:
        entry = self._entries.get(video_id)
        if entry is None:
            entry = _PrefetchEntry(video_id=video_id)
            self._entries[video_id] = entry
        return entry

    def _publish(self, event: PrefetchEvent) -> None:
        with self._lock:
            strong = list(self._subscribers)
            alive: list[Callable[[PrefetchEvent], None]] = []
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
                self._log.exception("Prefetch subscriber failed")
