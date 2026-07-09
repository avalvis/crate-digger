"""
ui/components/year_spinner.py
──────────────────────────────────────────────────────────────────────
Crate Digger — High-Precision Year Spinner

A custom spinbox-style year picker. Combines a numeric-only entry with
stepper buttons and mouse-wheel support for rapid year navigation.
Intuitive for the "enterprise" feel of the Digital Crate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Callable

import customtkinter as ctk

if TYPE_CHECKING:
    from ui.theme import Theme


class YearSpinner(ctk.CTkFrame):
    """
    Enhanced year input with stepper buttons and mouse-wheel support.
    """

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        variable: ctk.StringVar,
        *,
        min_year: int = 1900,
        max_year: int = 2100,
        placeholder: str = "Year",
        command: Optional[Callable[[int], None]] = None,
    ) -> None:
        super().__init__(parent, fg_color="transparent")

        self._theme = theme
        self._var = variable
        self._min = min_year
        self._max = max_year
        self._command = command

        # Sub-container for the layout
        self.grid_columnconfigure(0, weight=0)  # Down button
        self.grid_columnconfigure(1, weight=1)  # Entry
        self.grid_columnconfigure(2, weight=0)  # Up button

        # Outer border wrapper (to match other inputs)
        border = ctk.CTkFrame(
            self,
            fg_color=theme.border.strong,
            border_width=0,
            corner_radius=theme.radius.md,
        )
        border.grid(row=0, column=0, columnspan=3, sticky="nsew")
        border.grid_columnconfigure(0, weight=0)
        border.grid_columnconfigure(1, weight=1)
        border.grid_columnconfigure(2, weight=0)

        # 1. Decrement Button (-)
        self._btn_down = ctk.CTkButton(
            border,
            text="−",
            width=32,
            height=34,
            command=self._on_decrement,
            fg_color=theme.surface.raised,
            hover_color=theme.surface.elevated,
            text_color=theme.text.primary,
            font=(theme.font.family_ui, 16, "bold"),
            corner_radius=0,
        )
        self._btn_down.grid(row=0, column=0, padx=(2, 0), pady=2)

        # 2. Year Entry
        self._entry = ctk.CTkEntry(
            border,
            textvariable=variable,
            placeholder_text=placeholder,
            width=70,
            height=34,
            border_width=0,
            fg_color=theme.surface.raised,
            text_color=theme.text.primary,
            placeholder_text_color=theme.text.muted,
            font=theme.font.body,
            corner_radius=0,
            justify="center",
        )
        self._entry.grid(row=0, column=1, padx=1, pady=2, sticky="ew")

        # 3. Increment Button (+)
        self._btn_up = ctk.CTkButton(
            border,
            text="+",
            width=32,
            height=34,
            command=self._on_increment,
            fg_color=theme.surface.raised,
            hover_color=theme.surface.elevated,
            text_color=theme.text.primary,
            font=(theme.font.family_ui, 16, "bold"),
            corner_radius=0,
        )
        self._btn_up.grid(row=0, column=2, padx=(0, 2), pady=2)

        # Bindings for mouse wheel and input validation
        self._entry.bind("<MouseWheel>", self._on_mousewheel)
        self._entry.bind("<FocusOut>", self._validate)

    def _on_increment(self) -> None:
        if str(self._entry.cget("state")) == "disabled":
            return
        val = self._get_current_val()
        new_val = min(self._max, val + 1)
        self._set_val(new_val)

    def _on_decrement(self) -> None:
        if str(self._entry.cget("state")) == "disabled":
            return
        val = self._get_current_val()
        new_val = max(self._min, val - 1)
        self._set_val(new_val)

    def _on_mousewheel(self, event) -> None:
        if str(self._entry.cget("state")) == "disabled":
            return "break"
        if event.delta > 0:
            self._on_increment()
        else:
            self._on_decrement()

    def _get_current_val(self) -> int:
        raw = self._var.get().strip()
        if not raw:
            import datetime
            return datetime.datetime.now().year
        try:
            return int(raw)
        except ValueError:
            import datetime
            return datetime.datetime.now().year

    def _set_val(self, val: int) -> None:
        self._var.set(str(val))
        if self._command:
            self._command(val)

    def _validate(self, _event=None) -> None:
        if str(self._entry.cget("state")) == "disabled":
            return
        raw = self._var.get().strip()
        if not raw:
            return
        if not raw.isdigit():
            # If user typed garbage, reset to last valid or current year
            self._set_val(self._get_current_val())
            return
        
        val = int(raw)
        if val < self._min: self._set_val(self._min)
        elif val > self._max: self._set_val(self._max)

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable the spinner (used while a dig is in flight)."""
        state = "normal" if enabled else "disabled"
        try:
            self._entry.configure(state=state)
            self._btn_up.configure(state=state)
            self._btn_down.configure(state=state)
        except Exception:
            pass
