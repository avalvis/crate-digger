"""
ui/theme.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Design Tokens

Single source of truth for colors, spacing, typography, and radii.
All UI modules import from here; no raw hex codes elsewhere.

Design language:
  • Near-black surfaces — warm dark like a studio at 2am, not sterile.
    Clear contrast between bg / card / elevated so every layer is
    visually distinct without looking noisy.
  • ORANGE primary accent (#D4652A) — the SP-1200 button glow, the
    MPC 3000 indicator LED. Every CTA, every focus ring, every progress
    bar pulses with this color. Unmistakably warm and analog.
  • AMBER secondary accent (#C89028) — the vinyl label color. Active nav
    rail, selection highlights. Softer than orange; used for context, not
    action. Lives in the "purple" token slot so downstream code is unchanged.
  • Visible borders. Input fields and cards have clear edges — #362C1E
    against dark surfaces reads cleanly without looking harsh.
  • Typography: Segoe UI Variable / Inter for UI.
    JetBrains Mono / Consolas for numerics (BPM / key).
  • 4px base grid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import customtkinter as ctk

# ─── Core palette ───────────────────────────────────────────────────

# Near-black surfaces — warm, distinct tiers.
# Each tier is clearly lighter than the one below so cards, inputs,
# and popups all have visible lift.
_SURFACE_BLACK    = "#0A0908"   # app bg — near-true black
_SURFACE_BASE     = "#0F0E0B"   # sidebar / nav panel
_SURFACE_RAISED   = "#1C1914"   # cards, tab body, input fields
_SURFACE_ELEVATED = "#262118"   # dropdowns, hover states, modals
_SURFACE_OVERLAY  = "#31281E"   # tooltips, topmost floats

# Borders — warm, clearly visible, not harsh.
_BORDER_SUBTLE = "#2C2419"   # section dividers, card outlines
_BORDER_STRONG = "#443420"   # input borders, emphasis lines

# Text — warm cream hierarchy.
_TEXT_PRIMARY   = "#EDE5D8"  # warm cream-white
_TEXT_SECONDARY = "#9A8870"  # warm mid-tone
_TEXT_MUTED     = "#5C5040"  # placeholders, captions
_TEXT_DISABLED  = "#3A3020"

# PRIMARY accent — ORANGE.
# The SP-1200/MPC button glow. Every CTA, focus ring, progress bar,
# active spinner. The color that says "this app is alive."
_ACCENT_BLUE        = "#D4652A"   # orange — primary CTA
_ACCENT_BLUE_BRIGHT = "#E8723A"   # orange hover
_ACCENT_BLUE_DIM    = "#8A3D18"   # orange pressed / subdued

# SECONDARY accent — AMBER.
# The vinyl label color. Active nav rail, selection highlights.
# Softer warm than orange; used for state, not action.
# Stored under the "purple" slot — all downstream accent.purple refs
# automatically render amber with no other file changes needed.
_ACCENT_PURPLE        = "#C89028"   # amber — identity / selection
_ACCENT_PURPLE_BRIGHT = "#E0A832"
_ACCENT_PURPLE_DIM    = "#7A5818"

# Status colors.
_STATUS_SUCCESS = "#2ECC8A"   # green
_STATUS_WARNING = "#E0A832"   # amber (same warmth as secondary)
_STATUS_ERROR   = "#D4452A"   # red-orange (MPC error LED)
_STATUS_INFO    = _ACCENT_PURPLE   # amber info — warm, not clinical blue


# ─── Semantic token bundles ──────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SurfaceTokens:
    app:      str = _SURFACE_BLACK
    base:     str = _SURFACE_BASE
    raised:   str = _SURFACE_RAISED
    elevated: str = _SURFACE_ELEVATED
    overlay:  str = _SURFACE_OVERLAY


@dataclass(slots=True, frozen=True)
class BorderTokens:
    subtle:       str = _BORDER_SUBTLE
    strong:       str = _BORDER_STRONG
    focus:        str = _ACCENT_BLUE        # orange focus ring on inputs
    focus_bright: str = _ACCENT_BLUE_BRIGHT
    selection:    str = _ACCENT_PURPLE      # amber selection border


@dataclass(slots=True, frozen=True)
class TextTokens:
    primary:   str = _TEXT_PRIMARY
    secondary: str = _TEXT_SECONDARY
    muted:     str = _TEXT_MUTED
    disabled:  str = _TEXT_DISABLED
    on_accent: str = "#FFFFFF"


@dataclass(slots=True, frozen=True)
class AccentTokens:
    blue:          str = _ACCENT_BLUE          # orange
    blue_bright:   str = _ACCENT_BLUE_BRIGHT
    blue_dim:      str = _ACCENT_BLUE_DIM
    purple:        str = _ACCENT_PURPLE        # amber
    purple_bright: str = _ACCENT_PURPLE_BRIGHT
    purple_dim:    str = _ACCENT_PURPLE_DIM


@dataclass(slots=True, frozen=True)
class StatusTokens:
    success: str = _STATUS_SUCCESS
    warning: str = _STATUS_WARNING
    error:   str = _STATUS_ERROR
    info:    str = _STATUS_INFO


# ─── Spacing & radius (4px grid) ─────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SpacingTokens:
    xxs:  int = 2
    xs:   int = 4
    sm:   int = 8
    md:   int = 12
    lg:   int = 16
    xl:   int = 24
    xxl:  int = 32
    xxxl: int = 48


@dataclass(slots=True, frozen=True)
class RadiusTokens:
    none: int = 0
    sm:   int = 4
    md:   int = 8
    lg:   int = 10
    xl:   int = 14
    pill: int = 999


@dataclass(slots=True, frozen=True)
class StrokeTokens:
    hairline:   int = 1
    regular:    int = 1
    emphasized: int = 2
    thick:      int = 3


# ─── Typography ──────────────────────────────────────────────────────

_UI_FONT_STACK: tuple[str, ...] = (
    "Inter",
    "Segoe UI Variable",
    "Segoe UI",
    "SF Pro Text",
    "Helvetica Neue",
    "Arial",
)
_MONO_FONT_STACK: tuple[str, ...] = (
    "JetBrains Mono",
    "Fira Code",
    "Consolas",
    "SF Mono",
    "Menlo",
    "Courier New",
)


@dataclass(slots=True)
class FontTokens:
    family_ui:    str   = "Segoe UI"
    family_mono:  str   = "Consolas"

    micro:         tuple = field(default=())
    caption:       tuple = field(default=())
    body:          tuple = field(default=())
    body_emphasis: tuple = field(default=())
    subheading:    tuple = field(default=())
    heading:       tuple = field(default=())
    display:       tuple = field(default=())

    mono_body:    tuple = field(default=())
    mono_heading: tuple = field(default=())


# ─── The Theme ───────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Theme:
    surface: SurfaceTokens
    border:  BorderTokens
    text:    TextTokens
    accent:  AccentTokens
    status:  StatusTokens
    space:   SpacingTokens
    radius:  RadiusTokens
    stroke:  StrokeTokens
    font:    FontTokens

    sidebar_width:     int = 240
    content_max_width: int = 1400
    window_min_width:  int = 1120
    window_min_height: int = 720


# ─── Builder ─────────────────────────────────────────────────────────

_theme_singleton: Optional[Theme] = None


def build_theme() -> Theme:
    global _theme_singleton
    if _theme_singleton is not None:
        return _theme_singleton

    ui_family, mono_family = _resolve_font_families()

    font = FontTokens(
        family_ui=ui_family,
        family_mono=mono_family,
        micro=(ui_family, 11, "normal"),
        caption=(ui_family, 12, "normal"),
        body=(ui_family, 13, "normal"),
        body_emphasis=(ui_family, 13, "bold"),
        subheading=(ui_family, 15, "bold"),
        heading=(ui_family, 20, "bold"),
        display=(ui_family, 26, "bold"),
        mono_body=(mono_family, 12, "normal"),
        mono_heading=(mono_family, 18, "bold"),
    )

    _theme_singleton = Theme(
        surface=SurfaceTokens(),
        border=BorderTokens(),
        text=TextTokens(),
        accent=AccentTokens(),
        status=StatusTokens(),
        space=SpacingTokens(),
        radius=RadiusTokens(),
        stroke=StrokeTokens(),
        font=font,
    )
    return _theme_singleton


def apply_customtkinter_globals(theme: Optional[Theme] = None) -> None:
    """
    Must be called once, immediately after the CTk() root is created.
    Loads our custom theme JSON so every unexplicily-styled widget
    defaults to our palette rather than CTk's built-in blue theme.
    """
    ctk.set_appearance_mode("dark")

    # Load our custom theme JSON — this sets palette defaults for ALL
    # widget types so any widget we forget to explicitly style still
    # matches our look rather than falling back to CTk blue.
    _theme_json_path = Path(__file__).with_name("crate_digger_theme.json")
    if _theme_json_path.exists():
        try:
            ctk.set_default_color_theme(str(_theme_json_path))
        except Exception:
            ctk.set_default_color_theme("blue")
    else:
        ctk.set_default_color_theme("blue")

    ctk.set_widget_scaling(1.0)

    try:
        import tkinter as tk
        root = tk._default_root  # type: ignore[attr-defined]
        if root is not None:
            root.option_add("*highlightThickness", 0)
            root.option_add("*Entry.highlightThickness", 0)
            root.option_add("*Text.highlightThickness", 0)
            root.option_add("*Listbox.highlightThickness", 0)
            root.option_add("*Spinbox.highlightThickness", 0)
            root.option_add("*Button.highlightThickness", 0)
            root.option_add("*Checkbutton.highlightThickness", 0)
            root.option_add("*Radiobutton.highlightThickness", 0)
    except Exception:
        pass


# ─── Widget style helpers ────────────────────────────────────────────
# These return kwargs ready to spread into CTk widget constructors.
# Using helpers keeps the palette in one place — change a token here
# and every widget that uses it updates automatically.


def style_card(theme: Theme) -> dict:
    return dict(
        fg_color=theme.surface.raised,
        border_color=theme.border.subtle,
        border_width=theme.stroke.hairline,
        corner_radius=theme.radius.lg,
    )


def style_card_elevated(theme: Theme) -> dict:
    return dict(
        fg_color=theme.surface.elevated,
        border_color=theme.border.strong,
        border_width=theme.stroke.hairline,
        corner_radius=theme.radius.lg,
    )


def style_primary_button(theme: Theme) -> dict:
    """Solid orange CTA — Queue, Export, Dig."""
    return dict(
        fg_color=theme.accent.blue,
        hover_color=theme.accent.blue_bright,
        text_color=theme.text.on_accent,
        corner_radius=theme.radius.md,
        border_width=0,
        font=theme.font.body_emphasis,
        height=38,
    )


def style_secondary_button(theme: Theme) -> dict:
    """Outlined ghost for secondary actions."""
    return dict(
        fg_color="transparent",
        hover_color=theme.surface.elevated,
        text_color=theme.text.primary,
        border_color=theme.border.strong,
        border_width=theme.stroke.regular,
        corner_radius=theme.radius.md,
        font=theme.font.body,
        height=38,
    )


def style_ghost_button(theme: Theme) -> dict:
    """Nearly invisible at rest — toolbars and inline actions."""
    return dict(
        fg_color="transparent",
        hover_color=theme.surface.elevated,
        text_color=theme.text.secondary,
        border_width=0,
        corner_radius=theme.radius.sm,
        font=theme.font.body,
        height=32,
    )


def style_danger_button(theme: Theme) -> dict:
    """Destructive actions — Delete, Cancel all."""
    return dict(
        fg_color="transparent",
        hover_color=theme.status.error,
        text_color=theme.status.error,
        border_color=theme.status.error,
        border_width=theme.stroke.regular,
        corner_radius=theme.radius.md,
        font=theme.font.body_emphasis,
        height=38,
    )


def style_input(theme: Theme) -> dict:
    """CTkEntry / plain input fields."""
    return dict(
        fg_color=theme.surface.raised,
        border_color=theme.border.strong,
        border_width=theme.stroke.regular,
        text_color=theme.text.primary,
        placeholder_text_color=theme.text.muted,
        corner_radius=theme.radius.md,
        font=theme.font.body,
        height=40,
    )


def style_input_focused(theme: Theme) -> dict:
    """Reconfigure overrides for the focused state (orange glow)."""
    return dict(
        border_color=theme.accent.blue,
        border_width=theme.stroke.emphasized,
    )


def style_dropdown(theme: Theme) -> dict:
    """CTkOptionMenu — all colors explicit, including dropdown popup."""
    return dict(
        fg_color=theme.surface.raised,
        button_color=theme.surface.raised,
        button_hover_color=theme.surface.elevated,
        dropdown_fg_color=theme.surface.elevated,
        dropdown_hover_color=theme.surface.overlay,
        dropdown_text_color=theme.text.primary,
        text_color=theme.text.primary,
        font=theme.font.body,
        corner_radius=theme.radius.md,
    )


def style_progress(theme: Theme) -> dict:
    """Orange progress bar — standard download jobs."""
    return dict(
        fg_color=theme.surface.elevated,
        progress_color=theme.accent.blue,
        corner_radius=theme.radius.pill,
        height=6,
    )


def style_progress_purple(theme: Theme) -> dict:
    """Amber progress bar — Discovery-originated jobs."""
    return dict(
        fg_color=theme.surface.elevated,
        progress_color=theme.accent.purple,
        corner_radius=theme.radius.pill,
        height=6,
    )


def style_nav_item_active(theme: Theme) -> dict:
    """Fallback for plain CTkButton nav items."""
    return dict(
        fg_color=theme.surface.elevated,
        hover_color=theme.surface.elevated,
        text_color=theme.accent.purple,
        border_color=theme.accent.purple,
        border_width=theme.stroke.regular,
        corner_radius=theme.radius.md,
        anchor="w",
        font=theme.font.body_emphasis,
        height=44,
    )


def style_nav_item_inactive(theme: Theme) -> dict:
    return dict(
        fg_color="transparent",
        hover_color=theme.surface.raised,
        text_color=theme.text.secondary,
        border_width=0,
        corner_radius=theme.radius.md,
        anchor="w",
        font=theme.font.body,
        height=44,
    )


def style_label_heading(theme: Theme) -> dict:
    return dict(text_color=theme.text.primary, font=theme.font.heading)


def style_label_subheading(theme: Theme) -> dict:
    return dict(text_color=theme.text.primary, font=theme.font.subheading)


def style_label_body(theme: Theme) -> dict:
    return dict(text_color=theme.text.primary, font=theme.font.body)


def style_label_meta(theme: Theme) -> dict:
    return dict(text_color=theme.text.secondary, font=theme.font.caption)


def style_numeric_display(theme: Theme) -> dict:
    return dict(text_color=theme.text.primary, font=theme.font.mono_heading)


# ─── Font resolution ─────────────────────────────────────────────────


def _resolve_font_families() -> tuple[str, str]:
    try:
        import tkinter as tk
        import tkinter.font as tkfont

        needs_temp_root = tk._default_root is None  # type: ignore[attr-defined]
        temp_root = tk.Tk() if needs_temp_root else None
        if temp_root is not None:
            temp_root.withdraw()

        try:
            families = set(tkfont.families())
        finally:
            if temp_root is not None:
                temp_root.destroy()
    except Exception:
        return ("Segoe UI", "Consolas")

    def pick(stack: tuple[str, ...], fallback: str) -> str:
        for f in stack:
            if f in families:
                return f
        return fallback

    return (
        pick(_UI_FONT_STACK, "Segoe UI"),
        pick(_MONO_FONT_STACK, "Consolas"),
    )
