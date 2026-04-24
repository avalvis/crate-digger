"""
ui/components/sidebar.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Sidebar Navigation Button

A proper navigation button for the app shell sidebar. Replaces the
inline CTkButton stand-ins in ui/app.py.

Features over a plain CTkButton:
  • Three visual states (inactive, hover, active) with explicit
    border-stroke differentiation. The border is the "glow" channel —
    inactive = transparent, hover = subtle, active = accent-blue.
  • Icon + label layout using a two-column grid so icons align
    vertically across all nav items regardless of label length.
  • Optional unread-count badge (for future use: Vault "new since
    last visit", Queue pending count, etc.).
  • Left accent rail on active state — a 3px saturated stripe
    against the charcoal sidebar, the single most premium-reading
    detail in the whole shell.

This widget is a CTkFrame rather than subclassing CTkButton, because
CTk buttons don't cleanly support the multi-element layout (icon +
label + badge + accent rail) we need. Click handling is bound
manually to every child widget so the entire row is clickable.
"""
from __future__ import annotations

from typing import Callable, Optional

import customtkinter as ctk

from ui.theme import Theme


class SidebarNavButton(ctk.CTkFrame):
    """
    One entry in the sidebar nav rail. Owned by the app shell, which
    calls `set_active(bool)` when the tab selection changes.
    """

    # Fixed visual geometry — keeps nav items rhythmically aligned.
    _HEIGHT = 44
    _ICON_CELL_WIDTH = 28
    _ACCENT_RAIL_WIDTH = 3

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        *,
        label: str,
        icon_glyph: str = "",
        command: Optional[Callable[[], None]] = None,
        badge_text: Optional[str] = None,
    ) -> None:
        super().__init__(
            parent,
            fg_color="transparent",
            corner_radius=theme.radius.md,
            border_width=0,
            height=self._HEIGHT,
        )

        self._theme = theme
        self._command = command
        self._active = False
        self._hovered = False

        self.grid_propagate(False)
        self.pack_propagate(False)
        # Columns: 0 accent rail | 1 icon | 2 label | 3 badge (optional)
        self.grid_columnconfigure(0, weight=0, minsize=self._ACCENT_RAIL_WIDTH)
        self.grid_columnconfigure(1, weight=0, minsize=self._ICON_CELL_WIDTH)
        self.grid_columnconfigure(2, weight=1)
        self.grid_columnconfigure(3, weight=0)
        self.grid_rowconfigure(0, weight=1)

        # ── Accent rail ──
        # Rendered as a thin frame in col 0. Switched visible/hidden
        # on active state via fg_color swap (not destroy/recreate).
        self._rail = ctk.CTkFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
            width=self._ACCENT_RAIL_WIDTH,
        )
        self._rail.grid(row=0, column=0, sticky="ns",
                        padx=0, pady=theme.space.xs)
        self._rail.grid_propagate(False)

        # ── Icon ──
        # Single-char unicode glyph. When we move to real icon PNGs
        # from assets/, this label becomes a CTkLabel(image=...).
        self._icon_label = ctk.CTkLabel(
            self,
            text=icon_glyph,
            text_color=theme.text.secondary,
            font=(theme.font.family_ui, 14, "normal"),
            width=self._ICON_CELL_WIDTH,
        )
        self._icon_label.grid(row=0, column=1, sticky="nsew",
                              padx=(theme.space.sm, 0))

        # ── Label ──
        self._text_label = ctk.CTkLabel(
            self,
            text=label,
            text_color=theme.text.secondary,
            font=theme.font.body,
            anchor="w",
        )
        self._text_label.grid(row=0, column=2, sticky="nsew",
                              padx=(theme.space.sm, theme.space.sm))

        # ── Optional badge ──
        self._badge: Optional[ctk.CTkLabel] = None
        if badge_text:
            self.set_badge(badge_text)

        # ── Click + hover plumbing ──
        # Bind to every child too, so clicks anywhere in the row fire.
        for widget in (self, self._icon_label, self._text_label, self._rail):
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
            widget.bind("<Button-1>", self._on_click, add="+")

        self._apply_state()

    # ── Public API ──

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self._apply_state()

    def set_badge(self, text: Optional[str]) -> None:
        """Show or update the unread-count badge. Pass None to remove."""
        t = self._theme
        if not text:
            if self._badge is not None:
                self._badge.destroy()
                self._badge = None
            return

        if self._badge is None:
            self._badge = ctk.CTkLabel(
                self,
                text=str(text),
                text_color=t.text.on_accent,
                font=t.font.micro,
                fg_color=t.accent.purple,
                corner_radius=t.radius.pill,
                width=22, height=18,
            )
            self._badge.grid(row=0, column=3, sticky="e",
                             padx=(0, t.space.md))
        else:
            self._badge.configure(text=str(text))

    # ── Event handlers ──

    def _on_enter(self, _event) -> None:
        self._hovered = True
        self._apply_state()

    def _on_leave(self, _event) -> None:
        self._hovered = False
        self._apply_state()

    def _on_click(self, _event) -> None:
        if self._command is not None:
            self._command()

    # ── State rendering ──

    def _apply_state(self) -> None:
        """
        Compute and apply colors for the current (active, hover) state.
        Centralized here so any future state combinations (e.g. "disabled
        while loading") slot in as one more branch without scattering
        color logic across handlers.
        """
        t = self._theme

        if self._active:
            bg = t.surface.elevated
            rail_color = t.accent.purple   # amber — lit MPC pad
            icon_color = t.accent.purple
            text_color = t.text.primary
            border_color = t.border.subtle
            border_width = t.stroke.hairline
        elif self._hovered:
            bg = t.surface.raised
            rail_color = "transparent"
            icon_color = t.text.primary
            text_color = t.text.primary
            border_color = "transparent"
            border_width = 0
        else:
            bg = "transparent"
            rail_color = "transparent"
            icon_color = t.text.secondary
            text_color = t.text.secondary
            border_color = "transparent"
            border_width = 0

        # CTkFrame rejects "transparent" for border_color — always supply
        # a real color; when border_width=0 it's not visible anyway.
        safe_border = border_color if border_color != "transparent" else t.border.subtle
        self.configure(
            fg_color=bg,
            border_color=safe_border,
            border_width=border_width,
        )
        self._rail.configure(fg_color=rail_color)
        self._icon_label.configure(text_color=icon_color)
        self._text_label.configure(text_color=text_color)