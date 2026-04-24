"""
ui/tabs/digital_crate.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Digital Crate (Discovery) Tab

The "what am I digging today" surface. Four filter controls drive a
single-click Dig action that queries Discogs for a matching master
release, resolves it on YouTube Music, and presents the result as a
card the user can Queue or Skip.

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │  Digital Crate                                              │
  │  Surface a gem from Discogs that matches your filters and   │
  │  queue it for ingestion with one click.                     │
  │                                                             │
  │  ┌── Filters ──────────────────────────────────────────┐    │
  │  │  Decade    [▾]    Country     [▾]                   │    │
  │  │  Genre     [▾]    Style       [▾]                   │    │
  │  │  [Any filter empty? You'll get broader matches.]    │    │
  │  └─────────────────────────────────────────────────────┘    │
  │                                                             │
  │                    ┌─────────────┐                          │
  │                    │    ◆  Dig   │                          │
  │                    └─────────────┘                          │
  │                                                             │
  │  ┌── Last find ────────────────────────────────────────┐    │
  │  │  Pharoah Sanders                                    │    │
  │  │  Karma  ·  1969  ·  USA  ·  Spiritual Jazz          │    │
  │  │  Matched on YouTube Music  ·  94% confidence        │    │
  │  │  [Queue it]        [Skip]       [Open on Discogs]   │    │
  │  └─────────────────────────────────────────────────────┘    │
  └─────────────────────────────────────────────────────────────┘

During a Dig, the button swaps to a spinner + "Digging through the
crates…" label so the user has immediate feedback even when the
rate-limiter is making them wait for API capacity.
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

# Curated lists of the most useful filter values. Discogs supports
# dozens of genres and hundreds of styles; MVP surfaces the ones a
# sample-based producer is actually digging for. Users type-ahead
# into a Combobox so arbitrary values are still supported.

_DECADE_CHOICES: list[tuple[Optional[int], str]] = [
    (None, "Any decade"),
    (1950, "1950s"),
    (1960, "1960s"),
    (1970, "1970s"),
    (1980, "1980s"),
    (1990, "1990s"),
    (2000, "2000s"),
    (2010, "2010s"),
    (2020, "2020s"),
]

_COUNTRY_CHOICES: list[str] = [
    "",  # blank = any
    "Brazil",
    "Ethiopia",
    "France",
    "Germany",
    "Ghana",
    "Greece",
    "Italy",
    "Jamaica",
    "Japan",
    "Nigeria",
    "South Africa",
    "Turkey",
    "UK",
    "USA",
    "West Germany",
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
]

_STYLE_CHOICES: list[str] = [
    "",
    "Afrobeat",
    "Bolero",
    "Boogaloo",
    "Bossa Nova",
    "Cumbia",
    "Disco",
    "Dub",
    "Ethio-jazz",
    "Funk",
    "Fusion",
    "Jazz-Funk",
    "Latin Jazz",
    "Library Music",
    "Lounge",
    "MPB",
    "Neo Soul",
    "Psychedelic Rock",
    "Samba",
    "Soul",
    "Soul-Jazz",
    "Spiritual Jazz",
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
        self._decade_var: Optional[ctk.StringVar] = None
        self._country_var: Optional[ctk.StringVar] = None
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

        # Token-missing warning (shown only when no token configured)
        self._build_token_warning(content, row=2)

        # Filters card
        self._build_filters_card(content, row=3)

        # Dig button + spinner row
        self._build_dig_row(content, row=4)

        # Result area (empty state + real result card swap between them)
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
        card.grid_columnconfigure(0, weight=0)
        card.grid_columnconfigure(1, weight=1)
        card.grid_columnconfigure(2, weight=0)
        # Hidden by default; shown in _refresh_token_gate if no token.
        card.grid_remove()

        ctk.CTkLabel(
            card,
            text="⚠",
            text_color=t.status.warning,
            font=t.font.heading,
            width=36,
        ).grid(row=0, column=0, sticky="w", padx=(t.space.lg, 0), pady=t.space.md)

        msg = ctk.CTkFrame(card, fg_color="transparent")
        msg.grid(
            row=0, column=1, sticky="ew", padx=(t.space.sm, t.space.md), pady=t.space.md
        )
        msg.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            msg,
            text="Discogs API token required",
            text_color=t.text.primary,
            font=t.font.body_emphasis,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            msg,
            text="Add a personal access token in Settings to enable discovery.",
            text_color=t.text.secondary,
            font=t.font.caption,
            anchor="w",
            wraplength=520,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        ctk.CTkButton(
            card,
            text="Open Settings",
            command=lambda: self._ctx.switch_to_tab("settings"),
            **style_secondary_button(t),
            width=130,
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

        ctk.CTkLabel(
            inner,
            text="Filters",
            **style_label_subheading(t),
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, t.space.md))

        # Two-column form: decade/country on top row, genre/style below.
        self._decade_var = ctk.StringVar(value=_DECADE_CHOICES[0][1])
        self._country_var = ctk.StringVar(value=_COUNTRY_CHOICES[0])
        self._genre_var = ctk.StringVar(value=_GENRE_CHOICES[0])
        self._style_var = ctk.StringVar(value=_STYLE_CHOICES[0])

        self._build_filter_field(
            inner,
            row=1,
            col=0,
            label="Decade",
            widget=self._make_decade_menu(inner),
        )
        self._build_filter_field(
            inner,
            row=1,
            col=1,
            label="Country",
            widget=self._make_combobox(inner, self._country_var, _COUNTRY_CHOICES),
        )
        self._build_filter_field(
            inner,
            row=3,
            col=0,
            label="Genre",
            widget=self._make_combobox(inner, self._genre_var, _GENRE_CHOICES),
        )
        self._build_filter_field(
            inner,
            row=3,
            col=1,
            label="Style",
            widget=self._make_combobox(inner, self._style_var, _STYLE_CHOICES),
        )

        # Helper text
        ctk.CTkLabel(
            inner,
            text="Leave any field blank (or 'Any decade') for broader matches.",
            **style_label_meta(t),
            anchor="w",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(t.space.md, 0))

    def _build_filter_field(
        self,
        parent,
        row: int,
        col: int,
        *,
        label: str,
        widget: ctk.CTkBaseClass,
    ) -> None:
        t = self._theme
        ctk.CTkLabel(
            parent,
            text=label,
            **style_label_body(t),
            anchor="w",
        ).grid(
            row=row,
            column=col,
            sticky="w",
            padx=(0 if col == 0 else t.space.md, t.space.md),
            pady=(t.space.sm, 2),
        )
        widget.grid(
            row=row + 1,
            column=col,
            sticky="ew",
            padx=(0 if col == 0 else t.space.md, t.space.md),
        )
        parent.grid_columnconfigure(col, weight=1)

    def _make_decade_menu(self, parent) -> ctk.CTkFrame:
        t = self._theme
        wrapper = ctk.CTkFrame(
            parent,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        ctk.CTkOptionMenu(
            wrapper,
            variable=self._decade_var,
            values=[label for _val, label in _DECADE_CHOICES],
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            height=38,
        ).pack(fill="x", padx=2, pady=2)
        return wrapper

    def _make_combobox(
        self,
        parent,
        variable: ctk.StringVar,
        values: list[str],
    ) -> ctk.CTkFrame:
        """A combobox lets users type arbitrary values beyond the preset list."""
        t = self._theme
        wrapper = ctk.CTkFrame(
            parent,
            fg_color=t.border.strong,
            border_width=0,
            corner_radius=t.radius.md,
        )
        ctk.CTkComboBox(
            wrapper,
            variable=variable,
            values=values,
            fg_color=t.surface.raised,
            border_width=0,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            text_color_disabled=t.text.muted,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=max(0, t.radius.md - 2),
            height=38,
        ).pack(fill="x", padx=2, pady=2)
        return wrapper

    def _build_dig_row(self, parent, row: int) -> None:
        t = self._theme

        dig_row = ctk.CTkFrame(parent, fg_color="transparent")
        dig_row.grid(row=row, column=0, pady=(0, t.space.xl))

        # Primary Dig button. During an in-flight dig, the button is
        # disabled and its text changes to "Digging…"; a spinner sits
        # to its left so the user has continuous visual feedback even
        # when the rate limiter forces a wait.
        self._dig_button = ctk.CTkButton(
            dig_row,
            text="◆   Dig",
            command=self._on_dig_clicked,
            **style_primary_button(t),
            width=220,
        )
        # Slightly bigger than our standard primary button — this is
        # the hero CTA of the whole tab, it earns the extra height.
        self._dig_button.configure(font=t.font.subheading, height=52)
        self._dig_button.pack(side="left")

        # Spinner + status message appear to the right of the button.
        status_frame = ctk.CTkFrame(dig_row, fg_color="transparent")
        status_frame.pack(side="left", padx=(t.space.lg, 0))

        self._dig_spinner = Spinner(status_frame, t, size="md", color=t.accent.purple)
        self._dig_spinner.pack(side="left", padx=(0, t.space.sm))
        # Don't pack_forget the spinner — just don't start it at rest.
        # Hide its visible content by pre-packing with an empty state.

        self._dig_status_label = ctk.CTkLabel(
            status_frame,
            text="",
            text_color=t.text.secondary,
            font=t.font.body,
            anchor="w",
        )
        self._dig_status_label.pack(side="left")

        # Hide both until a dig is in progress.
        self._set_dig_status(None)

    def _build_result_area(self, parent, row: int) -> None:
        t = self._theme

        # Section heading
        ctk.CTkLabel(
            parent,
            text="Last find",
            **style_label_subheading(t),
        ).grid(row=row, column=0, sticky="w", pady=(0, t.space.md))

        # Empty state card
        empty = ctk.CTkFrame(parent, **style_card_elevated(t))
        empty.grid(row=row + 1, column=0, sticky="ew")
        empty.grid_columnconfigure(0, weight=1)

        empty_inner = ctk.CTkFrame(empty, fg_color="transparent")
        empty_inner.grid(row=0, column=0, pady=t.space.xxxl)

        ctk.CTkLabel(
            empty_inner,
            text="Nothing dug up yet.",
            text_color=t.text.secondary,
            font=t.font.subheading,
        ).pack()

        ctk.CTkLabel(
            empty_inner,
            text="Set your filters above and hit Dig to surface a gem.",
            **style_label_meta(t),
        ).pack(pady=(t.space.xs, 0))

        self._result_empty_card = empty

        # Real result card (hidden until a dig succeeds)
        card = ctk.CTkFrame(parent, **style_card_elevated(t))
        card.grid(row=row + 1, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_remove()  # hidden initially

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.xl, pady=t.space.xl)
        inner.grid_columnconfigure(0, weight=1)

        # Artist (small label above)
        self._result_artist = ctk.CTkLabel(
            inner,
            text="",
            text_color=t.accent.purple,
            font=t.font.body_emphasis,
            anchor="w",
        )
        self._result_artist.grid(row=0, column=0, sticky="w")

        # Title (prominent)
        self._result_title = ctk.CTkLabel(
            inner,
            text="",
            text_color=t.text.primary,
            font=t.font.heading,
            anchor="w",
            wraplength=700,
            justify="left",
        )
        self._result_title.grid(
            row=1, column=0, sticky="w", pady=(t.space.xxs, t.space.sm)
        )

        # Year · Country · Style
        self._result_meta = ctk.CTkLabel(
            inner,
            text="",
            text_color=t.text.secondary,
            font=t.font.body,
            anchor="w",
        )
        self._result_meta.grid(row=2, column=0, sticky="w")

        # Match info line (YouTube Music confidence)
        self._result_match = ctk.CTkLabel(
            inner,
            text="",
            text_color=t.text.muted,
            font=t.font.caption,
            anchor="w",
        )
        self._result_match.grid(
            row=3, column=0, sticky="w", pady=(t.space.xs, t.space.lg)
        )

        # Action button row
        actions = ctk.CTkFrame(inner, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="w")

        self._queue_button = ctk.CTkButton(
            actions,
            text="Queue it",
            command=self._on_queue_clicked,
            **style_primary_button(t),
            width=130,
        )
        self._queue_button.pack(side="left")

        self._skip_button = ctk.CTkButton(
            actions,
            text="Skip",
            command=self._on_skip_clicked,
            **style_secondary_button(t),
            width=100,
        )
        self._skip_button.pack(side="left", padx=(t.space.sm, 0))

        self._open_discogs_button = ctk.CTkButton(
            actions,
            text="Open on Discogs",
            command=self._on_open_discogs_clicked,
            **style_ghost_button(t),
        )
        self._open_discogs_button.pack(side="left", padx=(t.space.sm, 0))

        self._result_card = card

    # ── Token gating ──

    def _refresh_token_gate(self) -> None:
        """Show or hide the token warning based on current config state."""
        snap = self._ctx.config.snapshot()
        has_token = bool(snap.discogs_token)

        if self._token_warning_card is not None:
            if has_token:
                self._token_warning_card.grid_remove()
            else:
                self._token_warning_card.grid()

        # The Dig button stays interactive even without a token — we
        # want the user to discover the warning by trying. A disabled
        # button with no explanation is worse UX than a button that
        # gives a directed error on click.

    # ── Dig action ──

    def _on_dig_clicked(self) -> None:
        """Entry point for the Dig button. Handles reentrancy + dispatch."""
        with self._dig_lock:
            if self._digging:
                return
            self._digging = True

        # Immediate UI feedback: swap button to in-flight state before
        # we even check token presence, so the user always sees their
        # click registered.
        self._set_dig_in_flight(True)

        # Token check
        snap = self._ctx.config.snapshot()
        if not snap.discogs_token:
            self._set_dig_in_flight(False)
            self._digging = False
            self._ctx.publish_toast(
                "Add a Discogs token in Settings to start digging.",
                "warning",
                action_label="Open Settings",
                action_callback=lambda: self._ctx.switch_to_tab("settings"),
            )
            return

        if self._ctx.discovery is None:
            self._set_dig_in_flight(False)
            self._digging = False
            self._ctx.publish_toast(
                "Discovery engine is not available.",
                "error",
            )
            return

        # Fetch filter values from widgets
        filters = self._collect_filters()

        # Kick off the dig on a worker thread so the UI stays responsive.
        # The discovery engine's custom rate limiter may force a wait of
        # 5-20+ seconds; blocking the Tk thread would freeze the entire app.
        thread = threading.Thread(
            target=self._run_dig_worker,
            args=(filters,),
            name="digital-crate-dig",
            daemon=True,
        )
        thread.start()

    def _run_dig_worker(self, filters: DiscoveryFilters) -> None:
        """
        Runs on a worker thread. Calls DiscoveryEngine.dig(), then
        marshals the result back to the Tk thread for display.
        """
        assert self._ctx.discovery is not None
        suggestion: Optional[DiscoverySuggestion] = None
        error: Optional[Exception] = None

        try:
            suggestion = self._ctx.discovery.dig(filters)
        except Exception as e:
            error = e

        # Marshal onto Tk thread for UI updates.
        self.after(0, lambda: self._on_dig_finished(suggestion, error))

    def _on_dig_finished(
        self,
        suggestion: Optional[DiscoverySuggestion],
        error: Optional[Exception],
    ) -> None:
        """Tk-thread handler: display result or surface error."""
        self._digging = False
        self._set_dig_in_flight(False)

        if error is not None:
            self._handle_dig_error(error)
            return

        if suggestion is None:
            self._ctx.publish_toast(
                "Dig returned no result.",
                "warning",
            )
            return

        self._current_suggestion = suggestion
        self._render_suggestion(suggestion)
        self._ctx.publish_toast(
            f"Found: {suggestion.display_name}",
            "success",
        )

    def _handle_dig_error(self, error: Exception) -> None:
        """Map typed DiscoveryErrors to user-facing messages."""
        if isinstance(error, DiscoveryConfigError):
            self._ctx.publish_toast(
                str(error),
                "warning",
                action_label="Open Settings",
                action_callback=lambda: self._ctx.switch_to_tab("settings"),
            )
        elif isinstance(error, NoResultsError):
            self._ctx.publish_toast(
                "No matches for those filters. Try widening them.",
                "info",
            )
        elif isinstance(error, NoYouTubeMatchError):
            self._ctx.publish_toast(
                "Found a candidate on Discogs but couldn't match it on "
                "YouTube Music. Try digging again for another option.",
                "warning",
            )
        elif isinstance(error, DiscoveryThrottledError):
            self._ctx.publish_toast(
                "Discogs rate-limited us. Wait a minute and try again.",
                "warning",
            )
        elif isinstance(error, DiscoveryError):
            self._ctx.publish_toast(
                f"Dig failed: {error}",
                "error",
            )
        else:
            self._log.exception("Unexpected dig error", exc_info=error)
            self._ctx.publish_toast(
                f"Something went wrong: {error}",
                "error",
            )

    # ── Result rendering ──

    def _render_suggestion(self, s: DiscoverySuggestion) -> None:
        t = self._theme
        assert self._result_empty_card is not None
        assert self._result_card is not None

        # Hide empty state, show real card
        self._result_empty_card.grid_remove()
        self._result_card.grid()

        # Fill fields
        if self._result_artist is not None:
            self._result_artist.configure(text=s.artist)

        if self._result_title is not None:
            self._result_title.configure(text=s.title)

        if self._result_meta is not None:
            # Format: "1969  ·  USA  ·  Spiritual Jazz"
            parts: list[str] = []
            if s.year is not None:
                parts.append(str(s.year))
            if s.country:
                parts.append(s.country)
            if s.style:
                parts.append(s.style)
            elif s.genre:
                parts.append(s.genre)
            self._result_meta.configure(
                text="  ·  ".join(parts) if parts else "—",
            )

        if self._result_match is not None:
            confidence_pct = int(s.match_score * 100)
            duration_str = self._format_duration(s.youtube_duration_seconds)
            match_text = f"Matched on YouTube Music  ·  {confidence_pct}% confidence"
            if duration_str:
                match_text += f"  ·  {duration_str}"
            self._result_match.configure(text=match_text)

    def _on_queue_clicked(self) -> None:
        s = self._current_suggestion
        if s is None:
            return

        # Resolve stems preference + model from config (same pattern
        # as Manual Rip — respects live Settings changes).
        snap = self._ctx.config.snapshot()
        enable_stems = bool(snap.config.general.enable_stems_by_default)
        stem_model_raw = snap.config.stems.model
        try:
            stem_model = StemModel(stem_model_raw)
        except ValueError:
            stem_model = StemModel.HTDEMUCS_FT

        request = PipelineRequest(
            source_url=s.youtube_url,
            enable_stems=enable_stems,
            stem_model=stem_model,
            hint_genre=s.genre,
            hint_country=s.country,
            hint_year=s.year,
            hint_discogs_master_id=s.discogs_master_id,
            hint_discogs_release_id=s.discogs_release_id,
            source_platform_override="discogs_dig",
        )

        try:
            self._ctx.queue_manager.enqueue(request)
        except Exception as e:
            self._log.exception("Enqueue failed for discovery suggestion")
            self._ctx.publish_toast(f"Could not queue: {e}", "error")
            return

        self._ctx.publish_toast(
            f"Queued: {s.display_name}",
            "success",
        )

        # Clear the card so the user has a clean slate for the next Dig.
        self._clear_result()

    def _on_skip_clicked(self) -> None:
        """Clear the current result. The master_id is already recorded
        in discovery_history, so the next Dig won't re-suggest it."""
        self._clear_result()

    def _on_open_discogs_clicked(self) -> None:
        s = self._current_suggestion
        if s is None:
            return
        url = f"https://www.discogs.com/master/{s.discogs_master_id}"
        try:
            webbrowser.open(url)
        except Exception as e:
            self._log.warning("Could not open Discogs URL: %s", e)

    def _clear_result(self) -> None:
        self._current_suggestion = None
        if self._result_card is not None:
            self._result_card.grid_remove()
        if self._result_empty_card is not None:
            self._result_empty_card.grid()

    # ── Dig in-flight state ──

    def _set_dig_in_flight(self, in_flight: bool) -> None:
        """Swap the Dig button between rest and in-flight appearance."""
        t = self._theme
        if self._dig_button is None:
            return

        if in_flight:
            self._dig_button.configure(
                text="Digging…",
                state="disabled",
                fg_color=t.accent.blue_dim,
                hover_color=t.accent.blue_dim,
            )
            self._set_dig_status("Searching Discogs and YouTube Music…")
        else:
            self._dig_button.configure(
                text="◆   Dig",
                state="normal",
                fg_color=t.accent.blue,
                hover_color=t.accent.blue_bright,
            )
            self._set_dig_status(None)

    def _set_dig_status(self, message: Optional[str]) -> None:
        """Show/hide the spinner + status text next to the Dig button."""
        if self._dig_status_label is None or self._dig_spinner is None:
            return

        if message:
            self._dig_spinner.start()
            self._dig_status_label.configure(text=message)
        else:
            self._dig_spinner.stop()
            self._dig_status_label.configure(text="")

    # ── Filter collection ──

    def _collect_filters(self) -> DiscoveryFilters:
        """Read current widget values into a DiscoveryFilters."""
        assert self._decade_var is not None
        assert self._country_var is not None
        assert self._genre_var is not None
        assert self._style_var is not None

        # Decade: translate display label back to int|None
        decade_label = self._decade_var.get()
        decade = next(
            (val for val, label in _DECADE_CHOICES if label == decade_label),
            None,
        )

        def normalize(s: Optional[str]) -> Optional[str]:
            s = (s or "").strip()
            return s or None

        snap = self._ctx.config.snapshot()
        return DiscoveryFilters(
            decade=decade,
            country=normalize(self._country_var.get()),
            genre=normalize(self._genre_var.get()),
            style=normalize(self._style_var.get()),
            min_have=snap.config.discovery.default_min_have,
        )

    # ── Helpers ──

    @staticmethod
    def _format_duration(seconds: Optional[int]) -> Optional[str]:
        if not seconds or seconds <= 0:
            return None
        m, s = divmod(int(seconds), 60)
        if m >= 60:
            h, m = divmod(m, 60)
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def _on_content_configure(self, _event) -> None:
        t = self._theme
        parent_width = self._content.master.winfo_width()
        target = min(960, max(480, parent_width - 2 * t.space.xl))
        current_width = self._content.winfo_width()
        if abs(current_width - target) > 8:
            self._content.configure(width=target)
