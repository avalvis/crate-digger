"""
ui/tabs/digital_crate.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Digital Crate (Discovery) Tab

The "what am I digging today" surface. Six filter controls drive a
single-click Dig action that queries Discogs for a matching master
release, resolves it on YouTube Music, and presents the result as a
card the user can Queue, Skip, or Open in external services.
"""

from __future__ import annotations

import threading
import webbrowser
from typing import TYPE_CHECKING, Optional

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
from core.pipeline import PipelineRequest
from core.stems import StemModel
from ui.components.spinner import Spinner
from ui.components.year_spinner import YearSpinner
from ui.theme import (
    style_card,
    style_card_elevated,
    style_ghost_button,
    style_label_body,
    style_label_heading,
    style_label_meta,
    style_label_subheading,
    style_primary_button,
    style_secondary_button,
)

if TYPE_CHECKING:
    from ui.app import AppContext


# ─── Filter vocabularies ────────────────────────────────────────────

_FORMAT_CHOICES: list[str] = [
    "",
    "Vinyl",
    "CD",
    "Cassette",
    "7\"",
    "12\"",
    "LP",
    "Album",
    "Single",
    "EP",
    "Compilation",
]

_COUNTRY_CHOICES: list[str] = [
    "",  # blank = any
    "Argentina",
    "Australia",
    "Belgium",
    "Brazil",
    "Canada",
    "Colombia",
    "Cuba",
    "Ethiopia",
    "France",
    "Germany",
    "Ghana",
    "Greece",
    "India",
    "Italy",
    "Jamaica",
    "Japan",
    "Mexico",
    "Netherlands",
    "Nigeria",
    "Norway",
    "South Africa",
    "Spain",
    "Sweden",
    "Switzerland",
    "Turkey",
    "UK",
    "USA",
    "USSR",
    "Yugoslavia",
]

_GENRE_CHOICES: list[str] = [
    "",  # blank = any
    "Electronic",
    "Folk, World, & Country",
    "Funk / Soul",
    "Hip Hop",
    "Jazz",
    "Latin",
    "Pop",
    "Reggae",
    "Rock",
    "Stage & Screen",
    "Blues",
    "Non-Music",
    "Children's",
    "Brass & Military",
    "Classical",
]

_STYLE_CHOICES: list[str] = [
    "",
    "Acid Jazz",
    "Afrobeat",
    "Ambient",
    "Big Beat",
    "Blues Rock",
    "Bolero",
    "Boogaloo",
    "Bossa Nova",
    "Bouzouki",
    "Breakbeat",
    "Chanson",
    "Cumbia",
    "Dancehall",
    "Deep House",
    "Disco",
    "Downtempo",
    "Dub",
    "Éntekhno",
    "Ethio-jazz",
    "Experimental",
    "Flamenco",
    "Free Jazz",
    "Funk",
    "Fusion",
    "Garage House",
    "Gospel",
    "Hard Bop",
    "Highlife",
    "House",
    "Italo-Disco",
    "Jazz-Funk",
    "Jungle",
    "Krautrock",
    "Laïkó",
    "Latin Jazz",
    "Library Music",
    "Lounge",
    "Lo-Fi",
    "MPB",
    "Neo Soul",
    "New Wave",
    "No Wave",
    "Pachanga",
    "Post-Punk",
    "Prog Rock",
    "Psychedelic Rock",
    "Raï",
    "Rebetiko",
    "Rock & Roll",
    "Salsa",
    "Samba",
    "Shoegaze",
    "Ska",
    "Soul",
    "Soul-Jazz",
    "Spiritual Jazz",
    "Synth-pop",
    "Techno",
    "Trip Hop",
    "Tropicália",
]


