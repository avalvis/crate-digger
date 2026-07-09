"""
ui/tabs/digital_crate.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Digital Crate (Discovery) Tab

The "what am I digging today" surface. Era presets + filters drive a
single Dig that surfaces a *reel* of sample-friendly gems from Discogs,
each resolved on YouTube Music and previewable in-app with a scrubbable
waveform. Queue the ones you like; skipped gems can resurface later.

Guiding principle: weight, never exclude — the roulette tilts toward
boom-bap/lo-fi/Greek gems but can still surprise with a wildcard.
"""

from __future__ import annotations

import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk

from core.discovery import (
    DiscoveryConfigError,
    DiscoveryError,
    DiscoveryFilters,
    DiscoverySuggestion,
    DiscoveryThrottledError,
    NoResultsError,
    NoYouTubeMatchError,
)
from core.mpc_export import (
    MpcExportMode,
    MpcSampleExportCancelledError,
    MpcSampleExportError,
    MpcSampleResult,
    MPC_WORKFLOW_MAX_SECONDS,
    export_sample_to_mpc,
)
from core.pipeline import PipelineRequest
from core.sampling_taxonomy import sample_affinity
from ui.components.dig_roulette_spinner import DigRouletteSpinner
from ui.components.waveform_player import WaveformPlayer
from ui.components.year_spinner import YearSpinner
from ui.theme import (
    Theme,
    style_card_elevated,
    style_danger_button,
    style_ghost_button,
    style_label_body,
    style_label_meta,
    style_label_heading,
    style_label_subheading,
    style_primary_button,
    style_secondary_button,
)

if TYPE_CHECKING:
    from ui.app import AppContext


# ─── Filter vocabularies ────────────────────────────────────────────

_FORMAT_CHOICES: list[str] = [
    "", "Vinyl", "CD", "Cassette", "7\"", "12\"", "LP", "Album",
    "Single", "EP", "Compilation",
]

_COUNTRY_CHOICES: list[str] = [
    "", "Argentina", "Australia", "Belgium", "Brazil", "Canada", "Colombia",
    "Cuba", "Ethiopia", "France", "Germany", "Ghana", "Greece", "India",
    "Italy", "Jamaica", "Japan", "Mexico", "Netherlands", "Nigeria", "Norway",
    "South Africa", "Spain", "Sweden", "Switzerland", "Turkey", "UK", "USA",
    "USSR", "Yugoslavia",
]

_GENRE_CHOICES: list[str] = [
    "", "Electronic", "Folk, World, & Country", "Funk / Soul", "Hip Hop",
    "Jazz", "Latin", "Pop", "Reggae", "Rock", "Stage & Screen", "Blues",
    "Non-Music", "Children's", "Brass & Military", "Classical",
]

_STYLE_CHOICES: list[str] = [
    "", "Acid Jazz", "Afrobeat", "Ambient", "Big Beat", "Blues Rock",
    "Bolero", "Boogaloo", "Bossa Nova", "Bouzouki", "Breakbeat", "Chanson",
    "Cumbia", "Dancehall", "Deep House", "Disco", "Downtempo", "Dub",
    "Éntekhno", "Ethio-jazz", "Experimental", "Flamenco", "Free Jazz",
    "Funk", "Fusion", "Garage House", "Gospel", "Hard Bop", "Highlife",
    "House", "Italo-Disco", "Jazz-Funk", "Jungle", "Krautrock", "Laïkó",
    "Latin Jazz", "Library Music", "Lounge", "Lo-Fi", "MPB", "Neo Soul",
    "New Wave", "No Wave", "Pachanga", "Post-Punk", "Prog Rock",
    "Psychedelic Rock", "Raï", "Rebetiko", "Rock & Roll", "Salsa", "Samba",
    "Shoegaze", "Ska", "Soul", "Soul-Jazz", "Spiritual Jazz", "Synth-pop",
    "Techno", "Trip Hop", "Tropicália",
]


# ─── Era presets ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Preset:
    label: str
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    country: str = ""
    genre: str = ""
    style: str = ""


_PRESETS: list[_Preset] = [
    _Preset("70s Soul / Funk", 1970, 1979, genre="Funk / Soul"),
    _Preset("60s–70s Jazz", 1960, 1979, genre="Jazz"),
    _Preset("Greek 60s–80s", 1960, 1989, country="Greece"),
    _Preset("Library / OST", 1960, 1985, genre="Stage & Screen",
            style="Library Music"),
    _Preset("Brazilian", 1965, 1985, country="Brazil"),
    _Preset("Spiritual Jazz", 1965, 1979, genre="Jazz",
            style="Spiritual Jazz"),
    _Preset("Everything (weighted)"),
]


