"""
ui/events.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Tk-safe Event Bridge

Workers emit QueueEvents on the core event bus from arbitrary threads.
Tk/CustomTkinter is STRICTLY single-threaded — any widget operation
from a non-Tk thread produces undefined behavior (segfaults on macOS,
silent UI corruption on Windows, intermittent hangs on Linux).

This module bridges the two worlds:

    worker thread  ──QueueEvent──>  UiEventBridge.on_core_event
                                           │
                                           ▼
                                   thread-safe queue.Queue
                                           │
                        root.after(interval, drain_pump)
                                           │
                                           ▼
                                Tk main thread drains queue,
                                dispatches to UI subscribers

UI code (tabs, widgets) subscribes to the UiEventBridge, NOT to the
core EventBus directly. This enforces the rule architecturally: any
handler registered on UiEventBridge is guaranteed to run on the Tk
thread and is free to touch widgets.

Key correctness properties:
  • Coalescing: burst-y progress events (demucs spams ~10/sec) are
    collapsed to the most recent per (type, job_id) tuple BEFORE
    dispatch. The UI never gets behind because it's processing a
    stale progress number that's already been superseded.
  • Drainage cap: each pump cycle processes at most `max_per_cycle`
    events so a sudden surge doesn't starve Tk's event loop of
    redraw time. Pump reschedules itself if events remain.
  • Auto-pruning: UI subscribers registered with weak=True get
    garbage-collected naturally when their widget is destroyed.
  • Stoppable: `stop()` cancels the pump so app shutdown doesn't
    process events against a half-torn-down widget tree.
"""
from __future__ import annotations

import logging
import queue
import threading
import weakref
from collections import OrderedDict
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import customtkinter as ctk  # noqa: F401

from core.queue_manager import EventBus, QueueEvent, QueueEventType


# Subscriber callable type — runs on Tk thread, takes a QueueEvent.
UiEventHandler = Callable[[QueueEvent], None]


# Events of these types get coalesced on the (type, job_id) key. Keeping
# the set tight preserves semantic events (started/completed/failed/
# cancelled/enqueued/drained) which the UI may depend on seeing every
# instance of. Only rapid-fire progress updates benefit from coalescing.
_COALESCABLE_TYPES: frozenset[QueueEventType] = frozenset({
    QueueEventType.JOB_PROGRESS,
})


