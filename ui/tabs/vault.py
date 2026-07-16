"""
ui/tabs/vault.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Vault Tab

The library browser. Combines the VirtualDataTable with a filter row
above and a selection-aware toolbar below. Lives on top of the DB —
every user action (filter change, sort click, track deletion) runs
through VaultDatabase, keeping the filesystem and the index in sync.

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │  Vault                                    1,247 tracks · …  │
  │                                                             │
  │  ┌── Filters ──────────────────────────────────────────┐    │
  │  │  [⌕ search...........]  Genre▾  BPM [min]–[max]    │    │
  │  │                         Key▾    ☐ stems only        │    │
  │  │                                           [Reset]   │    │
  │  └─────────────────────────────────────────────────────┘    │
  │                                                             │
  │  ┌── Table ────────────────────────────────────────────┐    │
  │  │ Artist | Title | Genre | BPM | Key | Year | Added   │    │
  │  ├──────────────────────────────────────────────────────┤   │
  │  │ ≡ virtualized rows                                  │    │
  │  │                                                     │    │
  │  └──────────────────────────────────────────────────────┘   │
  │                                                             │
  │  3 selected   [Export to MPC]  [Reveal]  [Re-analyze]  [⋯] │
  └─────────────────────────────────────────────────────────────┘

Live updates: when a pipeline job completes (JOB_COMPLETED arrives via
the event bridge), the Vault tab refreshes its data. The refresh is
scheduled through a 250ms coalescing timer so a burst of completions
(e.g. Discovery dig → multiple queued) results in one refresh, not N.

Export to MPC: selection → filedialog for destination → modal progress
dialog → post-export toast with success/failure counts. The progress
dialog runs the MPCExporter on a worker thread, marshaling progress
events back to the Tk thread via `self.after(0, ...)`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import TYPE_CHECKING, Any, Callable, Optional

import customtkinter as ctk

from core.database import CrateRecord, DuplicateGroup, TrackFilter, TrackRecord
from core.exporter import (
    ExportCancelledError,
    ExportedFile,
    ExportProgress,
    ExportResult,
    ExportStage,
    MPCExporter,
)
from core.pipeline import PipelineRequest
from core.queue_manager import QueueEvent, QueueEventType
from core.stems import StemModel
from ui.components.data_table import (
    Column,
    SortDirection,
    TableSort,
    VirtualDataTable,
)
from ui.components.glow_entry import GlowEntry
from ui.components.progress_row import ProgressRow  # noqa: F401 — used indirectly
from ui.components.waveform_player import WaveformPlayer
from ui.theme import (
    Theme,
    style_card,
    style_danger_button,
    style_ghost_button,
    style_input,
    style_label_body,
    style_label_heading,
    style_label_meta,
    style_primary_button,
    style_secondary_button,
)

if TYPE_CHECKING:
    from ui.app import AppContext


# ─── Filter vocabulary ──────────────────────────────────────────────

_GENRE_ANY = "Any genre"
_KEY_ANY = "Any key"
_TAG_ANY = "Any tag"
_CRATE_ANY = "All crates"
_RATING_ANY = "Any rating"
_RATING_CHOICES = [_RATING_ANY, "★ 1+", "★ 2+", "★ 3+", "★ 4+", "★ 5"]

# Full Camelot wheel — what DJ software uses, what users think in.
_CAMELOT_KEYS: list[str] = [
    _KEY_ANY,
    "1A",
    "1B",
    "2A",
    "2B",
    "3A",
    "3B",
    "4A",
    "4B",
    "5A",
    "5B",
    "6A",
    "6B",
    "7A",
    "7B",
    "8A",
    "8B",
    "9A",
    "9B",
    "10A",
    "10B",
    "11A",
    "11B",
    "12A",
    "12B",
]

# Default BPM search range bounds — sensible for sample-based producers.
_BPM_ABSOLUTE_MIN = 40
_BPM_ABSOLUTE_MAX = 220


# ─── Filter state ───────────────────────────────────────────────────


@dataclass(slots=True)
class _VaultFilterState:
    """Mutable filter state; rebuilt into a DB TrackFilter on each query."""

    query: str = ""
    genre: Optional[str] = None
    min_bpm: Optional[float] = None
    max_bpm: Optional[float] = None
    camelot_key: Optional[str] = None
    has_stems: Optional[bool] = None
    min_rating: Optional[int] = None
    tag: Optional[str] = None
    crate_id: Optional[int] = None
    sort_key: str = "date_added"
    sort_desc: bool = True

    def to_track_filter(self, limit: int = 10_000) -> TrackFilter:
        return TrackFilter(
            query=self.query or None,
            genre=self.genre,
            min_bpm=self.min_bpm,
            max_bpm=self.max_bpm,
            camelot_key=self.camelot_key,
            has_stems=self.has_stems,
            min_rating=self.min_rating,
            tag=self.tag,
            crate_id=self.crate_id,
            order_by=self.sort_key,
            order_desc=self.sort_desc,
            limit=limit,
            offset=0,
        )


# ─── The tab ────────────────────────────────────────────────────────


class VaultTab(ctk.CTkFrame):
    """The Vault library browser tab."""

    # Debounce the refresh timer so a burst of JOB_COMPLETED events
    # doesn't trigger N full table rebuilds in quick succession.
    _REFRESH_DEBOUNCE_MS = 250

    # Debounce search entry typing so every keystroke doesn't re-query.
    _SEARCH_DEBOUNCE_MS = 200

    # Max rows loaded into the table at once. Even at 50k+ tracks, the
    # virtualized table only renders ~30-50 at a time, but keeping the
    # in-memory list bounded prevents memory bloat on pathological libraries.
    _MAX_ROWS = 10_000

    def __init__(self, parent: ctk.CTkBaseClass, ctx: "AppContext") -> None:
        super().__init__(parent, fg_color=ctx.theme.surface.app)

        self._ctx = ctx
        self._theme = ctx.theme
        self._log = ctx.logger.getChild("vault")

        # Filter state
        self._filters = _VaultFilterState()
        self._refresh_timer: Optional[str] = None
        self._search_timer: Optional[str] = None

        # Loaded rows + selection
        self._rows: list[TrackRecord] = []
        self._selected_tracks: list[TrackRecord] = []

        # Widget refs
        self._header_count_label: Optional[ctk.CTkLabel] = None
        self._search_entry: Optional[GlowEntry] = None
        self._genre_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._key_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._rating_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._tag_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._crate_dropdown: Optional[ctk.CTkOptionMenu] = None
        self._min_bpm_entry: Optional[ctk.CTkEntry] = None
        self._max_bpm_entry: Optional[ctk.CTkEntry] = None
        self._stems_only_var: Optional[ctk.BooleanVar] = None
        self._reset_button: Optional[ctk.CTkButton] = None
        self._table: Optional[VirtualDataTable] = None
        self._toolbar_frame: Optional[ctk.CTkFrame] = None
        self._selection_label: Optional[ctk.CTkLabel] = None
        self._export_button: Optional[ctk.CTkButton] = None
        self._reveal_button: Optional[ctk.CTkButton] = None
        self._reanalyze_button: Optional[ctk.CTkButton] = None
        self._delete_button: Optional[ctk.CTkButton] = None
        self._inspect_button: Optional[ctk.CTkButton] = None
        self._crate_button: Optional[ctk.CTkButton] = None

        # Maps crate dropdown labels → crate ids (label "All crates" → None).
        self._crate_label_to_id: dict[str, Optional[int]] = {}

        # Pending focus hint (from "Show in Vault" toast actions).
        # Set by `focus_track()`; consumed on next successful refresh.
        self._pending_focus_track_id: Optional[int] = None

        self._build_body()
        self._refresh_genres()
        self._refresh_tag_choices()
        self._refresh_crate_choices()
        self._refresh_data()

        # Subscribe to queue events for live updates on completion.
        ctx.event_bridge.subscribe(self._on_queue_event, weak=True)

    # ── Public API ──

    def focus_track(self, track_id: int) -> None:
        """
        Programmatic entry point called by the app shell when a toast's
        "Show in Vault" action fires. Triggers a refresh then scrolls to
        and selects the target row.
        """
        self._pending_focus_track_id = track_id
        # If the track was just added, a refresh may be needed before
        # we can find it.
        self._refresh_data()

    def on_tab_visible(self) -> None:
        """Refresh dynamic filter choices when returning to the Vault."""
        self._refresh_tag_choices()
        self._refresh_crate_choices()

    # ── Body construction ──

    def _build_body(self) -> None:
        t = self._theme

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)  # header
        self.grid_rowconfigure(1, weight=0)  # filters
        self.grid_rowconfigure(2, weight=1)  # table
        self.grid_rowconfigure(3, weight=0)  # toolbar

        self._build_header()
        self._build_filter_row()
        self._build_table()
        self._build_toolbar()

    def _build_header(self) -> None:
        t = self._theme

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=t.space.xl,
            pady=(t.space.xxl, t.space.md),
        )
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="Vault",
            **style_label_heading(t),
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            header,
            text="Find duplicates",
            command=self._on_find_duplicates_clicked,
            **style_ghost_button(t),
        ).grid(row=0, column=1, sticky="e", padx=(0, t.space.md))

        self._header_count_label = ctk.CTkLabel(
            header,
            text="",
            **style_label_meta(t),
            anchor="e",
        )
        self._header_count_label.grid(row=0, column=2, sticky="e")

    def _build_filter_row(self) -> None:
        t = self._theme

        card = ctk.CTkFrame(self, **{**style_card(t), "border_width": 0})
        card.grid(row=1, column=0, sticky="ew", padx=t.space.xl, pady=(0, t.space.md))
        card.grid_columnconfigure(0, weight=1)

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.grid(row=0, column=0, sticky="ew", padx=t.space.lg, pady=t.space.md)
        # Four-column grid so filter widgets wrap cleanly.
        for col in range(4):
            inner.grid_columnconfigure(col, weight=1)

        # Row 0: search + genre
        self._search_entry = GlowEntry(
            inner,
            t,
            placeholder="Search artist, title, album…",
            prefix_icon=None,
            on_submit=lambda _v: self._apply_filters_now(),
            height=40,
        )
        self._search_entry.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=(0, t.space.md),
            pady=(0, t.space.sm),
        )
        # Debounced search on every keystroke.
        self._search_entry._entry.bind(
            "<KeyRelease>",
            lambda _e: self._debounce_search(),
            add="+",
        )

        self._genre_dropdown = self._make_dropdown(
            inner,
            [_GENRE_ANY],
            self._on_genre_changed,
            width=180,
        )
        self._genre_dropdown.grid(
            row=0, column=2, sticky="ew", padx=(0, t.space.md), pady=(0, t.space.sm)
        )

        self._key_dropdown = self._make_dropdown(
            inner,
            _CAMELOT_KEYS,
            self._on_key_changed,
            width=140,
        )
        self._key_dropdown.grid(row=0, column=3, sticky="ew", pady=(0, t.space.sm))

        # Row 1: BPM range + stems-only + reset
        bpm_row = ctk.CTkFrame(inner, fg_color="transparent")
        bpm_row.grid(row=1, column=0, columnspan=2, sticky="w")

        ctk.CTkLabel(
            bpm_row,
            text="BPM",
            **style_label_meta(t),
        ).pack(side="left", padx=(0, t.space.sm))

        self._min_bpm_entry = self._make_bpm_entry(bpm_row)
        self._min_bpm_entry.pack(side="left", padx=(0, t.space.xs))

        ctk.CTkLabel(
            bpm_row,
            text="–",
            text_color=t.text.muted,
            font=t.font.body,
        ).pack(side="left", padx=t.space.xxs)

        self._max_bpm_entry = self._make_bpm_entry(bpm_row)
        self._max_bpm_entry.pack(side="left", padx=(t.space.xs, 0))

        # Stems-only toggle
        stems_frame = ctk.CTkFrame(inner, fg_color="transparent")
        stems_frame.grid(row=1, column=2, sticky="w")

        self._stems_only_var = ctk.BooleanVar(value=False)
        stems_switch = ctk.CTkSwitch(
            stems_frame,
            text="",
            variable=self._stems_only_var,
            onvalue=True,
            offvalue=False,
            command=self._on_stems_toggle_changed,
            progress_color=t.accent.blue,
            button_color=t.text.primary,
            button_hover_color=t.text.primary,
            fg_color=t.surface.elevated,
            width=36,
            height=20,
        )
        stems_switch.pack(side="left", padx=(0, t.space.sm))

        ctk.CTkLabel(
            stems_frame,
            text="Stems only",
            **style_label_body(t),
        ).pack(side="left")

        # Reset button on the right
        self._reset_button = ctk.CTkButton(
            inner,
            text="Reset filters",
            command=self._reset_filters,
            **style_ghost_button(t),
        )
        self._reset_button.grid(row=1, column=3, sticky="e")

        # Row 2: rating + tag + crate dropdowns
        self._rating_dropdown = self._make_dropdown(
            inner, _RATING_CHOICES, self._on_rating_changed, width=130)
        self._rating_dropdown.grid(row=2, column=0, sticky="w",
                                   padx=(0, t.space.md), pady=(t.space.sm, 0))

        self._tag_dropdown = self._make_dropdown(
            inner, [_TAG_ANY], self._on_tag_changed, width=180)
        self._tag_dropdown.grid(row=2, column=1, sticky="w",
                                padx=(0, t.space.md), pady=(t.space.sm, 0))

        self._crate_dropdown = self._make_dropdown(
            inner, [_CRATE_ANY], self._on_crate_changed, width=200)
        self._crate_dropdown.grid(row=2, column=2, sticky="w",
                                  pady=(t.space.sm, 0))

    def _build_table(self) -> None:
        t = self._theme

        columns = [
            Column("artist", "Artist", width=200),
            Column("title", "Title", width=260),
            Column("genre", "Genre", width=120),
            Column("bpm_display", "BPM", width=80, numeric=True),
            Column(
                "camelot_key",
                "Key",
                width=70,
                numeric=True,
                color_fn=self._camelot_color,
            ),
            Column("year", "Year", width=70, numeric=True),
            Column("stems_flag", "Stems", width=70, numeric=True),
            Column("added_display", "Added", width=130, sortable=True),
        ]

        self._table = VirtualDataTable(
            self,
            t,
            columns,
            multi_select=True,
            on_sort_changed=self._on_sort_changed,
            on_row_activated=self._on_row_activated,
            on_selection_changed=self._on_selection_changed,
        )
        self._table.grid(
            row=2, column=0, sticky="nsew", padx=t.space.xl, pady=(0, t.space.md)
        )
        # Announce the initial sort state.
        self._table.set_sort("date_added", SortDirection.DESC)

    def _build_toolbar(self) -> None:
        t = self._theme

        toolbar = ctk.CTkFrame(
            self,
            fg_color=t.surface.raised,
            border_color=t.border.subtle,
            border_width=t.stroke.hairline,
            corner_radius=t.radius.lg,
            height=64,
        )
        toolbar.grid(
            row=3, column=0, sticky="ew", padx=t.space.xl, pady=(0, t.space.xl)
        )
        toolbar.grid_propagate(False)
        toolbar.grid_columnconfigure(1, weight=1)
        self._toolbar_frame = toolbar

        # Selection count label on the left
        self._selection_label = ctk.CTkLabel(
            toolbar,
            text="No tracks selected",
            text_color=t.text.secondary,
            font=t.font.body,
            anchor="w",
        )
        self._selection_label.grid(
            row=0, column=0, sticky="w", padx=t.space.lg, pady=t.space.md
        )

        # Action buttons on the right
        actions = ctk.CTkFrame(toolbar, fg_color="transparent")
        actions.grid(row=0, column=2, sticky="e", padx=t.space.lg, pady=t.space.md)

        self._inspect_button = ctk.CTkButton(
            actions,
            text="Inspect",
            command=self._on_inspect_clicked,
            **style_ghost_button(t),
            state="disabled",
        )
        self._inspect_button.pack(side="left", padx=(0, t.space.xs))

        self._crate_button = ctk.CTkButton(
            actions,
            text="Add to crate",
            command=self._on_add_to_crate_clicked,
            **style_ghost_button(t),
            state="disabled",
        )
        self._crate_button.pack(side="left", padx=(0, t.space.xs))

        self._reveal_button = ctk.CTkButton(
            actions,
            text="Reveal",
            command=self._on_reveal_clicked,
            **style_ghost_button(t),
            state="disabled",
        )
        self._reveal_button.pack(side="left", padx=(0, t.space.xs))

        self._reanalyze_button = ctk.CTkButton(
            actions,
            text="Re-analyze",
            command=self._on_reanalyze_clicked,
            **style_ghost_button(t),
            state="disabled",
        )
        self._reanalyze_button.pack(side="left", padx=(0, t.space.xs))

        self._delete_button = ctk.CTkButton(
            actions,
            text="Delete",
            command=self._on_delete_clicked,
            **style_danger_button(t),
            state="disabled",
        )
        self._delete_button.pack(side="left", padx=(0, t.space.md))

        self._export_button = ctk.CTkButton(
            actions,
            text="Export to MPC",
            command=self._on_export_clicked,
            **style_primary_button(t),
            state="disabled",
            width=160,
        )
        self._export_button.pack(side="left")

    def _make_dropdown(
        self,
        parent,
        values: list[str],
        command: Callable[[str], None],
        *,
        width: int = 160,
    ) -> ctk.CTkOptionMenu:
        t = self._theme
        return ctk.CTkOptionMenu(
            parent,
            values=values,
            command=command,
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            dropdown_hover_color=t.surface.overlay,
            font=t.font.body,
            corner_radius=t.radius.md,
            width=width,
            height=38,
        )

    def _make_bpm_entry(self, parent) -> ctk.CTkEntry:
        t = self._theme
        entry = ctk.CTkEntry(
            parent,
            width=68,
            height=38,
            placeholder_text="—",
            **{k: v for k, v in style_input(t).items() if k != "height"},
            justify="center",
        )
        try:
            inner_entry = getattr(entry, "_entry", None)
            if inner_entry is not None:
                inner_entry.configure(
                    highlightthickness=0,
                    highlightbackground=t.surface.raised,
                    highlightcolor=t.surface.raised,
                    bd=0,
                    relief="flat",
                    insertbackground=t.text.primary,
                )
        except Exception:
            pass
        entry.bind("<FocusOut>", lambda _e: self._apply_bpm_range(), add="+")
        entry.bind("<Return>", lambda _e: self._apply_bpm_range(), add="+")
        return entry

    # ── Data refresh ──

    def _refresh_data(self) -> None:
        """Query the DB and repopulate the table."""
        try:
            # Ensure geometry is settled so the virtual table knows its viewport size.
            self.update_idletasks()

            db_filter = self._filters.to_track_filter(limit=self._MAX_ROWS)
            self._rows = self._ctx.database.list_tracks(db_filter)
            total_unfiltered = self._ctx.database.count_tracks()
            shown = len(self._rows)
            is_filtered = self._is_filter_active()

            # Project TrackRecords into dicts the VirtualDataTable expects.
            projected = [self._project_row(r) for r in self._rows]

            if self._table is not None:
                self._table.set_data(projected)

            self._update_header_count(shown, total_unfiltered, is_filtered)

            # Consume any pending focus hint from `focus_track()`.
            self._consume_pending_focus()

        except Exception as e:
            self._log.exception("Vault refresh failed")
            self._ctx.publish_toast(
                f"Could not load Vault: {e}",
                "error",
            )

    def _refresh_genres(self) -> None:
        """
        Populate the Genre dropdown from the distinct set of genres
        present in the DB. Runs once at tab open; refreshed on each
        completion event via the debounced refresh timer.
        """
        try:
            # We don't have a dedicated "distinct genres" DAO method,
            # but we can build one from a full read. For libraries
            # under ~50k tracks this is fast enough to not need a DB
            # index trick.
            all_tracks = self._ctx.database.list_tracks(
                TrackFilter(limit=self._MAX_ROWS),
            )
            genres_set = {t.genre for t in all_tracks if t.genre}
            genres = [_GENRE_ANY] + sorted(genres_set, key=str.lower)

            if self._genre_dropdown is not None:
                # Preserve current selection if still valid.
                current = self._genre_dropdown.get()
                self._genre_dropdown.configure(values=genres)
                if current in genres:
                    self._genre_dropdown.set(current)
                else:
                    self._genre_dropdown.set(_GENRE_ANY)
        except Exception:
            self._log.exception("Genre list refresh failed")

    def _refresh_tag_choices(self) -> None:
        """Populate the tag dropdown from the union of all track tags."""
        try:
            tags = self._ctx.database.list_distinct_tags()
            values = [_TAG_ANY] + tags
            if self._tag_dropdown is not None:
                current = self._tag_dropdown.get()
                self._tag_dropdown.configure(values=values)
                if current in values:
                    self._tag_dropdown.set(current)
                else:
                    self._tag_dropdown.set(_TAG_ANY)
        except Exception:
            self._log.exception("Tag list refresh failed")

    def _refresh_crate_choices(self) -> None:
        """Populate the crate dropdown from the DB, keeping label→id map."""
        try:
            crates = self._ctx.database.list_crates()
            self._crate_label_to_id = {_CRATE_ANY: None}
            values = [_CRATE_ANY]
            for c in crates:
                label = f"{c.name} ({c.track_count})"
                self._crate_label_to_id[label] = c.id
                values.append(label)
            if self._crate_dropdown is not None:
                # Preserve current crate selection by id if it still exists.
                prev_id = self._filters.crate_id
                self._crate_dropdown.configure(values=values)
                restored = None
                if prev_id is not None:
                    for label, cid in self._crate_label_to_id.items():
                        if cid == prev_id:
                            restored = label
                            break
                self._crate_dropdown.set(restored or _CRATE_ANY)
                if restored is None:
                    self._filters.crate_id = None
        except Exception:
            self._log.exception("Crate list refresh failed")

    def _schedule_refresh(self) -> None:
        """Debounced refresh — coalesces event bursts."""
        if self._refresh_timer is not None:
            try:
                self.after_cancel(self._refresh_timer)
            except Exception:
                pass
        self._refresh_timer = self.after(
            self._REFRESH_DEBOUNCE_MS,
            self._refresh_and_clear_timer,
        )

    def _refresh_and_clear_timer(self) -> None:
        self._refresh_timer = None
        self._refresh_genres()
        self._refresh_tag_choices()
        self._refresh_crate_choices()
        self._refresh_data()

    def _update_header_count(
        self,
        shown: int,
        total: int,
        is_filtered: bool,
    ) -> None:
        if self._header_count_label is None:
            return
        if total == 0:
            text = "No tracks yet"
        elif is_filtered and shown != total:
            text = f"{shown:,} of {total:,} tracks"
        else:
            text = f"{shown:,} track{'s' if shown != 1 else ''}"
        self._header_count_label.configure(text=text)

    # ── Row projection ──

    def _project_row(self, r: TrackRecord) -> dict[str, Any]:
        """Convert a TrackRecord into the dict shape VirtualDataTable wants."""
        # Keep the original record accessible via a private key so action
        # callbacks can recover the full object for DB operations.
        return {
            "_track": r,
            "id": r.id,
            "artist": r.artist,
            "title": r.title,
            "genre": r.genre or "—",
            "bpm_display": f"{r.bpm:.1f}" if r.bpm else "—",
            "bpm_numeric": r.bpm or 0,
            "camelot_key": r.camelot_key or "—",
            "year": r.year if r.year else "—",
            "stems_flag": "✓" if r.stems_separated else "",
            "added_display": self._format_date(r.date_added),
            "date_added": r.date_added or "",
        }

    def _camelot_color(self, row: dict) -> Optional[str]:
        """Color the Camelot column by key family for harmonic awareness."""
        t = self._theme
        key = row.get("camelot_key") or ""
        if key.endswith("A"):
            return t.accent.purple  # minor key → purple
        if key.endswith("B"):
            return t.accent.blue  # major key → blue
        return None

    @staticmethod
    def _format_date(iso: Optional[str]) -> str:
        """'2025-04-24T12:34:56+00:00' → '2025-04-24' for display."""
        if not iso:
            return "—"
        return iso.split("T", 1)[0]

    # ── Filter interactions ──

    def _debounce_search(self) -> None:
        if self._search_timer is not None:
            try:
                self.after_cancel(self._search_timer)
            except Exception:
                pass
        self._search_timer = self.after(
            self._SEARCH_DEBOUNCE_MS,
            self._apply_search_from_entry,
        )

    def _apply_search_from_entry(self) -> None:
        self._search_timer = None
        if self._search_entry is None:
            return
        self._filters.query = self._search_entry.get().strip()
        self._refresh_data()

    def _apply_filters_now(self) -> None:
        """Used by the GlowEntry's Enter-key binding."""
        if self._search_timer is not None:
            try:
                self.after_cancel(self._search_timer)
            except Exception:
                pass
            self._search_timer = None
        self._apply_search_from_entry()

    def _on_genre_changed(self, value: str) -> None:
        self._filters.genre = None if value == _GENRE_ANY else value
        self._refresh_data()

    def _on_key_changed(self, value: str) -> None:
        self._filters.camelot_key = None if value == _KEY_ANY else value
        self._refresh_data()

    def _on_stems_toggle_changed(self) -> None:
        if self._stems_only_var is None:
            return
        self._filters.has_stems = True if self._stems_only_var.get() else None
        self._refresh_data()

    def _on_rating_changed(self, value: str) -> None:
        if value == _RATING_ANY:
            self._filters.min_rating = None
        else:
            # Labels look like "★ 3+" or "★ 5"; the digit is the threshold.
            digits = "".join(ch for ch in value if ch.isdigit())
            self._filters.min_rating = int(digits) if digits else None
        self._refresh_data()

    def _on_tag_changed(self, value: str) -> None:
        self._filters.tag = None if value == _TAG_ANY else value
        self._refresh_data()

    def _on_crate_changed(self, value: str) -> None:
        self._filters.crate_id = self._crate_label_to_id.get(value)
        self._refresh_data()

    def _apply_bpm_range(self) -> None:
        """Validate both entries, coerce to filter state, refresh."""
        min_bpm = self._parse_bpm(self._min_bpm_entry)
        max_bpm = self._parse_bpm(self._max_bpm_entry)

        # Swap if user entered them reversed.
        if min_bpm is not None and max_bpm is not None and min_bpm > max_bpm:
            min_bpm, max_bpm = max_bpm, min_bpm
            if self._min_bpm_entry is not None:
                self._min_bpm_entry.delete(0, "end")
                self._min_bpm_entry.insert(0, f"{int(min_bpm)}")
            if self._max_bpm_entry is not None:
                self._max_bpm_entry.delete(0, "end")
                self._max_bpm_entry.insert(0, f"{int(max_bpm)}")

        self._filters.min_bpm = min_bpm
        self._filters.max_bpm = max_bpm
        self._refresh_data()

    @staticmethod
    def _parse_bpm(entry: Optional[ctk.CTkEntry]) -> Optional[float]:
        if entry is None:
            return None
        value = entry.get().strip()
        if not value:
            return None
        try:
            n = float(value)
        except ValueError:
            return None
        return max(_BPM_ABSOLUTE_MIN, min(_BPM_ABSOLUTE_MAX, n))

    def _reset_filters(self) -> None:
        self._filters = _VaultFilterState()
        # Reset widget states
        if self._search_entry is not None:
            self._search_entry.clear()
        if self._genre_dropdown is not None:
            self._genre_dropdown.set(_GENRE_ANY)
        if self._key_dropdown is not None:
            self._key_dropdown.set(_KEY_ANY)
        if self._min_bpm_entry is not None:
            self._min_bpm_entry.delete(0, "end")
        if self._max_bpm_entry is not None:
            self._max_bpm_entry.delete(0, "end")
        if self._stems_only_var is not None:
            self._stems_only_var.set(False)
        if self._rating_dropdown is not None:
            self._rating_dropdown.set(_RATING_ANY)
        if self._tag_dropdown is not None:
            self._tag_dropdown.set(_TAG_ANY)
        if self._crate_dropdown is not None:
            self._crate_dropdown.set(_CRATE_ANY)
        if self._table is not None:
            self._table.set_sort("date_added", SortDirection.DESC)
        self._refresh_data()

    def _is_filter_active(self) -> bool:
        f = self._filters
        return bool(
            f.query
            or f.genre
            or f.camelot_key
            or f.min_bpm is not None
            or f.max_bpm is not None
            or f.has_stems is not None
            or f.min_rating is not None
            or f.tag
            or f.crate_id is not None,
        )

    # ── Sort ──

    def _on_sort_changed(self, sort: TableSort) -> None:
        """
        Map the table's column key to the DB-side sortable column.
        Some display columns (`bpm_display`) sort on the real field.
        """
        column_to_db = {
            "artist": "artist",
            "title": "title",
            "genre": "genre",
            "bpm_display": "bpm",
            "camelot_key": "camelot_key",
            "year": "year",
            "added_display": "date_added",
        }
        self._filters.sort_key = column_to_db.get(sort.column_key, "date_added")
        self._filters.sort_desc = sort.direction == SortDirection.DESC
        self._refresh_data()

    # ── Row activation / selection ──

    def _on_row_activated(self, row: dict) -> None:
        """Double-click: open the track inspector (preview + metadata)."""
        track: TrackRecord = row.get("_track")
        if track is None:
            return
        self._open_inspector(track)

    def _on_selection_changed(self, rows: list[dict]) -> None:
        self._selected_tracks = [
            r["_track"] for r in rows if r.get("_track") is not None
        ]
        self._update_toolbar_state()

    def _update_toolbar_state(self) -> None:
        count = len(self._selected_tracks)
        if self._selection_label is not None:
            if count == 0:
                text = "No tracks selected"
            elif count == 1:
                text = "1 track selected"
            else:
                text = f"{count:,} tracks selected"
            self._selection_label.configure(text=text)

        enabled = "normal" if count > 0 else "disabled"
        for btn in (
            self._crate_button,
            self._reveal_button,
            self._reanalyze_button,
            self._delete_button,
            self._export_button,
        ):
            if btn is not None:
                btn.configure(state=enabled)

        # Inspect operates on a single track only.
        if self._inspect_button is not None:
            self._inspect_button.configure(
                state="normal" if count == 1 else "disabled"
            )

    # ── Pending focus ──

    def _consume_pending_focus(self) -> None:
        """
        If `focus_track()` was called, try to locate and select the
        target row after the current refresh.
        """
        tid = self._pending_focus_track_id
        if tid is None or self._table is None:
            return

        self._pending_focus_track_id = None

        # Find the track in current rows.
        for idx, track in enumerate(self._rows):
            if track.id == tid:
                # Table doesn't expose a public "scroll + select" API, but
                # `_set_cursor` does exactly this internally. We'd rather
                # not poke at privates, so we simulate it with keyboard-
                # style navigation: focus the table, then set selection.
                # VirtualDataTable's selection model works via _on_row_clicked
                # which takes a row index + event. We have neither in a clean
                # way from here, so we accept a mild layering compromise.
                try:
                    self._table.focus_set()
                    self._table.scroll_to_and_select(idx)
                except Exception:
                    self._log.debug("Could not focus row %d", idx)
                return

        # Track not found in the current filtered view — clear filters
        # and retry once, if we're filtering.
        if self._is_filter_active():
            self._reset_filters()
            self._pending_focus_track_id = tid
            self._refresh_data()
        else:
            self._ctx.publish_toast(
                "Newly added track not yet in view.",
                "info",
            )

    # ── Queue events (live updates) ──

    def _on_queue_event(self, event: QueueEvent) -> None:
        """
        Tk-thread handler. Refresh on JOB_COMPLETED and JOB_FAILED
        (the latter because a cancelled mid-ingest might have left
        a partial track row we want to reflect).
        """
        if event.type in (
            QueueEventType.JOB_COMPLETED,
            QueueEventType.JOB_FAILED,
        ):
            self._schedule_refresh()

    # ── Action: Inspect (preview + metadata editing) ──

    def _on_inspect_clicked(self) -> None:
        if len(self._selected_tracks) != 1:
            return
        self._open_inspector(self._selected_tracks[0])

    def _open_inspector(self, track: TrackRecord) -> None:
        _TrackInspectorDialog(
            parent=self,
            theme=self._theme,
            ctx=self._ctx,
            track=track,
            on_saved=self._on_inspector_saved,
        )

    def _on_inspector_saved(self) -> None:
        """Called after the inspector persists rating/tags/notes."""
        self._refresh_tag_choices()
        self._refresh_data()

    # ── Action: Add to crate ──

    def _on_add_to_crate_clicked(self) -> None:
        if not self._selected_tracks:
            return
        _AddToCrateDialog(
            parent=self,
            theme=self._theme,
            ctx=self._ctx,
            tracks=list(self._selected_tracks),
            on_done=self._on_crate_changed_externally,
        )

    def _on_crate_changed_externally(self) -> None:
        self._refresh_crate_choices()
        self._refresh_data()

    # ── Action: Find duplicates ──

    def _on_find_duplicates_clicked(self) -> None:
        try:
            groups = self._ctx.database.find_duplicates()
        except Exception as e:
            self._log.exception("Duplicate scan failed")
            self._ctx.publish_toast(f"Duplicate scan failed: {e}", "error")
            return

        if not groups:
            self._ctx.publish_toast("No duplicates found.", "success")
            return

        _DuplicatesDialog(
            parent=self,
            theme=self._theme,
            ctx=self._ctx,
            groups=groups,
            on_deleted=self._on_inspector_saved,
        )

    # ── Action: Reveal ──

    def _on_reveal_clicked(self) -> None:
        self._reveal_in_os(self._selected_tracks)

    def _reveal_in_os(self, tracks: list[TrackRecord]) -> None:
        """
        Open the containing folder for each selected track. Single-selection
        uses platform-native 'reveal and select' where available.
        """
        if not tracks:
            return

        if len(tracks) == 1:
            # Single-track: use the platform's "reveal and select" API
            # so the file itself is highlighted.
            track = tracks[0]
            self._reveal_single(Path(track.file_path))
            return

        # Multi-selection: just open each unique parent folder.
        unique_parents = set()
        for t in tracks:
            unique_parents.add(Path(t.file_path).parent)

        if len(unique_parents) > 5:
            # Protect the user from their own click on a big selection.
            self._ctx.publish_toast(
                f"Would open {len(unique_parents)} folders — too many. "
                "Select fewer tracks first.",
                "warning",
            )
            return

        for parent in unique_parents:
            try:
                webbrowser.open(parent.as_uri())
            except Exception as e:
                self._log.warning("Could not open %s: %s", parent, e)

    def _reveal_single(self, file_path: Path) -> None:
        """Platform-native 'reveal and select' for a single file."""
        if not file_path.exists():
            self._ctx.publish_toast(
                "File not found on disk. It may have been moved.",
                "warning",
            )
            return

        try:
            if sys.platform == "darwin":
                subprocess.run(
                    ["open", "-R", str(file_path)],
                    check=False,
                    **self._subprocess_platform_kwargs(),
                )
            elif sys.platform == "win32":
                subprocess.run(
                    ["explorer", "/select,", str(file_path)],
                    check=False,
                    **self._subprocess_platform_kwargs(),
                )
            else:
                # Most Linux file managers don't support file-selection
                # from the CLI; fall back to opening the parent.
                webbrowser.open(file_path.parent.as_uri())
        except Exception as e:
            self._log.warning("Reveal failed: %s", e)
            # Final fallback: open the parent folder in the default
            # file manager.
            try:
                webbrowser.open(file_path.parent.as_uri())
            except Exception:
                pass

    @staticmethod
    def _subprocess_platform_kwargs() -> dict:
        if sys.platform != "win32":
            return {}
        CREATE_NO_WINDOW = 0x08000000
        return {"creationflags": CREATE_NO_WINDOW}

    # ── Action: Re-analyze ──

    def _on_reanalyze_clicked(self) -> None:
        """Re-queue selected tracks through the pipeline from their local files."""
        if not self._selected_tracks:
            return

        count = len(self._selected_tracks)
        # Confirm before bulk operation.
        if count > 5:
            ok = messagebox.askyesno(
                "Re-analyze tracks",
                f"Re-analyze {count} tracks? Each will go through the "
                f"full pipeline again (download, analyze, tag, file).",
                parent=self.winfo_toplevel(),
            )
            if not ok:
                return

        # Use each track's source_url so the pipeline re-downloads fresh
        # from the authoritative source. If a track's source_url is
        # stale (e.g. video taken down), that job will fail — that's
        # correct behavior and surfaces the stale link to the user.
        snap = self._ctx.config.snapshot()
        try:
            stem_model = StemModel(snap.config.stems.model)
        except ValueError:
            stem_model = StemModel.HTDEMUCS

        queued = 0
        for track in self._selected_tracks:
            if not track.source_url:
                continue
            request = PipelineRequest(
                source_url=track.source_url,
                enable_stems=track.stems_separated,
                stem_model=stem_model,
                hint_genre=track.genre,
                hint_country=track.country,
                hint_year=track.year,
            )
            try:
                self._ctx.queue_manager.enqueue(request)
                queued += 1
            except Exception as e:
                self._log.warning(
                    "Could not enqueue track %d: %s",
                    track.id,
                    e,
                )

        self._ctx.publish_toast(
            f"Queued {queued} track{'s' if queued != 1 else ''} for re-analysis.",
            "success",
        )

    # ── Action: Delete ──

    def _on_delete_clicked(self) -> None:
        """Prompt to remove selected tracks from the Vault."""
        if not self._selected_tracks:
            return

        count = len(self._selected_tracks)
        _DeleteConfirmDialog(
            parent=self,
            theme=self._theme,
            tracks=list(self._selected_tracks),
            on_confirm=self._perform_delete,
        )

    def _perform_delete(
        self,
        tracks: list[TrackRecord],
        remove_files: bool,
    ) -> None:
        """Execute deletion after confirmation. DB first, then files."""
        removed_rows = 0
        removed_files = 0
        failed_files: list[str] = []

        for track in tracks:
            if track.id is None:
                continue

            # Remove DB row first. If this fails, we don't touch the file.
            try:
                self._ctx.database.delete_track(track.id)
                removed_rows += 1
            except Exception as e:
                self._log.warning(
                    "Could not delete track %d from DB: %s",
                    track.id,
                    e,
                )
                continue

            if remove_files:
                # Delete the containing track directory, which includes
                # the .m4a and any stems subfolder. This matches the
                # vault's one-track-per-directory layout.
                try:
                    track_dir = Path(track.file_path).parent
                    # Safety check: the directory name should look like
                    # the vault pattern (BPM_Key_Artist_Title). Refuse
                    # to delete something that isn't in the vault tree.
                    vault_root = (
                        Path(self._ctx.config.snapshot().config.general.vault_root)
                        .expanduser()
                        .resolve()
                    )
                    resolved = track_dir.resolve()
                    # Must be a descendant of vault_root.
                    resolved.relative_to(vault_root)  # raises if not
                    shutil.rmtree(track_dir, ignore_errors=False)
                    removed_files += 1
                except ValueError:
                    # Not inside the vault root — refuse and log.
                    self._log.warning(
                        "Refusing to delete %s — outside vault root.",
                        track.file_path,
                    )
                    failed_files.append(track.file_path)
                except OSError as e:
                    self._log.warning(
                        "Could not delete %s: %s",
                        track.file_path,
                        e,
                    )
                    failed_files.append(track.file_path)

        # Summary toast
        summary_parts = [
            f"Removed {removed_rows} row{'s' if removed_rows != 1 else ''}"
        ]
        if remove_files:
            summary_parts.append(
                f"deleted {removed_files} file{'s' if removed_files != 1 else ''}"
            )
            if failed_files:
                summary_parts.append(f"{len(failed_files)} failed")
        self._ctx.publish_toast(
            " · ".join(summary_parts),
            "success" if not failed_files else "warning",
        )

        self._selected_tracks = []
        self._refresh_data()

    # ── Action: Export to MPC ──

    def _on_export_clicked(self) -> None:
        if not self._selected_tracks:
            return
        if self._ctx.exporter is None:
            self._ctx.publish_toast(
                "Exporter not available.",
                "error",
            )
            return

        # Pick destination folder
        destination = filedialog.askdirectory(
            title="Choose destination for WAV export (SD card or folder)",
            mustexist=True,
            parent=self.winfo_toplevel(),
        )
        if not destination:
            return

        # Spawn the progress dialog; it owns the export thread.
        _ExportProgressDialog(
            parent=self,
            theme=self._theme,
            exporter=self._ctx.exporter,
            database=self._ctx.database,
            tracks=list(self._selected_tracks),
            destination=Path(destination),
            toast_publisher=self._ctx.publish_toast,
            logger=self._log.getChild("export"),
        )