class DigitalCrateTab(ctk.CTkFrame):
    """Discovery tab. One instance per app."""

    def __init__(self, parent: ctk.CTkBaseClass, ctx: "AppContext") -> None:
        super().__init__(parent, fg_color=ctx.theme.surface.app)

        self._ctx = ctx
        self._theme = ctx.theme
        self._log = ctx.logger.getChild("digital_crate")

        # State
        self._digging = False
        self._dig_lock = threading.Lock()
        self._cards: list[_ReelCard] = []

        # Widget refs
        self._from_var: Optional[ctk.StringVar] = None
        self._to_var: Optional[ctk.StringVar] = None
        self._query_var: Optional[ctk.StringVar] = None
        self._country_var: Optional[ctk.StringVar] = None
        self._format_var: Optional[ctk.StringVar] = None
        self._genre_var: Optional[ctk.StringVar] = None
        self._style_var: Optional[ctk.StringVar] = None
        self._prioritize_var: Optional[ctk.BooleanVar] = None
        self._compilations_var: Optional[ctk.BooleanVar] = None
        self._intensity_slider: Optional[ctk.CTkSlider] = None
        self._reel_size_var: Optional[ctk.StringVar] = None
        self._dig_button: Optional[ctk.CTkButton] = None
        self._dig_spinner: Optional[DigRouletteSpinner] = None
        self._dig_status_label: Optional[ctk.CTkLabel] = None
        self._filters_card: Optional[ctk.CTkFrame] = None
        self._preset_buttons: list[ctk.CTkButton] = []
        self._health_label: Optional[ctk.CTkLabel] = None
        self._reel_frame: Optional[ctk.CTkFrame] = None
        self._reel_empty: Optional[ctk.CTkFrame] = None
        self._recent_frame: Optional[ctk.CTkFrame] = None
        self._token_warning_card: Optional[ctk.CTkFrame] = None

        self._build_body()
        self._ctx.on_config_changed(self._on_config_changed)
        self._refresh_token_gate()
        self._update_health()
        self._refresh_recent_digs()

    # ── Tab lifecycle (app shell calls on show / re-click) ──

    def on_tab_visible(self) -> None:
        """Refresh gate, prefs, and health when the user returns to this tab."""
        self._sync_discovery_prefs()
        self._refresh_token_gate()
        self._update_health()

    def on_tab_hidden(self) -> None:
        """Pause reel previews when leaving the tab."""
        for card in self._cards:
            try:
                card.pause_preview()
            except Exception:
                pass

    def _on_config_changed(self) -> None:
        self._sync_discovery_prefs()
        self._refresh_token_gate()
        self._update_health()

    # ── Body construction ──

    def _build_body(self) -> None:
        t = self._theme
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=t.surface.app,
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=t.border.strong,
            scrollbar_button_hover_color=t.accent.purple,
        )
        scroll.grid(row=0, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        content = ctk.CTkFrame(scroll, fg_color="transparent")
        content.grid(row=0, column=0, sticky="ew", padx=t.space.xl,
                     pady=(t.space.xxl, t.space.xl))
        content.grid_columnconfigure(0, weight=1)
        self._content = content
        content.bind("<Configure>", self._on_content_configure)

        ctk.CTkLabel(content, text="Digital Crate",
                     **style_label_heading(t)).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            content,
            text=(
                "Spin the crate roulette. Pick an era preset or dial in "
                "filters, then Dig a reel of sample-friendly gems — preview "
                "each in-app and queue your favorites."
            ),
            **style_label_meta(t), wraplength=760, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xs, t.space.xl))

        next_row = 2
        next_row = self._build_token_warning(content, next_row)
        next_row = self._build_presets(content, next_row)
        next_row = self._build_filters_card(content, next_row)
        next_row = self._build_dig_row(content, next_row)
        next_row = self._build_reel_area(content, next_row)
        next_row = self._build_recent_digs(content, next_row)

    def _build_token_warning(self, parent, row: int) -> int:
        t = self._theme
        card = ctk.CTkFrame(
            parent, fg_color=t.surface.raised, border_color=t.status.warning,
            border_width=t.stroke.hairline, corner_radius=t.radius.lg,
        )
        card.grid(row=row, column=0, sticky="ew", pady=(0, t.space.lg))
        card.grid_columnconfigure(1, weight=1)
        card.grid_remove()

        ctk.CTkLabel(card, text="⚠", text_color=t.status.warning,
                     font=t.font.heading, width=36).grid(
            row=0, column=0, sticky="w", padx=(t.space.lg, 0), pady=t.space.md)
        msg = ctk.CTkFrame(card, fg_color="transparent")
        msg.grid(row=0, column=1, sticky="ew", padx=(t.space.sm, t.space.md),
                 pady=t.space.md)
        ctk.CTkLabel(msg, text="Discogs API token required",
                     text_color=t.text.primary, font=t.font.body_emphasis,
                     anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            msg, text="Add a personal access token in Settings to enable discovery.",
            text_color=t.text.secondary, font=t.font.caption, anchor="w",
            wraplength=520, justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ctk.CTkButton(card, text="Open Settings",
                      command=lambda: self._ctx.switch_to_tab("settings"),
                      **style_secondary_button(t), width=130).grid(
            row=0, column=2, sticky="e", padx=(0, t.space.lg), pady=t.space.md)
        self._token_warning_card = card
        return row + 1

    def _sync_discovery_prefs(self) -> None:
        """Pull discovery prefs from config (e.g. after Settings changes)."""
        disc = self._ctx.config.snapshot().config.discovery
        if self._prioritize_var is not None:
            self._prioritize_var.set(disc.prioritize_samples)
        if self._intensity_slider is not None:
            self._intensity_slider.set(disc.sample_weight_intensity)
        if self._compilations_var is not None:
            self._compilations_var.set(disc.allow_compilations)
        if self._reel_size_var is not None:
            self._reel_size_var.set(str(disc.reel_size))

    def _build_presets(self, parent, row: int) -> int:
        t = self._theme
        ctk.CTkLabel(parent, text="Era presets",
                     **style_label_subheading(t)).grid(
            row=row, column=0, sticky="w", pady=(0, t.space.sm))

        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=row + 1, column=0, sticky="ew", pady=(0, t.space.lg))
        # Use a flowing row of chip buttons.
        col = 0
        self._preset_buttons = []
        for preset in _PRESETS:
            btn = ctk.CTkButton(
                wrap, text=preset.label,
                command=lambda p=preset: self._apply_preset(p),
                fg_color=t.surface.raised, hover_color=t.surface.elevated,
                text_color=t.text.primary, border_color=t.border.strong,
                border_width=t.stroke.regular, corner_radius=t.radius.pill,
                font=t.font.caption, height=32,
            )
            btn.grid(row=0, column=col, padx=(0, t.space.sm), pady=t.space.xxs,
                     sticky="w")
            self._preset_buttons.append(btn)
            col += 1
        return row + 2

    def _build_filters_card(self, parent, row: int) -> int:
        t = self._theme
        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=row, column=0, sticky="ew", pady=(0, t.space.lg))
        card.grid_columnconfigure(0, weight=1)
        self._filters_card = card

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl, pady=t.space.xl)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(inner, text="Filters", **style_label_subheading(t)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, t.space.md))

        self._from_var = ctk.StringVar(value="")
        self._to_var = ctk.StringVar(value="")
        self._format_var = ctk.StringVar(value="")
        self._country_var = ctk.StringVar(value="")
        self._genre_var = ctk.StringVar(value="")
        self._style_var = ctk.StringVar(value="")
        self._query_var = ctk.StringVar(value="")

        # Era range: From / To
        era_wrap = ctk.CTkFrame(inner, fg_color="transparent")
        self._build_filter_field(inner, 1, 0, label="Era (from → to)",
                                 widget=era_wrap)
        era_wrap.grid_columnconfigure(0, weight=1)
        era_wrap.grid_columnconfigure(2, weight=1)
        YearSpinner(era_wrap, t, self._from_var, min_year=1900, max_year=2035,
                    placeholder="From").grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(era_wrap, text="→", text_color=t.text.muted,
                     font=t.font.body).grid(row=0, column=1, padx=t.space.sm)
        YearSpinner(era_wrap, t, self._to_var, min_year=1900, max_year=2035,
                    placeholder="To").grid(row=0, column=2, sticky="ew")

        self._build_filter_field(
            inner, 1, 1, label="Format",
            widget=self._make_combobox(inner, self._format_var, _FORMAT_CHOICES))
        self._build_filter_field(
            inner, 3, 0, label="Country",
            widget=self._make_combobox(inner, self._country_var, _COUNTRY_CHOICES))
        self._build_filter_field(
            inner, 3, 1, label="Genre",
            widget=self._make_combobox(inner, self._genre_var, _GENRE_CHOICES))
        self._build_filter_field(
            inner, 5, 0, label="Style",
            widget=self._make_combobox(inner, self._style_var, _STYLE_CHOICES))
        self._build_filter_field(
            inner, 5, 1, label="Search keywords",
            widget=self._make_entry(inner, self._query_var, "Artist, title, etc."))

        # Weighting + options row
        opts = ctk.CTkFrame(inner, fg_color="transparent")
        opts.grid(row=7, column=0, columnspan=2, sticky="ew",
                  pady=(t.space.md, 0))
        opts.grid_columnconfigure(0, weight=1)

        snap = self._ctx.config.snapshot()
        disc = snap.config.discovery

        self._prioritize_var = ctk.BooleanVar(value=disc.prioritize_samples)
        prio_row = ctk.CTkFrame(opts, fg_color="transparent")
        prio_row.grid(row=0, column=0, sticky="w")
        ctk.CTkSwitch(
            prio_row, text="Prioritize sample-friendly gems",
            variable=self._prioritize_var, onvalue=True, offvalue=False,
            command=self._on_prioritize_toggle,
            progress_color=t.accent.purple, button_color=t.text.primary,
            button_hover_color=t.text.primary, fg_color=t.surface.elevated,
            text_color=t.text.primary, font=t.font.body,
        ).pack(side="left")

        # Intensity slider
        intensity_row = ctk.CTkFrame(opts, fg_color="transparent")
        intensity_row.grid(row=1, column=0, sticky="w", pady=(t.space.sm, 0))
        ctk.CTkLabel(intensity_row, text="Tilt strength",
                     **style_label_meta(t)).pack(side="left",
                                                 padx=(0, t.space.sm))
        self._intensity_slider = ctk.CTkSlider(
            intensity_row, from_=0.0, to=1.0, number_of_steps=20, width=180,
            button_color=t.accent.purple, button_hover_color=t.accent.purple_bright,
            progress_color=t.accent.purple, fg_color=t.surface.elevated,
        )
        self._intensity_slider.set(disc.sample_weight_intensity)
        self._intensity_slider.pack(side="left")

        # Compilations + reel size
        self._compilations_var = ctk.BooleanVar(value=disc.allow_compilations)
        comp_row = ctk.CTkFrame(opts, fg_color="transparent")
        comp_row.grid(row=2, column=0, sticky="w", pady=(t.space.sm, 0))
        ctk.CTkSwitch(
            comp_row, text="Include compilations (Various Artists)",
            variable=self._compilations_var, onvalue=True, offvalue=False,
            progress_color=t.accent.purple, button_color=t.text.primary,
            button_hover_color=t.text.primary, fg_color=t.surface.elevated,
            text_color=t.text.primary, font=t.font.body,
        ).pack(side="left")

        size_row = ctk.CTkFrame(opts, fg_color="transparent")
        size_row.grid(row=3, column=0, sticky="w", pady=(t.space.sm, 0))
        ctk.CTkLabel(size_row, text="Reel size",
                     **style_label_meta(t)).pack(side="left",
                                                padx=(0, t.space.sm))
        self._reel_size_var = ctk.StringVar(value=str(disc.reel_size))
        self._make_size_dropdown(size_row).pack(side="left")

        ctk.CTkLabel(
            inner,
            text="Leave fields blank for a broad, weighted roulette. "
                 "Nothing is ever fully excluded.",
            **style_label_meta(t), anchor="w",
        ).grid(row=8, column=0, columnspan=2, sticky="w",
               pady=(t.space.md, 0))
        return row + 1

    def _make_size_dropdown(self, parent) -> ctk.CTkOptionMenu:
        t = self._theme
        return ctk.CTkOptionMenu(
            parent, variable=self._reel_size_var,
            values=[str(n) for n in (4, 6, 8, 10, 12, 16)],
            fg_color=t.surface.raised, button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated, text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay, font=t.font.body,
            corner_radius=t.radius.md, width=80, height=32,
        )

    def _make_entry(self, parent, variable: ctk.StringVar,
                    placeholder: str) -> ctk.CTkFrame:
        t = self._theme
        wrapper = ctk.CTkFrame(parent, fg_color=t.border.strong, border_width=0,
                               corner_radius=t.radius.md)
        ctk.CTkEntry(
            wrapper, textvariable=variable, placeholder_text=placeholder,
            fg_color=t.surface.raised, border_width=0, text_color=t.text.primary,
            placeholder_text_color=t.text.muted, font=t.font.body,
            corner_radius=max(0, t.radius.md - 2), height=38,
        ).pack(fill="x", padx=2, pady=2)
        return wrapper

    def _make_combobox(self, parent, variable: ctk.StringVar,
                       values: list[str]) -> ctk.CTkFrame:
        t = self._theme
        wrapper = ctk.CTkFrame(parent, fg_color=t.border.strong, border_width=0,
                               corner_radius=t.radius.md)
        cb = ctk.CTkComboBox(
            wrapper, variable=variable, values=values,
            fg_color=t.surface.raised, border_width=0,
            button_color=t.surface.raised, button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated, text_color=t.text.primary,
            text_color_disabled=t.text.muted, dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay, font=t.font.body,
            corner_radius=max(0, t.radius.md - 2), height=38,
        )
        cb.pack(fill="x", padx=2, pady=2)

        def on_wheel(event):
            current = variable.get()
            try:
                idx = values.index(current)
            except ValueError:
                idx = 0
            new_idx = (max(0, idx - 1) if event.delta > 0
                       else min(len(values) - 1, idx + 1))
            variable.set(values[new_idx])

        cb.bind("<MouseWheel>", on_wheel)
        return wrapper

    def _build_filter_field(self, parent, row: int, col: int, *, label: str,
                            widget: ctk.CTkBaseClass) -> None:
        t = self._theme
        ctk.CTkLabel(parent, text=label, **style_label_body(t), anchor="w").grid(
            row=row, column=col, sticky="w",
            padx=(0 if col == 0 else t.space.md, t.space.md), pady=(t.space.sm, 2))
        widget.grid(row=row + 1, column=col, sticky="ew",
                    padx=(0 if col == 0 else t.space.md, t.space.md))
        parent.grid_columnconfigure(col, weight=1)

    def _build_dig_row(self, parent, row: int) -> int:
        t = self._theme
        dig_row = ctk.CTkFrame(parent, fg_color="transparent")
        dig_row.grid(row=row, column=0, sticky="ew", pady=(0, t.space.lg))

        self._dig_button = ctk.CTkButton(
            dig_row, text="◆   Dig the crate", command=self._on_dig_clicked,
            **style_primary_button(t), width=240)
        self._dig_button.configure(font=t.font.subheading, height=52)
        self._dig_button.pack(side="left")

        status_frame = ctk.CTkFrame(dig_row, fg_color="transparent")
        status_frame.pack(side="left", padx=(t.space.lg, 0))
        self._dig_spinner = DigRouletteSpinner(status_frame, t)
        self._dig_status_label = ctk.CTkLabel(
            status_frame, text="", text_color=t.text.secondary,
            font=t.font.body, anchor="w")
        self._dig_status_label.pack(side="left")

        self._health_label = ctk.CTkLabel(
            dig_row, text="", text_color=t.text.muted, font=t.font.micro,
            anchor="e")
        self._health_label.pack(side="right")

        self._set_dig_status(None)
        return row + 1

    def _build_reel_area(self, parent, row: int) -> int:
        t = self._theme
        ctk.CTkLabel(parent, text="The reel",
                     **style_label_subheading(t)).grid(
            row=row, column=0, sticky="w", pady=(0, t.space.md))

        holder = ctk.CTkFrame(parent, fg_color="transparent")
        holder.grid(row=row + 1, column=0, sticky="ew")
        holder.grid_columnconfigure(0, weight=1)
        self._reel_frame = holder

        empty = ctk.CTkFrame(parent, **style_card_elevated(t))
        empty.grid(row=row + 1, column=0, sticky="ew")
        empty.grid_columnconfigure(0, weight=1)
        empty_inner = ctk.CTkFrame(empty, fg_color="transparent")
        empty_inner.grid(row=0, column=0, pady=t.space.xxxl)
        ctk.CTkLabel(empty_inner, text="Nothing dug up yet.",
                     text_color=t.text.secondary, font=t.font.subheading).pack()
        ctk.CTkLabel(empty_inner,
                     text="Pick a preset or set filters, then hit Dig.",
                     **style_label_meta(t)).pack(pady=(t.space.xs, 0))
        self._reel_empty = empty
        return row + 2

    def _build_recent_digs(self, parent, row: int) -> int:
        t = self._theme
        ctk.CTkLabel(parent, text="Recent digs",
                     **style_label_subheading(t)).grid(
            row=row, column=0, sticky="w", pady=(t.space.xl, t.space.md))
        frame = ctk.CTkFrame(parent, **style_card_elevated(t))
        frame.grid(row=row + 1, column=0, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)
        self._recent_frame = frame
        return row + 2

    # ── Presets ──

    def _apply_preset(self, preset: _Preset) -> None:
        self._from_var.set(str(preset.year_min) if preset.year_min else "")
        self._to_var.set(str(preset.year_max) if preset.year_max else "")
        self._country_var.set(preset.country)
        self._genre_var.set(preset.genre)
        self._style_var.set(preset.style)
        self._format_var.set("")
        self._query_var.set("")
        # One-click feel: presets immediately dig.
        self._on_dig_clicked()

    def _on_prioritize_toggle(self) -> None:
        # Persist so the choice sticks across sessions.
        try:
            self._ctx.config.update_discovery(
                prioritize_samples=bool(self._prioritize_var.get()))
        except Exception:
            self._log.debug("Could not persist prioritize toggle", exc_info=True)

    # ── Dig ──

    def _refresh_token_gate(self) -> None:
        snap = self._ctx.config.snapshot()
        ready = bool(snap.discogs_token and self._ctx.discovery)

        if self._token_warning_card is not None:
            if ready:
                self._token_warning_card.grid_remove()
            else:
                self._token_warning_card.grid()

        if self._dig_button is not None and not self._digging:
            self._dig_button.configure(
                state="normal" if ready else "disabled",
            )

        for btn in self._preset_buttons:
            if not self._digging:
                btn.configure(state="normal" if ready else "disabled")

    def _set_filters_enabled(self, enabled: bool) -> None:
        for btn in self._preset_buttons:
            if enabled:
                snap = self._ctx.config.snapshot()
                ready = bool(snap.discogs_token and self._ctx.discovery)
                btn.configure(state="normal" if ready else "disabled")
            else:
                btn.configure(state="disabled")

    def _on_dig_clicked(self) -> None:
        with self._dig_lock:
            if self._digging:
                return
            self._digging = True
        self._set_dig_in_flight(True)

        snap = self._ctx.config.snapshot()
        if not snap.discogs_token or not self._ctx.discovery:
            self._set_dig_in_flight(False)
            self._digging = False
            self._ctx.publish_toast("Add a Discogs token in Settings.",
                                    "warning")
            self._refresh_token_gate()
            return

        filters = self._collect_filters()
        count = self._collect_reel_size()
        try:
            self._ctx.config.update_discovery(
                prioritize_samples=bool(self._prioritize_var.get()),
                sample_weight_intensity=round(float(intensity := (
                    self._intensity_slider.get()
                    if self._intensity_slider is not None else 0.6
                )), 2),
                allow_compilations=bool(self._compilations_var.get()),
                reel_size=count,
            )
        except Exception:
            self._log.debug("Could not persist discovery prefs", exc_info=True)
        threading.Thread(target=self._run_dig_worker, args=(filters, count),
                         daemon=True).start()

    def _run_dig_worker(self, filters: DiscoveryFilters, count: int) -> None:
        results: list[DiscoverySuggestion] = []
        error: Optional[Exception] = None
        try:
            results = self._ctx.discovery.dig_many(filters, count=count)
        except Exception as e:  # noqa: BLE001 — surfaced via toast
            error = e
        self.after(0, lambda: self._on_dig_finished(results, error))

    def _on_dig_finished(self, results: list[DiscoverySuggestion],
                         error: Optional[Exception]) -> None:
        self._digging = False
        self._set_dig_in_flight(False)
        self._update_health()
        if error is not None:
            self._handle_dig_error(error)
            return
        if not results:
            self._ctx.publish_toast("No results found. Try wider filters.",
                                    "warning")
            return
        self._render_reel(results)
        self._ctx.publish_toast(
            f"Dug up {len(results)} gem{'s' if len(results) != 1 else ''}.",
            "success")

    def _handle_dig_error(self, error: Exception) -> None:
        if isinstance(error, NoResultsError):
            snap = self._ctx.config.snapshot().config.discovery
            self._ctx.publish_toast(
                "No diggable records in your want/have window. "
                f"With no filters we explore a random genre — try lowering "
                f"Min Have (now {snap.default_min_have}) or raising "
                f"Max Have (now {snap.max_have}) in Settings.",
                "info",
            )
        elif isinstance(error, NoYouTubeMatchError):
            self._ctx.publish_toast(
                "Found records on Discogs but none resolved on YouTube Music. "
                "Try a broader dig.", "warning")
        elif isinstance(error, DiscoveryThrottledError):
            self._ctx.publish_toast(
                "Discogs/YouTube rate-limited us. Wait a moment and dig again.",
                "warning")
        elif isinstance(error, DiscoveryConfigError):
            self._ctx.publish_toast(str(error), "error")
            self._refresh_token_gate()
        else:
            self._ctx.publish_toast(f"Dig failed: {error}", "error")

    def _render_reel(self, results: list[DiscoverySuggestion]) -> None:
        self._clear_cards()
        self._reel_empty.grid_remove()
        t = self._theme
        for i, s in enumerate(results):
            card = _ReelCard(
                self._reel_frame, self._ctx, s,
                on_queued=self._on_card_queued,
            )
            card.grid(row=i, column=0, sticky="ew", pady=(0, t.space.md))
            self._cards.append(card)

    def _on_card_queued(self, suggestion: DiscoverySuggestion) -> None:
        self._refresh_recent_digs()

    def _clear_cards(self) -> None:
        for card in self._cards:
            try:
                card.teardown()
                card.destroy()
            except Exception:
                pass
        self._cards = []

    # ── Recent digs ──

    def _refresh_recent_digs(self) -> None:
        frame = self._recent_frame
        if frame is None:
            return
        t = self._theme
        for child in frame.winfo_children():
            child.destroy()

        try:
            rows = self._ctx.database.list_recent_discoveries(limit=12)
        except Exception:
            rows = []

        if not rows:
            ctk.CTkLabel(frame, text="No digs recorded yet.",
                         **style_label_meta(t)).grid(
                row=0, column=0, sticky="w", padx=t.space.lg, pady=t.space.lg)
            return

        for i, rec in enumerate(rows):
            row_frame = ctk.CTkFrame(frame, fg_color="transparent")
            row_frame.grid(row=i, column=0, sticky="ew", padx=t.space.lg,
                           pady=(t.space.sm if i == 0 else 2,
                                 t.space.sm if i == len(rows) - 1 else 2))
            row_frame.grid_columnconfigure(0, weight=1)

            name = f"{rec.artist} — {rec.title}"
            ctk.CTkLabel(row_frame, text=name, text_color=t.text.primary,
                         font=t.font.body, anchor="w").grid(
                row=0, column=0, sticky="w")

            meta_parts = [str(x) for x in (rec.year, rec.country,
                                           rec.style or rec.genre) if x]
            ctk.CTkLabel(row_frame, text="   ·   ".join(meta_parts),
                         text_color=t.text.muted, font=t.font.micro,
                         anchor="e").grid(row=0, column=1, sticky="e",
                                          padx=(t.space.md, t.space.sm))

            badge = "queued" if rec.was_queued else "seen"
            color = t.status.success if rec.was_queued else t.text.muted
            ctk.CTkLabel(row_frame, text=badge, text_color=color,
                         font=t.font.micro).grid(row=0, column=2, sticky="e")

    # ── Health ──

    def _update_health(self) -> None:
        if self._health_label is None:
            return
        if self._ctx.discovery is None:
            self._health_label.configure(text="Discovery offline — add API token")
            return
        try:
            stats = self._ctx.discovery.get_stats()
        except Exception:
            self._health_label.configure(text="")
            return
        parts = [
            f"Discogs {stats.discogs_requests}",
            f"YTM {stats.ytm_requests}",
        ]
        if stats.throttle_events:
            parts.append(f"⚠ {stats.throttle_events} throttles")
        self._health_label.configure(text="   ·   ".join(parts))

    # ── Dig button state ──

    def _set_dig_in_flight(self, in_flight: bool) -> None:
        t = self._theme
        self._set_filters_enabled(not in_flight)
        if in_flight:
            self._dig_button.configure(text="Digging…", state="disabled",
                                       fg_color=t.accent.blue_dim)
            self._set_dig_status("Spinning the crate — searching Discogs & YouTube Music…")
        else:
            self._refresh_token_gate()
            self._dig_button.configure(text="◆   Dig the crate",
                                       fg_color=t.accent.blue)
            self._set_dig_status(None)

    def _set_dig_status(self, message: Optional[str]) -> None:
        if message:
            self._dig_spinner.start()
            self._dig_status_label.configure(text=message)
        else:
            self._dig_spinner.stop()
            self._dig_status_label.configure(text="")

    # ── Filter collection ──

    def _collect_filters(self) -> DiscoveryFilters:
        def parse_year(var: ctk.StringVar) -> Optional[int]:
            s = var.get().strip()
            return int(s) if s.isdigit() else None

        def norm(v: str) -> Optional[str]:
            return v.strip() or None

        year_min = parse_year(self._from_var)
        year_max = parse_year(self._to_var)
        # Swap if reversed so a sloppy entry still works.
        if year_min is not None and year_max is not None and year_min > year_max:
            year_min, year_max = year_max, year_min

        snap = self._ctx.config.snapshot()
        intensity = (self._intensity_slider.get()
                     if self._intensity_slider is not None
                     else snap.config.discovery.sample_weight_intensity)

        return DiscoveryFilters(
            year=None,
            year_min=year_min,
            year_max=year_max,
            country=norm(self._country_var.get()),
            genre=norm(self._genre_var.get()),
            style=norm(self._style_var.get()),
            format=norm(self._format_var.get()),
            query=norm(self._query_var.get()),
            min_have=snap.config.discovery.default_min_have,
            max_have=snap.config.discovery.max_have,
            prioritize_samples=bool(self._prioritize_var.get()),
            sample_intensity=float(intensity),
            allow_compilations=bool(self._compilations_var.get()),
        )

    def _collect_reel_size(self) -> int:
        try:
            return int(self._reel_size_var.get())
        except (ValueError, AttributeError):
            return self._ctx.config.snapshot().config.discovery.reel_size

    def _on_content_configure(self, _event) -> None:
        t = self._theme
        parent_width = self._content.master.winfo_width()
        target = min(1000, max(480, parent_width - 2 * t.space.xl))
        if abs(self._content.winfo_width() - target) > 8:
            self._content.configure(width=target)


