"""
ui/components/glow_entry.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Focus-Accented Input Field

Outer CTkFrame fg_color IS the "border" — inner content frame sits
1 px inset, so the outer color rings the content as a visible outline.
No CTk canvas border_width is used (that approach leaves corner gaps on
Windows when child Canvas windows cover the drawn outline). Here the
1 px ring is solid background color — it always renders perfectly.

Focus state: outer frame fg_color → accent orange.
Error state: outer frame fg_color → error red-orange.
Idle state:  outer frame fg_color → border.strong warm brown.
"""

from __future__ import annotations

from typing import Callable, Literal, Optional

import customtkinter as ctk

from ui.theme import Theme

ValidationState = Literal["ok", "error"]


class GlowEntry(ctk.CTkFrame):
    """
    Composed entry with a cleanly rendered focus ring. All content lives
    in a 1 px inset inner frame; the outer frame's bg color is the ring.
    """

    _DEFAULT_HEIGHT = 48

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        *,
        placeholder: str = "",
        prefix_icon: Optional[str] = None,
        show_clear_button: bool = True,
        validator: Optional[Callable[[str], bool]] = None,
        error_message: str = "Invalid input",
        on_submit: Optional[Callable[[str], None]] = None,
        height: Optional[int] = None,
        width: int = 0,
    ) -> None:
        self._height = int(height or self._DEFAULT_HEIGHT)

        # Outer frame — fg_color is the visible "border ring".
        # No border_width used; the 1 px padding of _inner creates the ring.
        super().__init__(
            parent,
            fg_color=theme.border.strong,
            border_width=0,
            corner_radius=theme.radius.md,
            height=self._height,
            width=width,
        )
        self.grid_propagate(False)

        # Inner content frame — exactly 2 px inset on every side.
        # 2 px ring is reliably visible at all DPI scales; 1 px can
        # drop to a sub-pixel and disappear on some Windows setups.
        # Corner radii satisfy pad + inner_r = outer_r (2+6=8) so the
        # ring width is uniform all the way around the corners.
        self._inner = ctk.CTkFrame(
            self,
            fg_color=theme.surface.raised,
            border_width=0,
            corner_radius=max(0, theme.radius.md - 2),
        )
        self._inner.place(x=2, y=2, relwidth=1.0, relheight=1.0, width=-4, height=-4)

        self._theme = theme
        self._validator = validator
        self._error_message = error_message
        self._on_submit = on_submit
        self._validation_state: ValidationState = "ok"
        self._has_focus = False

        self._build_body(theme, placeholder, prefix_icon, show_clear_button)
        self._error_label: Optional[ctk.CTkLabel] = None

    # ── Body construction ──

    def _build_body(
        self,
        theme: Theme,
        placeholder: str,
        prefix_icon: Optional[str],
        show_clear_button: bool,
    ) -> None:
        t = theme
        inn = self._inner

        inn.grid_rowconfigure(0, weight=1)
        inn.grid_columnconfigure(0, weight=0)
        inn.grid_columnconfigure(1, weight=1)
        inn.grid_columnconfigure(2, weight=0)

        # ── Prefix icon ──
        if prefix_icon:
            self._prefix_label = ctk.CTkLabel(
                inn,
                text=prefix_icon,
                text_color=t.text.muted,
                font=t.font.body_emphasis,
                width=20,
            )
            self._prefix_label.grid(
                row=0,
                column=0,
                sticky="nsw",
                padx=(t.space.md, t.space.xs),
            )
        else:
            self._prefix_label = None

        # ── Core entry ──
        self._entry = ctk.CTkEntry(
            inn,
            placeholder_text=placeholder,
            fg_color="transparent",
            border_width=0,
            text_color=t.text.primary,
            placeholder_text_color=t.text.muted,
            font=t.font.body,
            height=max(28, self._height - 10),
        )
        left_pad = 0 if prefix_icon else t.space.md
        right_pad = 0 if show_clear_button else t.space.md
        self._entry.grid(
            row=0,
            column=1,
            sticky="nsew",
            padx=(left_pad, right_pad),
        )

        # Suppress the native Tk Entry's own focus ring.
        try:
            inner_entry = getattr(self._entry, "_entry", None)
            if inner_entry is not None:
                inner_entry.configure(highlightthickness=0, bd=0, relief="flat")
        except Exception:
            pass

        self._entry.bind("<FocusIn>",    self._on_focus_in,    add="+")
        self._entry.bind("<FocusOut>",   self._on_focus_out,   add="+")
        self._entry.bind("<KeyRelease>", self._on_key_release, add="+")
        self._entry.bind("<Return>",     self._on_enter_pressed, add="+")
        self._entry.bind("<KP_Enter>",   self._on_enter_pressed, add="+")

        # ── Clear button ──
        self._clear_button: Optional[ctk.CTkButton] = None
        if show_clear_button:
            self._clear_button = ctk.CTkButton(
                inn,
                text="×",
                width=30,
                height=30,
                command=self._on_clear_clicked,
                fg_color="transparent",
                hover_color=t.surface.elevated,
                text_color=t.text.muted,
                border_width=0,
                corner_radius=t.radius.sm,
                font=t.font.subheading,
            )

    # ── Public API ──

    def get(self) -> str:
        return self._entry.get()

    def set(self, value: str) -> None:
        self._entry.delete(0, "end")
        if value:
            self._entry.insert(0, value)
        self._update_clear_visibility()

    def clear(self) -> None:
        self.set("")
        self.reset_validation()

    def focus(self) -> None:
        self._entry.focus_set()

    def configure_state(self, *, disabled: bool) -> None:
        t = self._theme
        if disabled:
            self._entry.configure(state="disabled")
            self._inner.configure(fg_color=t.surface.base)
            self.configure(fg_color=t.border.subtle)
        else:
            self._entry.configure(state="normal")
            self._inner.configure(fg_color=t.surface.raised)
            self._apply_ring_color()

    def bind_submit(self, callback: Callable[[str], None]) -> None:
        self._on_submit = callback

    # ── Validation ──

    def validate(self) -> bool:
        if self._validator is None:
            self._set_validation_state("ok")
            return True
        value = self.get().strip()
        ok = bool(self._validator(value)) if value else True
        self._set_validation_state("ok" if ok else "error")
        return ok

    def reset_validation(self) -> None:
        self._set_validation_state("ok")

    def grid_with_error_label(
        self,
        parent_row: int,
        parent_col: int,
        **grid_kwargs,
    ) -> None:
        self.grid(row=parent_row, column=parent_col, **grid_kwargs)

        if self._error_label is None:
            t = self._theme
            self._error_label = ctk.CTkLabel(
                self.master,
                text="",
                text_color=t.status.error,
                font=t.font.micro,
                anchor="w",
            )
        err_kwargs = dict(grid_kwargs)
        err_kwargs["row"] = parent_row + 1
        err_kwargs["column"] = parent_col
        err_kwargs["pady"] = (2, 0)
        err_kwargs["sticky"] = "w"
        self._error_label.grid_forget()
        if self._validation_state == "error":
            self._error_label.grid(**err_kwargs)
        self._error_label_grid_kwargs = err_kwargs

    # ── Event handlers ──

    def _on_focus_in(self, _event) -> None:
        self._has_focus = True
        self._apply_ring_color()

    def _on_focus_out(self, _event) -> None:
        self._has_focus = False
        if self._validator is not None:
            self.validate()
        self._apply_ring_color()

    def _on_key_release(self, _event) -> None:
        self._update_clear_visibility()
        if self._validation_state == "error":
            self._set_validation_state("ok")

    def _on_enter_pressed(self, _event) -> str:
        if self._on_submit is not None:
            value = self.get().strip()
            if self._validator is not None and not self._validator(value):
                self._set_validation_state("error")
                return "break"
            self._on_submit(value)
        return "break"

    def _on_clear_clicked(self) -> None:
        self.clear()
        self._entry.focus_set()

    # ── State rendering ──

    def _apply_ring_color(self) -> None:
        t = self._theme
        if self._validation_state == "error":
            self.configure(fg_color=t.status.error)
        elif self._has_focus:
            self.configure(fg_color=t.accent.blue)
        else:
            self.configure(fg_color=t.border.strong)

    # kept for any callers that used the old name
    def _apply_border_state(self) -> None:
        self._apply_ring_color()

    def _set_validation_state(self, state: ValidationState) -> None:
        if state == self._validation_state:
            return
        self._validation_state = state
        self._apply_ring_color()
        if self._error_label is not None:
            if state == "error":
                self._error_label.configure(text=self._error_message)
                if hasattr(self, "_error_label_grid_kwargs"):
                    self._error_label.grid(**self._error_label_grid_kwargs)
            else:
                self._error_label.grid_forget()

    def _update_clear_visibility(self) -> None:
        if self._clear_button is None:
            return
        t = self._theme
        has_content = bool(self.get())
        currently_visible = bool(self._clear_button.winfo_ismapped())
        if has_content and not currently_visible:
            self._clear_button.grid(
                row=0,
                column=2,
                sticky="nse",
                padx=(0, t.space.sm),
                pady=t.space.xs,
            )
        elif not has_content and currently_visible:
            self._clear_button.grid_forget()
