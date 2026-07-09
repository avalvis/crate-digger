"""
ui/components/dig_roulette_spinner.py
──────────────────────────────────────────────────────────────────────
Vinyl roulette indicator for the Digital Crate "Dig" operation.

A miniature spinning record with groove rings and an amber label —
matches the app's identity mark. Hidden when idle; animates only while
a dig is in flight.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk
import tkinter as tk

from ui.theme import Theme


class DigRouletteSpinner(ctk.CTkFrame):
    """Spinning vinyl indicator used during Discogs/YTM discovery."""

    _SIZE = 44
    _TICK_MS = 28
    _DEGREES_PER_TICK = 14

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        *,
        size: int = _SIZE,
    ) -> None:
        self._theme = theme
        self._size = size
        self._angle = 0.0
        self._running = False
        self._visible = False
        self._after_handle: Optional[str] = None
        self._pulse_phase = 0

        super().__init__(
            parent,
            width=size + 8,
            height=size + 8,
            fg_color="transparent",
            border_width=0,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._canvas = tk.Canvas(
            self,
            width=size + 8,
            height=size + 8,
            highlightthickness=0,
            bd=0,
            bg=self._resolve_parent_bg(),
        )
        self._canvas.pack(fill="both", expand=True)

        # Start hidden — only shown during an active dig.
        self.pack_forget()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._show()
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_handle is not None:
            try:
                self.after_cancel(self._after_handle)
            except Exception:
                pass
            self._after_handle = None
        self._hide()

    # ── Animation ──

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            if not self.winfo_exists():
                self.stop()
                return
        except Exception:
            self.stop()
            return
        self._angle = (self._angle + self._DEGREES_PER_TICK) % 360.0
        self._pulse_phase = (self._pulse_phase + 1) % 24
        self._paint_vinyl()
        self._after_handle = self.after(self._TICK_MS, self._tick)

    def _draw(self, no_color_updates: bool = False) -> None:
        """CustomTkinter internal hook — must not clash with canvas painting."""
        super()._draw(no_color_updates=no_color_updates)

    def _paint_vinyl(self) -> None:
        c = self._canvas
        c.delete("all")
        t = self._theme
        size = self._size
        cx = cy = (size + 8) // 2

        # Outer glow pulse while spinning.
        glow_alpha = 0.25 + 0.15 * abs((self._pulse_phase % 12) - 6) / 6
        glow_r = size // 2 + 2
        c.create_oval(
            cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r,
            fill="",
            outline=self._fade(t.accent.purple, glow_alpha),
            width=2,
        )

        # Vinyl body (rotates as a group via canvas tag).
        tag = "vinyl"
        body_r = size // 2 - 2
        c.create_oval(
            cx - body_r, cy - body_r, cx + body_r, cy + body_r,
            fill="#191410",
            outline=t.border.strong,
            width=1,
            tags=tag,
        )

        for shrink in (5, 9, 13):
            r = body_r - shrink
            if r > 6:
                c.create_oval(
                    cx - r, cy - r, cx + r, cy + r,
                    fill="",
                    outline="#231D13",
                    width=1,
                    tags=tag,
                )

        # Center label
        lr = max(6, body_r // 3)
        c.create_oval(
            cx - lr, cy - lr, cx + lr, cy + lr,
            fill="#7A5518",
            outline="#9A6D28",
            width=1,
            tags=tag,
        )
        c.create_oval(
            cx - 2, cy - 2, cx + 2, cy + 2,
            fill=t.surface.app,
            outline="",
            tags=tag,
        )

        c.tag_bind(tag, "<Button-1>", lambda _e: None)

    def _show(self) -> None:
        if self._visible:
            return
        self._visible = True
        self.pack(side="left", padx=(0, 8))

    def _hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self.pack_forget()
        self._canvas.delete("all")

    def _resolve_parent_bg(self) -> str:
        parent = self.master
        try:
            fg = parent.cget("fg_color")
            if isinstance(fg, (list, tuple)):
                return fg[1] if len(fg) > 1 else fg[0]
            if isinstance(fg, str) and fg.startswith("#"):
                return fg
        except Exception:
            pass
        return self._theme.surface.app

    @staticmethod
    def _fade(hex_color: str, alpha: float) -> str:
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except Exception:
            return hex_color
        br, bg_, bb = 0x08, 0x06, 0x04
        rr = int(r * alpha + br * (1 - alpha))
        rg = int(g * alpha + bg_ * (1 - alpha))
        rb = int(b * alpha + bb * (1 - alpha))
        return f"#{rr:02X}{rg:02X}{rb:02X}"
