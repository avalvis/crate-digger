"""
ui/components/spinner.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Indeterminate Progress Spinner

A small rotating arc used for operations without known total duration
(Discogs Dig requests, ffmpeg launch, model download). Renders on a
CTkCanvas because CTk doesn't ship a native spinner and drawing one
with CTkProgressBar in indeterminate mode looks cheap.

Implementation notes:
  • Canvas-based arc rotation — tight 12-frame animation loop at ~30fps.
  • Accent-colored arc on a transparent canvas; the "trail" is a
    faded version of the accent, producing a subtle motion smear
    without needing a real trail algorithm.
  • `start()` / `stop()` manage the animation; `stop()` cancels the
    `after` timer and hides the canvas. Idempotent.
  • Automatic pause on widget unmap — if the tab this spinner sits
    on gets hidden, we stop animating to save CPU.
"""

from __future__ import annotations

from typing import Optional

import customtkinter as ctk

from ui.theme import Theme


class Spinner(ctk.CTkFrame):
    """
    A compact indeterminate spinner. Size is fixed at construction
    (one of: `sm`=16px, `md`=24px, `lg`=32px). No scaling during
    animation — canvas redraws are cheap but avoid unnecessary cost
    by keeping dimensions static.
    """

    _SIZES = {"sm": 16, "md": 24, "lg": 32}
    _TICK_MS = 33  # ~30fps
    _DEGREES_PER_TICK = 12  # 360/30 → full rotation in 1s

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        *,
        size: str = "md",
        color: Optional[str] = None,
    ) -> None:
        px = self._SIZES.get(size, 24)
        self._theme = theme
        self._size = px
        self._color = color or theme.accent.blue
        self._angle = 0
        self._running = False
        self._after_handle: Optional[str] = None
        super().__init__(
            parent,
            width=px,
            height=px,
            fg_color="transparent",
            border_width=0,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)

        # CTkCanvas doesn't exist — use plain tk.Canvas with themed bg.
        # We read the parent's bg via CTk's resolved fg_color at runtime
        # so the spinner blends seamlessly against its container.
        import tkinter as tk

        self._canvas = tk.Canvas(
            self,
            width=px,
            height=px,
            highlightthickness=0,
            bd=0,
            bg=self._resolve_parent_bg(),
        )
        self._canvas.pack(fill="both", expand=True)

        # Draw initial frame so the spinner is visible even before start()
        self._draw()

        # Auto-pause when unmapped (hidden tab), auto-resume when re-mapped.
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Unmap>", self._on_unmap, add="+")

    # ── Public API ──

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tick()

    def stop(self) -> None:
        self._running = False
        if self._after_handle is not None:
            try:
                self.after_cancel(self._after_handle)
            except Exception:
                pass
            self._after_handle = None

    # ── Internals ──

    def _tick(self) -> None:
        if not self._running:
            return
        self._angle = (self._angle + self._DEGREES_PER_TICK) % 360
        self._draw()
        self._after_handle = self.after(self._TICK_MS, self._tick)

    def _draw(self, *args, **kwargs) -> None:
        if not hasattr(self, "_canvas"):
            return
        self._canvas.delete("all")
        size = self._size
        pad = max(2, size // 8)
        box = (pad, pad, size - pad, size - pad)

        # Background ring (very faint accent — provides context for the arc).
        self._canvas.create_arc(
            *box,
            start=0,
            extent=359,
            style="arc",
            outline=self._fade(self._color, 0.15),
            width=max(2, size // 10),
        )

        # Foreground arc — 90° sweep rotating through _angle.
        # Tk's arc `start` is degrees counter-clockwise from 3 o'clock.
        self._canvas.create_arc(
            *box,
            start=-self._angle,
            extent=-90,
            style="arc",
            outline=self._color,
            width=max(2, size // 10),
        )

    def _on_map(self, _event) -> None:
        # When remounted, refresh parent bg in case theme changed.
        self._canvas.configure(bg=self._resolve_parent_bg())
        if self._running:
            # Was running prior to unmap; resume.
            self._tick()

    def _on_unmap(self, _event) -> None:
        # Cancel the pending tick but keep _running=True so remap resumes.
        if self._after_handle is not None:
            try:
                self.after_cancel(self._after_handle)
            except Exception:
                pass
            self._after_handle = None

    def _resolve_parent_bg(self) -> str:
        """
        The tk.Canvas doesn't support 'transparent'. Match its bg to
        the CTk-resolved fg_color of the parent so it blends in.
        """
        parent = self.master
        try:
            # CTk's fg_color can be a tuple (light, dark) or a single hex.
            fg = parent.cget("fg_color")
            if isinstance(fg, (list, tuple)):
                return fg[1] if len(fg) > 1 else fg[0]
            if isinstance(fg, str) and fg.startswith("#"):
                return fg
        except Exception:
            pass
        # Fallback to the theme's base surface.
        return self._theme.surface.base

    @staticmethod
    def _fade(hex_color: str, alpha: float) -> str:
        """
        Tk can't composite alpha on a Canvas outline. Fake it by
        blending the color toward the dark surface at the given alpha.
        Accepts 0.0..1.0; lower = fainter.
        """
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except Exception:
            return hex_color
        # Blend toward #080604 (our warm surface.app).
        br, bg_, bb = 0x08, 0x06, 0x04
        rr = int(r * alpha + br * (1 - alpha))
        rg = int(g * alpha + bg_ * (1 - alpha))
        rb = int(b * alpha + bb * (1 - alpha))
        return f"#{rr:02X}{rg:02X}{rb:02X}"