# ─── Delete confirmation dialog ─────────────────────────────────────


class _DeleteConfirmDialog(ctk.CTkToplevel):
    """Themed modal for confirming track deletion."""

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        tracks: list[TrackRecord],
        on_confirm: Callable[[list[TrackRecord], bool], None],
    ) -> None:
        super().__init__(parent)

        self._theme = theme
        self._tracks = tracks
        self._on_confirm = on_confirm

        t = theme
        count = len(tracks)
        self.title("Delete tracks")
        self.configure(fg_color=t.surface.base)
        self.geometry("480x260")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent.winfo_toplevel())
        self._center_over(parent)

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.xl)

        ctk.CTkLabel(
            frame,
            text=f"Remove {count} track{'s' if count != 1 else ''} from the Vault?",
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).pack(anchor="w")

        # Track preview (first 3, then "and N more…")
        preview_lines = []
        for track in tracks[:3]:
            preview_lines.append(f"· {track.artist} — {track.title}")
        if len(tracks) > 3:
            preview_lines.append(f"…and {len(tracks) - 3} more")

        ctk.CTkLabel(
            frame,
            text="\n".join(preview_lines),
            text_color=t.text.secondary,
            font=t.font.caption,
            anchor="w",
            justify="left",
        ).pack(anchor="w", pady=(t.space.sm, t.space.md))

        # Remove-files checkbox
        self._remove_files_var = ctk.BooleanVar(value=False)
        cb = ctk.CTkCheckBox(
            frame,
            text="Also delete files on disk (cannot be undone)",
            variable=self._remove_files_var,
            text_color=t.text.primary,
            font=t.font.body,
            fg_color=t.status.error,
            hover_color=t.status.error,
            border_color=t.border.strong,
            checkmark_color=t.text.on_accent,
        )
        cb.pack(anchor="w", pady=(0, t.space.xl))

        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill="x")

        ctk.CTkButton(
            buttons,
            text="Cancel",
            command=self._cancel,
            **style_secondary_button(t),
        ).pack(side="right", padx=(t.space.sm, 0))

        ctk.CTkButton(
            buttons,
            text="Delete",
            command=self._confirm,
            **style_danger_button(t),
        ).pack(side="right")

        self.bind("<Escape>", lambda _e: self._cancel())

    def _center_over(self, parent: ctk.CTkBaseClass) -> None:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        self.geometry(f"+{px + (pw - 480) // 2}+{py + (ph - 260) // 2}")

    def _confirm(self) -> None:
        remove_files = bool(self._remove_files_var.get())
        try:
            self._on_confirm(self._tracks, remove_files)
        finally:
            self.destroy()

    def _cancel(self) -> None:
        self.destroy()