class UiEventBridge:
    """
    Marshals QueueEvents from worker threads onto the Tk main thread,
    with coalescing and backpressure handling.

    One instance per app, created and started from ui/app.py right
    after the CTk root is constructed.
    """

    def __init__(
        self,
        tk_root: "ctk.CTk",
        core_bus: EventBus,
        *,
        pump_interval_ms: int = 33,         # ~30Hz UI refresh cap
        max_per_cycle: int = 64,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._root = tk_root
        self._core_bus = core_bus
        self._pump_interval = int(pump_interval_ms)
        self._max_per_cycle = int(max_per_cycle)
        self._log = logger or logging.getLogger("cratedigger.ui.events")

        # Cross-thread queue. Workers push; the pump on the Tk thread drains.
        self._inbox: "queue.Queue[QueueEvent]" = queue.Queue()

        # UI subscribers — run only on the Tk thread.
        self._strong_subs: list[UiEventHandler] = []
        self._weak_subs: list[weakref.WeakMethod] = []
        self._subs_lock = threading.RLock()

        # Pump lifecycle state
        self._pump_handle: Optional[str] = None
        self._running = False
        self._unsub_from_core: Optional[Callable[[], None]] = None

        # Coalescing buffer (Tk-thread only — no lock needed)
        # Key: (type, job_id, stage) → latest event
        self._coalesce_buf: "OrderedDict[tuple, QueueEvent]" = OrderedDict()

        # Simple instrumentation — handy for debugging "where did the
        # UI freeze?" sessions. Accessible via `stats()`.
        self._events_received = 0
        self._events_coalesced = 0
        self._events_dispatched = 0

    # ── Lifecycle ──

    def start(self) -> None:
        """Subscribe to the core bus and start the Tk pump."""
        if self._running:
            return
        self._running = True

        # Receive from core (fires on worker threads — must stay fast
        # and must not touch widgets).
        self._unsub_from_core = self._core_bus.subscribe(self._on_core_event)

        # Kick off the pump on the Tk thread.
        self._schedule_pump()
        self._log.debug("UiEventBridge started (pump=%dms)", self._pump_interval)

    def stop(self) -> None:
        """
        Unsubscribe from core, cancel the pump, and drop any buffered
        events. Safe to call multiple times.
        """
        if not self._running:
            return
        self._running = False

        if self._unsub_from_core is not None:
            try:
                self._unsub_from_core()
            except Exception:
                pass
            self._unsub_from_core = None

        if self._pump_handle is not None:
            try:
                self._root.after_cancel(self._pump_handle)
            except Exception:
                pass
            self._pump_handle = None

        # Drop buffered & in-flight events
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break
        self._coalesce_buf.clear()

        with self._subs_lock:
            self._strong_subs.clear()
            self._weak_subs.clear()

        self._log.debug(
            "UiEventBridge stopped (received=%d coalesced=%d dispatched=%d)",
            self._events_received, self._events_coalesced, self._events_dispatched,
        )

    # ── Subscription ──

    def subscribe(
        self,
        handler: UiEventHandler,
        *,
        weak: bool = True,
    ) -> Callable[[], None]:
        """
        Register a UI handler. Runs on the Tk main thread — safe to
        touch widgets. Returns an unsubscribe function.

        `weak=True` is the default and strongly preferred for widget
        bound methods — when the widget is destroyed, the subscription
        auto-clears without explicit teardown.
        """
        with self._subs_lock:
            if weak and hasattr(handler, "__self__"):
                ref = weakref.WeakMethod(handler)  # type: ignore[arg-type]
                self._weak_subs.append(ref)
                def _unsub_weak() -> None:
                    with self._subs_lock:
                        try:
                            self._weak_subs.remove(ref)
                        except ValueError:
                            pass
                return _unsub_weak
            else:
                self._strong_subs.append(handler)
                def _unsub_strong() -> None:
                    with self._subs_lock:
                        try:
                            self._strong_subs.remove(handler)
                        except ValueError:
                            pass
                return _unsub_strong

    # ── Worker-thread entry point ──

    def _on_core_event(self, event: QueueEvent) -> None:
        """
        Called from worker threads. MUST NOT touch widgets or Tk vars.
        Enqueues the event for the Tk-thread pump to pick up.
        """
        if not self._running:
            return
        try:
            self._inbox.put_nowait(event)
        except queue.Full:
            # Inbox is unbounded by default; reaching this branch would
            # imply a bug. Log and drop so workers never block on UI.
            self._log.warning("UI event inbox full; dropping event.")

    # ── Tk-thread pump ──

    def _schedule_pump(self) -> None:
        if not self._running:
            return
        self._pump_handle = self._root.after(
            self._pump_interval, self._pump,
        )

    def _pump(self) -> None:
        """
        Drain the inbox into the coalesce buffer, then dispatch up to
        `max_per_cycle` events. Reschedules itself until stopped.
        """
        if not self._running:
            return

        # ── Drain inbox into coalesce buffer ──
        drained = 0
        while drained < self._max_per_cycle * 4:   # drain more than we dispatch
            try:
                event = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._events_received += 1
            self._add_to_buffer(event)
            drained += 1

        # ── Dispatch up to max_per_cycle events from the buffer ──
        dispatched = 0
        while dispatched < self._max_per_cycle and self._coalesce_buf:
            _key, event = self._coalesce_buf.popitem(last=False)  # FIFO
            self._dispatch_to_subscribers(event)
            self._events_dispatched += 1
            dispatched += 1

        # If there's still stuff in the buffer, don't wait the full
        # interval — pump again immediately on the next Tk idle cycle.
        if self._coalesce_buf:
            self._pump_handle = self._root.after(1, self._pump)
        else:
            self._schedule_pump()

    def _add_to_buffer(self, event: QueueEvent) -> None:
        """
        Add event to the coalesce buffer. For coalescable types, newer
        events replace older ones with the same (type, job_id, stage) key,
        keeping the UI from processing obsolete progress numbers.

        Keying on `stage` too (not just type+job_id) matters: a job races
        through several pipeline stages (artwork/tag/relocate/index) fast
        enough that they can all land in the same ~33ms pump window. If
        every stage shared one key, each new stage's event would silently
        overwrite the previous stage's *unshown* event, so the UI would
        visibly skip stages entirely — jumping from "Analyzing" straight
        to "Complete" with no frame in between. Splitting by stage
        guarantees at least one dispatched frame per stage transition,
        while same-stage progress ticks (e.g. BPM analysis's 0-99%) still
        coalesce exactly as before.
        """
        if event.type in _COALESCABLE_TYPES and event.job_id is not None:
            key: tuple = (event.type, event.job_id, event.stage)
            if key in self._coalesce_buf:
                self._events_coalesced += 1
                # Remove + re-insert to move it to the end (FIFO order
                # of most-recent-per-key is the intuitive behavior).
                del self._coalesce_buf[key]
            self._coalesce_buf[key] = event
        else:
            # Non-coalescable: every event gets a unique key via counter.
            # We use the received-counter so ordering is preserved.
            unique_key = ("_raw_", self._events_received)
            self._coalesce_buf[unique_key] = event

    def _dispatch_to_subscribers(self, event: QueueEvent) -> None:
        """Call every live subscriber. Exceptions are isolated per-handler."""
        with self._subs_lock:
            strong = list(self._strong_subs)
            alive_weak: list[UiEventHandler] = []
            live_refs: list[weakref.WeakMethod] = []
            for ref in self._weak_subs:
                cb = ref()
                if cb is not None:
                    alive_weak.append(cb)
                    live_refs.append(ref)
            self._weak_subs = live_refs

        for cb in strong + alive_weak:
            try:
                cb(event)
            except Exception:
                self._log.exception(
                    "UI subscriber raised on %s (job_id=%s)",
                    event.type, event.job_id,
                )

    # ── Introspection ──

    def stats(self) -> dict:
        """For debug overlays and test assertions."""
        return {
            "running": self._running,
            "received": self._events_received,
            "coalesced": self._events_coalesced,
            "dispatched": self._events_dispatched,
            "inbox_size": self._inbox.qsize(),
            "buffer_size": len(self._coalesce_buf),
        }