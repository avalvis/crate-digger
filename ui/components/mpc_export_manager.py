"""
ui/components/mpc_export_manager.py
──────────────────────────────────────────────────────────────────────
Single minimizable window for all Digital Crate MPC exports.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk

from core.mpc_export import MpcExportMode
from core.mpc_export_manager import MpcExportEvent, MpcExportEventType
from ui.theme import (
    Theme,
    style_danger_button,
    style_ghost_button,
    style_label_meta,
    style_secondary_button,
)

if TYPE_CHECKING:
    from ui.app import AppContext


_MODE_LABELS = {
    MpcExportMode.SONG: "SONG",
    MpcExportMode.STEMS: "STEMS",
    MpcExportMode.BOTH: "BOTH",
}


@dataclass(slots=True)
class _RowState:
    job_id: str
    display_name: str
    mode: MpcExportMode
    message: str = "Queued"
    percent: float = 0.0
    finished: bool = False
    failed: bool = False
    cancelled: bool = False


class _MpcExportRow(ctk.CTkFrame):
    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        *,
        job_id: str,
        display_name: str,
        mode: MpcExportMode,
        on_cancel: Callable[[str], None],
    ) -> None:
        super().__init__(
            parent,
            fg_color=theme.surface.raised,
            border_color=theme.border.subtle,
            border_width=theme.stroke.hairline,
            corner_radius=theme.radius.md,
        )
        t = theme
        self._theme = theme
        self._job_id = job_id
        self._on_cancel = on_cancel
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=t.space.md, pady=(t.space.sm, 0))
        top.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top, text=display_name, text_color=t.text.primary,
            font=t.font.body_emphasis, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            top, text=_MODE_LABELS.get(mode, mode.value.upper()),
            text_color=t.accent.purple, font=t.font.micro,
        ).grid(row=0, column=1, sticky="e", padx=(t.space.sm, 0))

        self._status = ctk.CTkLabel(
            top, text="Queued", text_color=t.text.secondary,
            font=t.font.caption, anchor="w",
        )
        self._status.grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        self._bar = ctk.CTkProgressBar(
            self, height=8, progress_color=t.accent.purple,
        )
        self._bar.grid(
            row=1, column=0, sticky="ew",
            padx=t.space.md, pady=(t.space.xs, t.space.sm),
        )
        self._bar.set(0.0)

        self._cancel_btn = ctk.CTkButton(
            self, text="Cancel", width=72, height=28,
            command=lambda: on_cancel(job_id),
            **style_ghost_button(t),
        )
        self._cancel_btn.grid(row=2, column=0, sticky="e", padx=t.space.md,
                              pady=(0, t.space.sm))

    def apply_event(self, event: MpcExportEvent) -> None:
        t = self._theme
        if event.type == MpcExportEventType.PROGRESS:
            self._status.configure(text=event.message or "Working…")
            self._bar.set(max(0.0, min(1.0, event.percent / 100.0)))
        elif event.type == MpcExportEventType.STARTED:
            self._status.configure(text=event.message or "Starting…")
        elif event.type == MpcExportEventType.COMPLETED:
            self._status.configure(
                text="Complete", text_color=t.status.success,
            )
            self._bar.set(1.0)
            self._cancel_btn.configure(state="disabled")
        elif event.type == MpcExportEventType.FAILED:
            self._status.configure(
                text=event.error_message or "Failed",
                text_color=t.status.error,
            )
            self._cancel_btn.configure(state="disabled")
        elif event.type == MpcExportEventType.CANCELLED:
            self._status.configure(text="Cancelled", text_color=t.text.muted)
            self._cancel_btn.configure(state="disabled")


class MpcExportManagerWindow(ctk.CTkToplevel):
    """Non-modal MPC export queue — minimizable to a bottom-right bar."""

    _WIDTH = 480
    _HEIGHT = 420
    _MINI_HEIGHT = 48

    def __init__(self, parent: ctk.CTkBaseClass, ctx: "AppContext") -> None:
        super().__init__(parent)
        self._ctx = ctx
        self._theme = ctx.theme
        t = self._theme
        self._rows: dict[str, _MpcExportRow] = {}
        self._minimized = False
        self._body: Optional[ctk.CTkFrame] = None
        self._mini_bar: Optional[ctk.CTkFrame] = None
        self._summary_label: Optional[ctk.CTkLabel] = None
        self._dest_label: Optional[ctk.CTkLabel] = None

        self.title("MPC Exports")
        self.configure(fg_color=t.surface.base)
        self.resizable(False, False)
        self.attributes("-topmost", False)

        self._build()
        self._position_bottom_right()
        self._refresh_summary()
        self._unsub = ctx.mpc_export_manager.subscribe(
            self._on_event, weak=True,
        )
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def lift_window(self) -> None:
        if self._minimized:
            self._restore()
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _build(self) -> None:
        t = self._theme
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")

        self._mini_bar = ctk.CTkFrame(
            self, fg_color=t.surface.elevated,
            corner_radius=t.radius.md, height=self._MINI_HEIGHT,
        )
        self._mini_bar.pack(fill="x", side="bottom", padx=t.space.sm, pady=t.space.sm)
        self._mini_bar.pack_propagate(False)
        self._mini_bar.bind("<Button-1>", lambda _e: self._restore())

        self._summary_label = ctk.CTkLabel(
            self._mini_bar, text="◈ MPC: idle",
            text_color=t.text.primary, font=t.font.body_emphasis,
            anchor="w",
        )
        self._summary_label.pack(side="left", padx=t.space.md, fill="y")
        self._summary_label.bind("<Button-1>", lambda _e: self._restore())

        ctk.CTkButton(
            self._mini_bar, text="▲", width=32, height=28,
            command=self._restore, **style_ghost_button(t),
        ).pack(side="right", padx=(0, t.space.xs))
        self._mini_bar.pack_forget()

        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=t.space.lg, pady=t.space.lg)
        body = self._body
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text="MPC Exports",
            text_color=t.text.primary, font=t.font.subheading,
        ).grid(row=0, column=0, sticky="w")

        dest = self._ctx.config.snapshot().config.general.mpc_samples_root
        short = self._truncate_path(Path(dest).expanduser())
        self._dest_label = ctk.CTkLabel(
            header, text=f"→ {short}",
            text_color=t.text.muted, font=t.font.caption, anchor="w",
        )
        self._dest_label.grid(row=1, column=0, sticky="w", pady=(2, t.space.sm))

        btn_row = ctk.CTkFrame(header, fg_color="transparent")
        btn_row.grid(row=0, column=1, rowspan=2, sticky="ne")
        ctk.CTkButton(
            btn_row, text="—", width=32, height=28,
            command=self._minimize, **style_ghost_button(t),
        ).pack(side="left", padx=(0, t.space.xs))
        ctk.CTkButton(
            btn_row, text="Clear finished", width=110, height=28,
            command=self._clear_finished, **style_secondary_button(t),
        ).pack(side="left")

        self._list = ctk.CTkScrollableFrame(
            body, fg_color=t.surface.app,
            corner_radius=t.radius.md,
            height=280,
        )
        self._list.grid(row=2, column=0, sticky="nsew", pady=(t.space.sm, 0))
        self._list.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            body, text="Close", command=self._on_close,
            **style_secondary_button(t), width=100,
        ).grid(row=3, column=0, sticky="e", pady=(t.space.md, 0))

    def _truncate_path(self, path: Path, max_len: int = 42) -> str:
        s = str(path)
        if len(s) <= max_len:
            return s
        return "…" + s[-(max_len - 1):]

    def _position_bottom_right(self) -> None:
        self.update_idletasks()
        try:
            parent = self.master
            px = parent.winfo_rootx()
            py = parent.winfo_rooty()
            pw = parent.winfo_width()
            ph = parent.winfo_height()
            w = self._WIDTH
            h = self._HEIGHT if not self._minimized else self._MINI_HEIGHT + 16
            x = px + pw - w - 24
            y = py + ph - h - 24
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    def _on_event(self, event: MpcExportEvent) -> None:
        self.after(0, lambda e=event: self._apply_event_ui(e))

    def _apply_event_ui(self, event: MpcExportEvent) -> None:
        if event.type == MpcExportEventType.ENQUEUED:
            if event.job_id not in self._rows:
                row = _MpcExportRow(
                    self._list,
                    self._theme,
                    job_id=event.job_id,
                    display_name=event.display_name,
                    mode=event.mode,
                    on_cancel=self._cancel_job,
                )
                row.grid(
                    row=len(self._rows), column=0, sticky="ew",
                    pady=(0, self._theme.space.sm),
                )
                self._rows[event.job_id] = row
        elif event.job_id in self._rows:
            self._rows[event.job_id].apply_event(event)
        self._refresh_summary()

    def _cancel_job(self, job_id: str) -> None:
        if self._ctx.mpc_export_manager is not None:
            self._ctx.mpc_export_manager.cancel_job(job_id)

    def _clear_finished(self) -> None:
        mgr = self._ctx.mpc_export_manager
        if mgr is None:
            return
        finished_ids = [
            jid for jid, row in self._rows.items()
            if row._cancel_btn.cget("state") == "disabled"
        ]
        mgr.clear_finished()
        for jid in finished_ids:
            row = self._rows.pop(jid, None)
            if row is not None:
                try:
                    row.destroy()
                except Exception:
                    pass
        self._repack_rows()

    def _repack_rows(self) -> None:
        for i, row in enumerate(self._rows.values()):
            row.grid(row=i, column=0, sticky="ew", pady=(0, self._theme.space.sm))

    def _refresh_summary(self) -> None:
        mgr = self._ctx.mpc_export_manager
        if mgr is None or self._summary_label is None:
            return
        running, queued = mgr.counts()
        if running == 0 and queued == 0:
            text = "◈ MPC: idle"
        else:
            parts = []
            if running:
                parts.append(f"{running} running")
            if queued:
                parts.append(f"{queued} queued")
            text = f"◈ MPC: {', '.join(parts)}"
        self._summary_label.configure(text=text)

    def _minimize(self) -> None:
        self._minimized = True
        if self._body is not None:
            self._body.pack_forget()
        if self._mini_bar is not None:
            self._mini_bar.pack(fill="x", side="bottom", padx=self._theme.space.sm,
                                pady=self._theme.space.sm)
        self.geometry(f"{self._WIDTH}x{self._MINI_HEIGHT + 16}")
        self._position_bottom_right()

    def _restore(self) -> None:
        self._minimized = False
        if self._mini_bar is not None:
            self._mini_bar.pack_forget()
        if self._body is not None:
            self._body.pack(fill="both", expand=True,
                            padx=self._theme.space.lg, pady=self._theme.space.lg)
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self._position_bottom_right()

    def _on_close(self) -> None:
        mgr = self._ctx.mpc_export_manager
        running, queued = (0, 0) if mgr is None else mgr.counts()
        if running or queued:
            self._minimize()
            return
        try:
            self._unsub()
        except Exception:
            pass
        self.destroy()

    def destroy(self) -> None:  # type: ignore[override]
        try:
            self._unsub()
        except Exception:
            pass
        super().destroy()
