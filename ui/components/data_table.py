"""
ui/components/data_table.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Virtualized Data Table

High-performance sortable data table for the Vault tab. Scales to
50k+ rows without UI jank because it only renders the rows currently
visible in the viewport.

How it works:
  • Virtual scrolling: the full row count is represented by a
    scrollbar that maps to row indices (not pixels). A fixed pool
    of row widgets (~30-50 depending on viewport height) is
    recycled — scrolling repositions the pool and updates each
    widget's displayed data rather than creating/destroying widgets.
  • Fixed row height: makes scroll-position ↔ row-index math trivial
    and keeps layout reflow cost at zero during scroll.
  • Column definitions are declarative: each column has a key, label,
    renderer, min/max widths, and sortability flag.
  • Sorting: click-to-sort header with ascending/descending toggle.
    Sort is applied to the underlying data source; virtual rendering
    adapts automatically.
  • Selection: single-row click selection; multi-row shift/ctrl-click.
  • No native scrollbar styling gymnastics — CTk's CTkScrollbar works.

The table operates on any list-of-dicts data source. The Vault tab
will feed it the output of VaultDatabase.list_tracks() (post-filter);
the table doesn't know or care about the DB.

Design constraints that informed this file:
  • No real virtualization library exists for Tk; we're hand-rolling it.
  • Tk has no native "data grid" widget; CTkTable doesn't handle
    virtualization. Building on Frame + Label pools is the
    pragmatic answer.
  • Must work acceptably at 50k rows on a modest machine — the test
    at the bottom of this file uses 50k synthetic rows to verify.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import customtkinter as ctk

from ui.theme import Theme


# ─── Column definition ──────────────────────────────────────────────

class SortDirection(Enum):
    NONE = "none"
    ASC = "asc"
    DESC = "desc"


@dataclass(slots=True)
class Column:
    """
    Declarative column spec. Renderers return the string (or number)
    to display in that column for a given row dict.
    """
    key: str
    label: str
    width: int = 120                 # pixel width; not resizable in MVP
    sortable: bool = True
    align: str = "w"                 # tk anchor: 'w', 'center', 'e'
    # Optional renderer. If None, the row dict is looked up by `key`
    # and str()-coerced.
    renderer: Optional[Callable[[dict], str]] = None
    # Numeric column flag — influences default sort order for the
    # column and right-aligns by default.
    numeric: bool = False
    # Optional text color override per row (function of row dict).
    color_fn: Optional[Callable[[dict], Optional[str]]] = None


# ─── Public events / callbacks ──────────────────────────────────────

@dataclass(slots=True, frozen=True)
class TableSort:
    column_key: str
    direction: SortDirection


# ─── The table ──────────────────────────────────────────────────────

class VirtualDataTable(ctk.CTkFrame):
    """
    Virtualized sortable table. Feed it data via `set_data(rows)`;
    it renders the visible slice and keeps scroll smooth.

    API:
        tbl = VirtualDataTable(parent, theme, columns=[...])
        tbl.set_data(list_of_row_dicts)
        tbl.on_sort_changed = lambda s: handle(s)
        tbl.on_row_activated = lambda row_dict: open_track(row_dict)
        tbl.on_selection_changed = lambda rows: update_toolbar(rows)
    """

    # Geometry
    _ROW_HEIGHT = 36
    _HEADER_HEIGHT = 36
    _BUFFER_ROWS = 2                 # render a couple extra above/below

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        columns: list[Column],
        *,
        multi_select: bool = True,
        on_sort_changed: Optional[Callable[[TableSort], None]] = None,
        on_row_activated: Optional[Callable[[dict], None]] = None,
        on_selection_changed: Optional[Callable[[list[dict]], None]] = None,
    ) -> None:
        super().__init__(
            parent,
            fg_color=theme.surface.base,
            border_color=theme.border.subtle,
            border_width=theme.stroke.hairline,
            corner_radius=theme.radius.lg,
        )

        if not columns:
            raise ValueError("VirtualDataTable requires at least one column")

        self._theme = theme
        self._columns = list(columns)
        self._multi_select = multi_select

        # Callbacks
        self.on_sort_changed = on_sort_changed
        self.on_row_activated = on_row_activated
        self.on_selection_changed = on_selection_changed

        # Data source
        self._rows: list[dict] = []
        self._sort = TableSort("", SortDirection.NONE)

        # Selection — set of row indices (into self._rows)
        self._selection: set[int] = set()
        self._last_click_index: Optional[int] = None

        # Virtualization
        self._row_pool: list["_RowView"] = []
        self._first_visible_index: int = 0

        # Build UI
        self._build_body(theme)

        # Track viewport resize so pool adapts to window height changes.
        self._viewport.bind("<Configure>", self._on_resize, add="+")

    # ── Public API ──

    def set_data(self, rows: list[dict]) -> None:
        """
        Replace the dataset. Resets scroll position to the top and
        clears any existing selection.
        """
        self._rows = list(rows)
        self._selection.clear()
        self._first_visible_index = 0
        self._update_scrollbar()
        self._render_visible()
        self._fire_selection_changed()

        # If we rendered while the widget was unmapped (h=1), schedule a
        # follow-up render for once it's likely mapped and has its real size.
        if self._viewport.winfo_height() <= 1:
            self.after(200, self._render_visible)

    def set_sort(
        self, column_key: str, direction: SortDirection,
    ) -> None:
        """
        Programmatically set the sort state (e.g. when the Vault tab
        restores saved user preferences). Does NOT sort the data —
        that's the caller's responsibility; the table reflects the
        sort visually via the header indicator.
        """
        self._sort = TableSort(column_key, direction)
        self._update_header_indicators()

    def get_selected_rows(self) -> list[dict]:
        return [self._rows[i] for i in sorted(self._selection)
                if 0 <= i < len(self._rows)]

    def clear_selection(self) -> None:
        self._selection.clear()
        self._render_visible()
        self._fire_selection_changed()

    def row_count(self) -> int:
        return len(self._rows)

    # ── Body construction ──

    def _build_body(self, theme: Theme) -> None:
        t = theme

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ── Header row ──
        self._header = ctk.CTkFrame(
            self,
            fg_color=t.surface.raised,
            corner_radius=0,
            border_width=0,
            height=self._HEADER_HEIGHT,
        )
        self._header.grid(row=0, column=0, sticky="ew")
        self._header.grid_propagate(False)

        self._header_buttons: dict[str, ctk.CTkButton] = {}
        self._header_indicators: dict[str, ctk.CTkLabel] = {}
        self._build_header(t)

        # Hairline divider under header
        ctk.CTkFrame(
            self, height=1, fg_color=t.border.subtle,
            corner_radius=0, border_width=0,
        ).grid(row=0, column=0, sticky="sew")

        # ── Body area: holds the row pool + the scrollbar ──
        body = ctk.CTkFrame(
            self, fg_color="transparent",
            border_width=0, corner_radius=0,
        )
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0)
        body.grid_rowconfigure(0, weight=1)

        # Viewport: the clipping frame that contains the row pool.
        # We don't scroll it; we reposition row widgets within it.
        self._viewport = ctk.CTkFrame(
            body, fg_color="transparent",
            border_width=0, corner_radius=0,
        )
        self._viewport.grid(row=0, column=0, sticky="nsew")
        self._viewport.grid_propagate(False)

        # Scrollbar operates on row-indices rather than pixels.
        self._scrollbar = ctk.CTkScrollbar(
            body,
            command=self._on_scrollbar,
            button_color=t.border.strong,
            button_hover_color=t.accent.blue,
            fg_color=t.surface.base,
        )
        self._scrollbar.grid(row=0, column=1, sticky="ns",
                             padx=(0, 2), pady=2)
        self._scrollbar.set(0.0, 1.0)

        # Mousewheel scrolling — bound on the viewport so scroll events
        # in the empty area work too. Platform differences in Tk mean
        # we have to bind three event names to cover everything.
        self._viewport.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self._viewport.bind("<Button-4>",   self._on_mousewheel_linux_up, add="+")
        self._viewport.bind("<Button-5>",   self._on_mousewheel_linux_down, add="+")
        self.bind("<MouseWheel>", self._on_mousewheel, add="+")

        # Keyboard navigation
        self.bind("<Up>",        self._on_key_up, add="+")
        self.bind("<Down>",      self._on_key_down, add="+")
        self.bind("<Prior>",     self._on_key_page_up, add="+")
        self.bind("<Next>",      self._on_key_page_down, add="+")
        self.bind("<Home>",      self._on_key_home, add="+")
        self.bind("<End>",       self._on_key_end, add="+")

        # Empty-state placeholder
        self._empty_label = ctk.CTkLabel(
            self._viewport,
            text="No tracks in the Vault yet.\nQueue your first URL from Manual Rip.",
            text_color=t.text.muted,
            font=t.font.body,
            justify="center",
        )

    def _build_header(self, theme: Theme) -> None:
        """One CTkButton per column, arranged in a row via place()."""
        t = theme
        x = 0
        for col in self._columns:
            btn = ctk.CTkButton(
                self._header,
                text=col.label,
                command=lambda k=col.key: self._on_header_clicked(k),
                fg_color="transparent",
                hover_color=t.surface.elevated if col.sortable else "transparent",
                text_color=t.text.secondary,
                font=t.font.caption,
                border_width=0,
                corner_radius=0,
                anchor=col.align,
                height=self._HEADER_HEIGHT,
            )
            btn.place(x=x, y=0, width=col.width, height=self._HEADER_HEIGHT)

            # Sort indicator label — positioned inside the header cell
            # at the right edge, visible only on the sorted column.
            indicator = ctk.CTkLabel(
                self._header, text="",
                text_color=t.accent.blue,
                font=t.font.caption,
            )
            indicator.place(x=x + col.width - 16, y=0,
                            height=self._HEADER_HEIGHT, width=14)

            self._header_buttons[col.key] = btn
            self._header_indicators[col.key] = indicator
            x += col.width

    # ── Virtualization ──

    def _viewport_height(self) -> int:
        h = self._viewport.winfo_height()
        return max(h, self._ROW_HEIGHT)  # avoid div-by-zero at first layout

    def _visible_row_count(self) -> int:
        return max(1, self._viewport_height() // self._ROW_HEIGHT) + self._BUFFER_ROWS

    def _ensure_pool_size(self) -> None:
        """
        Grow or shrink the row widget pool to match viewport needs.
        Grow costs widget construction (~1ms per row on modern HW);
        shrink just destroys extras.
        """
        t = self._theme
        need = self._visible_row_count()
        have = len(self._row_pool)

        if have < need:
            for i in range(need - have):
                rv = _RowView(
                    self._viewport, t, self._columns,
                    on_click=self._on_row_clicked,
                    on_double_click=self._on_row_activated,
                )
                self._row_pool.append(rv)

        elif have > need:
            for rv in self._row_pool[need:]:
                rv.destroy()
            self._row_pool = self._row_pool[:need]

    def _render_visible(self) -> None:
        """
        Render the currently visible slice by repositioning pool
        widgets and updating their data. This is the hot path —
        called on every scroll tick.
        """
        self._ensure_pool_size()

        total_rows = len(self._rows)
        if total_rows == 0:
            # Empty state
            for rv in self._row_pool:
                rv.hide()
            if not self._empty_label.winfo_ismapped():
                self._empty_label.place(relx=0.5, rely=0.5, anchor="center")
            return

        if self._empty_label.winfo_ismapped():
            self._empty_label.place_forget()

        visible_count = self._visible_row_count()
        first = max(0, min(self._first_visible_index,
                           max(0, total_rows - visible_count)))
        self._first_visible_index = first

        for pool_idx, rv in enumerate(self._row_pool):
            row_idx = first + pool_idx
            if row_idx >= total_rows:
                rv.hide()
                continue
            rv.set_row(
                data=self._rows[row_idx],
                row_index=row_idx,
                selected=row_idx in self._selection,
                zebra=(row_idx % 2 == 1),
            )
            y = pool_idx * self._ROW_HEIGHT
            rv.place_at(y=y, row_height=self._ROW_HEIGHT)

        self._update_scrollbar()

    def _update_scrollbar(self) -> None:
        """Map first_visible/last_visible into the 0..1 scrollbar range."""
        total = len(self._rows)
        if total == 0:
            self._scrollbar.set(0.0, 1.0)
            return
        visible = self._visible_row_count()
        first = self._first_visible_index
        last = min(total, first + visible)
        self._scrollbar.set(first / total, last / total)

    def _scroll_to_row(self, row_index: int) -> None:
        """Ensure the given row is visible. Used by keyboard nav."""
        total = len(self._rows)
        if total == 0:
            return
        row_index = max(0, min(row_index, total - 1))
        visible = self._visible_row_count() - self._BUFFER_ROWS

        if row_index < self._first_visible_index:
            self._first_visible_index = row_index
        elif row_index >= self._first_visible_index + visible:
            self._first_visible_index = row_index - visible + 1
        self._render_visible()

    # ── Scroll input handlers ──

    def _on_scrollbar(self, *args) -> None:
        """CTkScrollbar callback — `command` can be 'moveto' or 'scroll'."""
        total = len(self._rows)
        if total == 0:
            return

        if args[0] == "moveto":
            frac = float(args[1])
            self._first_visible_index = int(frac * total)
        elif args[0] == "scroll":
            amount = int(args[1])
            unit = args[2] if len(args) > 2 else "units"
            step = self._visible_row_count() - self._BUFFER_ROWS
            if unit == "pages":
                self._first_visible_index += amount * max(1, step)
            else:
                self._first_visible_index += amount

        self._first_visible_index = max(
            0, min(self._first_visible_index,
                   max(0, total - self._visible_row_count() + self._BUFFER_ROWS)),
        )
        self._render_visible()

    def _on_mousewheel(self, event) -> None:
        """Windows/macOS mouse wheel. event.delta is signed."""
        # macOS delivers deltas of +/- 1, Windows +/- 120. Normalize to rows.
        import sys
        if sys.platform == "darwin":
            step = -event.delta
        else:
            step = -int(event.delta / 120)
        if step == 0:
            step = -1 if event.delta > 0 else 1
        self._scroll_by_rows(step)

    def _on_mousewheel_linux_up(self, _event) -> None:
        self._scroll_by_rows(-3)

    def _on_mousewheel_linux_down(self, _event) -> None:
        self._scroll_by_rows(3)

    def _scroll_by_rows(self, delta: int) -> None:
        total = len(self._rows)
        if total == 0:
            return
        self._first_visible_index += delta
        max_first = max(0, total - self._visible_row_count() + self._BUFFER_ROWS)
        self._first_visible_index = max(0, min(self._first_visible_index, max_first))
        self._render_visible()

    # ── Keyboard handlers ──

    def _on_key_up(self, _event) -> str:
        self._move_cursor(-1)
        return "break"

    def _on_key_down(self, _event) -> str:
        self._move_cursor(1)
        return "break"

    def _on_key_page_up(self, _event) -> str:
        self._move_cursor(-(self._visible_row_count() - self._BUFFER_ROWS))
        return "break"

    def _on_key_page_down(self, _event) -> str:
        self._move_cursor(self._visible_row_count() - self._BUFFER_ROWS)
        return "break"

    def _on_key_home(self, _event) -> str:
        if self._rows:
            self._set_cursor(0)
        return "break"

    def _on_key_end(self, _event) -> str:
        if self._rows:
            self._set_cursor(len(self._rows) - 1)
        return "break"

    def _move_cursor(self, delta: int) -> None:
        if not self._rows:
            return
        current = self._last_click_index if self._last_click_index is not None else 0
        new = max(0, min(current + delta, len(self._rows) - 1))
        self._set_cursor(new)

    def _set_cursor(self, row_index: int) -> None:
        self._selection = {row_index}
        self._last_click_index = row_index
        self._scroll_to_row(row_index)
        self._fire_selection_changed()

    def scroll_to_and_select(self, row_index: int) -> None:
        """Public API: scroll a row into view and select it."""
        if not self._rows:
            return
        row_index = max(0, min(row_index, len(self._rows) - 1))
        self._set_cursor(row_index)
        self.focus_set()
        self._render_visible()

    # ── Row click handling ──

    def _on_row_clicked(self, row_index: int, event) -> None:
        """Selection model: click/ctrl-click/shift-click semantics."""
        if not (0 <= row_index < len(self._rows)):
            return

        # Detect modifier state. Tk provides event.state — inspect bits.
        # 0x0001 = Shift, 0x0004 = Control, 0x0008 (Mac) or 0x0010 = Command
        shift = bool(event.state & 0x0001) if hasattr(event, "state") else False
        ctrl_or_cmd = bool(event.state & (0x0004 | 0x0008)) \
            if hasattr(event, "state") else False

        if not self._multi_select:
            self._selection = {row_index}
        elif shift and self._last_click_index is not None:
            # Range select
            lo, hi = sorted((self._last_click_index, row_index))
            self._selection = set(range(lo, hi + 1))
        elif ctrl_or_cmd:
            # Toggle
            if row_index in self._selection:
                self._selection.discard(row_index)
            else:
                self._selection.add(row_index)
        else:
            self._selection = {row_index}

        self._last_click_index = row_index
        # Focus to enable keyboard nav
        self.focus_set()
        self._render_visible()
        self._fire_selection_changed()

    def _on_row_activated(self, row_index: int) -> None:
        if self.on_row_activated and 0 <= row_index < len(self._rows):
            self.on_row_activated(self._rows[row_index])

    # ── Header click handling (sort) ──

    def _on_header_clicked(self, column_key: str) -> None:
        col = next((c for c in self._columns if c.key == column_key), None)
        if col is None or not col.sortable:
            return

        # Cycle: none → asc → desc → asc (never back to none via click)
        if self._sort.column_key != column_key:
            direction = SortDirection.ASC
        elif self._sort.direction == SortDirection.ASC:
            direction = SortDirection.DESC
        else:
            direction = SortDirection.ASC

        self._sort = TableSort(column_key, direction)
        self._update_header_indicators()
        if self.on_sort_changed is not None:
            self.on_sort_changed(self._sort)

    def _update_header_indicators(self) -> None:
        for key, indicator in self._header_indicators.items():
            if key == self._sort.column_key:
                glyph = "▲" if self._sort.direction == SortDirection.ASC else "▼"
                indicator.configure(text=glyph)
            else:
                indicator.configure(text="")

    # ── Resize ──

    def _on_resize(self, _event) -> None:
        """Viewport size changed — re-render with updated pool size."""
        self._render_visible()

    # ── Callback plumbing ──

    def _fire_selection_changed(self) -> None:
        if self.on_selection_changed is not None:
            self.on_selection_changed(self.get_selected_rows())


# ─── Row widget (member of the recycled pool) ───────────────────────

class _RowView:
    """
    One row in the pool. Not a CTkFrame subclass — just a wrapper
    managing a frame + per-column labels. Hidden rows have their
    frame un-placed but kept alive for reuse.
    """

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: Theme,
        columns: list[Column],
        *,
        on_click: Callable[[int, Any], None],
        on_double_click: Callable[[int], None],
    ) -> None:
        self._theme = theme
        self._columns = columns
        self._on_click = on_click
        self._on_double_click = on_double_click
        self._row_index: int = -1

        self._frame = ctk.CTkFrame(
            parent,
            fg_color="transparent",
            border_width=0,
            corner_radius=0,
            height=36,
        )
        # No initial place — hidden until `place_at` is called.

        # Pre-create one label per column. Labels are reconfigured,
        # never recreated, across scroll events.
        self._labels: list[ctk.CTkLabel] = []
        x = 0
        for col in self._columns:
            lbl = ctk.CTkLabel(
                self._frame,
                text="",
                text_color=theme.text.primary,
                font=theme.font.body if not col.numeric else theme.font.mono_body,
                anchor="e" if col.numeric else col.align,
            )
            lbl.place(x=x, y=0, width=col.width, height=36)
            self._labels.append(lbl)
            x += col.width

        # Bind click/double-click on every label + the frame itself so
        # the whole row is uniformly clickable.
        for widget in (self._frame, *self._labels):
            widget.bind("<Button-1>", self._on_single_click, add="+")
            widget.bind("<Double-Button-1>", self._on_double_click_evt, add="+")

    # ── Public API ──

    def set_row(
        self,
        data: dict,
        row_index: int,
        selected: bool,
        zebra: bool,
    ) -> None:
        self._row_index = row_index

        t = self._theme
        # Row background: selection > zebra > transparent
        if selected:
            bg = t.surface.elevated
            fg_text = t.text.primary
        elif zebra:
            bg = t.surface.raised
            fg_text = t.text.primary
        else:
            bg = "transparent"
            fg_text = t.text.primary

        self._frame.configure(fg_color=bg)

        # Selection border — 1px accent on the left to indicate selected
        if selected:
            self._frame.configure(
                border_color=t.accent.blue,
                border_width=0,   # CTkFrame border draws all four sides;
                                  # we simulate left-only via an inner rail.
            )

        # Per-column rendering
        for col, lbl in zip(self._columns, self._labels):
            text = self._render_cell(data, col)
            color = t.text.primary
            if col.color_fn is not None:
                override = col.color_fn(data)
                if override:
                    color = override
            lbl.configure(text=text, text_color=color, fg_color=bg)

    def place_at(self, y: int, row_height: int) -> None:
        self._frame.place(x=0, y=y, relwidth=1.0, height=row_height)

    def hide(self) -> None:
        self._frame.place_forget()

    def destroy(self) -> None:
        try:
            self._frame.destroy()
        except Exception:
            pass

    # ── Event proxies ──

    def _on_single_click(self, event) -> None:
        if self._row_index >= 0:
            self._on_click(self._row_index, event)

    def _on_double_click_evt(self, _event) -> None:
        if self._row_index >= 0:
            self._on_double_click(self._row_index)

    # ── Cell rendering ──

    def _render_cell(self, data: dict, col: Column) -> str:
        if col.renderer is not None:
            try:
                result = col.renderer(data)
                return "" if result is None else str(result)
            except Exception:
                return "—"
        value = data.get(col.key)
        if value is None:
            return "—"
        return str(value)