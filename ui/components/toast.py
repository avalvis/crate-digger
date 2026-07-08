"""
ui/components/toast.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Toast Notifications

Non-blocking status messages that slide in from the top-right of the
content area, stack vertically, and auto-dismiss after a timeout.
Replaces the placeholder `_ToastLayer` embedded in ui/app.py.

Design goals:
  • Never modal. Never steal focus. The user is always in control
    of the main UI; toasts are advisory only.
  • Status-colored left stripe for quick visual categorization.
  • Optional action button: "Export failed" can include a "View log"
    button; "Discovery complete" can include "Queue it".
  • Smooth slide-in animation via place_configure coordinate tweens.
  • Auto-dismiss with manual close affordance (×).
  • Accessible: hover pauses the dismiss timer so users have time
    to read longer messages.

The ToastLayer is the container that manages the stack; Toast is
the individual card. Only ToastLayer is exposed publicly — tabs
call `layer.show(...)` and never construct Toasts directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional

import customtkinter as ctk

from ui.theme import Theme


ToastKind = Literal["info", "success", "warning", "error"]


@dataclass(slots=True, frozen=True)
class _ToastGeometry:
    """Tunable layout constants — kept as a struct for easy tweaking."""
    width: int = 340
    height: int = 64
    gap: int = 8                # vertical space between stacked toasts
    margin_right: int = 20
    margin_top: int = 20
    slide_px_per_tick: int = 22
    slide_tick_ms: int = 12     # ~83fps target; Tk usually delivers 30-60
    default_duration_ms: int = 4000
    stripe_width: int = 3


class ToastLayer:
    """
    Top-right toast stack. One instance per app, constructed by
    ui/app.py and held on the AppContext for tabs to use via
    `ctx.publish_toast(...)`.
    """

    MAX_VISIBLE = 5

    def __init__(
        self,
        host: ctk.CTkBaseClass,
        theme: Theme,
        *,
        geometry: Optional[_ToastGeometry] = None,
    ) -> None:
        self._host = host
        self._theme = theme
        self._geom = geometry or _ToastGeometry()
        self._active: list["Toast"] = []

    def show(
        self,
        message: str,
        *,
        kind: ToastKind = "info",
        duration_ms: Optional[int] = None,
        action_label: Optional[str] = None,
        action_callback: Optional[Callable[[], None]] = None,
        on_action_error: Optional[Callable[[Exception], None]] = None,
    ) -> "Toast":
        """
        Display a toast. Returns the Toast handle so callers can
        dismiss it early if needed (e.g. replacing "Exporting…" with
        "Export complete" on the same toast).
        """
        # Enforce cap by dismissing the oldest before constructing new.
        while len(self._active) >= self.MAX_VISIBLE:
            self._active[0].dismiss(animated=False)

        t = Toast(
            self._host,
            theme=self._theme,
            geometry=self._geom,
            message=message,
            kind=kind,
            duration_ms=duration_ms or self._geom.default_duration_ms,
            action_label=action_label,
            action_callback=action_callback,
            on_action_error=on_action_error,
            on_dismissed=self._on_toast_dismissed,
        )
        self._active.append(t)
        self._reflow()
        self.lift()
        t.slide_in()
        return t

    def lift(self) -> None:
        """Keep toasts above tab content after tab switches."""
        for toast in self._active:
            try:
                toast.lift()
            except Exception:
                pass

    def clear(self) -> None:
        """Dismiss every active toast immediately. Used during shutdown."""
        for t in list(self._active):
            t.dismiss(animated=False)

    # ── Internal ──

    def _on_toast_dismissed(self, toast: "Toast") -> None:
        if toast in self._active:
            self._active.remove(toast)
        self._reflow()

    def _reflow(self) -> None:
        """Re-stack active toasts top-down after add/remove."""
        g = self._geom
        for i, t in enumerate(self._active):
            target_y = g.margin_top + i * (g.height + g.gap)
            t.move_to_y(target_y)


class Toast(ctk.CTkFrame):
    """
    One toast card. Not instantiated directly by callers — owned by
    ToastLayer which handles stacking, reflow, and lifecycle.
    """

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        *,
        theme: Theme,
        geometry: _ToastGeometry,
        message: str,
        kind: ToastKind,
        duration_ms: int,
        action_label: Optional[str],
        action_callback: Optional[Callable[[], None]],
        on_action_error: Optional[Callable[[Exception], None]],
        on_dismissed: Callable[["Toast"], None],
    ) -> None:
        super().__init__(
            parent,
            fg_color=theme.surface.elevated,
            border_color=theme.border.strong,
            border_width=theme.stroke.hairline,
            corner_radius=theme.radius.md,
        )

        self._theme = theme
        self._geom = geometry
        self._duration_ms = duration_ms
        self._on_dismissed = on_dismissed
        self._on_action_error = on_action_error
        self._dismiss_handle: Optional[str] = None
        self._slide_handle: Optional[str] = None
        self._current_y: int = geometry.margin_top
        self._target_y: int = geometry.margin_top
        self._dismissed = False

        self._build_body(theme, message, kind, action_label, action_callback)
        self._schedule_dismiss()

        # Hover-pause: keep toast visible as long as user is reading it.
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")

    # ── Construction ──

    def _build_body(
        self,
        theme: Theme,
        message: str,
        kind: ToastKind,
        action_label: Optional[str],
        action_callback: Optional[Callable[[], None]],
    ) -> None:
        t = theme
        g = self._geom

        # Root grid: stripe | body | actions
        self.grid_columnconfigure(0, weight=0, minsize=g.stripe_width)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=0)
        self.grid_rowconfigure(0, weight=1)

        stripe_color = self._stripe_color(kind)

        # ── Left status stripe ──
        stripe = ctk.CTkFrame(
            self,
            fg_color=stripe_color,
            corner_radius=0,
            border_width=0,
            width=g.stripe_width,
        )
        stripe.grid(row=0, column=0, sticky="ns")
        stripe.grid_propagate(False)

        # ── Message body ──
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=0, column=1, sticky="nsew",
                  padx=(t.space.md, t.space.sm), pady=t.space.sm)
        body.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            body,
            text=message,
            text_color=t.text.primary,
            font=t.font.body,
            anchor="w",
            justify="left",
            wraplength=g.width - 100,
        ).grid(row=0, column=0, sticky="w")

        # ── Actions column: optional action button + close (×) ──
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=0, column=2, sticky="ns",
                     padx=(0, t.space.sm), pady=t.space.xs)

        if action_label and action_callback:
            ctk.CTkButton(
                actions,
                text=action_label,
                command=lambda: self._on_action_clicked(action_callback),
                fg_color="transparent",
                hover_color=t.surface.overlay,
                text_color=stripe_color,
                border_width=0,
                corner_radius=t.radius.sm,
                font=t.font.caption,
                height=26,
                width=0,   # auto-size to text
            ).pack(side="left", padx=(0, t.space.xs))

        ctk.CTkButton(
            actions,
            text="×",
            command=lambda: self.dismiss(animated=True),
            fg_color="transparent",
            hover_color=t.surface.overlay,
            text_color=t.text.muted,
            border_width=0,
            corner_radius=t.radius.sm,
            font=t.font.subheading,
            width=24, height=24,
        ).pack(side="left")

    @staticmethod
    def _stripe_color(kind: ToastKind) -> str:
        """Resolve the status stripe color from the theme at lookup time."""
        # Deferred import style — this function runs on self._theme via
        # its closure in _build_body. But for clarity we let the caller
        # grab the color there directly.
        raise RuntimeError("unreachable — resolved via theme in _build_body")

    def _stripe_color(self, kind: ToastKind) -> str:       # type: ignore[no-redef]
        t = self._theme
        return {
            "info":    t.accent.blue,
            "success": t.status.success,
            "warning": t.status.warning,
            "error":   t.status.error,
        }[kind]

    # ── Public API ──

    def slide_in(self) -> None:
        """Place off-screen right, then animate into target position."""
        g = self._geom
        # Start one width past the right edge
        start_x_offset = -g.margin_right + g.width + 40
        self.place(
            in_=self._host_of_place_parent(),
            relx=1.0, y=self._target_y,
            x=start_x_offset, anchor="ne",
            width=g.width, height=g.height,
        )
        self._animate_x_to(-g.margin_right)

    def move_to_y(self, y: int) -> None:
        """Smoothly slide this toast to a new y-coordinate (stack reflow)."""
        self._target_y = y
        # Short cross-axis tween; reuses the same tick cadence.
        if self._slide_handle is None:
            self._slide_handle = self.after(
                self._geom.slide_tick_ms, self._tick_y,
            )

    def dismiss(self, *, animated: bool = True) -> None:
        """Cancel timers and remove. Idempotent."""
        if self._dismissed:
            return
        self._dismissed = True
        self._cancel_dismiss()
        self._cancel_slide()

        if animated:
            self._animate_out_and_destroy()
        else:
            try:
                self.destroy()
            finally:
                self._on_dismissed(self)

    # ── Animation plumbing ──

    def _host_of_place_parent(self):
        """
        The `in_` target for `.place()`. We place against the master
        container (the content host in the app shell).
        """
        return self.master

    def _animate_x_to(self, target_x: int) -> None:
        """Horizontal slide animation. Tail-recursive via root.after."""
        def step() -> None:
            if self._dismissed:
                return
            try:
                current = int(self.place_info().get("x", 0))
            except Exception:
                return
            delta = target_x - current
            if abs(delta) <= self._geom.slide_px_per_tick:
                self.place_configure(x=target_x)
                return
            step_x = (
                self._geom.slide_px_per_tick
                if delta > 0 else -self._geom.slide_px_per_tick
            )
            self.place_configure(x=current + step_x)
            self.after(self._geom.slide_tick_ms, step)
        step()

    def _tick_y(self) -> None:
        """Vertical position animation — for stack reflow."""
        if self._dismissed:
            self._slide_handle = None
            return
        try:
            info = self.place_info()
            current = int(info.get("y", self._target_y))
        except Exception:
            self._slide_handle = None
            return

        delta = self._target_y - current
        if abs(delta) <= self._geom.slide_px_per_tick:
            self.place_configure(y=self._target_y)
            self._slide_handle = None
            return

        step_y = (
            self._geom.slide_px_per_tick
            if delta > 0 else -self._geom.slide_px_per_tick
        )
        self.place_configure(y=current + step_y)
        self._slide_handle = self.after(self._geom.slide_tick_ms, self._tick_y)

    def _animate_out_and_destroy(self) -> None:
        """Slide right-off-screen, then destroy. Calls back on_dismissed."""
        g = self._geom
        start_x = -g.margin_right

        def step(offset: int) -> None:
            try:
                self.place_configure(x=start_x + offset)
            except Exception:
                self._safe_destroy()
                return
            if offset >= g.width + 40:
                self._safe_destroy()
                return
            self.after(g.slide_tick_ms,
                       lambda: step(offset + g.slide_px_per_tick))
        step(0)

    def _safe_destroy(self) -> None:
        try:
            self.destroy()
        except Exception:
            pass
        try:
            self._on_dismissed(self)
        except Exception:
            pass

    # ── Timer plumbing ──

    def _schedule_dismiss(self) -> None:
        self._dismiss_handle = self.after(
            self._duration_ms, lambda: self.dismiss(animated=True),
        )

    def _cancel_dismiss(self) -> None:
        if self._dismiss_handle is not None:
            try:
                self.after_cancel(self._dismiss_handle)
            except Exception:
                pass
            self._dismiss_handle = None

    def _cancel_slide(self) -> None:
        if self._slide_handle is not None:
            try:
                self.after_cancel(self._slide_handle)
            except Exception:
                pass
            self._slide_handle = None

    # ── Hover pause ──

    def _on_enter(self, _event) -> None:
        self._cancel_dismiss()

    def _on_leave(self, _event) -> None:
        # Resume dismissal on leave, but give a shorter grace period
        # than the original duration — user has already seen it.
        if not self._dismissed:
            self._dismiss_handle = self.after(
                max(1500, self._duration_ms // 2),
                lambda: self.dismiss(animated=True),
            )

    # ── Action handling ──

    def _on_action_clicked(self, cb: Callable[[], None]) -> None:
        try:
            cb()
        except Exception as e:
            if self._on_action_error is not None:
                try:
                    self._on_action_error(e)
                except Exception:
                    pass
        self.dismiss(animated=True)