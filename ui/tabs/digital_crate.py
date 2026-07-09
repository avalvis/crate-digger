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

import queue
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk

from core.discovery import (
    DiscoveryCancelledError,
    DiscoveryConfigError,
    DiscoveryError,
    DiscoveryFilters,
    DiscoverySuggestion,
    DiscoveryThrottledError,
    NoResultsError,
    NoYouTubeMatchError,
)
from core.mpc_export import MpcExportMode
from core.pipeline import PipelineRequest
from core.preview_prefetch import PrefetchEvent, PrefetchEventType, PrefetchState
from core.sampling_taxonomy import sample_affinity
from ui.components.mpc_export_manager import MpcExportManagerWindow
from ui.components.dig_roulette_spinner import DigRouletteSpinner
from ui.components.waveform_player import WaveformPlayer
from ui.components.year_spinner import YearSpinner
from ui.theme import (
    Theme,
    style_card_elevated,
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
        self._dig_generation = 0
        self._dig_cancel_event: Optional[threading.Event] = None
        self._dig_progress_queue: queue.Queue[str] = queue.Queue()
        self._dig_progress_after: Optional[str] = None
        self._dig_lock = threading.Lock()
        self._cards: list[_ReelCard] = []
        self._filters_locked = False
        self._filter_widgets: list[ctk.CTkBaseClass] = []
        self._render_generation = 0
        self._pending_render: list[DiscoverySuggestion] = []
        self._render_index = 0
        self._render_after: Optional[str] = None
        self._config_save_after: Optional[str] = None
        self._prefetch_watchdog_after: Optional[str] = None
        self._prefetch_inbox: queue.Queue[PrefetchEvent] = queue.Queue()
        self._mpc_inbox: queue.Queue = queue.Queue()
        self._prefetch_drain_after: Optional[str] = None
        self._mpc_drain_after: Optional[str] = None
        self._prefetch_progress_latest: dict[str, PrefetchEvent] = {}
        self._health_update_after: Optional[str] = None

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
        self._reel_scroll: Optional[ctk.CTkScrollableFrame] = None
        self._scroll_content: Optional[ctk.CTkFrame] = None
        self._recent_frame: Optional[ctk.CTkFrame] = None
        self._token_warning_card: Optional[ctk.CTkFrame] = None
        self._mpc_manager_window: Optional[MpcExportManagerWindow] = None
        self._video_to_card: dict[str, _ReelCard] = {}
        self._scroll_pause_after: Optional[str] = None
        self._prefetch_unsub: Optional[Callable[[], None]] = None
        self._mpc_unsub: Optional[Callable[[], None]] = None

        self._build_body()
        self._wire_prefetch_events()
        self._wire_mpc_events()
        self._schedule_prefetch_drain()
        self._schedule_mpc_drain()
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
        self._cancel_pending_render()
        for card in self._cards:
            try:
                card.pause_preview()
            except Exception:
                pass

    def _on_config_changed(self) -> None:
        self._sync_discovery_prefs()
        self._refresh_token_gate()
        self._update_health()
        pf = self._ctx.preview_prefetch
        if pf is not None:
            disc = self._ctx.config.snapshot().config.discovery
            pf.configure(
                max_workers=disc.preview_prefetch_concurrency,
                keep_decoded=disc.preview_prefetch_keep_decoded,
            )

    # ── Body construction ──

    def _build_body(self) -> None:
        t = self._theme
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color=t.surface.app)
        header.grid(row=0, column=0, sticky="ew", padx=t.space.xl,
                    pady=(t.space.lg, t.space.sm))
        header.grid_columnconfigure(0, weight=1)
        header.grid_columnconfigure(1, weight=0)

        ctk.CTkLabel(header, text="Digital Crate",
                     **style_label_heading(t)).grid(row=0, column=0, sticky="w")
        self._health_label = ctk.CTkLabel(
            header, text="", text_color=t.text.muted, font=t.font.micro,
            anchor="e")
        self._health_label.grid(row=0, column=1, sticky="e", padx=(t.space.md, 0))
        self._update_health()

        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color=t.surface.app,
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=t.border.strong,
            scrollbar_button_hover_color=t.accent.purple,
        )
        scroll.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        scroll.grid_columnconfigure(0, weight=1)
        scroll.grid_rowconfigure(0, weight=1)
        self._reel_scroll = scroll
        self._bind_reel_scroll_pause(scroll)
        self._bind_reel_mousewheel(scroll)

        content = ctk.CTkFrame(scroll, fg_color="transparent")
        content.grid(row=0, column=0, sticky="nsew", padx=t.space.xl,
                     pady=(t.space.md, t.space.xl))
        content.grid_columnconfigure(0, weight=1)
        self._scroll_content = content

        ctk.CTkLabel(
            content,
            text=(
                "Pick an era preset or dial in filters, then Dig a reel of "
                "sample-friendly gems — preview each in-app and queue favorites."
            ),
            **style_label_meta(t), wraplength=760, justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, t.space.lg))

        next_row = 1
        next_row = self._build_token_warning(content, next_row)
        next_row = self._build_presets(content, next_row)
        next_row = self._build_filters_card(content, next_row)
        next_row = self._build_dig_row(content, next_row)
        next_row = self._build_reel_area(content, next_row)
        self._build_recent_digs(content, next_row)

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
        self._from_spinner = YearSpinner(
            era_wrap, t, self._from_var, min_year=1900, max_year=2035,
            placeholder="From",
        )
        self._from_spinner.grid(row=0, column=0, sticky="ew")
        self._filter_widgets.append(self._from_spinner)
        ctk.CTkLabel(era_wrap, text="→", text_color=t.text.muted,
                     font=t.font.body).grid(row=0, column=1, padx=t.space.sm)
        self._to_spinner = YearSpinner(
            era_wrap, t, self._to_var, min_year=1900, max_year=2035,
            placeholder="To",
        )
        self._to_spinner.grid(row=0, column=2, sticky="ew")
        self._filter_widgets.append(self._to_spinner)

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
        self._prioritize_switch = ctk.CTkSwitch(
            prio_row, text="Prioritize sample-friendly gems",
            variable=self._prioritize_var, onvalue=True, offvalue=False,
            command=self._on_prioritize_toggle,
            progress_color=t.accent.purple, button_color=t.text.primary,
            button_hover_color=t.text.primary, fg_color=t.surface.elevated,
            text_color=t.text.primary, font=t.font.body,
        )
        self._prioritize_switch.pack(side="left")
        self._filter_widgets.append(self._prioritize_switch)

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
        self._filter_widgets.append(self._intensity_slider)

        # Compilations + reel size
        self._compilations_var = ctk.BooleanVar(value=disc.allow_compilations)
        comp_row = ctk.CTkFrame(opts, fg_color="transparent")
        comp_row.grid(row=2, column=0, sticky="w", pady=(t.space.sm, 0))
        self._compilations_switch = ctk.CTkSwitch(
            comp_row, text="Include compilations (Various Artists)",
            variable=self._compilations_var, onvalue=True, offvalue=False,
            progress_color=t.accent.purple, button_color=t.text.primary,
            button_hover_color=t.text.primary, fg_color=t.surface.elevated,
            text_color=t.text.primary, font=t.font.body,
        )
        self._compilations_switch.pack(side="left")
        self._filter_widgets.append(self._compilations_switch)

        size_row = ctk.CTkFrame(opts, fg_color="transparent")
        size_row.grid(row=3, column=0, sticky="w", pady=(t.space.sm, 0))
        ctk.CTkLabel(size_row, text="Reel size",
                     **style_label_meta(t)).pack(side="left",
                                                padx=(0, t.space.sm))
        self._reel_size_var = ctk.StringVar(value=str(disc.reel_size))
        self._reel_size_menu = self._make_size_dropdown(size_row)
        self._reel_size_menu.pack(side="left")
        self._filter_widgets.append(self._reel_size_menu)

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
        entry = ctk.CTkEntry(
            wrapper, textvariable=variable, placeholder_text=placeholder,
            fg_color=t.surface.raised, border_width=0, text_color=t.text.primary,
            placeholder_text_color=t.text.muted, font=t.font.body,
            corner_radius=max(0, t.radius.md - 2), height=38,
        )
        entry.pack(fill="x", padx=2, pady=2)
        self._filter_widgets.append(entry)
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
        self._filter_widgets.append(cb)

        def on_wheel(event):
            if self._filters_locked:
                return "break"
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
        dig_row.grid_columnconfigure(0, weight=1)

        self._dig_button = ctk.CTkButton(
            dig_row, text="◆   Dig the crate", command=self._on_dig_clicked,
            **style_primary_button(t), width=280)
        self._dig_button.configure(font=t.font.body_emphasis, height=48)
        self._dig_button.grid(row=0, column=0, pady=(0, t.space.sm))

        status_frame = ctk.CTkFrame(dig_row, fg_color="transparent")
        status_frame.grid(row=1, column=0, sticky="ew")
        self._dig_spinner = DigRouletteSpinner(status_frame, t, size=32)
        self._dig_spinner.pack(side="left")
        self._dig_status_label = ctk.CTkLabel(
            status_frame, text="", text_color=t.text.secondary,
            font=t.font.caption, anchor="w", wraplength=520, justify="left")
        self._dig_status_label.pack(side="left", padx=(t.space.sm, 0))

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
                     text="Set filters above, then Dig the crate to spin up a reel.",
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
        if self._filters_locked or self._digging:
            return
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
        if self._filters_locked:
            return
        self._schedule_discovery_pref_save()

    def _schedule_discovery_pref_save(self) -> None:
        if self._config_save_after is not None:
            try:
                self.after_cancel(self._config_save_after)
            except Exception:
                pass
        self._config_save_after = self.after(400, self._persist_discovery_prefs)

    def _persist_discovery_prefs(self) -> None:
        self._config_save_after = None
        if self._filters_locked:
            self._schedule_discovery_pref_save()
            return
        try:
            self._ctx.config.update_discovery(
                prioritize_samples=bool(self._prioritize_var.get()))
        except Exception:
            self._log.debug("Could not persist prioritize toggle", exc_info=True)

    def _persist_dig_prefs_worker(
        self,
        prioritize: bool,
        intensity: float,
        allow_compilations: bool,
        reel_size: int,
    ) -> None:
        try:
            self._ctx.config.update_discovery(
                prioritize_samples=prioritize,
                sample_weight_intensity=intensity,
                allow_compilations=allow_compilations,
                reel_size=reel_size,
            )
        except Exception:
            self._log.debug("Could not persist discovery prefs", exc_info=True)

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
        self._filters_locked = not enabled
        if not enabled:
            self._dismiss_filter_focus()
        for widget in self._filter_widgets:
            self._set_widget_enabled(widget, enabled)
        for btn in self._preset_buttons:
            if enabled:
                snap = self._ctx.config.snapshot()
                ready = bool(snap.discogs_token and self._ctx.discovery)
                btn.configure(state="normal" if ready else "disabled")
            else:
                btn.configure(state="disabled")

    def _set_widget_enabled(self, widget: ctk.CTkBaseClass, enabled: bool) -> None:
        if isinstance(widget, YearSpinner):
            widget.set_enabled(enabled)
            return
        state = "normal" if enabled else "disabled"
        try:
            widget.configure(state=state)
        except Exception:
            pass

    def _dismiss_filter_focus(self) -> None:
        """Close dropdowns / entries before disabling filter controls."""
        try:
            if self._dig_button is not None:
                self._dig_button.focus_set()
        except Exception:
            try:
                self.focus_set()
            except Exception:
                pass

    def _cancel_pending_render(self) -> None:
        if self._render_after is not None:
            try:
                self.after_cancel(self._render_after)
            except Exception:
                pass
            self._render_after = None
        self._pending_render = []
        self._render_index = 0

    def _is_alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _safe_after(self, ms: int, fn: Callable[[], None]) -> Optional[str]:
        if not self._is_alive():
            return None

        def _wrapped() -> None:
            if not self._is_alive():
                return
            try:
                fn()
            except Exception:
                self._log.exception("Digital Crate deferred callback failed")

        return self.after(ms, _wrapped)

    def _stop_prefetch_workers(self) -> None:
        """Cancel in-flight preview warmup — safe from any thread."""
        self._cancel_prefetch_watchdog()
        while True:
            try:
                self._prefetch_inbox.get_nowait()
            except queue.Empty:
                break
        self._prefetch_progress_latest.clear()
        pf = self._ctx.preview_prefetch
        if pf is not None:
            pf.cancel_batch()

    def _schedule_prefetch_drain(self) -> None:
        if not self._is_alive():
            return
        if self._prefetch_drain_after is not None:
            return
        self._prefetch_drain_after = self.after(33, self._drain_prefetch_inbox)

    def _drain_prefetch_inbox(self) -> None:
        self._prefetch_drain_after = None
        if not self._is_alive():
            return
        processed = 0
        while processed < 48:
            try:
                event = self._prefetch_inbox.get_nowait()
            except queue.Empty:
                break
            if event.type == PrefetchEventType.PROGRESS:
                self._prefetch_progress_latest[event.video_id] = event
            else:
                self._flush_prefetch_progress()
                self._apply_prefetch_event(event)
            processed += 1
        self._flush_prefetch_progress()
        if self._is_alive():
            self._schedule_prefetch_drain()

    def _flush_prefetch_progress(self) -> None:
        if not self._prefetch_progress_latest:
            return
        latest = self._prefetch_progress_latest
        self._prefetch_progress_latest = {}
        for event in latest.values():
            self._apply_prefetch_event(event)

    def _schedule_mpc_drain(self) -> None:
        if not self._is_alive():
            return
        if self._mpc_drain_after is not None:
            return
        self._mpc_drain_after = self.after(33, self._drain_mpc_inbox)

    def _drain_mpc_inbox(self) -> None:
        self._mpc_drain_after = None
        if not self._is_alive():
            return
        processed = 0
        while processed < 32:
            try:
                event = self._mpc_inbox.get_nowait()
            except queue.Empty:
                break
            self._apply_mpc_export_event(event)
            processed += 1
        if self._is_alive():
            self._schedule_mpc_drain()

    def _schedule_health_update(self) -> None:
        if self._health_update_after is not None:
            return
        self._health_update_after = self.after(500, self._do_health_update)

    def _do_health_update(self) -> None:
        self._health_update_after = None
        self._update_health()

    def _on_dig_clicked(self) -> None:
        with self._dig_lock:
            if self._digging:
                self._cancel_dig()
                return
            self._digging = True
            self._dig_generation += 1
            generation = self._dig_generation
            self._dig_cancel_event = threading.Event()
        self._cancel_pending_render()
        self._stop_prefetch_workers()
        self._stop_dig_progress_drain()
        self._set_dig_in_flight(True)

        snap = self._ctx.config.snapshot()
        if not snap.discogs_token or not self._ctx.discovery:
            self._finish_dig_session(generation, unlock_filters=True)
            self._ctx.publish_toast("Add a Discogs token in Settings.",
                                    "warning")
            self._refresh_token_gate()
            return

        filters = self._collect_filters()
        count = self._collect_reel_size()
        intensity = (
            self._intensity_slider.get()
            if self._intensity_slider is not None else 0.6
        )
        self._schedule_discovery_pref_save()
        cancel_event = self._dig_cancel_event
        threading.Thread(
            target=self._persist_dig_prefs_worker,
            args=(
                bool(self._prioritize_var.get()),
                round(float(intensity), 2),
                bool(self._compilations_var.get()),
                count,
            ),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._run_dig_worker,
            args=(filters, count, generation, cancel_event),
            daemon=True,
        ).start()

    def _cancel_dig(self) -> None:
        if self._dig_cancel_event is not None:
            self._dig_cancel_event.set()
        self._set_dig_status("Stopping…", animate=True)
        if self._dig_button is not None:
            self._dig_button.configure(state="disabled")

    def _dig_progress_callback(self, message: str) -> None:
        try:
            self._dig_progress_queue.put_nowait(message)
        except queue.Full:
            pass
        self._schedule_dig_progress_drain()

    def _schedule_dig_progress_drain(self) -> None:
        if not self._is_alive() or not self._digging:
            return
        if self._dig_progress_after is not None:
            return
        self._dig_progress_after = self.after(150, self._drain_dig_progress)

    def _drain_dig_progress(self) -> None:
        self._dig_progress_after = None
        if not self._is_alive() or not self._digging:
            return
        latest: Optional[str] = None
        while True:
            try:
                latest = self._dig_progress_queue.get_nowait()
            except queue.Empty:
                break
        if latest:
            self._set_dig_status(latest, animate=True)
        if self._digging:
            self._schedule_dig_progress_drain()

    def _stop_dig_progress_drain(self) -> None:
        if self._dig_progress_after is not None:
            try:
                self.after_cancel(self._dig_progress_after)
            except Exception:
                pass
            self._dig_progress_after = None
        while True:
            try:
                self._dig_progress_queue.get_nowait()
            except queue.Empty:
                break

    def _run_dig_worker(
        self,
        filters: DiscoveryFilters,
        count: int,
        generation: int,
        cancel_event: Optional[threading.Event],
    ) -> None:
        results: list[DiscoverySuggestion] = []
        error: Optional[Exception] = None
        cancelled = False
        try:
            results = self._ctx.discovery.dig_many(
                filters,
                count=count,
                cancel_event=cancel_event,
                progress=self._dig_progress_callback,
            )
        except DiscoveryCancelledError:
            cancelled = True
        except Exception as e:  # noqa: BLE001 — surfaced via toast
            error = e
        if not self._is_alive():
            return
        if cancelled:
            self._safe_after(
                0, lambda: self._on_dig_cancelled(generation),
            )
            return
        self._safe_after(
            0,
            lambda: self._on_dig_finished(results, error, generation),
        )

    def _on_dig_cancelled(self, generation: int) -> None:
        if generation != self._dig_generation:
            return
        self._ctx.publish_toast("Dig cancelled.", "info")
        self._finish_dig_session(generation, unlock_filters=True)

    def _finish_dig_session(
        self,
        generation: int,
        *,
        unlock_filters: bool,
    ) -> None:
        if generation != self._dig_generation:
            return
        self._digging = False
        self._dig_cancel_event = None
        self._stop_dig_progress_drain()
        if unlock_filters:
            self._set_dig_in_flight(False)
            self._clear_dig_status()

    def _on_dig_finished(
        self,
        results: list[DiscoverySuggestion],
        error: Optional[Exception],
        generation: int,
    ) -> None:
        if generation != self._dig_generation:
            return
        try:
            self._update_health()
            if error is not None:
                self._handle_dig_error(error)
                return
            if not results:
                self._ctx.publish_toast("No results found. Try wider filters.",
                                        "warning")
                return
            self._render_reel_chunked(results, generation)
        except Exception as e:  # noqa: BLE001
            self._log.exception("Dig finish handler failed")
            self._ctx.publish_toast(f"Could not show dig results: {e}", "error")
            self._finish_dig_session(generation, unlock_filters=True)
        finally:
            if error is not None or not results:
                self._finish_dig_session(generation, unlock_filters=True)

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

    def _start_prefetch(self, results: list[DiscoverySuggestion]) -> None:
        """Warm previews in the background — never blocks the dig-complete UX."""
        pf = self._ctx.preview_prefetch
        if pf is None:
            return
        snap = self._ctx.config.snapshot().config.discovery
        if not snap.preview_prefetch_enabled:
            return
        ids = [s.youtube_video_id for s in results if s.youtube_video_id]
        if not ids:
            return
        pf.enqueue_batch(ids)
        self._schedule_prefetch_watchdog()

    def _schedule_prefetch_watchdog(self) -> None:
        if self._prefetch_watchdog_after is not None:
            try:
                self.after_cancel(self._prefetch_watchdog_after)
            except Exception:
                pass
        self._prefetch_watchdog_after = self._safe_after(
            5000, self._prefetch_status_watchdog,
        )

    def _prefetch_status_watchdog(self) -> None:
        self._prefetch_watchdog_after = None
        pf = self._ctx.preview_prefetch
        if pf is None:
            return
        pf.reap_stale_jobs()
        if pf.is_batch_drained() or pf.is_batch_idle():
            self._update_health()
            return
        self._update_health()
        self._schedule_prefetch_watchdog()

    def _render_reel_chunked(
        self,
        results: list[DiscoverySuggestion],
        generation: int,
    ) -> None:
        self._cancel_pending_render()
        self._clear_cards()
        self._video_to_card.clear()
        self._reel_empty.grid_remove()
        self._pending_render = list(results)
        self._render_index = 0
        self._render_generation = generation
        self._set_dig_status("Building reel…", animate=True)
        self._render_next_chunk()

    _REEL_RENDER_CHUNK = 2

    def _render_next_chunk(self) -> None:
        self._render_after = None
        if self._render_generation != self._dig_generation:
            return
        if self._reel_frame is None:
            self._finish_dig_session(self._render_generation, unlock_filters=True)
            return

        t = self._theme
        end = min(
            self._render_index + self._REEL_RENDER_CHUNK,
            len(self._pending_render),
        )
        for i in range(self._render_index, end):
            s = self._pending_render[i]
            try:
                card = _ReelCard(
                    self._reel_frame, self._ctx, s,
                    on_queued=self._on_card_queued,
                    on_mpc_confirm=self._on_card_mpc_confirm,
                )
                card.grid(row=i, column=0, sticky="ew", pady=(0, t.space.md))
                self._cards.append(card)
                if s.youtube_video_id:
                    self._video_to_card[s.youtube_video_id] = card
                    pf = self._ctx.preview_prefetch
                    if pf is not None:
                        card.apply_prefetch_state(
                            pf.get_state(s.youtube_video_id),
                        )
            except Exception:
                self._log.exception("Failed to build reel card for %s", s.display_name)

        self._render_index = end
        if self._render_index < len(self._pending_render):
            self._render_after = self._safe_after(16, self._render_next_chunk)
            self._refresh_reel_scroll()
            return

        self._pending_render = []
        results_count = len(self._cards)
        self._ctx.publish_toast(
            f"Dug up {results_count} gem{'s' if results_count != 1 else ''}.",
            "success",
        )
        self._finish_dig_session(self._render_generation, unlock_filters=True)
        self._start_prefetch([c._s for c in self._cards])
        self._refresh_reel_scroll()

    def _render_reel(self, results: list[DiscoverySuggestion]) -> None:
        """Synchronous render — kept for callers/tests; prefer chunked path."""
        self._render_reel_chunked(results, self._dig_generation)

    def _on_card_mpc_confirm(
        self, suggestion: DiscoverySuggestion, mode: MpcExportMode,
    ) -> None:
        mgr = self._ctx.mpc_export_manager
        if mgr is None:
            self._ctx.publish_toast("MPC export manager unavailable.", "error")
            return
        try:
            self._ensure_mpc_manager_window()
            mgr.enqueue(suggestion, mode)
        except Exception as e:  # noqa: BLE001
            self._ctx.publish_toast(f"Could not queue MPC export: {e}", "error")
            return
        self._ctx.publish_toast(f"Queued MPC export: {suggestion.display_name}", "info")

    def _on_card_queued(self, suggestion: DiscoverySuggestion) -> None:
        self._refresh_recent_digs()

    def _clear_cards(self) -> None:
        ids = [
            c._s.youtube_video_id for c in self._cards
            if c._s.youtube_video_id
        ]
        pf = self._ctx.preview_prefetch
        if pf is not None and ids:
            pf.cancel_batch(ids)
        for card in self._cards:
            try:
                card.teardown()
                card.destroy()
            except Exception:
                pass
        self._cards = []
        self._video_to_card.clear()

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
        pf = self._ctx.preview_prefetch
        if pf is not None and not pf.is_batch_idle():
            done, total = pf.batch_progress()
            if total > 0 and done < total:
                parts.append(f"Previews {done}/{total}")
        self._health_label.configure(text="   ·   ".join(parts))

    # ── Dig button state ──

    def _set_dig_in_flight(self, in_flight: bool) -> None:
        t = self._theme
        self._set_filters_enabled(not in_flight)
        if in_flight:
            self._dig_button.configure(
                text="■   Stop dig", state="normal",
                fg_color=t.status.warning,
                hover_color=t.accent.blue_dim,
            )
            self._set_dig_status(
                "Searching Discogs…",
                animate=True,
            )
            self._schedule_dig_progress_drain()
        else:
            self._refresh_token_gate()
            self._dig_button.configure(text="◆   Dig the crate",
                                       fg_color=t.accent.blue)

    def _clear_dig_status(self) -> None:
        try:
            self._dig_spinner.stop()
            self._dig_status_label.configure(text="")
        except Exception:
            pass

    def _cancel_prefetch_watchdog(self) -> None:
        if self._prefetch_watchdog_after is not None:
            try:
                self.after_cancel(self._prefetch_watchdog_after)
            except Exception:
                pass
            self._prefetch_watchdog_after = None

    def _cancel_prefetch_status_updates(self) -> None:
        self._cancel_prefetch_watchdog()

    def _set_dig_status(self, message: Optional[str], *, animate: bool = True) -> None:
        try:
            if message:
                if animate:
                    self._dig_spinner.start()
                else:
                    self._dig_spinner.stop()
                self._dig_status_label.configure(text=message)
            else:
                self._clear_dig_status()
        except Exception:
            self._log.debug("Could not update dig status", exc_info=True)

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

    def _bind_reel_mousewheel(self, scroll: ctk.CTkScrollableFrame) -> None:
        on_wheel = self._reel_wheel_handler

        for widget in (scroll, self._scroll_content, self._reel_frame):
            if widget is not None:
                widget.bind("<MouseWheel>", on_wheel, add="+")
        try:
            canvas = getattr(scroll, "_parent_canvas", None)
            if canvas is not None:
                canvas.bind("<MouseWheel>", on_wheel, add="+")
                canvas.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"), add="+")
                canvas.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"), add="+")
        except Exception:
            pass

    def _refresh_reel_scroll(self) -> None:
        scroll = self._reel_scroll
        if scroll is None:
            return
        try:
            canvas = getattr(scroll, "_parent_canvas", None)
            if canvas is not None:
                def _update() -> None:
                    try:
                        bbox = canvas.bbox("all")
                        if bbox:
                            canvas.configure(scrollregion=bbox)
                    except Exception:
                        pass
                self.after_idle(_update)
            for card in self._cards:
                try:
                    card.bind("<MouseWheel>", self._reel_wheel_handler, add="+")
                except Exception:
                    pass
        except Exception:
            self._log.debug("Could not refresh reel scroll region", exc_info=True)

    def _reel_wheel_handler(self, event) -> str:
        scroll = self._reel_scroll
        if scroll is None:
            return "break"
        canvas = getattr(scroll, "_parent_canvas", None)
        if canvas is None:
            return "break"
        try:
            delta = int(-1 * (event.delta / 120))
            if delta == 0:
                delta = -1 if event.delta > 0 else 1
            canvas.yview_scroll(delta, "units")
        except Exception:
            pass
        return "break"

    def _bind_reel_scroll_pause(self, scroll: ctk.CTkScrollableFrame) -> None:
        def pause(_event=None) -> None:
            self._set_reel_scroll_paused(True)
            if self._scroll_pause_after is not None:
                try:
                    self.after_cancel(self._scroll_pause_after)
                except Exception:
                    pass
            self._scroll_pause_after = self.after(120, self._resume_reel_scroll)

        scroll.bind("<MouseWheel>", pause, add="+")
        scroll.bind("<Button-4>", pause, add="+")
        scroll.bind("<Button-5>", pause, add="+")
        try:
            sb = getattr(scroll, "_scrollbar", None)
            if sb is not None:
                sb.bind("<ButtonPress-1>", pause, add="+")
                sb.bind("<B1-Motion>", pause, add="+")
        except Exception:
            pass

    def _resume_reel_scroll(self) -> None:
        self._scroll_pause_after = None
        self._set_reel_scroll_paused(False)

    def _set_reel_scroll_paused(self, paused: bool) -> None:
        for card in self._cards:
            try:
                card.set_scroll_paused(paused)
            except Exception:
                pass

    def _wire_prefetch_events(self) -> None:
        pf = self._ctx.preview_prefetch
        if pf is None:
            return
        self._prefetch_unsub = pf.subscribe(self._on_prefetch_event, weak=True)

    def _wire_mpc_events(self) -> None:
        mgr = self._ctx.mpc_export_manager
        if mgr is None:
            return
        self._mpc_unsub = mgr.subscribe(self._on_mpc_export_event, weak=True)

    def _on_prefetch_event(self, event: PrefetchEvent) -> None:
        # Prefetch workers call subscribers off-thread — never touch Tk here.
        try:
            self._prefetch_inbox.put_nowait(event)
        except Exception:
            pass

    def _apply_prefetch_event(self, event: PrefetchEvent) -> None:
        try:
            card = self._video_to_card.get(event.video_id)
            if card is not None:
                if event.type == PrefetchEventType.PROGRESS:
                    card.apply_prefetch_event(event, throttle=True)
                else:
                    card.apply_prefetch_event(event)
            if event.type == PrefetchEventType.BATCH_DRAINED:
                self._schedule_health_update()
        except Exception:
            self._log.exception("Prefetch UI update failed")

    def _on_mpc_export_event(self, event) -> None:
        try:
            self._mpc_inbox.put_nowait(event)
        except Exception:
            pass

    def _apply_mpc_export_event(self, event) -> None:
        card = self._video_to_card.get(event.video_id)
        if card is not None:
            card.apply_mpc_event(event)

    def _ensure_mpc_manager_window(self) -> MpcExportManagerWindow:
        if self._mpc_manager_window is None or not self._mpc_manager_window.winfo_exists():
            self._mpc_manager_window = MpcExportManagerWindow(
                self.winfo_toplevel(), self._ctx,
            )
        self._mpc_manager_window.lift_window()
        return self._mpc_manager_window


