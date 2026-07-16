"""
core/pipeline.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Ingestion Pipeline (the choreographer)

Stitches the engine together for a single URL-in → indexed-track-out
workflow. The canonical stages:

    1. download       — yt-dlp to staging dir as .m4a
    2. analyze        — librosa BPM + Krumhansl-Schmuckler key
    3. artwork        — fetch YT thumbnail, 1:1 center-crop
    4. tag            — embed covr + all atoms via mutagen
    5. relocate       — move from staging into ~/Music/CrateDigger_Vault/
                         [YYYY-MM-DD]/[Artist]_[Title]/
    6. index          — upsert into vault.db
    7. stems (opt)    — demucs separation into the track's vault dir

Each stage reports granular progress via callback. The stage percentages
are weighted by their real wall-clock cost so the UI bar advances at a
rate that actually reflects time-to-completion:

    download   20%   — network-bound, typically fastest
    analyze    20%   — ~2-4s per track
    artwork     3%   — HTTP + Pillow, sub-second
    tag         2%   — mutagen write + fsync
    relocate    3%   — filesystem move
    index       2%   — single SQLite INSERT
    stems      50%   — demucs dominates when enabled (0% when disabled,
                        other stages redistribute proportionally)

This module owns vault-path construction. No other module decides
where a file lives on disk.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from core.analyzer import (
    AnalysisCancelledError,
    AnalysisResult,
    AudioAnalyzer,
)
from core.artwork import ArtworkError, ArtworkProcessor
from core.database import TrackRecord, VaultDatabase
from core.downloader import (
    DownloadCancelledError,
    Downloader,
    DownloadResult,
)
from core.metadata import MetadataWriter, TrackTags
from core.stems import (
    StemModel,
    StemSeparationCancelledError,
    StemSeparator,
    StemsResult,
)
from utils.paths import build_vault_track_dir, sanitize_filename_component

# Type alias for the optional AI enricher passed in from main.py.
# Callable[[youtube_title, uploader], (artist, title)] — empty strings mean "no result".
_AiEnricher = Callable[[str, str], tuple[str, str]]

# Matches channel/label brand names — both standalone words ("Universal Music")
# and compound forms ("GRMusic", "MusicFactory"). The suffix alternation
# catches the common "XYZMusic" / "XYZRecords" / "XYZTV" channel naming pattern.
_CHANNEL_NAME_HINT_RE = re.compile(
    r"\b(music|records?|tv|channel|official|vevo|sounds?|media|entertainment|"
    r"label|studio|network|group|digital|worldwide|global|nation)\b"
    r"|(?:music|records?|tv|media|entertainment|vevo|sounds?|label)$",
    re.IGNORECASE,
)

# Bracketed suffixes in video titles that are generally not part of the song title.
_TITLE_NOISE_SUFFIX_RE = re.compile(
    r"\s*(?:\(|\[|\{).{0,120}(?:official|video|audio|lyrics?|live|remix|"
    r"slowed|reverb|sped\s*up|nightcore|prod\.|clip|visualizer).{0,120}(?:\)|\]|\})\s*$",
    re.IGNORECASE,
)


# ─── Public types ────────────────────────────────────────────────────


class PipelineStage(str, Enum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    ANALYZING = "analyzing"
    FETCHING_ARTWORK = "fetching_artwork"
    TAGGING = "tagging"
    RELOCATING = "relocating"
    INDEXING = "indexing"
    SEPARATING_STEMS = "separating_stems"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class PipelineProgress:
    """One event per pipeline update. UI-ready."""

    stage: PipelineStage
    overall_percent: float  # 0..100 weighted by stage cost
    stage_percent: float  # 0..100 within current stage
    message: str = ""
    # Optional enrichment surfaced as it becomes known
    display_name: Optional[str] = None
    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    camelot_key: Optional[str] = None


@dataclass(slots=True, frozen=True)
class PipelineRequest:
    """Everything needed to run the pipeline for one URL."""

    source_url: str
    enable_stems: bool = False
    stem_model: StemModel = StemModel.HTDEMUCS
    # Per-download AI metadata flag. When True (and the pipeline was
    # constructed with an ai_enricher), DeepSeek is called to extract
    # the original artist/title from the YouTube video title.
    use_ai_metadata: bool = True
    # Optional metadata hints from upstream (e.g. Discogs Dig provides
    # genre/year that YouTube alone won't surface).
    hint_genre: Optional[str] = None
    hint_country: Optional[str] = None
    hint_year: Optional[int] = None
    hint_discogs_master_id: Optional[int] = None
    hint_discogs_release_id: Optional[int] = None
    source_platform_override: Optional[str] = None


@dataclass(slots=True, frozen=True)
class PipelineResult:
    """Final outcome of a successful run."""

    track_id: int
    final_audio_path: Path
    stems_path: Optional[Path]
    analysis: AnalysisResult
    download: DownloadResult
    total_elapsed_seconds: float


# ─── Exceptions ──────────────────────────────────────────────────────


class PipelineError(Exception):
    """Base class for pipeline failures."""


class PipelineCancelledError(PipelineError):
    """Caller cancelled mid-run via cancel_event."""


# ─── Stage weights ───────────────────────────────────────────────────

# Weights sum to 100 in each profile. Used for mapping per-stage percent
# into overall progress. When stems are disabled, the stems weight is
# redistributed proportionally across the remaining stages.

_WEIGHTS_NO_STEMS = {
    PipelineStage.DOWNLOADING: 40,
    PipelineStage.ANALYZING: 40,
    PipelineStage.FETCHING_ARTWORK: 6,
    PipelineStage.TAGGING: 4,
    PipelineStage.RELOCATING: 6,
    PipelineStage.INDEXING: 4,
}

_WEIGHTS_WITH_STEMS = {
    PipelineStage.DOWNLOADING: 20,
    PipelineStage.ANALYZING: 20,
    PipelineStage.FETCHING_ARTWORK: 3,
    PipelineStage.TAGGING: 2,
    PipelineStage.RELOCATING: 3,
    PipelineStage.INDEXING: 2,
    PipelineStage.SEPARATING_STEMS: 50,
}


# ─── The Pipeline ────────────────────────────────────────────────────


class IngestionPipeline:
    """
    Orchestrates the full engine for a single URL. Stateless across
    calls; safe to share across worker threads.
    """

    def __init__(
        self,
        *,
        downloader: Downloader,
        artwork: ArtworkProcessor,
        analyzer: AudioAnalyzer,
        metadata_writer: MetadataWriter,
        stem_separator: StemSeparator,
        database: VaultDatabase,
        vault_root: Path,
        staging_root: Path,
        ai_enricher: Optional[_AiEnricher] = None,
        folder_scheme: str = "date/artist_title",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._dl = downloader
        self._art = artwork
        self._analyzer = analyzer
        self._meta = metadata_writer
        self._stems = stem_separator
        self._db = database
        self._vault_root = Path(vault_root)
        self._staging_root = Path(staging_root)
        self._ai_enricher = ai_enricher
        self._folder_scheme = folder_scheme
        self._log = logger or logging.getLogger("cratedigger.pipeline")

        self._vault_root.mkdir(parents=True, exist_ok=True)
        self._staging_root.mkdir(parents=True, exist_ok=True)

    # ── Public API ──

    def update_ai_enricher(self, enricher: Optional[_AiEnricher]) -> None:
        """Called by Settings when the DeepSeek key is saved or cleared."""
        self._ai_enricher = enricher

    def update_folder_scheme(self, folder_scheme: str) -> None:
        """Apply a Settings folder-layout change to subsequent relocations."""
        self._folder_scheme = folder_scheme

    def run(
        self,
        request: PipelineRequest,
        progress_callback: Optional[Callable[[PipelineProgress], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> PipelineResult:
        """
        Run the full pipeline for one URL. On any failure, staging
        artifacts are cleaned up and a PipelineError is raised.
        """
        weights = _WEIGHTS_WITH_STEMS if request.enable_stems else _WEIGHTS_NO_STEMS
        progress = _ProgressTracker(weights, progress_callback)
        started = time.monotonic()

        # Per-run staging dir. Cleaned up on completion/failure.
        job_staging = self._staging_root / f"job_{int(time.time() * 1000)}"
        job_staging.mkdir(parents=True, exist_ok=True)

        try:
            self._check_cancel(cancel_event)

            # ── 1. DOWNLOAD ──
            progress.enter(PipelineStage.DOWNLOADING, "Downloading audio")
            download = self._run_download(
                request,
                job_staging,
                progress,
                cancel_event,
            )
            progress.display_name = (
                f"{(download.artist or download.uploader or 'Unknown')} — "
                f"{(download.track or download.title or 'Unknown')}"
            )
            progress.emit(100.0, f"Downloaded: {progress.display_name}")

            # ── 2. ANALYZE ──
            self._check_cancel(cancel_event)
            progress.enter(PipelineStage.ANALYZING, "Analyzing BPM + key")
            analysis = self._run_analysis(
                download.audio_path,
                progress,
                cancel_event,
            )
            progress.bpm = analysis.bpm
            progress.musical_key = analysis.musical_key
            progress.camelot_key = analysis.camelot_key
            progress.emit(
                100.0,
                f"BPM {analysis.bpm:.1f}  Key {analysis.musical_key} "
                f"({analysis.camelot_key})",
            )

            # ── 3. ARTWORK ──
            self._check_cancel(cancel_event)
            progress.enter(PipelineStage.FETCHING_ARTWORK, "Fetching artwork")
            artwork_bytes = self._run_artwork(download, progress)
            progress.emit(100.0, "Artwork ready")

            # ── 4. TAG ──
            self._check_cancel(cancel_event)
            progress.enter(PipelineStage.TAGGING, "Writing metadata")
            resolved_artist, resolved_title = self._resolve_names(
                download,
                request,
            )
            # Refresh display_name now that we have the resolved (possibly AI-
            # enriched) names — earlier we used the raw download fields.
            progress.display_name = f"{resolved_artist} — {resolved_title}"
            tags = self._build_tags(
                download,
                analysis,
                request,
                artwork_bytes,
                resolved_artist,
                resolved_title,
            )
            self._meta.apply(download.audio_path, tags)
            progress.emit(100.0, "Metadata embedded")

            # ── 5. RELOCATE ──
            self._check_cancel(cancel_event)
            progress.enter(PipelineStage.RELOCATING, "Filing track in vault")
            final_audio_path, track_dir = self._relocate_to_vault(
                src=download.audio_path,
                genre=request.hint_genre,
                bpm=analysis.bpm,
                camelot_key=analysis.camelot_key,
                artist=resolved_artist,
                title=resolved_title,
            )
            progress.emit(100.0, f"Filed: {final_audio_path.parent.name}")

            # ── 6. INDEX ──
            self._check_cancel(cancel_event)
            progress.enter(PipelineStage.INDEXING, "Indexing in vault database")
            track_id = self._index_track(
                final_audio_path,
                download,
                analysis,
                request,
                resolved_artist,
                resolved_title,
            )
            progress.emit(100.0, f"Indexed (id={track_id})")

            # ── 7. STEMS (optional) ──
            stems_dir: Optional[Path] = None
            if request.enable_stems:
                self._check_cancel(cancel_event)
                progress.enter(
                    PipelineStage.SEPARATING_STEMS,
                    f"Separating stems ({request.stem_model.value})",
                )
                stems_dir = track_dir / "stems"
                stems_dir.mkdir(parents=True, exist_ok=True)
                self._run_stems(
                    final_audio_path,
                    stems_dir,
                    request.stem_model,
                    progress,
                    cancel_event,
                )

                # Update index row with stems path + flag
                track_rec = self._db.get_track(track_id)
                track_rec.stems_separated = True
                track_rec.stems_path = str(stems_dir)
                self._db.upsert_track(track_rec)
                progress.emit(100.0, "Stems complete")

            # ── COMPLETE ──
            progress.complete(
                f"Ready: {progress.display_name}" if progress.display_name else "Ready"
            )

            elapsed = time.monotonic() - started
            self._log.info(
                "Pipeline complete for %s in %.1fs (track_id=%d, stems=%s)",
                request.source_url,
                elapsed,
                track_id,
                bool(request.enable_stems),
            )
            return PipelineResult(
                track_id=track_id,
                final_audio_path=final_audio_path,
                stems_path=stems_dir,
                analysis=analysis,
                download=download,
                total_elapsed_seconds=elapsed,
            )

        except PipelineCancelledError:
            progress.cancelled("Cancelled by user")
            self._log.info("Pipeline cancelled for %s", request.source_url)
            raise
        except PipelineError:
            progress.failed("Pipeline error")
            raise
        except Exception as e:
            progress.failed(f"Error: {e}")
            self._log.exception("Pipeline failed for %s", request.source_url)
            raise PipelineError(str(e)) from e
        finally:
            shutil.rmtree(job_staging, ignore_errors=True)

    # ── Stage implementations ──

    def _run_download(
        self,
        req: PipelineRequest,
        staging: Path,
        progress: "_ProgressTracker",
        cancel_event: Optional[threading.Event],
    ) -> DownloadResult:
        def on_dl_progress(p) -> None:
            progress.emit(p.percent, p.message)

        try:
            return self._dl.download(
                req.source_url,
                staging,
                progress_callback=on_dl_progress,
                cancel_event=cancel_event,
            )
        except DownloadCancelledError as e:
            raise PipelineCancelledError(str(e)) from e
        except Exception as e:
            raise PipelineError(f"Download failed: {e}") from e

    def _run_analysis(
        self,
        audio_path: Path,
        progress: "_ProgressTracker",
        cancel_event: Optional[threading.Event],
    ) -> AnalysisResult:
        def on_a_progress(p) -> None:
            progress.emit(p.percent, p.message)

        try:
            return self._analyzer.analyze(
                audio_path,
                progress_callback=on_a_progress,
                cancel_event=cancel_event,
            )
        except AnalysisCancelledError as e:
            raise PipelineCancelledError(str(e)) from e
        except Exception as e:
            raise PipelineError(f"Analysis failed: {e}") from e

    def _run_artwork(
        self,
        download: DownloadResult,
        progress: "_ProgressTracker",
    ) -> Optional[bytes]:
        """Artwork is best-effort — missing cover never fails the pipeline."""
        if not download.thumbnail_url:
            self._log.info("No thumbnail URL; skipping artwork.")
            progress.emit(100.0, "No artwork available")
            return None
        try:
            progress.emit(30.0, "Downloading thumbnail")
            result = self._art.fetch_and_process(download.thumbnail_url)
            progress.emit(90.0, "Artwork processed")
            return result.data
        except ArtworkError as e:
            self._log.warning("Artwork fetch failed (continuing): %s", e)
            progress.emit(100.0, "Artwork unavailable — continuing")
            return None

    def _run_stems(
        self,
        audio_path: Path,
        output_dir: Path,
        model: StemModel,
        progress: "_ProgressTracker",
        cancel_event: Optional[threading.Event],
    ) -> StemsResult:
        def on_s_progress(p) -> None:
            progress.emit(p.percent, p.message)

        try:
            return self._stems.separate(
                audio_path,
                output_dir,
                progress_callback=on_s_progress,
                cancel_event=cancel_event,
                model=model,
            )
        except StemSeparationCancelledError as e:
            raise PipelineCancelledError(str(e)) from e
        except Exception as e:
            # Stems are opt-in, but once the user opts in, a failure
            # here is a real error. We don't silently fall back.
            raise PipelineError(f"Stem separation failed: {e}") from e

    # ── Name resolution & tag construction ──

    def _resolve_names(
        self,
        download: DownloadResult,
        request: PipelineRequest,
    ) -> tuple[str, str]:
        """
        Resolve the best artist + title for this track.

        When AI is ON (use_ai_metadata=True and enricher present):
            AI is called first. If it returns a result, that wins.
            Falls through to the heuristic chain only if AI returns empty.

        When AI is OFF:
            1. YouTube Music structured fields (artist / track atoms).
            2. Deterministic "Artist - Title" parse from the video title.
            3. Uploader-based conservative fallback.
        """
        raw_title = download.title or ""
        uploader  = download.uploader or ""

        # ── AI path (explicit user preference — tried first) ──
        if self._ai_enricher is not None and request.use_ai_metadata:
            ai_artist, ai_title = self._ai_enricher(raw_title, uploader)
            if ai_artist or ai_title:
                artist = (ai_artist or download.artist or uploader or "Unknown Artist").strip()
                title  = (ai_title  or raw_title or "Unknown Title").strip()
                if artist.endswith(" - Topic"):
                    artist = artist[: -len(" - Topic")].strip()
                self._log.info(
                    "AI metadata: %r → artist=%r title=%r",
                    raw_title, artist, title,
                )
                return artist or "Unknown Artist", title or "Unknown Title"
            self._log.debug(
                "AI returned no result for %r; falling back to heuristic.",
                raw_title,
            )

        # ── Heuristic path ──

        # Structured fields (YouTube Music official uploads).
        if download.artist and download.track:
            artist = download.artist.strip()
            title  = download.track.strip()
            if artist.endswith(" - Topic"):
                artist = artist[: -len(" - Topic")].strip()
            return artist or "Unknown Artist", title or "Unknown Title"

        # Deterministic "Artist - Title" parse from the raw video title.
        parsed = self._parse_artist_title_from_video_title(raw_title)
        if parsed is not None:
            artist, title = parsed
            return artist or "Unknown Artist", title or "Unknown Title"

        # Conservative uploader fallback — avoid channel-brand names.
        title  = self._clean_video_title(download.track or raw_title or "Unknown Title")
        artist = (download.artist or "").strip()
        if not artist:
            if uploader and not _CHANNEL_NAME_HINT_RE.search(uploader):
                artist = uploader
        if not artist:
            artist = "Unknown Artist"
        if artist.endswith(" - Topic"):
            artist = artist[: -len(" - Topic")].strip()
        return artist or "Unknown Artist", title or "Unknown Title"

    def _parse_artist_title_from_video_title(
        self, raw_title: str
    ) -> Optional[tuple[str, str]]:
        """
        Parse common title forms like "Artist - Title" or "Artist: Title".
        Returns None when ambiguous.
        """
        title = self._clean_video_title(raw_title)
        if not title:
            return None

        for sep in (" - ", " – ", " — ", ": ", " | "):
            if sep not in title:
                continue
            left, right = title.split(sep, 1)
            left = left.strip(" -–—:|\t")
            right = right.strip(" -–—:|\t")
            # Clean any pipe-brand residue from the title half.
            pipe_m = re.search(r"\s*\|\s*(.+)$", right)
            if pipe_m and _CHANNEL_NAME_HINT_RE.search(pipe_m.group(1)):
                right = right[: pipe_m.start()].strip()
            if not left or not right:
                continue
            # Guard against parsing noisy prefixes like "Official Video - ...".
            if _CHANNEL_NAME_HINT_RE.search(left) and len(left) <= 20:
                continue
            if len(left) > 90 or len(right) > 160:
                continue
            return left, right
        return None

    def _clean_video_title(self, title: str) -> str:
        """Strip common upload noise while preserving the core song title."""
        cleaned = (title or "").strip()
        if not cleaned:
            return ""

        # Remove repeated bracketed suffixes from the end.
        for _ in range(3):
            next_cleaned = _TITLE_NOISE_SUFFIX_RE.sub("", cleaned).strip()
            if next_cleaned == cleaned:
                break
            cleaned = next_cleaned

        # Strip pipe-separated channel branding only when what follows the
        # pipe looks like a label/channel name. Checked against the same
        # brand-hint regex used for uploader filtering. This avoids nuking
        # "Artist | Title" formats while catching "Song - Title | GRMusic".
        pipe_match = re.search(r"\s*\|\s*(.+)$", cleaned)
        if pipe_match and _CHANNEL_NAME_HINT_RE.search(pipe_match.group(1)):
            cleaned = cleaned[: pipe_match.start()].strip()

        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip(" -–—|\t")

    def _build_tags(
        self,
        download: DownloadResult,
        analysis: AnalysisResult,
        request: PipelineRequest,
        artwork: Optional[bytes],
        resolved_artist: str,
        resolved_title: str,
    ) -> TrackTags:
        return TrackTags(
            title=resolved_title,
            artist=resolved_artist,
            album=download.album,
            album_artist=resolved_artist,
            genre=request.hint_genre,
            year=request.hint_year,
            comment="Ingested via Crate Digger",
            bpm=analysis.bpm,
            musical_key=analysis.musical_key,
            camelot_key=analysis.camelot_key,
            artwork_jpeg=artwork,
            source_url=download.source_url,
        )

    # ── Filesystem relocation ──

    def _relocate_to_vault(
        self,
        *,
        src: Path,
        genre: Optional[str],
        bpm: Optional[float],
        camelot_key: Optional[str],
        artist: str,
        title: str,
    ) -> tuple[Path, Path]:
        """
        Move `src` into the vault using the configured folder scheme.
        and return (final_audio_path, track_dir).

        Every path component is sanitized via utils.paths.
        Collisions are resolved with a numeric suffix.
        """
        track_dir = build_vault_track_dir(
            self._vault_root,
            genre=genre,
            bpm=bpm,
            camelot_key=camelot_key,
            artist=artist,
            title=title,
            scheme=self._folder_scheme,
        )

        # Collision avoidance at the directory level
        base_dir = track_dir
        n = 2
        while track_dir.exists() and any(track_dir.iterdir()):
            track_dir = base_dir.with_name(f"{base_dir.name} ({n})")
            n += 1
            if n > 999:
                raise PipelineError(f"Too many collisions at {base_dir.parent}")

        track_dir.mkdir(parents=True, exist_ok=True)

        # Final filename mirrors the track_dir name for easy browsing
        filename = sanitize_filename_component(
            f"{artist} - {title}",
            max_length=180,
        )
        dest = track_dir / f"{filename}.m4a"

        # Cross-filesystem move — shutil.move handles the copy+delete
        # fallback when staging and vault are on different drives.
        try:
            shutil.move(str(src), str(dest))
        except OSError as e:
            # Clean up empty track_dir on failure
            try:
                track_dir.rmdir()
            except OSError:
                pass
            raise PipelineError(f"Could not move to vault: {e}") from e

        return dest, track_dir

    # ── DB indexing ──

    def _index_track(
        self,
        final_path: Path,
        download: DownloadResult,
        analysis: AnalysisResult,
        request: PipelineRequest,
        resolved_artist: str,
        resolved_title: str,
    ) -> int:
        file_size = final_path.stat().st_size
        platform = (
            request.source_platform_override or download.source_platform or "manual"
        )
        if request.hint_discogs_master_id:
            platform = "discogs_dig"

        record = TrackRecord(
            file_path=str(final_path),
            file_size_bytes=file_size,
            artist=resolved_artist,
            title=resolved_title,
            album=download.album,
            genre=request.hint_genre,
            country=request.hint_country,
            year=request.hint_year,
            duration_seconds=analysis.duration_seconds,
            bpm=analysis.bpm,
            bpm_confidence=analysis.bpm_confidence,
            musical_key=analysis.musical_key,
            camelot_key=analysis.camelot_key,
            key_confidence=analysis.key_confidence,
            artwork_embedded=(download.thumbnail_url is not None),
            source_url=download.source_url,
            source_platform=platform,
            discogs_master_id=request.hint_discogs_master_id,
            discogs_release_id=request.hint_discogs_release_id,
        )
        track_id = self._db.upsert_track(record)

        # Link discovery_history row back to the track, if applicable
        if request.hint_discogs_master_id:
            self._db.mark_discovery_queued(
                request.hint_discogs_master_id,
                track_id,
            )
        return track_id

    # ── Cancel plumbing ──

    @staticmethod
    def _check_cancel(event: Optional[threading.Event]) -> None:
        if event is not None and event.is_set():
            raise PipelineCancelledError("Cancelled by user.")


# ─── Weighted-progress tracker ───────────────────────────────────────


@dataclass(slots=True)
class _ProgressTracker:
    """
    Converts per-stage 0..100 updates into an overall 0..100 by stage weight.
    Also carries enrichment (display_name, bpm, key) so every progress event
    emitted to the UI includes the freshest info.
    """

    weights: dict[PipelineStage, int]
    callback: Optional[Callable[[PipelineProgress], None]]
    current_stage: PipelineStage = PipelineStage.PENDING
    completed_weight: int = 0

    # Enrichment surfaced as it becomes known
    display_name: Optional[str] = None
    bpm: Optional[float] = None
    musical_key: Optional[str] = None
    camelot_key: Optional[str] = None

    def enter(self, stage: PipelineStage, message: str) -> None:
        # When leaving a stage, bank its weight as completed.
        if self.current_stage in self.weights:
            self.completed_weight += self.weights[self.current_stage]
        self.current_stage = stage
        self._send(stage_percent=0.0, message=message)

    def emit(self, stage_percent: float, message: str) -> None:
        self._send(stage_percent=stage_percent, message=message)

    def complete(self, message: str) -> None:
        # Bank the final stage's weight
        if self.current_stage in self.weights:
            self.completed_weight += self.weights[self.current_stage]
        self.current_stage = PipelineStage.COMPLETE
        self._send(stage_percent=100.0, message=message, force_overall=100.0)

    def failed(self, message: str) -> None:
        self.current_stage = PipelineStage.FAILED
        self._send(stage_percent=0.0, message=message)

    def cancelled(self, message: str) -> None:
        self.current_stage = PipelineStage.CANCELLED
        self._send(stage_percent=0.0, message=message)

    def _send(
        self,
        stage_percent: float,
        message: str,
        force_overall: Optional[float] = None,
    ) -> None:
        if self.callback is None:
            return

        if force_overall is not None:
            overall = force_overall
        else:
            stage_weight = self.weights.get(self.current_stage, 0)
            overall = self.completed_weight + (stage_percent / 100.0) * stage_weight
            overall = max(0.0, min(99.9, overall))

        self.callback(
            PipelineProgress(
                stage=self.current_stage,
                overall_percent=overall,
                stage_percent=max(0.0, min(100.0, stage_percent)),
                message=message,
                display_name=self.display_name,
                bpm=self.bpm,
                musical_key=self.musical_key,
                camelot_key=self.camelot_key,
            )
        )