# ─── Export progress dialog ─────────────────────────────────────────


@dataclass(slots=True)
class _ExportState:
    """Mutable snapshot of export progress. Updated on Tk thread only."""

    stage: ExportStage = ExportStage.PREPARING
    overall_percent: float = 0.0
    current_file_percent: float = 0.0
    current_index: int = 0
    total_files: int = 0
    current_filename: str = ""
    elapsed_seconds: float = 0.0
    finished: bool = False
    result: Optional[ExportResult] = None
    error: Optional[Exception] = None


class _ExportProgressDialog(ctk.CTkToplevel):
    """
    Modal-ish progress window for MPC export. Runs the exporter on a
    worker thread; updates render via `self.after(0, ...)` dispatches.
    """

    _WIDTH = 520
    _HEIGHT = 280

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        exporter: MPCExporter,
        database: Any,  # VaultDatabase — kept loose to avoid import cycles
        tracks: list[TrackRecord],
        destination: Path,
        toast_publisher: Callable[..., None],
        logger: logging.Logger,
    ) -> None:
        super().__init__(parent)

        self._theme = theme
        self._exporter = exporter
        self._database = database
        self._tracks = tracks
        self._destination = destination
        self._toast_publisher = toast_publisher
        self._log = logger

        self._state = _ExportState(total_files=len(tracks))
        self._cancel_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        t = theme
        self.title("Export to MPC")
        self.configure(fg_color=t.surface.base)
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self._center_over(parent)

        # Intercept close: behave like cancel.
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)

        # Prevent accidental parent interaction. We don't call grab_set
        # so user can still interact with the rest of the app (checking
        # queue status, etc.) — matches what professional apps do for
        # long-running operations.

        self._build_body()
        self._start_export()

    # ── Body ──

    def _build_body(self) -> None:
        t = self._theme

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.xl)
        frame.grid_columnconfigure(0, weight=1)

        # Title
        ctk.CTkLabel(
            frame,
            text=f"Exporting {len(self._tracks)} track"
            f"{'s' if len(self._tracks) != 1 else ''} to WAV",
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        # Destination
        dest_str = str(self._destination)
        if len(dest_str) > 60:
            dest_str = "…" + dest_str[-57:]
        ctk.CTkLabel(
            frame,
            text=dest_str,
            text_color=t.text.secondary,
            font=t.font.mono_body,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(t.space.xxs, t.space.lg))

        # Current file label
        self._current_file_label = ctk.CTkLabel(
            frame,
            text="Preparing…",
            text_color=t.text.primary,
            font=t.font.body,
            anchor="w",
        )
        self._current_file_label.grid(row=2, column=0, sticky="ew")

        # File counter / percent line
        self._counter_label = ctk.CTkLabel(
            frame,
            text="",
            text_color=t.text.secondary,
            font=t.font.mono_body,
            anchor="w",
        )
        self._counter_label.grid(row=3, column=0, sticky="ew", pady=(2, t.space.sm))

        # Overall progress bar
        self._progress_bar = ctk.CTkProgressBar(
            frame,
            fg_color=t.surface.elevated,
            progress_color=t.accent.blue,
            corner_radius=t.radius.pill,
            height=6,
        )
        self._progress_bar.grid(row=4, column=0, sticky="ew")
        self._progress_bar.set(0.0)

        # Per-file progress bar (thinner, purple, below main bar)
        self._file_bar = ctk.CTkProgressBar(
            frame,
            fg_color=t.surface.elevated,
            progress_color=t.accent.purple,
            corner_radius=t.radius.pill,
            height=3,
        )
        self._file_bar.grid(row=5, column=0, sticky="ew", pady=(t.space.xs, t.space.xl))
        self._file_bar.set(0.0)

        # Buttons
        button_row = ctk.CTkFrame(frame, fg_color="transparent")
        button_row.grid(row=6, column=0, sticky="ew")

        self._cancel_button = ctk.CTkButton(
            button_row,
            text="Cancel",
            command=self._on_cancel_clicked,
            **style_secondary_button(t),
        )
        self._cancel_button.pack(side="right")

        self._close_button = ctk.CTkButton(
            button_row,
            text="Close",
            command=self.destroy,
            **style_primary_button(t),
            state="disabled",
        )
        self._close_button.pack(side="right", padx=(0, t.space.sm))

        self.bind("<Escape>", lambda _e: self._on_close_request())

    def _center_over(self, parent: ctk.CTkBaseClass) -> None:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        self.geometry(
            f"+{px + (pw - self._WIDTH) // 2}+{py + (ph - self._HEIGHT) // 2}"
        )

    # ── Export execution ──

    def _start_export(self) -> None:
        source_paths = [Path(t.file_path) for t in self._tracks]

        self._worker_thread = threading.Thread(
            target=self._run_export_worker,
            args=(source_paths,),
            name="mpc-export",
            daemon=True,
        )
        self._worker_thread.start()

    def _run_export_worker(self, source_paths: list[Path]) -> None:
        """Runs on worker thread. Marshals progress back to Tk."""

        def on_progress(p: ExportProgress) -> None:
            self.after(0, lambda p=p: self._apply_progress(p))

        try:
            result = self._exporter.export_batch(
                source_paths,
                self._destination,
                flatten=True,
                progress_callback=on_progress,
                cancel_event=self._cancel_event,
            )
            self.after(0, lambda: self._on_export_finished(result, None))
        except ExportCancelledError:
            self.after(0, lambda: self._on_export_finished(None, e))
        except Exception as e:
            self._log.exception("Export failed")
            self.after(0, lambda e=e: self._on_export_finished(None, e))

    def _apply_progress(self, p: ExportProgress) -> None:
        """Runs on Tk thread."""
        self._state.stage = p.stage
        self._state.overall_percent = p.overall_percent
        self._state.current_file_percent = p.current_file_percent
        self._state.current_index = p.current_index
        self._state.total_files = p.total_files
        self._state.current_filename = p.current_filename
        self._state.elapsed_seconds = p.elapsed_seconds

        self._render_state()

    def _render_state(self) -> None:
        s = self._state

        # Current file label
        filename = s.current_filename or "Preparing…"
        if len(filename) > 55:
            filename = filename[:52] + "…"
        self._current_file_label.configure(text=filename)

        # Counter line — "3 of 12  ·  42%  ·  12.4s"
        parts: list[str] = []
        if s.total_files > 0:
            parts.append(f"{s.current_index} of {s.total_files}")
        parts.append(f"{int(s.overall_percent)}%")
        if s.elapsed_seconds > 0:
            parts.append(f"{s.elapsed_seconds:.1f}s")
        self._counter_label.configure(text="  ·  ".join(parts))

        # Bars
        self._progress_bar.set(max(0.0, min(1.0, s.overall_percent / 100.0)))
        self._file_bar.set(max(0.0, min(1.0, s.current_file_percent / 100.0)))

    def _on_export_finished(
        self,
        result: Optional[ExportResult],
        error: Optional[Exception],
    ) -> None:
        """Tk-thread handler. Render final state + log exports to DB."""
        t = self._theme
        self._state.finished = True
        self._state.result = result
        self._state.error = error

        # Swap cancel/close buttons
        self._cancel_button.configure(state="disabled")
        self._close_button.configure(state="normal")

        if error is not None:
            self._progress_bar.configure(progress_color=t.status.error)
            self._file_bar.configure(progress_color=t.status.error)
            if isinstance(error, ExportCancelledError):
                self._current_file_label.configure(
                    text="Cancelled.",
                    text_color=t.text.muted,
                )
                self._toast_publisher(
                    "Export cancelled.",
                    "warning",
                )
            else:
                self._current_file_label.configure(
                    text=f"Failed: {error}",
                    text_color=t.status.error,
                )
                self._toast_publisher(
                    f"Export failed: {error}",
                    "error",
                )
            return

        assert result is not None

        # Log each success to the DB's mpc_exports table.
        self._log_exports(result)

        success_count = len(result.exported)
        fail_count = len(result.failed)
        self._progress_bar.set(1.0)
        self._file_bar.set(1.0)

        if fail_count == 0:
            self._current_file_label.configure(
                text=f"Exported {success_count} file"
                f"{'s' if success_count != 1 else ''}.",
                text_color=t.status.success,
            )
            self._toast_publisher(
                f"Exported {success_count} file"
                f"{'s' if success_count != 1 else ''} to "
                f"{self._destination.name}.",
                "success",
            )
        else:
            self._progress_bar.configure(progress_color=t.status.warning)
            self._current_file_label.configure(
                text=f"{success_count} exported · {fail_count} failed",
                text_color=t.status.warning,
            )
            self._toast_publisher(
                f"Export finished with issues: "
                f"{success_count} exported, {fail_count} failed.",
                "warning",
            )

    def _log_exports(self, result: ExportResult) -> None:
        """Write mpc_exports rows for each successful file."""
        from core.database import ExportRecord

        for exp in result.exported:
            # Locate the track record by matching source_path to file_path.
            track_id = self._find_track_id(exp)
            if track_id is None:
                continue
            try:
                self._database.record_export(
                    ExportRecord(
                        track_id=track_id,
                        destination_path=str(exp.destination_path),
                        destination_device=self._destination.name,
                        wav_size_bytes=exp.wav_size_bytes,
                    )
                )
            except Exception as e:
                self._log.warning(
                    "Could not log export for track %d: %s",
                    track_id,
                    e,
                )

    def _find_track_id(self, exp: ExportedFile) -> Optional[int]:
        """Look up the track whose file_path matches the source path."""
        for track in self._tracks:
            if Path(track.file_path).resolve() == Path(exp.source_path).resolve():
                return track.id
        return None

    # ── User actions ──

    def _on_cancel_clicked(self) -> None:
        """User clicked Cancel. Signal the worker and await shutdown."""
        if self._state.finished:
            return
        self._cancel_button.configure(state="disabled", text="Cancelling…")
        self._cancel_event.set()

    def _on_close_request(self) -> None:
        """Close button / Escape / WM_DELETE. Cancel if still running."""
        if self._state.finished:
            self.destroy()
            return
        self._on_cancel_clicked()
        # Don't destroy immediately — let the worker unwind so the user
        # sees the "Cancelled" state before the dialog closes.


# ─── Track inspector (preview + metadata editing) ───────────────────


class _TrackInspectorDialog(ctk.CTkToplevel):
    """
    Single-track inspector: an in-app waveform preview of the local file
    plus editable star rating, tags and notes. Persists straight to the
    Vault DB on Save and calls `on_saved` so the parent can refresh.
    """

    _WIDTH = 620
    _HEIGHT = 560

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        ctx: "AppContext",
        track: TrackRecord,
        on_saved: Callable[[], None],
    ) -> None:
        super().__init__(parent)

        self._theme = theme
        self._ctx = ctx
        self._track = track
        self._on_saved = on_saved
        self._log = ctx.logger.getChild("vault.inspect")

        self._rating: int = int(track.rating or 0)
        self._star_buttons: list[ctk.CTkButton] = []
        self._player: Optional[WaveformPlayer] = None
        self._cancel_event = threading.Event()
        self._preview_started = False

        t = theme
        self.title(f"{track.artist} — {track.title}")
        self.configure(fg_color=t.surface.base)
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self._center_over(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._build_body()
        self.bind("<Escape>", lambda _e: self._close())

    def _build_body(self) -> None:
        t = self._theme
        track = self._track

        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.lg)
        frame.grid_columnconfigure(0, weight=1)

        # Title + subtitle
        ctk.CTkLabel(
            frame,
            text=track.title or "Untitled",
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            frame,
            text=track.artist or "Unknown artist",
            text_color=t.text.secondary,
            font=t.font.body,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(0, t.space.xs))

        # Metadata chips line
        meta_bits: list[str] = []
        if track.genre:
            meta_bits.append(track.genre)
        if track.bpm:
            meta_bits.append(f"{track.bpm:.1f} BPM")
        if track.camelot_key:
            meta_bits.append(track.camelot_key)
        if track.year:
            meta_bits.append(str(track.year))
        ctk.CTkLabel(
            frame,
            text="   ·   ".join(meta_bits) if meta_bits else "—",
            text_color=t.text.muted,
            font=t.font.caption,
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(0, t.space.md))

        # Waveform preview
        self._player = WaveformPlayer(
            frame,
            t,
            height=90,
            initial_volume=self._ctx.config.snapshot().config.ui.preview_volume,
        )
        self._player.grid(row=3, column=0, sticky="ew", pady=(0, t.space.md))
        self._start_preview()

        # Rating
        ctk.CTkLabel(
            frame, text="Rating", **style_label_meta(t), anchor="w"
        ).grid(row=4, column=0, sticky="w")
        star_row = ctk.CTkFrame(frame, fg_color="transparent")
        star_row.grid(row=5, column=0, sticky="w", pady=(t.space.xxs, t.space.md))
        for i in range(1, 6):
            btn = ctk.CTkButton(
                star_row,
                text="★",
                width=34,
                height=34,
                fg_color="transparent",
                hover_color=t.surface.elevated,
                text_color=t.text.muted,
                font=t.font.subheading,
                command=lambda n=i: self._set_rating(n),
            )
            btn.pack(side="left", padx=(0, 2))
            self._star_buttons.append(btn)
        ctk.CTkButton(
            star_row,
            text="Clear",
            width=54,
            height=34,
            command=lambda: self._set_rating(0),
            **style_ghost_button(t),
        ).pack(side="left", padx=(t.space.sm, 0))
        self._render_stars()

        # Tags
        ctk.CTkLabel(
            frame, text="Tags (comma-separated)", **style_label_meta(t),
            anchor="w",
        ).grid(row=6, column=0, sticky="w")
        self._tags_entry = ctk.CTkEntry(
            frame,
            height=38,
            **{k: v for k, v in style_input(t).items() if k != "height"},
        )
        self._tags_entry.grid(row=7, column=0, sticky="ew", pady=(t.space.xxs, t.space.md))
        if track.tags:
            self._tags_entry.insert(0, ", ".join(track.tags))

        # Notes
        ctk.CTkLabel(
            frame, text="Notes", **style_label_meta(t), anchor="w"
        ).grid(row=8, column=0, sticky="w")
        self._notes_box = ctk.CTkTextbox(
            frame,
            height=90,
            fg_color=t.surface.raised,
            text_color=t.text.primary,
            border_color=t.border.subtle,
            border_width=t.stroke.hairline,
            corner_radius=t.radius.md,
            font=t.font.body,
        )
        self._notes_box.grid(row=9, column=0, sticky="ew", pady=(t.space.xxs, t.space.lg))
        if track.notes:
            self._notes_box.insert("1.0", track.notes)

        # Buttons
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=10, column=0, sticky="ew")
        ctk.CTkButton(
            buttons, text="Export chop kit",
            command=self._export_chop_kit,
            **style_ghost_button(t),
        ).pack(side="left")
        ctk.CTkButton(
            buttons, text="Cancel", command=self._close,
            **style_secondary_button(t),
        ).pack(side="right", padx=(t.space.sm, 0))
        ctk.CTkButton(
            buttons, text="Save", command=self._save,
            **style_primary_button(t),
        ).pack(side="right")

    # ── Preview ──

    def _start_preview(self) -> None:
        if self._preview_started or self._player is None:
            return
        if self._ctx.preview is None:
            self._player.set_error("Preview service unavailable.")
            return
        path = Path(self._track.file_path)
        if not path.exists():
            self._player.set_error("File not found on disk.")
            return
        self._preview_started = True
        self._player.set_loading("Decoding…")
        threading.Thread(
            target=self._preview_worker, args=(path,),
            name="vault-preview", daemon=True,
        ).start()

    def _preview_worker(self, path: Path) -> None:
        try:
            data = self._ctx.preview.load_file(
                path, cancel_event=self._cancel_event
            )
        except Exception as e:  # noqa: BLE001 — surfaced in UI
            self.after(0, lambda err=e: self._on_preview_error(err))
            return
        self.after(0, lambda: self._on_preview_ready(data))

    def _on_preview_ready(self, data) -> None:
        if self._player is not None:
            self._player.set_preview(data)

    def _on_preview_error(self, error: Exception) -> None:
        if self._player is not None:
            self._player.set_error(f"Preview failed: {error}")

    # ── Rating ──

    def _set_rating(self, value: int) -> None:
        self._rating = max(0, min(5, int(value)))
        self._render_stars()

    def _render_stars(self) -> None:
        t = self._theme
        for idx, btn in enumerate(self._star_buttons, start=1):
            filled = idx <= self._rating
            btn.configure(
                text="★" if filled else "☆",
                text_color=t.accent.blue if filled else t.text.muted,
            )

    # ── Export chop kit ──

    def _export_chop_kit(self) -> None:
        if self._ctx.exporter is None:
            self._ctx.publish_toast("Exporter not available.", "error")
            return
        destination = filedialog.askdirectory(
            title="Choose destination for chop kit",
            mustexist=True,
            parent=self.winfo_toplevel(),
        )
        if not destination:
            return
        path = Path(self._track.file_path)
        self._ctx.publish_toast("Analyzing chops…", "info")
        threading.Thread(
            target=self._chop_kit_worker,
            args=(path, Path(destination)),
            name="vault-chop-kit",
            daemon=True,
        ).start()

    def _chop_kit_worker(self, path: Path, destination: Path) -> None:
        try:
            from core.analyzer import AudioAnalyzer
            from core.chopper import AudioChopper

            ffmpeg = (
                self._ctx.exporter.ffmpeg_path
                if self._ctx.exporter is not None
                else None
            )
            chopper = AudioChopper(
                analyzer=AudioAnalyzer(ffmpeg_path=ffmpeg),
            )
            plan = chopper.plan(path, cancel_event=self._cancel_event)
            result = self._ctx.exporter.export_chop_kit(
                path, plan, destination,
                cancel_event=self._cancel_event,
            )
        except Exception as e:  # noqa: BLE001
            self.after(0, lambda err=e: self._on_chop_kit_error(err))
            return
        self.after(0, lambda: self._on_chop_kit_done(result))

    def _on_chop_kit_done(self, result) -> None:
        n = len(result.exported)
        self._ctx.publish_toast(
            f"Exported chop kit: {n} file{'s' if n != 1 else ''} → "
            f"{result.destination_root.name}",
            "success",
        )

    def _on_chop_kit_error(self, error: Exception) -> None:
        self._ctx.publish_toast(f"Chop kit export failed: {error}", "error")

    # ── Save ──

    def _save(self) -> None:
        tags = [
            s.strip()
            for s in self._tags_entry.get().split(",")
            if s.strip()
        ]
        notes = self._notes_box.get("1.0", "end").strip()
        tid = self._track.id
        if tid is None:
            self._close()
            return
        try:
            self._ctx.database.set_track_rating(
                tid, self._rating if self._rating > 0 else None
            )
            self._ctx.database.set_track_annotations(
                tid, notes=notes, tags=tags
            )
            self._ctx.publish_toast("Track updated.", "success")
        except Exception as e:  # noqa: BLE001
            self._log.exception("Saving inspector edits failed")
            self._ctx.publish_toast(f"Could not save: {e}", "error")
            return
        try:
            self._on_saved()
        finally:
            self._close()

    # ── Lifecycle ──

    def _center_over(self, parent: ctk.CTkBaseClass) -> None:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        self.geometry(
            f"+{px + (pw - self._WIDTH) // 2}+{py + (ph - self._HEIGHT) // 2}"
        )

    def _close(self) -> None:
        self._cancel_event.set()
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self.destroy()


