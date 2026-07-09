"""
ui/components/progress_row.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Queue Job Progress Row

One row per in-flight or recently-completed queue job. Shown stacked
in the Manual Rip tab's queue drawer and anywhere else we need a
visual representation of pipeline progress.

Each row displays:
  • Display name (artist — title) once resolved, URL otherwise
  • Current stage label + percentage
  • A thin progress bar (accent-colored by source platform:
    blue for YouTube direct, purple for Discogs-originated)
  • Once analysis completes, BPM / Key / Camelot chips appear inline
  • Cancel button while in-flight; open-in-Vault button after completion
  • Status icon: spinner during work, check on success, × on failure

The row is event-driven: callers feed it QueueEvents via `apply_event`;
internal state rendering is derived entirely from the latest event's
fields. No polling, no queries — pure reactive rendering.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import customtkinter as ctk

from core.pipeline import PipelineStage
from core.queue_manager import QueueEvent, QueueEventType
from ui.components.spinner import Spinner
from ui.theme import (
    Theme,
    style_ghost_button,
    style_progress,
    style_progress_purple,
)


# Mapping from stage to user-facing label. Kept here (not in pipeline.py)
# because the UI may want different phrasing than the internal enum.
_STAGE_LABELS: dict[PipelineStage, str] = {
    PipelineStage.PENDING:           "Waiting…",
    PipelineStage.DOWNLOADING:       "Downloading",
    PipelineStage.ANALYZING:         "Analyzing BPM + key",
    PipelineStage.FETCHING_ARTWORK:  "Fetching artwork",
    PipelineStage.TAGGING:           "Writing metadata",
    PipelineStage.RELOCATING:        "Filing in vault",
    PipelineStage.INDEXING:          "Indexing",
    PipelineStage.SEPARATING_STEMS:  "Separating stems",
    PipelineStage.COMPLETE:          "Complete",
    PipelineStage.FAILED:            "Failed",
    PipelineStage.CANCELLED:         "Cancelled",
}


@dataclass(slots=True)
class ProgressRowState:
    """Latest known state for a row, derived from events."""
    job_id: int
    source_url: str
    display_name: Optional[str] = None
    stage: PipelineStage = PipelineStage.PENDING
    overall_percent: float = 0.0
    message: str = ""
    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    camelot_key: Optional[str] = None
    is_complete: bool = False
    is_failed: bool = False
    is_cancelled: bool = False
    error_message: Optional[str] = None
    track_id: Optional[int] = None
    final_path: Optional[str] = None
    is_discovery: bool = False       # affects progress bar color
    ai_enriched: bool = False        # shows ✦ AI chip on completion


class ProgressRow(ctk.CTkFrame):
    """
    One queue job's progress row. Call `apply_event(event)` whenever
    a new QueueEvent with matching job_id arrives.
    """

    _HEIGHT = 76

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        *,
        job_id: int,
        source_url: str,
        is_discovery: bool = False,
        ai_enriched: bool = False,
        on_cancel: Optional[Callable[[int], None]] = None,
        on_open_in_vault: Optional[Callable[[int], None]] = None,
        on_dismiss: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            fg_color=theme.surface.raised,
            border_color=theme.border.subtle,
            border_width=theme.stroke.hairline,
            corner_radius=theme.radius.md,
            height=self._HEIGHT,
        )
        self.grid_propagate(False)

        self._theme = theme
        self._state = ProgressRowState(
            job_id=job_id,
            source_url=source_url,
            is_discovery=is_discovery,
            ai_enriched=ai_enriched,
        )
        self._on_cancel = on_cancel
        self._on_open_in_vault = on_open_in_vault
        self._on_dismiss = on_dismiss

        self._build_body(theme)
        self._render()

    # ── Public API ──

    @property
    def job_id(self) -> int:
        return self._state.job_id

    @property
    def is_terminal(self) -> bool:
        """Once terminal, no further events will arrive for this job."""
        s = self._state
        return s.is_complete or s.is_failed or s.is_cancelled

    def apply_event(self, event: QueueEvent) -> None:
        """Update state from a QueueEvent and re-render."""
        if event.job_id != self._state.job_id:
            return

        s = self._state
        if event.display_name:
            s.display_name = event.display_name
        if event.stage is not None:
            s.stage = event.stage
        if event.overall_percent is not None:
            s.overall_percent = max(s.overall_percent, event.overall_percent)
        if event.message:
            s.message = event.message
        if event.bpm is not None:
            s.bpm = event.bpm
        if event.musical_key:
            s.musical_key = event.musical_key
        if event.camelot_key:
            s.camelot_key = event.camelot_key

        if event.type == QueueEventType.JOB_COMPLETED:
            s.is_complete = True
            s.track_id = event.track_id
            s.final_path = event.final_path
            s.overall_percent = 100.0
            s.stage = PipelineStage.COMPLETE
        elif event.type == QueueEventType.JOB_FAILED:
            s.is_failed = True
            s.error_message = event.error_message or event.message
            s.stage = PipelineStage.FAILED
        elif event.type == QueueEventType.JOB_CANCELLED:
            s.is_cancelled = True
            s.stage = PipelineStage.CANCELLED

        self._render()

    # ── Body construction ──

    def _build_body(self, theme: Theme) -> None:
        t = theme

        self.grid_columnconfigure(0, weight=0)   # status icon / spinner
        self.grid_columnconfigure(1, weight=1)   # label + progress
        self.grid_columnconfigure(2, weight=0)   # chips + actions

        # ── Status column ──
        self._status_frame = ctk.CTkFrame(self, fg_color="transparent", width=36)
        self._status_frame.grid(row=0, column=0, rowspan=2, sticky="nsew",
                                padx=(t.space.md, t.space.sm), pady=t.space.sm)
        self._status_frame.grid_propagate(False)

        self._spinner = Spinner(self._status_frame, t, size="md")
        self._spinner.place(relx=0.5, rely=0.5, anchor="center")

        # Terminal icon (replaces spinner on complete/failed)
        self._status_icon = ctk.CTkLabel(
            self._status_frame,
            text="",
            text_color=t.text.primary,
            font=t.font.subheading,
        )

        # ── Main body column ──
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=1, sticky="nsew", pady=(t.space.sm, 0))
        body.grid_columnconfigure(0, weight=1)

        self._title_label = ctk.CTkLabel(
            body,
            text="",
            text_color=t.text.primary,
            font=t.font.body_emphasis,
            anchor="w",
        )
        self._title_label.grid(row=0, column=0, sticky="ew",
                               padx=(0, t.space.md))

        meta_row = ctk.CTkFrame(body, fg_color="transparent")
        meta_row.grid(row=1, column=0, sticky="ew",
                      padx=(0, t.space.md), pady=(2, 0))
        meta_row.grid_columnconfigure(0, weight=1)

        self._stage_label = ctk.CTkLabel(
            meta_row, text="",
            text_color=t.text.secondary,
            font=t.font.caption,
            anchor="w",
        )
        self._stage_label.grid(row=0, column=0, sticky="w")

        self._percent_label = ctk.CTkLabel(
            meta_row, text="",
            text_color=t.text.secondary,
            font=t.font.mono_body,
            anchor="e",
        )
        self._percent_label.grid(row=0, column=1, sticky="e")

        # ── Progress bar ──
        self._progress_bar = ctk.CTkProgressBar(
            self, **(style_progress_purple(t) if self._state.is_discovery
                     else style_progress(t)),
        )
        self._progress_bar.grid(row=1, column=1, sticky="ew",
                                padx=(0, t.space.md),
                                pady=(t.space.xs, t.space.sm))
        self._progress_bar.set(0.0)

        # ── Right column: chips + actions ──
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=2, rowspan=2, sticky="nse",
                   padx=(0, t.space.md), pady=t.space.sm)

        self._chips_row = ctk.CTkFrame(right, fg_color="transparent")
        self._chips_row.pack(side="top", anchor="e")

        self._chip_bpm: Optional[ctk.CTkLabel] = None
        self._chip_key: Optional[ctk.CTkLabel] = None
        self._chip_ai: Optional[ctk.CTkLabel] = None

        self._actions_row = ctk.CTkFrame(right, fg_color="transparent")
        self._actions_row.pack(side="top", anchor="e", pady=(t.space.xs, 0))

        # Buttons created on demand in _render() based on state.
        self._btn_cancel: Optional[ctk.CTkButton] = None
        self._btn_open: Optional[ctk.CTkButton] = None
        self._btn_dismiss: Optional[ctk.CTkButton] = None

    # ── Rendering ──

    def _render(self) -> None:
        t = self._theme
        s = self._state

        # Title: display_name once known, else URL.
        title = s.display_name or s.source_url
        if len(title) > 70:
            title = title[:67] + "…"
        self._title_label.configure(text=title)

        # Stage line: prefer the live pipeline message (sub-step detail),
        # fall back to the static stage label when none has arrived yet.
        stage_text = self._status_text()
        if s.is_failed:
            self._stage_label.configure(
                text=stage_text,
                text_color=t.status.error,
            )
        elif s.is_cancelled:
            self._stage_label.configure(
                text=stage_text, text_color=t.text.muted,
            )
        else:
            self._stage_label.configure(
                text=stage_text, text_color=t.text.secondary,
            )

        pct_display = int(s.overall_percent)
        self._percent_label.configure(text=f"{pct_display}%")

        # Progress bar fill + color on terminal state
        self._progress_bar.set(max(0.0, min(1.0, s.overall_percent / 100.0)))
        if s.is_failed or s.is_cancelled:
            self._progress_bar.configure(
                progress_color=t.status.error if s.is_failed else t.text.muted,
            )

        # Status indicator: spinner during active work, icon on terminal.
        if self.is_terminal:
            self._spinner.stop()
            self._spinner.place_forget()
            if s.is_complete:
                glyph, color = "✓", t.status.success
            elif s.is_failed:
                glyph, color = "✕", t.status.error
            else:
                glyph, color = "⊘", t.text.muted
            self._status_icon.configure(text=glyph, text_color=color)
            self._status_icon.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self._status_icon.place_forget()
            if not self._spinner.winfo_ismapped():
                self._spinner.place(relx=0.5, rely=0.5, anchor="center")
            self._spinner.start()

        # BPM / Key chips — appear as analyzer reports them during the run.
        self._render_chips()

        # Action buttons
        self._render_actions()

    def _status_text(self) -> str:
        s = self._state
        if s.is_failed and s.error_message:
            stage_text = s.message or _STAGE_LABELS.get(s.stage, s.stage.value)
            return f"{stage_text}  ·  {s.error_message[:80]}"
        if s.message:
            return s.message
        return _STAGE_LABELS.get(s.stage, s.stage.value)

    def _render_chips(self) -> None:
        t = self._theme
        s = self._state

        # BPM chip
        if s.bpm is not None:
            text = f"{s.bpm:.1f} BPM"
            if self._chip_bpm is None:
                self._chip_bpm = self._make_chip(text, t.accent.blue)
                self._chip_bpm.pack(side="left", padx=(0, t.space.xs))
            else:
                self._chip_bpm.configure(text=text)

        # Key chip (prefer musical + camelot, fall back to whichever)
        key_text: Optional[str] = None
        if s.musical_key and s.camelot_key:
            key_text = f"{s.musical_key}  ·  {s.camelot_key}"
        elif s.musical_key:
            key_text = s.musical_key
        elif s.camelot_key:
            key_text = s.camelot_key

        if key_text:
            if self._chip_key is None:
                self._chip_key = self._make_chip(key_text, t.accent.purple)
                self._chip_key.pack(side="left", padx=(0, t.space.xs))
            else:
                self._chip_key.configure(text=key_text)

        # ✦ AI chip — appears only after the job completes so we know
        # AI enrichment was actually requested and ran for this track.
        if s.ai_enriched and s.is_complete:
            if self._chip_ai is None:
                self._chip_ai = self._make_chip("✦ AI", t.accent.blue)
                self._chip_ai.pack(side="left", padx=(0, t.space.xs))

    def _render_actions(self) -> None:
        t = self._theme
        s = self._state

        # Cancel button during active work
        if not self.is_terminal and self._on_cancel is not None:
            if self._btn_cancel is None:
                self._btn_cancel = ctk.CTkButton(
                    self._actions_row,
                    text="Cancel",
                    command=lambda: self._on_cancel(s.job_id),  # type: ignore[misc]
                    **style_ghost_button(t),
                )
                self._btn_cancel.pack(side="left")
        else:
            if self._btn_cancel is not None:
                self._btn_cancel.destroy()
                self._btn_cancel = None

        # Open-in-Vault button on successful completion
        if s.is_complete and self._on_open_in_vault is not None:
            if self._btn_open is None:
                self._btn_open = ctk.CTkButton(
                    self._actions_row,
                    text="Open in Vault",
                    command=lambda: self._on_open_in_vault(s.track_id)  # type: ignore[misc]
                                   if s.track_id else None,
                    **style_ghost_button(t),
                )
                self._btn_open.pack(side="left", padx=(t.space.xs, 0))

        # Dismiss button on terminal failure/cancellation
        if (s.is_failed or s.is_cancelled) and self._on_dismiss is not None:
            if self._btn_dismiss is None:
                self._btn_dismiss = ctk.CTkButton(
                    self._actions_row,
                    text="Dismiss",
                    command=lambda: self._on_dismiss(s.job_id),  # type: ignore[misc]
                    **style_ghost_button(t),
                )
                self._btn_dismiss.pack(side="left", padx=(t.space.xs, 0))

    def _make_chip(self, text: str, border_color: str) -> ctk.CTkLabel:
        """A small outlined pill showing a single metadata value."""
        t = self._theme
        return ctk.CTkLabel(
            self._chips_row,
            text=text,
            text_color=t.text.primary,
            font=t.font.mono_body,
            fg_color=t.surface.elevated,
            corner_radius=t.radius.pill,
            padx=10, pady=2,
            # Border "chip" — CTkLabel supports fg_color but not border;
            # the corner_radius + elevated fg gives a distinct chip look.
        )