# ─── MPC mode popover (per card) ───────────────────────────────────────


class _MpcModePopover(ctk.CTkFrame):
    """Compact mode picker anchored on a reel card."""

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        ctx: "AppContext",
        suggestion: DiscoverySuggestion,
        *,
        on_confirm: Callable[[MpcExportMode], None],
        on_cancel: Callable[[], None],
    ) -> None:
        t = ctx.theme
        super().__init__(
            parent,
            fg_color=t.surface.elevated,
            border_color=t.border.strong,
            border_width=t.stroke.regular,
            corner_radius=t.radius.md,
        )
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self.grid_columnconfigure(0, weight=1)

        from core.mpc_export import MPC_WORKFLOW_MAX_SECONDS

        ctk.CTkLabel(
            self, text="Export to MPC folder",
            text_color=t.text.primary, font=t.font.body_emphasis, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=t.space.md, pady=(t.space.sm, 0))

        self._mode_var = ctk.StringVar(value=MpcExportMode.BOTH.value)
        options = (
            (MpcExportMode.SONG, "Original song only"),
            (MpcExportMode.STEMS, f"Stems only ({int(MPC_WORKFLOW_MAX_SECONDS)}s)"),
            (MpcExportMode.BOTH, "Both (recommended)"),
        )
        for idx, (mode, label) in enumerate(options):
            ctk.CTkRadioButton(
                self, text=label, variable=self._mode_var, value=mode.value,
                fg_color=t.accent.blue, hover_color=t.accent.blue_bright,
                text_color=t.text.primary, font=t.font.caption,
            ).grid(row=1 + idx, column=0, sticky="w", padx=t.space.md, pady=2)

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="e", padx=t.space.md, pady=t.space.sm)
        ctk.CTkButton(
            buttons, text="Cancel", command=on_cancel, width=80,
            **{**style_ghost_button(t), "height": 28},
        ).pack(side="right")
        ctk.CTkButton(
            buttons, text="Queue export", command=self._confirm, width=110,
            **{**style_primary_button(t), "height": 28},
        ).pack(side="right", padx=(0, t.space.sm))

    def _confirm(self) -> None:
        try:
            mode = MpcExportMode(self._mode_var.get())
        except ValueError:
            mode = MpcExportMode.BOTH
        self._on_confirm(mode)