# ─── Add-to-crate dialog ────────────────────────────────────────────


class _AddToCrateDialog(ctk.CTkToplevel):
    """Pick an existing crate or create a new one, then add tracks to it."""

    _WIDTH = 460
    _HEIGHT = 300

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        ctx: "AppContext",
        tracks: list[TrackRecord],
        on_done: Callable[[], None],
    ) -> None:
        super().__init__(parent)

        self._theme = theme
        self._ctx = ctx
        self._tracks = tracks
        self._on_done = on_done
        self._log = ctx.logger.getChild("vault.crate")

        try:
            self._crates = ctx.database.list_crates()
        except Exception:
            self._crates = []
        self._label_to_id: dict[str, int] = {
            f"{c.name} ({c.track_count})": int(c.id)
            for c in self._crates
            if c.id is not None
        }

        t = theme
        self.title("Add to crate")
        self.configure(fg_color=t.surface.base)
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent.winfo_toplevel())
        self._center_over(parent)

        self._build_body()
        self.bind("<Escape>", lambda _e: self.destroy())

    def _build_body(self) -> None:
        t = self._theme
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.xl)
        frame.grid_columnconfigure(0, weight=1)

        count = len(self._tracks)
        ctk.CTkLabel(
            frame,
            text=f"Add {count} track{'s' if count != 1 else ''} to a crate",
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, t.space.md))

        # Existing crate picker
        existing_labels = list(self._label_to_id.keys())
        ctk.CTkLabel(
            frame, text="Existing crate", **style_label_meta(t), anchor="w"
        ).grid(row=1, column=0, sticky="w")
        self._existing_menu = ctk.CTkOptionMenu(
            frame,
            values=existing_labels or ["— none yet —"],
            fg_color=t.surface.raised,
            button_color=t.surface.raised,
            button_hover_color=t.surface.elevated,
            dropdown_fg_color=t.surface.elevated,
            text_color=t.text.primary,
            dropdown_text_color=t.text.primary,
            font=t.font.body,
            corner_radius=t.radius.md,
        )
        self._existing_menu.grid(row=2, column=0, sticky="ew", pady=(t.space.xxs, t.space.xs))
        if not existing_labels:
            self._existing_menu.configure(state="disabled")
        ctk.CTkButton(
            frame,
            text="Add to selected crate",
            command=self._add_to_existing,
            **style_secondary_button(t),
            state="normal" if existing_labels else "disabled",
        ).grid(row=3, column=0, sticky="ew", pady=(0, t.space.lg))

        # New crate
        ctk.CTkLabel(
            frame, text="Or create a new crate", **style_label_meta(t),
            anchor="w",
        ).grid(row=4, column=0, sticky="w")
        new_row = ctk.CTkFrame(frame, fg_color="transparent")
        new_row.grid(row=5, column=0, sticky="ew", pady=(t.space.xxs, 0))
        new_row.grid_columnconfigure(0, weight=1)
        self._new_entry = ctk.CTkEntry(
            new_row,
            height=38,
            placeholder_text="Crate name…",
            **{k: v for k, v in style_input(t).items() if k != "height"},
        )
        self._new_entry.grid(row=0, column=0, sticky="ew", padx=(0, t.space.sm))
        ctk.CTkButton(
            new_row,
            text="Create + add",
            command=self._create_and_add,
            **style_primary_button(t),
            width=120,
        ).grid(row=0, column=1)

    def _add_to_existing(self) -> None:
        label = self._existing_menu.get()
        crate_id = self._label_to_id.get(label)
        if crate_id is None:
            return
        self._do_add(crate_id)

    def _create_and_add(self) -> None:
        name = self._new_entry.get().strip()
        if not name:
            self._ctx.publish_toast("Enter a crate name.", "warning")
            return
        try:
            crate_id = self._ctx.database.create_crate(name)
        except Exception as e:  # noqa: BLE001
            self._log.exception("Crate creation failed")
            self._ctx.publish_toast(f"Could not create crate: {e}", "error")
            return
        self._do_add(crate_id)

    def _do_add(self, crate_id: int) -> None:
        track_ids = [t.id for t in self._tracks if t.id is not None]
        try:
            added = self._ctx.database.add_tracks_to_crate(crate_id, track_ids)
        except Exception as e:  # noqa: BLE001
            self._log.exception("Add-to-crate failed")
            self._ctx.publish_toast(f"Could not add to crate: {e}", "error")
            return
        self._ctx.publish_toast(
            f"Added {added} track{'s' if added != 1 else ''} to crate.",
            "success",
        )
        try:
            self._on_done()
        finally:
            self.destroy()

    def _center_over(self, parent: ctk.CTkBaseClass) -> None:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        self.geometry(
            f"+{px + (pw - self._WIDTH) // 2}+{py + (ph - self._HEIGHT) // 2}"
        )


