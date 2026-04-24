"""
ui/tabs/manual_rip.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Manual Rip Tab

The simplest ingest surface: paste a URL, optionally toggle stems,
queue it. Shows a live queue drawer below the input with one
ProgressRow per active/recent job.

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │  Manual Rip                                                 │
  │  Paste a YouTube or YouTube Music URL to add a track to     │
  │  the vault.                                                 │
  │                                                             │
  │  ┌──────────────────────────────────────────────┐  [Queue]  │
  │  │ ▸ https://...                              × │           │
  │  └──────────────────────────────────────────────┘           │
  │  [ ] Split stems after download                             │
  │                                                             │
  │  ─── Queue ─────────────────────────────  [Clear completed] │
  │  ┌─────────────────────────────────────────────────────────┐│
  │  │ ✓  Pharoah Sanders — Harvest Time                       ││
  │  │    Complete  ·  94 BPM  ·  Am (8A)      [Open in Vault] ││
  │  ├─────────────────────────────────────────────────────────┤│
  │  │ ◐  Alice Coltrane — Turiya and Ramakrishna              ││
  │  │    Analyzing BPM + key  ·  42%                          ││
  │  └─────────────────────────────────────────────────────────┘│
  └─────────────────────────────────────────────────────────────┘

The tab owns a dict of `job_id → ProgressRow` and dispatches incoming
events to the correct row. Rows are created on JOB_ENQUEUED and never
destroyed until the user clicks "Dismiss" or "Clear completed".
"""

from __future__ import annotations

import re
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import customtkinter as ctk

from core.pipeline import PipelineRequest
from core.queue_manager import QueueEvent, QueueEventType
from core.stems import StemModel
from ui.components.glow_entry import GlowEntry
from ui.components.progress_row import ProgressRow
from ui.theme import (
    style_card,
    style_card_elevated,
    style_ghost_button,
    style_label_body,
    style_label_heading,
    style_label_meta,
    style_label_subheading,
    style_primary_button,
)

if TYPE_CHECKING:
    from ui.app import AppContext


# Quick URL recognizer — strict pattern; Downloader does the real validation.
_URL_RE = re.compile(
    r"^https?://"
    r"(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?v=|shorts/|live/)[A-Za-z0-9_-]{6,}"
    r"|youtu\.be/[A-Za-z0-9_-]{6,})"
    r"(?:[?&#].*)?$",
    re.IGNORECASE,
)


