"""
ui/components/waveform_player.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Waveform Preview Player

A compact, self-contained audio preview widget: a drawn waveform with a
click/drag scrubber, an animated playhead, play/pause, and volume.

Playback runs through a `sounddevice.OutputStream` fed from an in-memory
float32 PCM buffer (see core.preview.PreviewData) with a movable read
pointer, giving frame-accurate seeking and instant pause/resume. If
sounddevice / PortAudio is unavailable, the widget still draws the
waveform and simply disables the transport (graceful degradation).

Lifecycle:
    player = WaveformPlayer(parent, theme)
    player.set_loading("Fetching…")     # while a worker decodes
    player.set_preview(preview_data)     # attach decoded PCM + peaks
    player.set_error("…")                # on failure

Only one player sounds at a time: starting playback stops any other
WaveformPlayer that's currently playing (class-level registry).
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import TYPE_CHECKING, Callable, Optional

import customtkinter as ctk
import numpy as np
import tkinter as tk

if TYPE_CHECKING:
    from core.preview import PreviewData
    from ui.theme import Theme

_log = logging.getLogger("cratedigger.ui.waveform_player")


class WaveformPlayer(ctk.CTkFrame):
    """Waveform + transport for previewing a single track in-app."""

    _TICK_MS = 33  # ~30fps playhead animation

    # Registry of live players so we can enforce single-playback.
    _live_players: "weakref.WeakSet[WaveformPlayer]" = weakref.WeakSet()

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        theme: "Theme",
        *,
        height: int = 96,
        initial_volume: float = 0.85,
    ) -> None:
        super().__init__(parent, fg_color="transparent")

        self._theme = theme
        self._wave_height = int(height)
        self._volume = float(max(0.0, min(1.0, initial_volume)))

        # Playback state
        self._data: Optional["PreviewData"] = None
        self._stream = None  # sounddevice.OutputStream
        self._read_pos = 0
        self._pos_lock = threading.Lock()
        self._playing = False
        self._finished = False
        self._tick_handle: Optional[str] = None
        self._destroyed = False
        self._on_load_full: Optional[Callable[[], None]] = None
        self._full_load_btn: Optional[ctk.CTkButton] = None

        # Cached draw geometry
        self._peaks: Optional[np.ndarray] = None
        self._canvas_w = 1
        self._canvas_h = self._wave_height

        self._build_body()
        WaveformPlayer._live_players.add(self)

        self.bind("<Destroy>", self._on_destroy, add="+")

    # ── Body ──

    def _build_body(self) -> None:
        t = self._theme
        self.grid_columnconfigure(1, weight=1)

        # Play / pause button
        self._play_btn = ctk.CTkButton(
            self,
            text="▶",
            width=40,
            height=40,
            command=self._toggle_play,
            fg_color=t.accent.blue,
            hover_color=t.accent.blue_bright,
            text_color=t.text.on_accent,
            corner_radius=t.radius.md,
            font=(t.font.family_ui, 15, "bold"),
            state="disabled",
        )
        self._play_btn.grid(row=0, column=0, rowspan=2, padx=(0, t.space.md),
                            pady=0, sticky="ns")

        # Waveform canvas
        self._canvas = tk.Canvas(
            self,
            height=self._wave_height,
            highlightthickness=0,
            bd=0,
            bg=self._resolve_bg(),
        )
        self._canvas.grid(row=0, column=1, sticky="ew")
        self._canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self._canvas.bind("<Button-1>", self._on_seek_click, add="+")
        self._canvas.bind("<B1-Motion>", self._on_seek_click, add="+")

        # Status / time line under the waveform
        info_row = ctk.CTkFrame(self, fg_color="transparent")
        info_row.grid(row=1, column=1, sticky="ew", pady=(t.space.xxs, 0))
        info_row.grid_columnconfigure(0, weight=1)

        self._time_label = ctk.CTkLabel(
            info_row,
            text="",
            text_color=t.text.muted,
            font=t.font.mono_body,
            anchor="w",
        )
        self._time_label.grid(row=0, column=0, sticky="w")

        self._full_load_btn = ctk.CTkButton(
            info_row,
            text="Load full track",
            width=110,
            height=24,
            command=self._on_load_full_clicked,
            fg_color=t.surface.elevated,
            hover_color=t.surface.raised,
            text_color=t.accent.blue,
            border_width=1,
            border_color=t.border.strong,
            corner_radius=t.radius.sm,
            font=t.font.micro,
        )
        # Hidden until a partial preview is attached.

        # Volume slider
        self._volume_slider = ctk.CTkSlider(
            info_row,
            from_=0.0,
            to=1.0,
            number_of_steps=20,
            width=90,
            command=self._on_volume_changed,
            button_color=t.accent.blue,
            button_hover_color=t.accent.blue_bright,
            progress_color=t.accent.blue,
            fg_color=t.surface.elevated,
            height=12,
        )
        self._volume_slider.set(self._volume)
        self._volume_slider.grid(row=0, column=1, sticky="e", padx=(t.space.sm, 0))

        self._draw_placeholder("")

    # ── Public API ──

    def set_loading(self, message: str = "Loading preview…") -> None:
        self._teardown_stream()
        self._data = None
        self._peaks = None
        self._on_load_full = None
        self._playing = False
        self._finished = False
        self._play_btn.configure(state="disabled", text="▶")
        self._time_label.configure(text=message, text_color=self._theme.text.muted)
        self._hide_full_load_button()
        self._draw_placeholder(message, animated=True)

    def set_error(self, message: str) -> None:
        self._teardown_stream()
        self._data = None
        self._peaks = None
        self._on_load_full = None
        self._play_btn.configure(state="disabled", text="▶")
        self._time_label.configure(
            text=message, text_color=self._theme.status.error
        )
        self._hide_full_load_button()
        self._draw_placeholder(message)

    def set_preview(
        self,
        data: "PreviewData",
        *,
        on_load_full: Optional[Callable[[], None]] = None,
    ) -> None:
        """Attach decoded PCM + peaks and enable the transport."""
        if self._destroyed:
            return
        self._teardown_stream()
        self._data = data
        self._peaks = data.peaks
        self._on_load_full = on_load_full if data.is_partial else None
        with self._pos_lock:
            self._read_pos = 0
        self._playing = False
        self._finished = False
        self._play_btn.configure(state="normal", text="▶")
        if data.is_partial and on_load_full is not None:
            self._show_full_load_button()
        else:
            self._hide_full_load_button()
        self._update_time_label()
        self._redraw_waveform()

    def clear(self) -> None:
        self._teardown_stream()
        self._data = None
        self._peaks = None
        self._on_load_full = None
        self._playing = False
        self._play_btn.configure(state="disabled", text="▶")
        self._time_label.configure(text="")
        self._hide_full_load_button()
        self._draw_placeholder("")

    def set_full_loading(self, loading: bool) -> None:
        """Show loading state on the 'Load full track' control only."""
        if self._full_load_btn is None:
            return
        if loading:
            self._full_load_btn.configure(state="disabled", text="Loading…")
            self._time_label.configure(
                text="Loading full track…",
                text_color=self._theme.text.muted,
            )
        else:
            self._full_load_btn.configure(state="normal", text="Load full track")
            self._update_time_label()

    def stop(self) -> None:
        """Stop playback (used when another player starts or on hide)."""
        if self._playing:
            self._pause()

    def set_volume(self, volume: float) -> None:
        self._volume = float(max(0.0, min(1.0, volume)))
        try:
            self._volume_slider.set(self._volume)
        except Exception:
            pass

    @property
    def volume(self) -> float:
        return self._volume

    # ── Transport ──

    def _toggle_play(self) -> None:
        if self._data is None:
            return
        if self._playing:
            self._pause()
        else:
            self._play()

    def _play(self) -> None:
        if self._data is None:
            return
        # If we're at the end, restart from the top.
        with self._pos_lock:
            if self._read_pos >= self._data.frame_count:
                self._read_pos = 0
        self._finished = False

        # Enforce single-playback across all players.
        for other in list(WaveformPlayer._live_players):
            if other is not self:
                try:
                    other.stop()
                except Exception:
                    pass

        if self._stream is None:
            if not self._open_stream():
                # Playback unavailable — leave the waveform usable.
                self._time_label.configure(
                    text="Audio output unavailable on this system.",
                    text_color=self._theme.status.warning,
                )
                return
        try:
            self._stream.start()
        except Exception:
            self._time_label.configure(
                text="Could not start audio output.",
                text_color=self._theme.status.warning,
            )
            return

        self._playing = True
        self._play_btn.configure(text="⏸")
        self._schedule_tick()

    def _pause(self) -> None:
        self._playing = False
        self._play_btn.configure(text="▶")
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
        self._cancel_tick()
        self._update_playhead()

    def _open_stream(self) -> bool:
        try:
            import sounddevice as sd
        except Exception:
            _log.exception("sounddevice import failed")
            return False
        if self._data is None:
            return False
        try:
            self._stream = sd.OutputStream(
                samplerate=self._data.samplerate,
                channels=self._data.channels,
                dtype="float32",
                callback=self._audio_callback,
                finished_callback=None,
            )
            return True
        except Exception:
            _log.exception(
                "Could not open OutputStream (samplerate=%s, channels=%s)",
                self._data.samplerate, self._data.channels,
            )
            self._stream = None
            return False

    def _audio_callback(self, outdata, frames, time_info, status) -> None:
        """Runs on the PortAudio thread. Feed frames from the buffer."""
        data = self._data
        if data is None:
            outdata.fill(0)
            return
        with self._pos_lock:
            start = self._read_pos
            end = min(start + frames, data.frame_count)
            chunk = data.samples[start:end]
            self._read_pos = end
        n = chunk.shape[0]
        if n > 0:
            outdata[:n] = chunk * self._volume
        if n < frames:
            outdata[n:] = 0
            # Reached the end — mark finished; the tick loop stops the
            # stream and resets the UI. We keep outputting silence for the
            # ~33ms until then rather than raising from the audio thread.
            self._finished = True

    # ── Animation / playhead ──

    def _schedule_tick(self) -> None:
        self._cancel_tick()
        self._tick_handle = self.after(self._TICK_MS, self._tick)

    def _cancel_tick(self) -> None:
        if self._tick_handle is not None:
            try:
                self.after_cancel(self._tick_handle)
            except Exception:
                pass
            self._tick_handle = None

    def _tick(self) -> None:
        if self._destroyed:
            return
        if self._finished:
            self._on_playback_finished()
            return
        self._update_playhead()
        self._update_time_label()
        if self._playing:
            self._schedule_tick()

    def _on_playback_finished(self) -> None:
        self._playing = False
        self._finished = False
        self._play_btn.configure(text="▶")
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
        with self._pos_lock:
            self._read_pos = 0
        self._cancel_tick()
        self._redraw_waveform()
        self._update_time_label()

    # ── Drawing ──

    def _on_canvas_configure(self, event) -> None:
        self._canvas_w = max(1, event.width)
        self._canvas_h = max(1, event.height)
        if self._peaks is not None:
            self._redraw_waveform()
        else:
            self._draw_placeholder(self._current_placeholder_text())

    def _current_placeholder_text(self) -> str:
        try:
            return self._time_label.cget("text")
        except Exception:
            return ""

    def _progress_fraction(self) -> float:
        if self._data is None or self._data.frame_count <= 0:
            return 0.0
        with self._pos_lock:
            pos = self._read_pos
        return max(0.0, min(1.0, pos / self._data.frame_count))

    def _redraw_waveform(self) -> None:
        c = self._canvas
        if self._destroyed:
            return
        c.delete("all")
        peaks = self._peaks
        if peaks is None or peaks.size == 0:
            return
        t = self._theme
        w = self._canvas_w
        h = self._canvas_h
        mid = h / 2.0
        played_frac = self._progress_fraction()
        played_x = played_frac * w

        # One bar per 2px keeps redraw cheap and the wave crisp.
        step = 2
        n_peaks = peaks.size
        for x in range(0, w, step):
            idx = int((x / w) * n_peaks)
            if idx >= n_peaks:
                idx = n_peaks - 1
            amp = float(peaks[idx])
            bar_h = max(1.0, amp * (mid - 2))
            color = t.accent.blue if x <= played_x else t.border.strong
            c.create_line(x, mid - bar_h, x, mid + bar_h, fill=color, width=1)

        # Playhead
        if self._data is not None:
            c.create_line(
                played_x, 0, played_x, h,
                fill=t.accent.blue_bright, width=1,
            )

    def _update_playhead(self) -> None:
        # A full redraw is cheap enough at ~300-500 bars; keeps played /
        # unplayed coloring correct as the head moves.
        self._redraw_waveform()

    def _draw_placeholder(self, message: str, *, animated: bool = False) -> None:
        c = self._canvas
        if self._destroyed:
            return
        c.delete("all")
        t = self._theme
        w = self._canvas_w
        h = self._canvas_h
        mid = h / 2.0
        # A faint flat baseline suggests "waveform goes here".
        c.create_line(0, mid, w, mid, fill=t.border.subtle, width=1)

    def _update_time_label(self) -> None:
        if self._data is None:
            return
        total = self._data.duration_seconds
        cur = self._progress_fraction() * total
        text = f"{self._fmt_time(cur)} / {self._fmt_time(total)}"
        if self._data.is_partial:
            text += " · quick"
            if self._data.full_duration_seconds:
                text += f" ({self._fmt_time(self._data.full_duration_seconds)} total)"
        self._time_label.configure(
            text=text,
            text_color=self._theme.text.secondary,
        )

    def _on_load_full_clicked(self) -> None:
        if self._on_load_full is None:
            return
        self._full_load_btn.configure(state="disabled", text="Loading…")
        try:
            self._on_load_full()
        except Exception:
            self._full_load_btn.configure(state="normal", text="Load full track")

    def _show_full_load_button(self) -> None:
        if self._full_load_btn is None:
            return
        t = self._theme
        self._full_load_btn.configure(state="normal", text="Load full track")
        self._full_load_btn.grid(
            row=0, column=1, sticky="e", padx=(t.space.sm, t.space.sm),
        )
        self._volume_slider.grid(row=0, column=2, sticky="e")

    def _hide_full_load_button(self) -> None:
        if self._full_load_btn is None:
            return
        self._full_load_btn.grid_forget()
        self._volume_slider.grid(row=0, column=1, sticky="e")

    # ── Seeking / volume ──

    def _on_seek_click(self, event) -> None:
        if self._data is None or self._canvas_w <= 0:
            return
        frac = max(0.0, min(1.0, event.x / self._canvas_w))
        with self._pos_lock:
            self._read_pos = int(frac * self._data.frame_count)
        self._finished = False
        self._redraw_waveform()
        self._update_time_label()

    def _on_volume_changed(self, value: float) -> None:
        self._volume = float(max(0.0, min(1.0, value)))

    # ── Teardown ──

    def _teardown_stream(self) -> None:
        self._cancel_tick()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _on_destroy(self, event) -> None:
        # Only react to this widget's own destruction, not children's.
        if event.widget is not self:
            return
        self._destroyed = True
        self._teardown_stream()
        try:
            WaveformPlayer._live_players.discard(self)
        except Exception:
            pass

    # ── Helpers ──

    def _resolve_bg(self) -> str:
        parent = self.master
        try:
            fg = parent.cget("fg_color")
            if isinstance(fg, (list, tuple)):
                return fg[1] if len(fg) > 1 else fg[0]
            if isinstance(fg, str) and fg.startswith("#"):
                return fg
        except Exception:
            pass
        return self._theme.surface.raised

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}:{seconds % 60:02d}"