# ─── Duplicates dialog ──────────────────────────────────────────────


class _DuplicatesDialog(ctk.CTkToplevel):
    """
    Lists clusters of likely-duplicate tracks (identical checksum or the
    same normalized artist+title) and lets the user remove DB rows for the
    copies they don't want to keep.
    """

    _WIDTH = 720
    _HEIGHT = 600

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        ctx: "AppContext",
        groups: list[DuplicateGroup],
        on_deleted: Callable[[], None],
    ) -> None:
        super().__init__(parent)

        self._theme = theme
        self._ctx = ctx
        self._groups = groups
        self._on_deleted = on_deleted
        self._log = ctx.logger.getChild("vault.dupes")

        # track_id → BooleanVar for the "remove" checkboxes.
        self._marks: dict[int, ctk.BooleanVar] = {}

        t = theme
        self.title("Duplicate tracks")
        self.configure(fg_color=t.surface.base)
        self.geometry(f"{self._WIDTH}x{self._HEIGHT}")
        self.transient(parent.winfo_toplevel())
        self._center_over(parent)

        self._build_body()
        self.bind("<Escape>", lambda _e: self.destroy())

    def _build_body(self) -> None:
        t = self._theme
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.pack(fill="both", expand=True, padx=t.space.xl, pady=t.space.lg)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        total = sum(len(g.tracks) for g in self._groups)
        ctk.CTkLabel(
            frame,
            text=f"{len(self._groups)} duplicate group"
            f"{'s' if len(self._groups) != 1 else ''} · {total} tracks",
            text_color=t.text.primary,
            font=t.font.subheading,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, t.space.sm))

        scroll = ctk.CTkScrollableFrame(
            frame, fg_color=t.surface.raised, corner_radius=t.radius.lg
        )
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        row = 0
        for gi, group in enumerate(self._groups):
            reason = (
                "Byte-identical files"
                if group.reason == "checksum"
                else "Same artist + title"
            )
            ctk.CTkLabel(
                scroll,
                text=f"Group {gi + 1} · {reason}",
                text_color=t.text.secondary,
                font=t.font.caption,
                anchor="w",
            ).grid(row=row, column=0, sticky="ew", padx=t.space.md,
                   pady=(t.space.sm, t.space.xxs))
            row += 1

            # Keep the first track by default; pre-mark the rest for removal.
            for ti, track in enumerate(group.tracks):
                if track.id is None:
                    continue
                var = ctk.BooleanVar(value=ti > 0)
                self._marks[track.id] = var
                label = f"{track.artist} — {track.title}"
                sub = self._format_track_meta(track)
                cb = ctk.CTkCheckBox(
                    scroll,
                    text=f"  {label}\n  {sub}",
                    variable=var,
                    text_color=t.text.primary,
                    font=t.font.body,
                    fg_color=t.status.error,
                    hover_color=t.status.error,
                    border_color=t.border.strong,
                    checkmark_color=t.text.on_accent,
                )
                cb.grid(row=row, column=0, sticky="ew", padx=t.space.lg,
                        pady=(0, t.space.xxs))
                row += 1

        # Buttons
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", pady=(t.space.md, 0))
        ctk.CTkLabel(
            buttons,
            text="Checked tracks will be removed from the Vault (DB only).",
            text_color=t.text.muted,
            font=t.font.caption,
            anchor="w",
        ).pack(side="left")
        ctk.CTkButton(
            buttons, text="Close", command=self.destroy,
            **style_secondary_button(t),
        ).pack(side="right", padx=(t.space.sm, 0))
        ctk.CTkButton(
            buttons, text="Remove checked", command=self._remove_checked,
            **style_danger_button(t),
        ).pack(side="right")

    @staticmethod
    def _format_track_meta(track: TrackRecord) -> str:
        bits: list[str] = []
        if track.genre:
            bits.append(track.genre)
        if track.bpm:
            bits.append(f"{track.bpm:.0f} BPM")
        if track.year:
            bits.append(str(track.year))
        bits.append(Path(track.file_path).name)
        return "   ·   ".join(bits)

    def _remove_checked(self) -> None:
        to_remove = [tid for tid, var in self._marks.items() if var.get()]
        if not to_remove:
            self._ctx.publish_toast("Nothing checked.", "info")
            return
        removed = 0
        for tid in to_remove:
            try:
                self._ctx.database.delete_track(tid)
                removed += 1
            except Exception as e:  # noqa: BLE001
                self._log.warning("Could not delete track %d: %s", tid, e)
        self._ctx.publish_toast(
            f"Removed {removed} duplicate{'s' if removed != 1 else ''}.",
            "success",
        )
        try:
            self._on_deleted()
        finally:
            self.destroy()

    def _center_over(self, parent: ctk.CTkBaseClass) -> None:
        parent.update_idletasks()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        self.geometry(
            f"+{px + (pw - self._WIDTH) // 2}+{py + (ph - self._HEIGHT) // 2}"
        )