# ─── Reel card ───────────────────────────────────────────────────────


class _ReelCard(ctk.CTkFrame):
    """One discovery suggestion: metadata + lazy waveform preview + actions."""

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        ctx: "AppContext",
        suggestion: DiscoverySuggestion,
        *,
        on_queued: Optional[Callable[[DiscoverySuggestion], None]] = None,
        on_mpc_confirm: Optional[Callable[[DiscoverySuggestion, MpcExportMode], None]] = None,
    ) -> None:
        t = ctx.theme
        super().__init__(parent, **style_card_elevated(t))

        self._ctx = ctx
        self._theme = t
        self._log = ctx.logger.getChild("reel_card")
        self._s = suggestion
        self._on_queued = on_queued
        self._on_mpc_confirm = on_mpc_confirm

        self._cancel_event = threading.Event()
        self._preview_started = False
        self._full_preview_loading = False
        self._recorded = False
        self._queued = False
        self._player: Optional[WaveformPlayer] = None
        self._player_slot: Optional[ctk.CTkFrame] = None
        self._wave_placeholder: Optional[ctk.CTkLabel] = None
        self._prefetch_chip: Optional[ctk.CTkLabel] = None
        self._preview_btn: Optional[ctk.CTkButton] = None
        self._queue_btn: Optional[ctk.CTkButton] = None
        self._mpc_btn: Optional[ctk.CTkButton] = None
        self._mpc_popover: Optional[_MpcModePopover] = None
        self._inner: Optional[ctk.CTkFrame] = None
        self._last_prefetch_pct = -1.0
        self._last_prefetch_msg = ""

        self.grid_columnconfigure(0, weight=1)
        self._build()

    def _safe_after(self, ms: int, fn: Callable[[], None]) -> Optional[str]:
        def _wrapped() -> None:
            if self._cancel_event.is_set():
                return
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            try:
                fn()
            except Exception:
                self._log.debug("Reel card callback failed", exc_info=True)

        try:
            return self.after(ms, _wrapped)
        except Exception:
            return None

    def _build(self) -> None:
        t = self._theme
        s = self._s
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl,
                   pady=t.space.lg)
        inner.grid_columnconfigure(0, weight=1)
        self._inner = inner

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

        affinity = sample_affinity(
            genres=[s.genre] if s.genre else [],
            styles=[s.style] if s.style else [],
            country=s.country, year=s.year,
        )
        chip_row = 3
        if affinity >= 1.25:
            ctk.CTkLabel(
                inner, text="◆ sample-friendly", text_color=t.accent.blue,
                font=t.font.micro, anchor="w").grid(row=chip_row, column=0,
                                                    sticky="w",
                                                    pady=(t.space.xxs, 0))
            chip_row += 1

        self._prefetch_chip = ctk.CTkLabel(
            inner, text="", text_color=t.text.muted, font=t.font.micro,
            anchor="w",
        )
        self._prefetch_chip.grid(row=chip_row, column=0, sticky="w",
                                 pady=(t.space.xxs, 0))
        chip_row += 1

        self._player_slot = ctk.CTkFrame(inner, fg_color="transparent")
        self._player_slot.grid(row=chip_row, column=0, sticky="ew",
                               pady=(t.space.md, t.space.md))
        self._player_slot.grid_columnconfigure(0, weight=1)
        self._wave_placeholder = ctk.CTkLabel(
            self._player_slot,
            text="Press Preview to load waveform",
            text_color=t.text.muted, font=t.font.caption, anchor="w",
        )
        self._wave_placeholder.grid(row=0, column=0, sticky="ew")

        actions = ctk.CTkFrame(inner, fg_color="transparent")
        actions.grid(row=chip_row + 1, column=0, sticky="w")

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

    # ── Prefetch / MPC status ──

    def apply_prefetch_state(self, state: PrefetchState) -> None:
        labels = {
            PrefetchState.PENDING: "Preview queued",
            PrefetchState.DOWNLOADING: "Fetching…",
            PrefetchState.DECODING: "Decoding…",
            PrefetchState.READY: "Ready",
            PrefetchState.FAILED: "Tap Preview",
            PrefetchState.CANCELLED: "",
        }
        self._set_prefetch_chip(labels.get(state, ""), state)

    def apply_prefetch_event(
        self, event: PrefetchEvent, *, throttle: bool = False,
    ) -> None:
        if throttle and event.type == PrefetchEventType.PROGRESS:
            msg = event.message or "Warming…"
            if (
                event.percent - self._last_prefetch_pct < 8.0
                and msg == self._last_prefetch_msg
            ):
                return
            self._last_prefetch_pct = event.percent
            self._last_prefetch_msg = msg
        if event.type == PrefetchEventType.PROGRESS:
            msg = event.message or "Warming…"
            self._set_prefetch_chip(msg, event.state)
        elif event.type == PrefetchEventType.STATE_CHANGED:
            msg = event.message or {
                PrefetchState.READY: "Ready",
                PrefetchState.FAILED: "Tap Preview",
                PrefetchState.DOWNLOADING: "Fetching…",
                PrefetchState.DECODING: "Decoding…",
            }.get(event.state, "")
            self._set_prefetch_chip(msg, event.state)
            if event.state == PrefetchState.READY and event.data and self._preview_started:
                self._show_preview_data(event.data, full=False)

    def _set_prefetch_chip(self, text: str, state: PrefetchState) -> None:
        if self._prefetch_chip is None:
            return
        t = self._theme
        if not text:
            self._prefetch_chip.configure(text="")
            return
        color = t.text.muted
        if state == PrefetchState.READY:
            color = t.status.success
        elif state == PrefetchState.FAILED:
            color = t.status.error
        elif state in (PrefetchState.DOWNLOADING, PrefetchState.DECODING):
            color = t.accent.purple
        self._prefetch_chip.configure(text=text, text_color=color)

    def apply_mpc_event(self, event) -> None:
        from core.mpc_export_manager import MpcExportEventType
        if self._mpc_btn is None:
            return
        t = self._theme
        if event.type == MpcExportEventType.ENQUEUED:
            self._mpc_btn.configure(text="Queued", state="disabled")
        elif event.type == MpcExportEventType.STARTED:
            self._mpc_btn.configure(text="Exporting…", state="disabled")
        elif event.type == MpcExportEventType.COMPLETED:
            self._mpc_btn.configure(
                text="Sent to MPC ✓", state="disabled", fg_color=t.status.success,
            )
            self._ctx.publish_toast(
                f"MPC export ready: {event.track_dir or event.display_name}",
                "success",
            )
        elif event.type == MpcExportEventType.FAILED:
            self._mpc_btn.configure(
                text="↻  Retry MPC", state="normal", fg_color=t.surface.raised,
            )
        elif event.type == MpcExportEventType.CANCELLED:
            self._mpc_btn.configure(
                text="◈  MPC Workflow", state="normal", fg_color=t.surface.raised,
            )

    def set_scroll_paused(self, paused: bool) -> None:
        if self._player is not None:
            self._player.set_scroll_paused(paused)

    # ── Lazy player ──

    def _ensure_player(self) -> WaveformPlayer:
        if self._player is not None:
            return self._player
        assert self._player_slot is not None
        if self._wave_placeholder is not None:
            self._wave_placeholder.grid_remove()
        self._player = WaveformPlayer(
            self._player_slot, self._theme, height=84,
            initial_volume=self._ctx.config.snapshot().config.ui.preview_volume,
        )
        self._player.grid(row=0, column=0, sticky="ew")
        return self._player

    # ── Preview ──

    def _on_preview_clicked(self) -> None:
        if self._preview_started:
            return
        if self._ctx.preview is None:
            self._ctx.publish_toast("Preview engine unavailable.", "error")
            return
        self._preview_started = True
        self._full_preview_loading = False
        if self._preview_btn is not None:
            self._preview_btn.configure(state="disabled", text="Loading…")
        self._record(was_queued=False)

        pf = self._ctx.preview_prefetch
        vid = self._s.youtube_video_id
        if pf is not None:
            cached = pf.get_decoded(vid)
            if cached is not None:
                player = self._ensure_player()
                player.grid()
                player.set_loading("Loading preview…")
                self._safe_after(
                    0, lambda d=cached: self._show_preview_data(d, full=False),
                )
                return
            state = pf.get_state(vid)
            if state in (
                PrefetchState.PENDING,
                PrefetchState.DOWNLOADING,
                PrefetchState.DECODING,
            ):
                player = self._ensure_player()
                player.grid()
                player.set_loading("Almost ready…")
                threading.Thread(
                    target=self._wait_prefetch_worker, daemon=True,
                ).start()
                return

        player = self._ensure_player()
        player.grid()
        player.set_loading("Fetching quick preview…")
        threading.Thread(
            target=self._preview_worker,
            kwargs={"full": False},
            daemon=True,
        ).start()

    def _wait_prefetch_worker(self) -> None:
        pf = self._ctx.preview_prefetch
        if pf is None:
            self._safe_after(0, self._preview_worker_ui_start)
            return
        data = pf.wait_ready(self._s.youtube_video_id, timeout=120.0)
        if data is not None:
            self._safe_after(0, lambda d=data: self._show_preview_data(d, full=False))
        else:
            self._safe_after(0, self._preview_worker_ui_start)

    def _preview_worker_ui_start(self) -> None:
        player = self._ensure_player()
        player.set_loading("Fetching quick preview…")
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
        import time as time_mod
        last_progress_at = 0.0

        def on_progress(pct: float, msg: str) -> None:
            if self._cancel_event.is_set():
                return
            nonlocal last_progress_at
            now = time_mod.monotonic()
            if now - last_progress_at < 0.2:
                return
            last_progress_at = now
            self._safe_after(0, lambda m=msg: self._on_preview_progress(m))

        try:
            if full:
                data = self._ctx.preview.fetch(
                    self._s.youtube_video_id,
                    cancel_event=self._cancel_event,
                )
            else:
                data = self._ctx.preview.fetch_quick(
                    self._s.youtube_video_id,
                    progress_callback=on_progress,
                    cancel_event=self._cancel_event,
                )
        except Exception as e:  # noqa: BLE001
            self._safe_after(0, lambda err=e, f=full: self._on_preview_error(err, full=f))
            return
        self._safe_after(0, lambda d=data, f=full: self._show_preview_data(d, full=f))

    def _on_preview_progress(self, message: str) -> None:
        if self._prefetch_chip is not None:
            self._set_prefetch_chip(message, PrefetchState.DOWNLOADING)

    def _show_preview_data(self, data, *, full: bool) -> None:
        if self._cancel_event.is_set():
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        player = self._ensure_player()
        if full:
            self._full_preview_loading = False
            player.set_preview(data)
        else:
            player.set_preview(data, on_load_full=self._on_load_full_preview)
        if self._preview_btn is not None:
            self._preview_btn.pack_forget()
            self._preview_btn.grid_remove()
        self._safe_after(50, self._request_scroll_refresh)

    def _request_scroll_refresh(self) -> None:
        node = self
        for _ in range(10):
            node = getattr(node, "master", None)
            if node is None:
                return
            refresh = getattr(node, "_refresh_reel_scroll", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass
                return

    def _on_preview_error(self, error: Exception, *, full: bool = False) -> None:
        if full:
            self._full_preview_loading = False
            if self._player is not None:
                self._player.set_full_loading(False)
            self._ctx.publish_toast(f"Full preview failed: {error}", "warning")
            return
        player = self._ensure_player()
        player.set_error(f"Preview failed: {error}")
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

    def _on_mpc_clicked(self) -> None:
        if self._ctx.preview is None or self._ctx.stem_separator is None \
                or self._ctx.exporter is None or self._ctx.mpc_export_manager is None:
            self._ctx.publish_toast(
                "MPC workflow is unavailable (missing engines).",
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
        self._toggle_mpc_popover()

    def _toggle_mpc_popover(self) -> None:
        if self._mpc_popover is not None:
            self._close_mpc_popover()
            return
        assert self._inner is not None
        self._mpc_popover = _MpcModePopover(
            self._inner,
            self._ctx,
            self._s,
            on_confirm=self._confirm_mpc_mode,
            on_cancel=self._close_mpc_popover,
        )
        t = self._theme
        self._mpc_popover.grid(
            row=20, column=0, sticky="ew", pady=(t.space.sm, 0),
        )

    def _close_mpc_popover(self) -> None:
        if self._mpc_popover is not None:
            try:
                self._mpc_popover.destroy()
            except Exception:
                pass
            self._mpc_popover = None

    def _confirm_mpc_mode(self, mode: MpcExportMode) -> None:
        self._close_mpc_popover()
        if self._on_mpc_confirm is not None:
            self._on_mpc_confirm(self._s, mode)

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

    def pause_preview(self) -> None:
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass

    def teardown(self) -> None:
        self._cancel_event.set()
        self._close_mpc_popover()
        if self._player is not None:
            try:
                self._player.stop()
                self._player.destroy()
            except Exception:
                pass
            self._player = None