# ─── MPC workflow progress dialog ────────────────────────────────────


class _MpcWorkflowDialog(ctk.CTkToplevel):
    """Choose export contents, then show progress for MPC workflow."""

    _WIDTH = 520
    _HEIGHT_CHOICE = 340
    _HEIGHT_PROGRESS = 240

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        ctx: "AppContext",
        suggestion: DiscoverySuggestion,
        *,
        on_finished: Callable[[bool], None],
    ) -> None:
        super().__init__(parent)

        self._ctx = ctx
        self._theme = ctx.theme
        self._s = suggestion
        self._on_finished = on_finished
        self._log = ctx.logger.getChild("mpc_dialog")
        self._cancel_event = threading.Event()
        self._finished = False
        self._mode: Optional[MpcExportMode] = None
        self._body_frame: Optional[ctk.CTkFrame] = None
        self._status_label: Optional[ctk.CTkLabel] = None
        self._progress_bar: Optional[ctk.CTkProgressBar] = None
        self._cancel_btn: Optional[ctk.CTkButton] = None
        self._close_btn: Optional[ctk.CTkButton] = None

        t = self._theme
        self.title("MPC Workflow")
        self.configure(fg_color=t.surface.base)
        self.geometry(f"{self._WIDTH}x{self._HEIGHT_CHOICE}")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self._center_over(parent)
        self._body_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._body_frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.xl)
        self._build_choice_phase()

    def _center_over(self, parent: ctk.CTkBaseClass) -> None:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        h = self._HEIGHT_CHOICE
        self.geometry(f"+{px + (pw - self._WIDTH) // 2}+{py + (ph - h) // 2}")

    def _build_choice_phase(self) -> None:
        assert self._body_frame is not None
        frame = self._body_frame
        for child in frame.winfo_children():
            child.destroy()
        frame.grid_columnconfigure(0, weight=1)
        t = self._theme

        ctk.CTkLabel(
            frame,
            text=self._s.display_name,
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
            wraplength=self._WIDTH - 64,
            justify="left",
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            frame,
            text="What should go to your MPC folder?",
            text_color=t.text.secondary,
            font=t.font.body,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xs, t.space.md))

        self._mode_var = ctk.StringVar(value=MpcExportMode.BOTH.value)

        options = (
            (
                MpcExportMode.SONG,
                "Original song only",
                "Full track as original.wav — fastest when you just want the record.",
            ),
            (
                MpcExportMode.STEMS,
                "Stems only",
                f"First {int(MPC_WORKFLOW_MAX_SECONDS)}s split into vocals / drums / bass / other.",
            ),
            (
                MpcExportMode.BOTH,
                "Both (recommended)",
                "Full original.wav plus the trimmed stem set.",
            ),
        )
        for idx, (mode, title, blurb) in enumerate(options):
            row = ctk.CTkFrame(frame, fg_color=t.surface.raised, corner_radius=t.radius.md)
            row.grid(row=2 + idx, column=0, sticky="ew", pady=(0, t.space.sm))
            row.grid_columnconfigure(1, weight=1)

            ctk.CTkRadioButton(
                row,
                text="",
                variable=self._mode_var,
                value=mode.value,
                width=20,
                radiobutton_width=18,
                radiobutton_height=18,
                fg_color=t.accent.blue,
                hover_color=t.accent.blue_bright,
            ).grid(row=0, column=0, rowspan=2, padx=(t.space.md, t.space.sm), pady=t.space.md)

            ctk.CTkLabel(
                row, text=title, text_color=t.text.primary,
                font=t.font.body_emphasis, anchor="w",
            ).grid(row=0, column=1, sticky="w", pady=(t.space.sm, 0))

            ctk.CTkLabel(
                row, text=blurb, text_color=t.text.secondary,
                font=t.font.caption, anchor="w", justify="left",
                wraplength=self._WIDTH - 120,
            ).grid(row=1, column=1, sticky="w", pady=(0, t.space.sm))

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="e", pady=(t.space.md, 0))

        ctk.CTkButton(
            buttons, text="Cancel", command=self._cancel_choice,
            **style_secondary_button(t), width=100,
        ).pack(side="right")

        ctk.CTkButton(
            buttons, text="Start export", command=self._begin_export,
            **style_primary_button(t), width=120,
        ).pack(side="right", padx=(0, t.space.sm))

    def _build_progress_phase(self) -> None:
        assert self._body_frame is not None
        frame = self._body_frame
        for child in frame.winfo_children():
            child.destroy()
        frame.grid_columnconfigure(0, weight=1)
        t = self._theme
        self.geometry(f"{self._WIDTH}x{self._HEIGHT_PROGRESS}")
        self._center_over(self.master)

        mode_label = {
            MpcExportMode.SONG: "Exporting original song…",
            MpcExportMode.STEMS: "Exporting stems…",
            MpcExportMode.BOTH: "Exporting original + stems…",
        }.get(self._mode or MpcExportMode.STEMS, "Exporting…")

        ctk.CTkLabel(
            frame,
            text=self._s.display_name,
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
            wraplength=self._WIDTH - 64,
            justify="left",
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            frame,
            text=mode_label,
            text_color=t.text.secondary,
            font=t.font.caption,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xs, t.space.md))

        self._status_label = ctk.CTkLabel(
            frame, text="Starting…", text_color=t.text.secondary,
            font=t.font.body, anchor="w",
        )
        self._status_label.grid(row=2, column=0, sticky="ew")

        self._progress_bar = ctk.CTkProgressBar(
            frame, height=10, progress_color=t.accent.purple,
        )
        self._progress_bar.grid(
            row=3, column=0, sticky="ew", pady=(t.space.sm, t.space.md),
        )
        self._progress_bar.set(0.0)

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="e")

        self._cancel_btn = ctk.CTkButton(
            buttons, text="Cancel", command=self._on_cancel,
            **style_danger_button(t), width=100,
        )
        self._cancel_btn.pack(side="right")

        self._close_btn = ctk.CTkButton(
            buttons, text="Close", command=self._close,
            **style_secondary_button(t), width=100, state="disabled",
        )
        self._close_btn.pack(side="right", padx=(0, t.space.sm))

    def _cancel_choice(self) -> None:
        self._on_finished(False)
        self._close()

    def _begin_export(self) -> None:
        try:
            self._mode = MpcExportMode(self._mode_var.get())
        except ValueError:
            self._mode = MpcExportMode.BOTH
        self._build_progress_phase()
        self._start_worker()

    def _start_worker(self) -> None:
        snap = self._ctx.config.snapshot().config
        dest = snap.general.mpc_samples_root
        staging = snap.general.staging_root
        mode = self._mode or MpcExportMode.BOTH
        threading.Thread(
            target=self._worker,
            args=(dest, staging, mode),
            name=f"mpc-workflow-{self._s.youtube_video_id}",
            daemon=True,
        ).start()

    def _worker(
        self, destination_root: str, staging_root: str, mode: MpcExportMode,
    ) -> None:
        s = self._s
        try:
            result = export_sample_to_mpc(
                video_id=s.youtube_video_id,
                artist=s.artist,
                title=s.title,
                destination_root=Path(destination_root),
                staging_root=Path(staging_root),
                preview=self._ctx.preview,  # type: ignore[arg-type]
                stem_separator=self._ctx.stem_separator,  # type: ignore[arg-type]
                exporter=self._ctx.exporter,  # type: ignore[arg-type]
                mode=mode,
                progress_callback=lambda label, pct: self.after(
                    0, lambda l=label, p=pct: self._apply_progress(l, p),
                ),
                cancel_event=self._cancel_event,
                logger=self._log,
            )
        except MpcSampleExportCancelledError:
            self.after(0, self._finish_cancelled)
            return
        except MpcSampleExportError as e:
            self.after(0, lambda err=e: self._finish_error(err))
            return
        except Exception as e:  # noqa: BLE001 — surfaced in dialog
            self._log.exception("Unexpected MPC export failure for %s", s.display_name)
            self.after(0, lambda err=e: self._finish_error(err))
            return
        self.after(0, lambda r=result: self._finish_ok(r))

    def _apply_progress(self, label: str, percent: float) -> None:
        if self._finished or self._status_label is None or self._progress_bar is None:
            return
        self._status_label.configure(text=label)
        self._progress_bar.set(max(0.0, min(1.0, percent / 100.0)))

    def _finish_ok(self, result: MpcSampleResult) -> None:
        if self._finished:
            return
        self._finished = True
        t = self._theme
        parts: list[str] = []
        if result.original is not None:
            parts.append("original")
        if result.stems:
            parts.append(f"{len(result.stems)} stems")
        summary = " + ".join(parts) if parts else "files"
        if self._progress_bar is not None:
            self._progress_bar.set(1.0)
            self._progress_bar.configure(progress_color=t.status.success)
        if self._status_label is not None:
            self._status_label.configure(
                text=f"Done — {summary} saved to {result.track_dir}",
                text_color=t.status.success,
                wraplength=self._WIDTH - 64,
                justify="left",
            )
        if self._cancel_btn is not None:
            self._cancel_btn.configure(state="disabled")
        if self._close_btn is not None:
            self._close_btn.configure(state="normal")
        self._ctx.publish_toast(f"MPC export ready: {result.track_dir}", "success")
        self._on_finished(True)

    def _finish_error(self, error: Exception) -> None:
        if self._finished:
            return
        self._finished = True
        t = self._theme
        if self._progress_bar is not None:
            self._progress_bar.configure(progress_color=t.status.error)
        if self._status_label is not None:
            self._status_label.configure(
                text=f"Failed: {error}",
                text_color=t.status.error,
                wraplength=self._WIDTH - 64,
                justify="left",
            )
        if self._cancel_btn is not None:
            self._cancel_btn.configure(state="disabled")
        if self._close_btn is not None:
            self._close_btn.configure(state="normal")
        self._ctx.publish_toast(f"MPC workflow failed: {error}", "error")
        self._on_finished(False)

    def _finish_cancelled(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._status_label is not None:
            self._status_label.configure(
                text="Cancelled.",
                text_color=self._theme.text.muted,
            )
        if self._cancel_btn is not None:
            self._cancel_btn.configure(state="disabled")
        if self._close_btn is not None:
            self._close_btn.configure(state="normal")
        self._ctx.publish_toast("MPC workflow cancelled.", "warning")
        self._on_finished(False)

    def _on_cancel(self) -> None:
        if self._finished:
            self._close()
            return
        if self._mode is None:
            self._cancel_choice()
            return
        self._cancel_event.set()
        if self._status_label is not None:
            self._status_label.configure(text="Cancelling…")

    def _close(self) -> None:
        try:
            self.destroy()
        except Exception:
            pass


# ─── Reel card ───────────────────────────────────────────────────────


class _ReelCard(ctk.CTkFrame):
    """One discovery suggestion: metadata + waveform preview + actions."""

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        ctx: "AppContext",
        suggestion: DiscoverySuggestion,
        *,
        on_queued: Optional[Callable[[DiscoverySuggestion], None]] = None,
    ) -> None:
        t = ctx.theme
        super().__init__(parent, **style_card_elevated(t))

        self._ctx = ctx
        self._theme = t
        self._log = ctx.logger.getChild("reel_card")
        self._s = suggestion
        self._on_queued = on_queued

        self._cancel_event = threading.Event()
        self._preview_started = False
        self._full_preview_loading = False
        self._recorded = False
        self._queued = False
        self._mpc_exporting = False
        self._mpc_dialog: Optional[_MpcWorkflowDialog] = None
        self._player: Optional[WaveformPlayer] = None
        self._preview_btn: Optional[ctk.CTkButton] = None
        self._queue_btn: Optional[ctk.CTkButton] = None
        self._mpc_btn: Optional[ctk.CTkButton] = None

        self.grid_columnconfigure(0, weight=1)
        self._build()

    def _build(self) -> None:
        t = self._theme
        s = self._s
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl,
                   pady=t.space.lg)
        inner.grid_columnconfigure(0, weight=1)

        # Title / meta
        ctk.CTkLabel(inner, text=s.artist, text_color=t.accent.purple,
                     font=t.font.body_emphasis, anchor="w").grid(
            row=0, column=0, sticky="w")
        ctk.CTkLabel(inner, text=s.title, text_color=t.text.primary,
                     font=t.font.subheading, anchor="w", wraplength=640,
                     justify="left").grid(row=1, column=0, sticky="w",
                                          pady=(t.space.xxs, t.space.xs))

        meta_parts = [str(x) for x in (s.year, s.country, s.style or s.genre)
                      if x]
        conf = int(s.match_score * 100)
        meta_parts.append(f"YT {conf}%")
        ctk.CTkLabel(inner, text="   ·   ".join(meta_parts),
                     text_color=t.text.secondary, font=t.font.caption,
                     anchor="w").grid(row=2, column=0, sticky="w")

        # Sample-friendly chip when the taxonomy rates this highly.
        affinity = sample_affinity(
            genres=[s.genre] if s.genre else [],
            styles=[s.style] if s.style else [],
            country=s.country, year=s.year,
        )
        if affinity >= 1.25:
            ctk.CTkLabel(
                inner, text="◆ sample-friendly", text_color=t.accent.blue,
                font=t.font.micro, anchor="w").grid(row=3, column=0,
                                                    sticky="w",
                                                    pady=(t.space.xxs, 0))

        # Preview player (hidden until loaded)
        self._player = WaveformPlayer(
            inner, t, height=84,
            initial_volume=self._ctx.config.snapshot().config.ui.preview_volume,
        )
        self._player.grid(row=4, column=0, sticky="ew",
                          pady=(t.space.md, t.space.md))
        self._player.grid_remove()

        # Actions
        actions = ctk.CTkFrame(inner, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="w")

        self._preview_btn = ctk.CTkButton(
            actions, text="▶  Preview", command=self._on_preview_clicked,
            **style_secondary_button(t), width=120)
        self._preview_btn.pack(side="left", padx=(0, t.space.sm))

        self._queue_btn = ctk.CTkButton(
            actions, text="Queue it", command=self._on_queue_clicked,
            **style_primary_button(t), width=120)
        self._queue_btn.pack(side="left", padx=(0, t.space.sm))

        self._mpc_btn = ctk.CTkButton(
            actions, text="◈  MPC Workflow", command=self._on_mpc_clicked,
            **style_secondary_button(t), width=150)
        self._mpc_btn.pack(side="left", padx=(0, t.space.sm))

        ctk.CTkButton(actions, text="Discogs",
                      command=self._on_open_discogs, **style_ghost_button(t),
                      width=80).pack(side="left")
        ctk.CTkButton(actions, text="YouTube",
                      command=self._on_open_youtube, **style_ghost_button(t),
                      width=80).pack(side="left", padx=(t.space.sm, 0))

    # ── Preview ──

    def _on_preview_clicked(self) -> None:
        if self._preview_started or self._player is None:
            return
        if self._ctx.preview is None:
            self._ctx.publish_toast("Preview engine unavailable.", "error")
            return
        self._preview_started = True
        self._full_preview_loading = False
        if self._preview_btn is not None:
            self._preview_btn.configure(state="disabled", text="Loading…")
        self._player.grid()
        self._player.set_loading("Fetching quick preview…")
        self._record(was_queued=False)
        threading.Thread(
            target=self._preview_worker,
            kwargs={"full": False},
            daemon=True,
        ).start()

    def _on_load_full_preview(self) -> None:
        if self._full_preview_loading or self._ctx.preview is None:
            return
        self._full_preview_loading = True
        if self._player is not None:
            self._player.set_full_loading(True)
        threading.Thread(
            target=self._preview_worker,
            kwargs={"full": True},
            daemon=True,
        ).start()

    def _preview_worker(self, *, full: bool) -> None:
        try:
            if full:
                data = self._ctx.preview.fetch(
                    self._s.youtube_video_id,
                    cancel_event=self._cancel_event,
                )
            else:
                data = self._ctx.preview.fetch_quick(
                    self._s.youtube_video_id,
                    cancel_event=self._cancel_event,
                )
        except Exception as e:  # noqa: BLE001 — surfaced in the player
            self.after(0, lambda err=e, f=full: self._on_preview_error(err, full=f))
            return
        self.after(0, lambda d=data, f=full: self._on_preview_ready(d, full=f))

    def _on_preview_ready(self, data, *, full: bool) -> None:
        if self._player is None:
            return
        if full:
            self._full_preview_loading = False
            self._player.set_preview(data)
        else:
            self._player.set_preview(
                data,
                on_load_full=self._on_load_full_preview,
            )
        if self._preview_btn is not None:
            self._preview_btn.grid_remove()
            self._preview_btn.pack_forget()

    def _on_preview_error(self, error: Exception, *, full: bool = False) -> None:
        if full:
            self._full_preview_loading = False
            if self._player is not None:
                self._player.set_full_loading(False)
            self._ctx.publish_toast(f"Full preview failed: {error}", "warning")
            return
        if self._player is not None:
            self._player.set_error(f"Preview failed: {error}")
        if self._preview_btn is not None:
            self._preview_btn.configure(state="normal", text="↻  Retry")
        self._preview_started = False

    # ── Actions ──

    def _on_queue_clicked(self) -> None:
        if self._queued:
            return
        s = self._s
        snap = self._ctx.config.snapshot()
        request = PipelineRequest(
            source_url=s.youtube_url,
            enable_stems=snap.config.general.enable_stems_by_default,
            hint_genre=s.genre, hint_country=s.country, hint_year=s.year,
            hint_discogs_master_id=s.discogs_master_id,
            hint_discogs_release_id=s.discogs_release_id,
            source_platform_override="discogs_dig",
        )
        try:
            self._ctx.queue_manager.enqueue(request)
        except Exception as e:  # noqa: BLE001
            self._ctx.publish_toast(f"Could not queue: {e}", "error")
            return
        self._queued = True
        self._record(was_queued=True)
        t = self._theme
        if self._queue_btn is not None:
            self._queue_btn.configure(text="Queued ✓", state="disabled",
                                      fg_color=t.status.success)
        self._ctx.publish_toast(f"Queued: {s.display_name}", "success")
        if self._on_queued is not None:
            self._on_queued(s)

    # ── MPC sample workflow ──

    def _on_mpc_clicked(self) -> None:
        if self._mpc_exporting:
            return
        if self._ctx.preview is None or self._ctx.stem_separator is None \
                or self._ctx.exporter is None:
            self._ctx.publish_toast(
                "MPC workflow is unavailable (missing preview/stems/exporter engine).",
                "error",
            )
            return

        if not (self._s.youtube_video_id or "").strip():
            self._ctx.publish_toast(
                "No YouTube match for this dig — cannot run MPC workflow.",
                "error",
            )
            return

        snap = self._ctx.config.snapshot().config
        dest = Path(snap.general.mpc_samples_root).expanduser()
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._ctx.publish_toast(
                f"MPC destination folder is not writable: {dest} ({e})",
                "error",
            )
            return

        self._mpc_exporting = True
        if self._mpc_btn is not None:
            self._mpc_btn.configure(state="disabled", text="Working…")

        self._mpc_dialog = _MpcWorkflowDialog(
            self.winfo_toplevel(),
            self._ctx,
            self._s,
            on_finished=self._on_mpc_dialog_finished,
        )

    def _on_mpc_dialog_finished(self, success: bool) -> None:
        self._mpc_exporting = False
        self._mpc_dialog = None
        if self._mpc_btn is None:
            return
        t = self._theme
        if success:
            self._mpc_btn.configure(
                text="Sent to MPC ✓",
                state="disabled",
                fg_color=t.status.success,
            )
        else:
            self._mpc_btn.configure(
                text="↻  Retry MPC Workflow",
                state="normal",
                fg_color=t.surface.raised,
            )

    def _record(self, *, was_queued: bool) -> None:
        if self._ctx.discovery is None:
            return
        if self._recorded and not was_queued:
            return
        self._recorded = True
        try:
            self._ctx.discovery.record_suggestion(self._s, was_queued=was_queued)
        except Exception:
            self._log.debug("record_suggestion failed", exc_info=True)

    def _on_open_discogs(self) -> None:
        webbrowser.open(
            f"https://www.discogs.com/master/{self._s.discogs_master_id}")

    def _on_open_youtube(self) -> None:
        webbrowser.open(self._s.youtube_url)

    # ── Teardown ──

    def pause_preview(self) -> None:
        """Stop playback when the tab is hidden (keeps the card alive)."""
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass

    def teardown(self) -> None:
        self._cancel_event.set()
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