class DigitalCrateTab(ctk.CTkFrame):
    """Discovery tab. One instance per app."""

    def __init__(self, parent: ctk.CTkBaseClass, ctx: "AppContext") -> None:
        super().__init__(parent, fg_color=ctx.theme.surface.app)

        self._ctx = ctx
        self._theme = ctx.theme
        self._log = ctx.logger.getChild("digital_crate")

        # State
        self._digging: bool = False
        self._current_suggestion: Optional[DiscoverySuggestion] = None
        self._dig_lock = threading.Lock()  # serialize dig button clicks

        # Widget refs (populated by _build_body)
        self._year_var: Optional[ctk.StringVar] = None
        self._query_var: Optional[ctk.StringVar] = None
        self._country_var: Optional[ctk.StringVar] = None
        self._format_var: Optional[ctk.StringVar] = None
        self._genre_var: Optional[ctk.StringVar] = None
        self._style_var: Optional[ctk.StringVar] = None
        self._dig_button: Optional[ctk.CTkButton] = None
        self._dig_spinner: Optional[Spinner] = None
        self._dig_status_label: Optional[ctk.CTkLabel] = None
        self._result_card: Optional[ctk.CTkFrame] = None
        self._result_empty_card: Optional[ctk.CTkFrame] = None
        self._result_artist: Optional[ctk.CTkLabel] = None
        self._result_title: Optional[ctk.CTkLabel] = None
        self._result_meta: Optional[ctk.CTkLabel] = None
        self._result_match: Optional[ctk.CTkLabel] = None
        self._queue_button: Optional[ctk.CTkButton] = None
        self._skip_button: Optional[ctk.CTkButton] = None
        self._open_discogs_button: Optional[ctk.CTkButton] = None
        self._open_youtube_button: Optional[ctk.CTkButton] = None
        self._token_warning_card: Optional[ctk.CTkFrame] = None

        self._build_body()
        self._refresh_token_gate()

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
        content.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=t.space.xl,
            pady=(t.space.xxl, t.space.xl),
        )
        content.grid_columnconfigure(0, weight=1)
        self._content = content
        content.bind("<Configure>", self._on_content_configure)

        # Heading
        ctk.CTkLabel(
            content,
            text="Digital Crate",
            **style_label_heading(t),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            content,
            text=(
                "Surface a gem from Discogs that matches your filters "
                "and queue it for ingestion with one click."
            ),
            **style_label_meta(t),
            wraplength=720,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xs, t.space.xl))

        # Token-missing warning
        self._build_token_warning(content, row=2)

        # Filters card
        self._build_filters_card(content, row=3)

        # Dig button + spinner row
        self._build_dig_row(content, row=4)

        # Result area
        self._build_result_area(content, row=5)

    def _build_token_warning(self, parent, row: int) -> None:
        t = self._theme
        card = ctk.CTkFrame(
            parent,
            fg_color=t.surface.raised,
            border_color=t.status.warning,
            border_width=t.stroke.hairline,
            corner_radius=t.radius.lg,
        )
        card.grid(row=row, column=0, sticky="ew", pady=(0, t.space.lg))
        card.grid_columnconfigure(1, weight=1)
        card.grid_remove()

        ctk.CTkLabel(
            card, text="⚠", text_color=t.status.warning, font=t.font.heading, width=36
        ).grid(row=0, column=0, sticky="w", padx=(t.space.lg, 0), pady=t.space.md)

        msg = ctk.CTkFrame(card, fg_color="transparent")
        msg.grid(row=0, column=1, sticky="ew", padx=(t.space.sm, t.space.md), pady=t.space.md)
        ctk.CTkLabel(
            msg, text="Discogs API token required", text_color=t.text.primary, font=t.font.body_emphasis, anchor="w"
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            msg, text="Add a personal access token in Settings to enable discovery.",
            text_color=t.text.secondary, font=t.font.caption, anchor="w", wraplength=520, justify="left"
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        ctk.CTkButton(
            card, text="Open Settings", command=lambda: self._ctx.switch_to_tab("settings"),
            **style_secondary_button(t), width=130
        ).grid(row=0, column=2, sticky="e", padx=(0, t.space.lg), pady=t.space.md)

        self._token_warning_card = card

    def _build_filters_card(self, parent, row: int) -> None:
        t = self._theme
        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=row, column=0, sticky="ew", pady=(0, t.space.lg))
        card.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl, pady=t.space.xl)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(inner, text="Filters", **style_label_subheading(t)).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, t.space.md)
        )

        self._year_var = ctk.StringVar(value="")
        self._format_var = ctk.StringVar(value="")
        self._country_var = ctk.StringVar(value="")
        self._genre_var = ctk.StringVar(value="")
        self._style_var = ctk.StringVar(value="")
        self._query_var = ctk.StringVar(value="")

        self._build_filter_field(
            inner,
            row=1,
            col=0,
            label="Year",
            widget=YearSpinner(inner, self._theme, self._year_var, placeholder="e.g. 1978"),
        )
        self._build_filter_field(inner, 1, 1, label="Format", widget=self._make_combobox(inner, self._format_var, _FORMAT_CHOICES))
        self._build_filter_field(inner, 3, 0, label="Country", widget=self._make_combobox(inner, self._country_var, _COUNTRY_CHOICES))
        self._build_filter_field(inner, 3, 1, label="Genre", widget=self._make_combobox(inner, self._genre_var, _GENRE_CHOICES))
        self._build_filter_field(inner, 5, 0, label="Style", widget=self._make_combobox(inner, self._style_var, _STYLE_CHOICES))
        self._build_filter_field(inner, 5, 1, label="Search Keywords", widget=self._make_entry(inner, self._query_var, "Artist, title, etc."))

        ctk.CTkLabel(
            inner, text="Leave any field blank for broader matches. Enter exact year for precision.",
            **style_label_meta(t), anchor="w"
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(t.space.md, 0))

    def _make_entry(self, parent, variable: ctk.StringVar, placeholder: str) -> ctk.CTkFrame:
        t = self._theme
        wrapper = ctk.CTkFrame(parent, fg_color=t.border.strong, border_width=0, corner_radius=t.radius.md)
        ctk.CTkEntry(
            wrapper, textvariable=variable, placeholder_text=placeholder,
            fg_color=t.surface.raised, border_width=0, text_color=t.text.primary,
            placeholder_text_color=t.text.muted, font=t.font.body, corner_radius=max(0, t.radius.md - 2), height=38
        ).pack(fill="x", padx=2, pady=2)
        return wrapper

    def _make_combobox(self, parent, variable: ctk.StringVar, values: list[str]) -> ctk.CTkFrame:
        t = self._theme
        wrapper = ctk.CTkFrame(parent, fg_color=t.border.strong, border_width=0, corner_radius=t.radius.md)
        cb = ctk.CTkComboBox(
            wrapper, variable=variable, values=values,
            fg_color=t.surface.raised, border_width=0, button_color=t.surface.raised,
            button_hover_color=t.surface.elevated, dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary, text_color_disabled=t.text.muted,
            dropdown_text_color=t.text.primary, dropdown_hover_color=t.surface.overlay,
            font=t.font.body, corner_radius=max(0, t.radius.md - 2), height=38
        )
        cb.pack(fill="x", padx=2, pady=2)
        
        # Mouse wheel support for the closed combobox value selection
        def on_wheel(event):
            current = variable.get()
            try:
                idx = values.index(current)
            except ValueError:
                idx = 0
            
            if event.delta > 0:
                new_idx = max(0, idx - 1)
            else:
                new_idx = min(len(values) - 1, idx + 1)
            
            variable.set(values[new_idx])

        cb.bind("<MouseWheel>", on_wheel)
        return wrapper

    def _build_filter_field(self, parent, row: int, col: int, *, label: str, widget: ctk.CTkBaseClass) -> None:
        t = self._theme
        ctk.CTkLabel(parent, text=label, **style_label_body(t), anchor="w").grid(
            row=row, column=col, sticky="w", padx=(0 if col == 0 else t.space.md, t.space.md), pady=(t.space.sm, 2)
        )
        widget.grid(row=row+1, column=col, sticky="ew", padx=(0 if col == 0 else t.space.md, t.space.md))
        parent.grid_columnconfigure(col, weight=1)

    def _build_dig_row(self, parent, row: int) -> None:
        t = self._theme
        dig_row = ctk.CTkFrame(parent, fg_color="transparent")
        dig_row.grid(row=row, column=0, pady=(0, t.space.xl))
        self._dig_button = ctk.CTkButton(
            dig_row, text="◆   Dig", command=self._on_dig_clicked,
            **style_primary_button(t), width=220
        )
        self._dig_button.configure(font=t.font.subheading, height=52)
        self._dig_button.pack(side="left")
        status_frame = ctk.CTkFrame(dig_row, fg_color="transparent")
        status_frame.pack(side="left", padx=(t.space.lg, 0))
        self._dig_spinner = Spinner(status_frame, t, size="md", color=t.accent.purple)
        self._dig_spinner.pack(side="left", padx=(0, t.space.sm))
        self._dig_status_label = ctk.CTkLabel(status_frame, text="", text_color=t.text.secondary, font=t.font.body, anchor="w")
        self._dig_status_label.pack(side="left")
        self._set_dig_status(None)

    def _build_result_area(self, parent, row: int) -> None:
        t = self._theme
        ctk.CTkLabel(parent, text="Last find", **style_label_subheading(t)).grid(row=row, column=0, sticky="w", pady=(0, t.space.md))
        empty = ctk.CTkFrame(parent, **style_card_elevated(t))
        empty.grid(row=row + 1, column=0, sticky="ew")
        empty.grid_columnconfigure(0, weight=1)
        empty_inner = ctk.CTkFrame(empty, fg_color="transparent")
        empty_inner.grid(row=0, column=0, pady=t.space.xxxl)
        ctk.CTkLabel(empty_inner, text="Nothing dug up yet.", text_color=t.text.secondary, font=t.font.subheading).pack()
        ctk.CTkLabel(empty_inner, text="Set your filters above and hit Dig to surface a gem.", **style_label_meta(t)).pack(pady=(t.space.xs, 0))
        self._result_empty_card = empty

        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=row + 1, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_remove()
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl, pady=t.space.xl)
        inner.grid_columnconfigure(0, weight=1)
        self._result_artist = ctk.CTkLabel(inner, text="", text_color=t.accent.purple, font=t.font.body_emphasis, anchor="w")
        self._result_artist.grid(row=0, column=0, sticky="w")
        self._result_title = ctk.CTkLabel(inner, text="", text_color=t.text.primary, font=t.font.heading, anchor="w", wraplength=700, justify="left")
        self._result_title.grid(row=1, column=0, sticky="w", pady=(t.space.xxs, t.space.sm))
        self._result_meta = ctk.CTkLabel(inner, text="", text_color=t.text.secondary, font=t.font.body, anchor="w")
        self._result_meta.grid(row=2, column=0, sticky="w")
        self._result_match = ctk.CTkLabel(inner, text="", text_color=t.text.muted, font=t.font.caption, anchor="w")
        self._result_match.grid(row=3, column=0, sticky="w", pady=(t.space.xs, t.space.lg))
        actions = ctk.CTkFrame(inner, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="w")
        self._queue_button = ctk.CTkButton(actions, text="Queue it", command=self._on_queue_clicked, **style_primary_button(t), width=130)
        self._queue_button.pack(side="left")
        self._skip_button = ctk.CTkButton(actions, text="Skip", command=self._on_skip_clicked, **style_secondary_button(t), width=100)
        self._skip_button.pack(side="left", padx=(t.space.sm, 0))
        self._open_discogs_button = ctk.CTkButton(actions, text="Open on Discogs", command=self._on_open_discogs_clicked, **style_ghost_button(t))
        self._open_discogs_button.pack(side="left", padx=(t.space.sm, 0))
        self._open_youtube_button = ctk.CTkButton(actions, text="Open on YouTube", command=self._on_open_youtube_clicked, **style_ghost_button(t))
        self._open_youtube_button.pack(side="left", padx=(t.space.sm, 0))
        self._result_card = card

    def _refresh_token_gate(self) -> None:
        snap = self._ctx.config.snapshot()
        if self._token_warning_card:
            if snap.discogs_token: self._token_warning_card.grid_remove()
            else: self._token_warning_card.grid()

    def _on_dig_clicked(self) -> None:
        with self._dig_lock:
            if self._digging: return
            self._digging = True
        self._set_dig_in_flight(True)
        snap = self._ctx.config.snapshot()
        if not snap.discogs_token:
            self._set_dig_in_flight(False)
            self._digging = False
            self._ctx.publish_toast("Add a Discogs token in Settings.", "warning")
            return
        if not self._ctx.discovery:
            self._set_dig_in_flight(False)
            self._digging = False
            return
        filters = self._collect_filters()
        threading.Thread(target=self._run_dig_worker, args=(filters,), daemon=True).start()

    def _run_dig_worker(self, filters: DiscoveryFilters) -> None:
        suggestion, error = None, None
        try: suggestion = self._ctx.discovery.dig(filters)
        except Exception as e: error = e
        self.after(0, lambda: self._on_dig_finished(suggestion, error))

    def _on_dig_finished(self, suggestion: Optional[DiscoverySuggestion], error: Optional[Exception]) -> None:
        self._digging = False
        self._set_dig_in_flight(False)
        if error:
            self._handle_dig_error(error)
            return
        if not suggestion:
            self._ctx.publish_toast("No results found.", "warning")
            return
        self._current_suggestion = suggestion
        self._render_suggestion(suggestion)

    def _handle_dig_error(self, error: Exception) -> None:
        if isinstance(error, NoResultsError):
            self._ctx.publish_toast("No matches found. Try wider filters.", "info")
        else:
            self._ctx.publish_toast(f"Dig failed: {error}", "error")

    def _render_suggestion(self, s: DiscoverySuggestion) -> None:
        self._result_empty_card.grid_remove()
        self._result_card.grid()
        self._result_artist.configure(text=s.artist)
        self._result_title.configure(text=s.title)
        parts = [str(x) for x in [s.year, s.country, s.style or s.genre] if x]
        self._result_meta.configure(text="  ·  ".join(parts))
        conf = int(s.match_score * 100)
        self._result_match.configure(text=f"Matched on YouTube Music  ·  {conf}% confidence")

    def _on_queue_clicked(self) -> None:
        s = self._current_suggestion
        if not s: return
        snap = self._ctx.config.snapshot()
        request = PipelineRequest(
            source_url=s.youtube_url,
            enable_stems=snap.config.general.enable_stems_by_default,
            hint_genre=s.genre, hint_country=s.country, hint_year=s.year,
            hint_discogs_master_id=s.discogs_master_id,
            hint_discogs_release_id=s.discogs_release_id,
            source_platform_override="discogs_dig",
        )
        self._ctx.queue_manager.enqueue(request)
        self._ctx.publish_toast(f"Queued: {s.display_name}", "success")
        self._clear_result()

    def _on_skip_clicked(self) -> None: self._clear_result()

    def _on_open_discogs_clicked(self) -> None:
        if self._current_suggestion: webbrowser.open(f"https://www.discogs.com/master/{self._current_suggestion.discogs_master_id}")

    def _on_open_youtube_clicked(self) -> None:
        if self._current_suggestion: webbrowser.open(self._current_suggestion.youtube_url)

    def _clear_result(self) -> None:
        self._current_suggestion = None
        self._result_card.grid_remove()
        self._result_empty_card.grid()

    def _set_dig_in_flight(self, in_flight: bool) -> None:
        t = self._theme
        if in_flight:
            self._dig_button.configure(text="Digging…", state="disabled", fg_color=t.accent.blue_dim)
            self._set_dig_status("Searching Discogs and YouTube Music…")
        else:
            self._dig_button.configure(text="◆   Dig", state="normal", fg_color=t.accent.blue)
            self._set_dig_status(None)

    def _set_dig_status(self, message: Optional[str]) -> None:
        if message:
            self._dig_spinner.start()
            self._dig_status_label.configure(text=message)
        else:
            self._dig_spinner.stop()
            self._dig_status_label.configure(text="")

    def _collect_filters(self) -> DiscoveryFilters:
        y_str = self._year_var.get().strip()
        year = int(y_str) if y_str.isdigit() else None
        def norm(v): return v.strip() or None
        snap = self._ctx.config.snapshot()
        return DiscoveryFilters(
            year=year, country=norm(self._country_var.get()),
            genre=norm(self._genre_var.get()), style=norm(self._style_var.get()),
            format=norm(self._format_var.get()), query=norm(self._query_var.get()),
            min_have=snap.config.discovery.default_min_have,
        )

    def _on_content_configure(self, _event) -> None:
        t = self._theme
        parent_width = self._content.master.winfo_width()
        target = min(960, max(480, parent_width - 2 * t.space.xl))
        if abs(self._content.winfo_width() - target) > 8:
            self._content.configure(width=target)