class ManualRipTab(ctk.CTkFrame):
    """The URL-paste ingest tab."""

    def __init__(self, parent: ctk.CTkBaseClass, ctx: "AppContext") -> None:
        super().__init__(parent, fg_color=ctx.theme.surface.app)

        self._ctx = ctx
        self._theme = ctx.theme
        self._log = ctx.logger.getChild("manual_rip")

        # job_id → ProgressRow
        self._rows: dict[int, ProgressRow] = {}
        # Remember job_ids we've dismissed so late-arriving events don't
        # resurrect them.
        self._dismissed: set[int] = set()

        self._build_body()

        # Subscribe to the UI-safe event bridge. weak=True so if this
        # tab were ever destroyed, the subscription clears automatically.
        ctx.event_bridge.subscribe(self._on_queue_event, weak=True)

    # ── Body construction ──

    def _build_body(self) -> None:
        t = self._theme

        # Outer grid: content row expands, one column centers content.
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Scrollable content area so the queue drawer can grow arbitrarily
        # without pushing the URL input off-screen.
        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=t.surface.app,
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=t.border.strong,
            scrollbar_button_hover_color=t.accent.blue,
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        # Content inside the scrollable frame is width-capped + centered.
        content = ctk.CTkFrame(scroll, fg_color="transparent")
        content.grid(
            row=0,
            column=0,
            padx=t.space.xl,
            pady=(t.space.xxl, t.space.xl),
            sticky="ew",
        )
        content.grid_columnconfigure(0, weight=1)

        # Cap the content width so input doesn't stretch across a huge
        # monitor. The tab reads more intentional at a controlled width.
        self._content = content
        content.bind("<Configure>", self._on_content_configure)

        # ── Heading ──
        ctk.CTkLabel(
            content,
            text="Manual Rip",
            **style_label_heading(t),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            content,
            text=(
                "Paste a YouTube or YouTube Music URL to add a track to "
                "the vault. We'll download the native AAC stream, "
                "analyze BPM + key, and file it automatically."
            ),
            **style_label_meta(t),
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xs, t.space.xl))

        # ── Input card ──
        self._build_input_card(content, row=2)

        # ── Queue drawer ──
        self._build_queue_drawer(content, row=3)

    def _build_input_card(self, parent, row: int) -> None:
        t = self._theme

        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=row, column=0, sticky="ew", pady=(0, t.space.xl))
        card.grid_columnconfigure(0, weight=1)

        # Padding wrapper
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl, pady=t.space.xl)
        inner.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            inner,
            text="Source URL",
            **style_label_subheading(t),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, t.space.sm))

        # URL + Queue button row
        input_row = ctk.CTkFrame(inner, fg_color="transparent")
        input_row.grid(row=1, column=0, columnspan=2, sticky="ew")
        input_row.grid_columnconfigure(0, weight=1)

        self._url_entry = GlowEntry(
            input_row,
            t,
            placeholder="https://music.youtube.com/watch?v=…",
            prefix_icon="▸",
            validator=_is_supported_url,
            error_message="Paste a YouTube or YouTube Music URL.",
            on_submit=self._on_submit,
        )
        self._url_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.md))

        self._submit_button = ctk.CTkButton(
            input_row,
            text="Queue",
            command=self._on_queue_button_clicked,
            **style_primary_button(t),
            width=124,
        )
        self._submit_button.configure(height=48)
        self._submit_button.grid(row=0, column=1, sticky="e")

        # Error label slot. GlowEntry paints its own border red, but
        # a text message below gives users a clear reason on first use.
        self._error_label = ctk.CTkLabel(
            inner,
            text="",
            text_color=t.status.error,
            font=t.font.micro,
            anchor="w",
        )
        self._error_label.grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(t.space.xs, 0)
        )
        self._error_label.grid_remove()

        # Stems toggle row
        toggle_row = ctk.CTkFrame(inner, fg_color="transparent")
        toggle_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(t.space.lg, 0))

        # Switch default comes from user config so the Settings tab
        # preference is honored immediately on tab open.
        initial_stems = (
            self._ctx.config.snapshot().config.general.enable_stems_by_default
        )
        self._stems_var = ctk.BooleanVar(value=bool(initial_stems))

        self._stems_switch = ctk.CTkSwitch(
            toggle_row,
            text="",
            variable=self._stems_var,
            onvalue=True,
            offvalue=False,
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated,
            width=40,
            height=22,
        )
        self._stems_switch.pack(side="left", padx=(0, t.space.md))

        stems_labels = ctk.CTkFrame(toggle_row, fg_color="transparent")
        stems_labels.pack(side="left")

        ctk.CTkLabel(
            stems_labels,
            text="Split stems after download",
            **style_label_body(t),
        ).pack(anchor="w")

        self._stems_meta_label = ctk.CTkLabel(
            stems_labels,
            text=self._stems_meta_text(),
            **style_label_meta(t),
        )
        self._stems_meta_label.pack(anchor="w")

    def _build_queue_drawer(self, parent, row: int) -> None:
        t = self._theme

        # Section header with action
        header_row = ctk.CTkFrame(parent, fg_color="transparent")
        header_row.grid(row=row, column=0, sticky="ew", pady=(0, t.space.md))
        header_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header_row,
            text="Queue",
            **style_label_subheading(t),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header_row,
            text="Clear completed",
            command=self._clear_completed,
            **style_ghost_button(t),
        ).grid(row=0, column=1, sticky="e")

        # Empty-state card — replaced by rows as they arrive.
        self._empty_state = ctk.CTkFrame(parent, **style_card(t))
        self._empty_state.grid(row=row + 1, column=0, sticky="ew")
        self._empty_state.grid_columnconfigure(0, weight=1)

        empty_inner = ctk.CTkFrame(self._empty_state, fg_color="transparent")
        empty_inner.grid(row=0, column=0, pady=t.space.xxxl)

        ctk.CTkLabel(
            empty_inner,
            text="No jobs yet.",
            text_color=t.text.secondary,
            font=t.font.subheading,
        ).pack()

        ctk.CTkLabel(
            empty_inner,
            text="Paste a URL above and click Queue to ingest your first track.",
            **style_label_meta(t),
        ).pack(pady=(t.space.xs, 0))

        # Container that holds the actual ProgressRows.
        self._rows_container = ctk.CTkFrame(parent, fg_color="transparent")
        self._rows_container.grid(row=row + 1, column=0, sticky="ew")
        self._rows_container.grid_columnconfigure(0, weight=1)
        # Kept hidden until we have at least one row.
        self._rows_container.grid_remove()

    # ── User actions ──

    def _on_queue_button_clicked(self) -> None:
        """Explicit button click — delegates to the same submit path."""
        value = self._url_entry.get().strip()
        self._on_submit(value)

    def _on_submit(self, value: str) -> None:
        """Validated submit path — called by button and by Enter key."""
        value = value.strip()
        if not value:
            self._show_inline_error("Paste a URL first.")
            return

        if not _is_supported_url(value):
            self._show_inline_error(
                "That doesn't look like a supported YouTube or YouTube Music URL."
            )
            # Let the GlowEntry paint the red border too.
            self._url_entry.validate()
            return

        self._hide_inline_error()

        # Resolve the stems model from user config, not hard-coded default.
        # This lets Settings's model dropdown flow through immediately.
        snap = self._ctx.config.snapshot()
        stem_model_raw = snap.config.stems.model
        try:
            stem_model = StemModel(stem_model_raw)
        except ValueError:
            self._log.warning(
                "Invalid stem model in config: %r — falling back to htdemucs_ft",
                stem_model_raw,
            )
            stem_model = StemModel.HTDEMUCS_FT

        request = PipelineRequest(
            source_url=value,
            enable_stems=bool(self._stems_var.get()),
            stem_model=stem_model,
        )

        try:
            self._ctx.queue_manager.enqueue(request)
        except Exception as e:
            self._log.exception("Enqueue failed")
            self._ctx.publish_toast(f"Could not enqueue: {e}", kind="error")
            return

        # Clear the input so the user can paste the next URL without
        # having to select-all-delete. The focus stays on the entry.
        self._url_entry.clear()
        self._url_entry.focus()

    def _clear_completed(self) -> None:
        """Remove terminal rows from the drawer."""
        removed = 0
        for job_id, row in list(self._rows.items()):
            if row.is_terminal:
                try:
                    row.destroy()
                except Exception:
                    pass
                self._rows.pop(job_id, None)
                self._dismissed.add(job_id)
                removed += 1

        if removed:
            self._ctx.publish_toast(
                f"Cleared {removed} completed job{'s' if removed != 1 else ''}.",
                kind="info",
            )

        self._reflow_rows()

    def _cancel_job(self, job_id: int) -> None:
        """ProgressRow cancel-button callback."""
        if self._ctx.queue_manager.cancel(job_id):
            self._log.info("Cancel requested for job %d", job_id)
        else:
            self._ctx.publish_toast(
                "Could not cancel — job may have finished already.",
                kind="warning",
            )

    def _dismiss_row(self, job_id: int) -> None:
        """ProgressRow dismiss-button callback (terminal-only)."""
        row = self._rows.pop(job_id, None)
        if row is not None:
            try:
                row.destroy()
            except Exception:
                pass
            self._dismissed.add(job_id)
            self._reflow_rows()

    def _open_in_vault(self, track_id: Optional[int]) -> None:
        """
        ProgressRow "Open in Vault" callback. For MVP, this opens the
        containing folder in the OS file manager. Once the Vault tab
        is built, we'll switch this to selecting-and-focusing the row.
        """
        if track_id is None:
            return
        try:
            track = self._ctx.database.get_track(track_id)
        except Exception as e:
            self._log.warning("Could not load track %d: %s", track_id, e)
            return

        parent_dir = Path(track.file_path).parent
        try:
            webbrowser.open(parent_dir.as_uri())
        except Exception as e:
            self._log.warning("Could not open %s: %s", parent_dir, e)
            self._ctx.publish_toast(
                f"Could not open folder: {e}",
                kind="error",
            )

    # ── Event handling (runs on Tk thread via bridge) ──

    def _on_queue_event(self, event: QueueEvent) -> None:
        """
        Dispatch a QueueEvent to the appropriate ProgressRow, creating
        one if needed. Called on the Tk thread.
        """
        # Some events (QUEUE_DRAINED, WORKER_ERROR) don't map to a
        # specific job — the sidebar status handler in app.py owns those.
        if event.job_id is None:
            return

        # Don't resurrect dismissed rows when late events arrive.
        if event.job_id in self._dismissed:
            return

        row = self._rows.get(event.job_id)

        if row is None:
            # First event for this job. JOB_ENQUEUED is the canonical
            # creator, but if a worker was faster than the bridge we
            # might see JOB_STARTED first — either way, create the row.
            if event.type not in (
                QueueEventType.JOB_ENQUEUED,
                QueueEventType.JOB_STARTED,
                QueueEventType.JOB_PROGRESS,
            ):
                # FAILED/COMPLETED/CANCELLED without a row — job was
                # enqueued before this tab subscribed (unlikely, but
                # be robust). Create the row and immediately apply.
                pass
            row = self._create_row(event)

        row.apply_event(event)

    def _create_row(self, event: QueueEvent) -> ProgressRow:
        """Instantiate a ProgressRow, insert at the top of the stack."""
        t = self._theme
        # Discovery-originated jobs get the purple progress bar variant.
        is_discovery = False  # Manual Rip is always direct submissions

        row = ProgressRow(
            self._rows_container,
            t,
            job_id=event.job_id,  # type: ignore[arg-type]
            source_url=event.source_url or "",
            is_discovery=is_discovery,
            on_cancel=self._cancel_job,
            on_open_in_vault=self._open_in_vault,
            on_dismiss=self._dismiss_row,
        )
        self._rows[event.job_id] = row  # type: ignore[assignment]

        self._reflow_rows()
        return row

    def _reflow_rows(self) -> None:
        """
        Re-grid rows in newest-first order and toggle empty-state
        visibility. Called whenever rows are added or removed.
        """
        t = self._theme

        if not self._rows:
            self._rows_container.grid_remove()
            self._empty_state.grid()
            return

        self._empty_state.grid_remove()
        self._rows_container.grid()

        # Sort newest first by job_id (monotonically increasing).
        ordered = sorted(self._rows.items(), key=lambda kv: kv[0], reverse=True)

        for i, (_jid, row) in enumerate(ordered):
            row.grid(
                row=i, column=0, sticky="ew", pady=(0 if i == 0 else t.space.sm, 0)
            )

    # ── Inline error plumbing ──

    def _show_inline_error(self, message: str) -> None:
        self._error_label.configure(text=message)
        self._error_label.grid()

    def _hide_inline_error(self) -> None:
        self._error_label.configure(text="")
        self._error_label.grid_remove()

    # ── Helpers ──

    def _stems_meta_text(self) -> str:
        """Meta caption beneath the stems toggle — mentions the model."""
        snap = self._ctx.config.snapshot()
        model = snap.config.stems.model
        return (
            f"Separates into vocals, drums, bass, and other using "
            f"{model}. Adds several minutes per track."
        )

    def _on_content_configure(self, _event) -> None:
        """Cap content width at 960px regardless of window size."""
        t = self._theme
        parent_width = self._content.master.winfo_width()
        target = min(960, max(480, parent_width - 2 * t.space.xl))
        # Only re-apply if changed meaningfully, to avoid reflow loops.
        current_width = self._content.winfo_width()
        if abs(current_width - target) > 8:
            self._content.configure(width=target)


# ─── Helpers ─────────────────────────────────────────────────────────


def _is_supported_url(value: str) -> bool:
    """Lightweight front-door validator. Downloader does the real check."""
    value = (value or "").strip()
    if not value:
        return False
    return bool(_URL_RE.match(value))
