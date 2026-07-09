"""
core/discovery.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Gem Discovery (Discogs + YouTube Music matcher)

The "Dig" button workflow:
    1. User picks Era (decade range), Country, Genre/Style in the UI.
    2. Query Discogs for master releases matching the filters.
    3. Rank by community stats (want/have ratio) BLENDED with a
       sample-friendliness affinity (genre/style/era/country) so the
       roulette tilts toward boom-bap/lo-fi gems — while still able to
       surface anything (weight, never exclude).
    4. Query ytmusicapi for the exact audio match, preferring
       official album versions over live/remixes.
    5. Return a reel of enriched suggestions ready to preview + queue.

`dig_many()` returns a batch for the reel UI and does NOT record the
suggestions — the caller records via `record_suggestion()` only once the
user previews or queues one. Masters shown on a reel are tracked for the
app session so the next Dig explores a wider slice of the catalog.
`dig()` remains for single-shot callers and records immediately.

Rate limiting (critical):
  • Discogs free tier: 60 requests/min authenticated, 25/min anonymous.
  • ytmusicapi: no official quota but excessive requests get IPs
    soft-banned for a few minutes.
  • We enforce our own token-bucket limiter (55 req/min for Discogs,
    45 req/min for YTM) so the app is always well under Discogs's
    hard limit even if the user spams the Dig button.
  • On 429 response or explicit Retry-After, we respect the header
    and block the limiter for the returned duration rather than
    raising.
  • All API calls are wrapped in exponential backoff (3 retries,
    1s → 2s → 4s) for transient network errors.

Zero UI awareness. Returns a typed DiscoverySuggestion; the UI tab
and pipeline decide what to do with it.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Optional

import requests

from core.database import DiscoveryRecord, VaultDatabase
from core.sampling_taxonomy import (
    blended_score,
    pick_wide_open_discogs_seed,
    sample_affinity,
)


# ─── Public types ────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class DiscoveryFilters:
    """User-selected filters from the Digital Crate tab."""
    year: Optional[int] = None             # Exact year (overrides year_min/max)
    year_min: Optional[int] = None         # Era range lower bound (inclusive)
    year_max: Optional[int] = None         # Era range upper bound (inclusive)
    country: Optional[str] = None          # Discogs country string
    genre: Optional[str] = None            # Discogs top-level genre
    style: Optional[str] = None            # Discogs style
    format: Optional[str] = None           # Discogs format (Vinyl, CD, etc.)
    query: Optional[str] = None            # Free-text keyword search
    min_have: int = 10                     # min community `have` count
    max_have: int = 3000                   # max `have` count — excludes
                                            # mainstream/overly common records

    # Sample-friendliness weighting (see core.sampling_taxonomy).
    prioritize_samples: bool = True        # tilt ranking toward sample-friendly
    sample_intensity: float = 0.6          # 0=pure desirability, 1=affinity-led
    allow_compilations: bool = False       # include "Various Artists" masters


@dataclass(slots=True, frozen=True)
class DiscogsCandidate:
    """A raw Discogs master release candidate pre-YT-match."""
    master_id: int
    release_id: Optional[int]
    artist: str
    title: str
    year: Optional[int]
    country: Optional[str]
    genres: tuple[str, ...]
    styles: tuple[str, ...]
    have: int
    want: int

    @property
    def desirability(self) -> float:
        """Want-to-have ratio, a common proxy for 'underrated gem' status."""
        return (self.want / self.have) if self.have > 0 else 0.0

    @property
    def sample_affinity(self) -> float:
        """Sample-friendliness multiplier from the taxonomy (>0 always)."""
        return sample_affinity(
            genres=self.genres,
            styles=self.styles,
            country=self.country,
            year=self.year,
        )

    def rank_score(self, *, prioritize: bool, intensity: float) -> float:
        """Final ranking score: desirability blended with sample affinity."""
        if not prioritize:
            return self.desirability
        return blended_score(self.desirability, self.sample_affinity, intensity)


@dataclass(slots=True, frozen=True)
class DiscoverySuggestion:
    """Final, UI-ready suggestion with YT match resolved."""
    # Discogs-sourced
    discogs_master_id: int
    discogs_release_id: Optional[int]
    artist: str
    title: str
    year: Optional[int]
    country: Optional[str]
    genre: Optional[str]
    style: Optional[str]

    # YouTube-sourced
    youtube_url: str
    youtube_video_id: str
    youtube_title: str                      # raw YT title for debugging
    youtube_duration_seconds: Optional[int]
    match_score: float                      # 0..1, quality of YT match

    @property
    def display_name(self) -> str:
        return f"{self.artist} — {self.title}"


@dataclass(slots=True)
class _CallStats:
    """Instrumentation for the UI's discovery-health indicator."""
    discogs_requests: int = 0
    discogs_rate_waits: float = 0.0
    ytm_requests: int = 0
    ytm_rate_waits: float = 0.0
    throttle_events: int = 0
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=10))


# ─── Public exceptions ───────────────────────────────────────────────

class DiscoveryError(Exception):
    """Base class for discovery failures."""


class DiscoveryConfigError(DiscoveryError):
    """Missing or invalid API credentials."""


class DiscoveryThrottledError(DiscoveryError):
    """API rate-limited us even after respecting our own limiter."""


class NoResultsError(DiscoveryError):
    """No matching Discogs masters after filtering and dedup."""


class NoYouTubeMatchError(DiscoveryError):
    """Discogs candidate found but no suitable YouTube match."""


class DiscoveryCancelledError(DiscoveryError):
    """User cancelled an in-flight dig."""


# ─── Rate limiter ────────────────────────────────────────────────────

class _TokenBucket:
    """
    Thread-safe sliding-window rate limiter. Enforces `max_calls` per
    `window_seconds`. `acquire()` blocks until a slot is available.

    Additionally supports a `pause_until` mechanism — when the API
    returns 429 Retry-After, we set a global pause that overrides the
    token window for all callers until it expires.
    """

    def __init__(self, max_calls: int, window_seconds: float) -> None:
        self._max = max_calls
        self._window = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._pause_until: float = 0.0
        self._cond = threading.Condition(self._lock)

    def acquire(self) -> float:
        """Block until a slot is free. Returns seconds spent waiting."""
        waited_total = 0.0
        while True:
            with self._cond:
                now = time.monotonic()

                # Honor any server-imposed pause first.
                if now < self._pause_until:
                    delay = self._pause_until - now
                    self._cond.wait(timeout=delay)
                    waited_total += delay
                    continue

                # Prune calls older than the window.
                cutoff = now - self._window
                while self._calls and self._calls[0] < cutoff:
                    self._calls.popleft()

                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return waited_total

                # At capacity — sleep until the oldest call ages out.
                delay = self._calls[0] + self._window - now
                self._cond.wait(timeout=max(delay, 0.01))
                waited_total += delay

    def pause(self, seconds: float) -> None:
        """Force all callers to wait at least `seconds` before next acquire."""
        with self._cond:
            self._pause_until = max(
                self._pause_until, time.monotonic() + max(seconds, 0.0),
            )
            self._cond.notify_all()


# ─── Discovery engine ───────────────────────────────────────────────

class DiscoveryEngine:
    """
    High-level "Dig" facade. One instance per app; threadsafe across
    workers. Dependencies (DB, logger, HTTP session) are injected.
    """

    DISCOGS_BASE = "https://api.discogs.com"
    USER_AGENT = "CrateDigger/0.1 +https://github.com/josh/cratedigger"

    # Max pages of Discogs search results to fetch per Dig. Discogs
    # returns 50 per page. Narrow-filter digs (country, era range, etc.)
    # fetch more pages in random order until TARGET_POOL_SIZE is met.
    MAX_SEARCH_PAGES = 8
    MAX_SEARCH_PAGES_NARROW = 18

    # Stop paging once we have this many post-filter candidates.
    TARGET_POOL_SIZE = 120

    # Relaxed min_have ceiling when filters already narrow the catalog.
    FILTERED_MIN_HAVE_CAP = 15
    FILTERED_MIN_HAVE_FLOOR = 5

    # Masters surfaced on recent reels (preview not required) — excluded
    # from the next dig so repeated clicks explore outward.
    SESSION_SURFACED_MAX = 250

    # Discogs sort keys that reshuffle which masters appear in each window.
    _DISCOGS_SORT_FIELDS: tuple[str, ...] = (
        "want", "have", "year", "title", "label", "catno", "released",
    )

    # How many Discogs candidates to try when the first YT match fails.
    MAX_YT_MATCH_ATTEMPTS = 10

    def __init__(
        self,
        db: VaultDatabase,
        discogs_token: Optional[str],
        logger: Optional[logging.Logger] = None,
        *,
        session: Optional[requests.Session] = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._db = db
        self._token = (discogs_token or "").strip() or None
        self._log = logger or logging.getLogger("cratedigger.discovery")
        self._rng = rng or random.Random()

        self._session = session or self._build_session()

        # Token is optional at construction — we only enforce presence
        # on the first Dig call, so the app can boot cleanly even before
        # the user has entered their token in Settings.

        # Authenticated: 60/min; anonymous: 25/min. Stay well under both.
        self._discogs_limiter = _TokenBucket(
            max_calls=55 if self._token else 20,
            window_seconds=60.0,
        )
        # YTM has no published quota; 45/min is conservative and
        # well below what triggers IP throttling in practice.
        self._ytm_limiter = _TokenBucket(max_calls=45, window_seconds=60.0)

        self._stats = _CallStats()

        # Recently surfaced master IDs (current app session). LRU so memory
        # stays bounded; cleared only when the engine is recreated.
        self._session_surfaced: OrderedDict[int, None] = OrderedDict()

        # ytmusicapi.YTMusic is thread-safe in practice for search/get
        # calls; we hold one client for the app lifetime.
        self._ytm_client: Any = None
        self._ytm_lock = threading.Lock()

    # ── Public API ──

    def update_discogs_token(self, token: Optional[str]) -> None:
        """Called by Settings UI when the user pastes a new token."""
        self._token = (token or "").strip() or None
        # Rescale limiter to the new auth tier
        self._discogs_limiter = _TokenBucket(
            max_calls=55 if self._token else 20,
            window_seconds=60.0,
        )

    def get_stats(self) -> _CallStats:
        """Return a snapshot of call-stats for the health-indicator UI."""
        return self._stats

    def dig(self, filters: DiscoveryFilters) -> DiscoverySuggestion:
        """
        Single-shot entry point. Returns one fully-resolved suggestion
        and records it immediately (legacy behavior used by scripts and
        any caller that wants one gem per click).

        Raises:
            DiscoveryConfigError     — missing token
            NoResultsError           — no Discogs masters match
            NoYouTubeMatchError      — Discogs matches found but none resolvable
            DiscoveryThrottledError  — rate limited beyond our ability to wait
        """
        results = self.dig_many(filters, count=1)
        if not results:
            raise NoYouTubeMatchError(
                "No Discogs candidate resolved on YouTube Music."
            )
        suggestion = results[0]
        # Legacy contract: record on surface.
        self.record_suggestion(suggestion, was_queued=False)
        return suggestion

    def dig_many(
        self,
        filters: DiscoveryFilters,
        count: int = 8,
        *,
        cancel_event: Optional[threading.Event] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> list[DiscoverySuggestion]:
        """
        Surface up to `count` resolved suggestions for the reel UI.

        Ranking blends Discogs desirability with sample-friendliness
        affinity (genre/style/era/country) when `filters.prioritize_samples`
        is set. Suggestions are NOT recorded here — call
        `record_suggestion()` when the user previews or queues one.
        Masters returned on the reel are remembered for the session so
        the next Dig explores further into the catalog.
        """
        if not self._token:
            raise DiscoveryConfigError(
                "Discogs API token is required. Add one in Settings."
            )

        count = max(1, int(count))
        self._log.info("Dig (batch of %d) started: %s", count, filters)
        self._emit_progress(progress, "Searching Discogs…")

        candidates = self._search_discogs(
            filters, cancel_event=cancel_event, progress=progress,
        )
        self._check_cancel(cancel_event)
        if not candidates:
            raise NoResultsError(
                "No Discogs masters matched the filters. Try widening them."
            )

        pool = self._rank_and_shuffle(candidates, filters)
        self._emit_progress(
            progress,
            f"Found {len(candidates)} records — matching on YouTube Music…",
        )

        # Walk the ranked pool, resolving YT matches until we fill the
        # reel or exhaust a generous attempt budget.
        suggestions: list[DiscoverySuggestion] = []
        seen_video_ids: set[str] = set()
        last_error: Optional[Exception] = None
        max_attempts = min(
            len(pool),
            max(count * 5 + self.MAX_YT_MATCH_ATTEMPTS, len(pool) // 2),
        )

        for cand in pool[:max_attempts]:
            self._check_cancel(cancel_event)
            if len(suggestions) >= count:
                break
            self._emit_progress(
                progress,
                f"Matching on YouTube — {len(suggestions)}/{count}…",
            )
            try:
                suggestion = self._match_youtube(cand)
            except NoYouTubeMatchError as e:
                last_error = e
                self._log.debug(
                    "No YT match for %s — %s: %s", cand.artist, cand.title, e,
                )
                continue
            # De-dupe within a single reel (different masters can resolve
            # to the same upload).
            if suggestion.youtube_video_id in seen_video_ids:
                continue
            seen_video_ids.add(suggestion.youtube_video_id)
            suggestions.append(suggestion)
            self._log.info(
                "Reel match %d/%d: %s — %s (YT %.2f, affinity %.2f)",
                len(suggestions), count, cand.artist, cand.title,
                suggestion.match_score, cand.sample_affinity,
            )

        if not suggestions:
            raise NoYouTubeMatchError(
                f"Tried {max_attempts} Discogs candidates; none resolved on "
                f"YouTube Music. Last error: {last_error}"
            )

        self._remember_surfaced(s.discogs_master_id for s in suggestions)
        return suggestions

    def record_suggestion(
        self, suggestion: DiscoverySuggestion, *, was_queued: bool = False,
    ) -> None:
        """
        Persist a suggestion to discovery_history so it isn't re-surfaced.
        Called by the UI when the user previews or queues a gem.
        """
        try:
            self._db.record_discovery(DiscoveryRecord(
                discogs_master_id=suggestion.discogs_master_id,
                discogs_release_id=suggestion.discogs_release_id,
                artist=suggestion.artist,
                title=suggestion.title,
                year=suggestion.year,
                country=suggestion.country,
                genre=suggestion.genre,
                style=suggestion.style,
                was_queued=was_queued,
            ))
        except Exception:
            self._log.exception(
                "Could not record discovery for master %s",
                suggestion.discogs_master_id,
            )

    @staticmethod
    def _emit_progress(
        progress: Optional[Callable[[str], None]], message: str,
    ) -> None:
        if progress is None:
            return
        try:
            progress(message)
        except Exception:
            pass

    @staticmethod
    def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise DiscoveryCancelledError("Dig cancelled by user")

    def _remember_surfaced(self, master_ids: Iterable[int]) -> None:
        """Track masters shown on a reel so the next dig explores outward."""
        for mid in master_ids:
            self._session_surfaced[mid] = None
            self._session_surfaced.move_to_end(mid)
        while len(self._session_surfaced) > self.SESSION_SURFACED_MAX:
            self._session_surfaced.popitem(last=False)

    def _rank_and_shuffle(
        self, candidates: list[DiscogsCandidate], filters: DiscoveryFilters,
    ) -> list[DiscogsCandidate]:
        """
        Exploration-friendly pool: drop session-surfaced and DB-recorded
        masters, then rank the full list with per-dig score jitter so
        repeated digs draw from the whole candidate set — not just a
        fixed top tier.
        """
        session_seen = set(self._session_surfaced)
        fresh = [
            c for c in candidates
            if c.master_id not in session_seen
            and not self._db.is_already_suggested(c.master_id)
        ]
        if not fresh:
            fresh = [
                c for c in candidates
                if not self._db.is_already_suggested(c.master_id)
            ]
            if fresh:
                self._log.info(
                    "Session pool exhausted; reusing %d DB-fresh candidates.",
                    len(fresh),
                )
        if not fresh:
            self._log.info(
                "All %d candidates already suggested; reusing pool.",
                len(candidates),
            )
            fresh = list(candidates)

        prioritize = filters.prioritize_samples
        intensity = filters.sample_intensity

        def _jittered_score(c: DiscogsCandidate) -> float:
            base = c.rank_score(prioritize=prioritize, intensity=intensity)
            return base * (0.25 + 0.75 * self._rng.random())

        return sorted(fresh, key=_jittered_score, reverse=True)

    @staticmethod
    def _has_narrow_filters(filters: DiscoveryFilters) -> bool:
        """True when the user narrowed the catalog beyond wide-open dig."""
        return not DiscoveryEngine._is_wide_open(filters)

    @staticmethod
    def _effective_min_have(filters: DiscoveryFilters) -> int:
        """
        Relax min_have when country/era/genre filters already shrink the
        pool — many regional gems never hit high collector counts.
        """
        if not DiscoveryEngine._has_narrow_filters(filters):
            return filters.min_have
        return max(
            DiscoveryEngine.FILTERED_MIN_HAVE_FLOOR,
            min(filters.min_have, DiscoveryEngine.FILTERED_MIN_HAVE_CAP),
        )

    def _pick_discogs_sort(self) -> tuple[str, str]:
        """Random sort axis so each dig sees a different Discogs window."""
        return (
            self._rng.choice(self._DISCOGS_SORT_FIELDS),
            self._rng.choice(("asc", "desc")),
        )

    def _page_budget(
        self, filters: DiscoveryFilters, *, has_range: bool,
    ) -> int:
        if self._has_narrow_filters(filters):
            return self.MAX_SEARCH_PAGES_NARROW
        if has_range:
            return self.MAX_SEARCH_PAGES + 3
        return self.MAX_SEARCH_PAGES

    # ── Discogs ──

    def _search_discogs(
        self,
        filters: DiscoveryFilters,
        *,
        cancel_event: Optional[threading.Event] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> list[DiscogsCandidate]:
        """Paginated Discogs search with exploration-friendly paging."""
        effective = filters
        min_have_floor = self._effective_min_have(filters)
        wide_open = self._is_wide_open(filters)

        if wide_open:
            field_name, seed_value = pick_wide_open_discogs_seed(self._rng)
            effective = replace(filters, **{field_name: seed_value})
            self._log.info(
                "Wide-open dig — exploring %s=%r",
                field_name, seed_value,
            )

        sort_field, sort_order = self._pick_discogs_sort()
        params: dict[str, Any] = {
            "type": "master",
            "per_page": 50,
            "sort": sort_field,
            "sort_order": sort_order,
        }
        if effective.year is not None:
            params["year"] = effective.year
        if effective.country:
            params["country"] = effective.country
        if effective.genre:
            params["genre"] = effective.genre
        if effective.style:
            params["style"] = effective.style
        if effective.format:
            params["format"] = effective.format
        if effective.query:
            params["q"] = effective.query

        has_range = effective.year is None and (
            effective.year_min is not None or effective.year_max is not None
        )
        page_budget = self._page_budget(effective, has_range=has_range)

        probe = self._discogs_get("/database/search", {**params, "page": 1})
        pagination = probe.get("pagination") or {}
        total_pages = max(1, int(pagination.get("pages") or 1))

        seen_masters: set[int] = set()
        candidates: list[DiscogsCandidate] = []

        def _ingest(data: dict[str, Any]) -> None:
            for r in data.get("results") or []:
                cand = self._result_to_candidate(
                    r, allow_compilations=effective.allow_compilations,
                )
                if cand is None or cand.master_id in seen_masters:
                    continue
                if cand.have < min_have_floor or cand.have > effective.max_have:
                    continue
                if not self._year_in_range(cand.year, effective):
                    continue
                seen_masters.add(cand.master_id)
                candidates.append(cand)

        if not wide_open:
            _ingest(probe)

        remaining = list(range(1, total_pages + 1))
        if not wide_open:
            remaining = [p for p in remaining if p != 1]
        self._rng.shuffle(remaining)

        if wide_open:
            preferred = [p for p in range(2, min(5, total_pages + 1))]
            rest = [p for p in remaining if p not in preferred]
            self._rng.shuffle(preferred)
            self._rng.shuffle(rest)
            page_order = preferred + rest
        else:
            page_order = remaining

        max_extra = max(0, page_budget - 1)
        page_order = page_order[:max_extra]

        fetches = 1
        for page in page_order:
            self._check_cancel(cancel_event)
            if fetches >= page_budget:
                break
            if page == 1 and not wide_open:
                continue
            self._emit_progress(
                progress,
                f"Searching Discogs — page {fetches}/{page_budget} "
                f"({len(candidates)} candidates)…",
            )
            data = self._discogs_get(
                "/database/search", {**params, "page": page},
            )
            _ingest(data)
            fetches += 1
            if len(candidates) >= self.TARGET_POOL_SIZE:
                break

        if min_have_floor != filters.min_have:
            self._log.info(
                "Narrow-filter dig — min_have relaxed %d → %d",
                filters.min_have, min_have_floor,
            )
        self._log.debug(
            "Discogs yielded %d candidates (sort=%s %s, pages=%d/%d, "
            "min_have=%d, filters=%s)",
            len(candidates), sort_field, sort_order, fetches, total_pages,
            min_have_floor, effective,
        )
        return candidates

    @staticmethod
    def _is_wide_open(filters: DiscoveryFilters) -> bool:
        """True when the user left every Discogs filter at its default."""
        return (
            filters.query is None
            and not filters.country
            and not filters.genre
            and not filters.style
            and not filters.format
            and filters.year is None
            and filters.year_min is None
            and filters.year_max is None
        )

    @staticmethod
    def _year_in_range(
        year: Optional[int], filters: DiscoveryFilters,
    ) -> bool:
        """Client-side era-range gate. Unknown years pass (don't punish)."""
        if filters.year is not None:
            return True  # exact-year already applied server-side
        if year is None:
            return True
        if filters.year_min is not None and year < filters.year_min:
            return False
        if filters.year_max is not None and year > filters.year_max:
            return False
        return True

    # Artist values that represent compilations — no single YouTube video
    # can match "Various Artists", so skip these early.
    _VARIOUS_NAMES: frozenset[str] = frozenset({
        "various", "various artists", "v/a", "va",
        "varios artistas", "artistes variés",
    })

    @staticmethod
    def _result_to_candidate(
        r: dict[str, Any], *, allow_compilations: bool = False,
    ) -> Optional[DiscogsCandidate]:
        """Parse a /database/search hit into a candidate, or None if unusable."""
        master_id = r.get("master_id") or r.get("id")
        title_full = r.get("title") or ""
        if not master_id or not title_full:
            return None

        # Discogs search titles are "Artist - Title" strings.
        if " - " in title_full:
            artist, title = title_full.split(" - ", 1)
        else:
            artist, title = "", title_full

        # Compilations ("Various Artists") can't be matched to a single
        # YouTube video, so by default we skip them to save YTM budget.
        # The user can opt in via filters.allow_compilations.
        if (
            not allow_compilations
            and artist.strip().lower() in DiscoveryEngine._VARIOUS_NAMES
        ):
            return None

        year_raw = r.get("year")
        try:
            year = int(year_raw) if year_raw else None
        except (TypeError, ValueError):
            year = None

        community = r.get("community") or {}
        have = int(community.get("have") or 0)
        want = int(community.get("want") or 0)

        return DiscogsCandidate(
            master_id=int(master_id),
            release_id=(int(r["id"]) if r.get("id") else None),
            artist=artist.strip(),
            title=title.strip(),
            year=year,
            country=r.get("country"),
            genres=tuple(r.get("genre") or ()),
            styles=tuple(r.get("style") or ()),
            have=have,
            want=want,
        )

    def _discogs_get(
        self, endpoint: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        """Rate-limited, retry-wrapped Discogs GET."""
        url = f"{self.DISCOGS_BASE}{endpoint}"
        headers = {
            "User-Agent": self.USER_AGENT,
            "Authorization": f"Discogs token={self._token}",
        }

        def _do() -> dict[str, Any]:
            waited = self._discogs_limiter.acquire()
            self._stats.discogs_rate_waits += waited
            self._stats.discogs_requests += 1
            try:
                resp = self._session.get(
                    url, headers=headers, params=params, timeout=(5, 20),
                )
            except requests.RequestException as e:
                raise DiscoveryError(f"Discogs network error: {e}") from e

            # Discogs 429 includes a Retry-After header — honor it.
            if resp.status_code == 429:
                ra = _parse_retry_after(resp.headers.get("Retry-After"))
                self._stats.throttle_events += 1
                self._log.warning(
                    "Discogs rate-limited us; pausing %.1fs (Retry-After).", ra,
                )
                self._discogs_limiter.pause(ra)
                raise _RetryableError(f"429 Too Many Requests (retry in {ra}s)")

            if resp.status_code >= 500:
                raise _RetryableError(f"Discogs {resp.status_code}")

            if resp.status_code >= 400:
                raise DiscoveryError(
                    f"Discogs {resp.status_code}: "
                    f"{resp.text[:200] if resp.text else '(empty)'}"
                )

            try:
                return resp.json()
            except ValueError as e:
                raise DiscoveryError(f"Invalid JSON from Discogs: {e}") from e

        return self._with_backoff(
            _do, what=f"Discogs GET {endpoint}",
        )

    # ── YouTube Music matcher ──

    def _match_youtube(
        self, cand: DiscogsCandidate,
    ) -> DiscoverySuggestion:
        """
        Search YTM for the best audio match for `cand`.

        Tries multiple queries in priority order (primary artist + title,
        then title alone) and two YTM filter modes (songs → videos) so
        that older recordings not indexed as songs are still reachable.
        """
        ytm = self._get_ytm_client()
        primary_artist = _extract_primary_artist(cand.artist)

        # Ordered query attempts — simpler queries often score better
        # than the full compound Discogs credit string.
        queries = [f"{primary_artist} {cand.title}"]
        if primary_artist.lower() != cand.artist.replace("*", "").strip().lower():
            # Also try without disambiguation cleanup in case YTM knows the name
            queries.append(f"{cand.artist.replace('*', '').strip()} {cand.title}")
        queries.append(cand.title)  # title-only last resort

        # Try songs filter first; fall back to videos for older content
        # that may only exist as channel uploads rather than music entries.
        for query in queries:
            for ytm_filter in ("songs", "videos"):
                results = self._ytm_search(ytm, query, ytm_filter)
                if not results:
                    continue

                scored = self._score_ytm_results(results, cand, primary_artist)
                if not scored:
                    continue

                best_score, best = scored[0]
                video_id = best.get("videoId")
                if not video_id:
                    continue

                self._log.debug(
                    "YTM match: query=%r filter=%s score=%.2f",
                    query, ytm_filter, best_score,
                )
                return DiscoverySuggestion(
                    discogs_master_id=cand.master_id,
                    discogs_release_id=cand.release_id,
                    artist=cand.artist,
                    title=cand.title,
                    year=cand.year,
                    country=cand.country,
                    genre=(cand.genres[0] if cand.genres else None),
                    style=(cand.styles[0] if cand.styles else None),
                    youtube_url=f"https://music.youtube.com/watch?v={video_id}",
                    youtube_video_id=video_id,
                    youtube_title=str(best.get("title") or ""),
                    youtube_duration_seconds=_ytm_duration_seconds(best),
                    match_score=best_score,
                )

        raise NoYouTubeMatchError(
            f"No YTM result passed quality threshold for {cand.artist} — {cand.title}"
        )

    def _ytm_search(
        self, ytm: Any, query: str, ytm_filter: str,
    ) -> list[dict[str, Any]]:
        """Rate-limited, retry-wrapped YTM search. Returns [] on failure."""
        def _do() -> list[dict[str, Any]]:
            waited = self._ytm_limiter.acquire()
            self._stats.ytm_rate_waits += waited
            self._stats.ytm_requests += 1
            with self._ytm_lock:
                return ytm.search(query=query, filter=ytm_filter, limit=15) or []

        try:
            return self._with_backoff(_do, what=f"YTM search ({ytm_filter!r})")
        except DiscoveryError as e:
            self._log.debug("YTM search failed for %r (%s): %s", query, ytm_filter, e)
            return []

    @staticmethod
    def _score_ytm_results(
        results: list[dict[str, Any]],
        cand: DiscogsCandidate,
        primary_artist: Optional[str] = None,
    ) -> list[tuple[float, dict[str, Any]]]:
        """
        Rank YTM results by fit to the Discogs candidate.

        Uses the cleaned primary artist (not the full compound credit string)
        for overlap scoring. Thresholds are intentionally permissive on the
        artist axis since name variations are common across data sources.
        Returns [(score, result), ...] sorted descending.
        """
        target_artist = _norm(primary_artist or _extract_primary_artist(cand.artist))
        target_title = _norm(cand.title)

        scored: list[tuple[float, dict[str, Any]]] = []
        for r in results:
            yt_title = _norm(r.get("title") or "")
            yt_artist = _norm(
                ", ".join(a.get("name", "") for a in (r.get("artists") or []))
            )

            if not yt_title:
                continue

            # Hard filter: skip live/remix/cover unless the Discogs title
            # itself implies it (e.g. a live album or remix release).
            banned = ("live", "remix", "cover", "karaoke", "reaction", "tutorial")
            if any(b in yt_title for b in banned) and not any(
                b in target_title for b in banned
            ):
                continue

            # Title: precision-weighted so '(Remastered)' suffixes don't tank short titles.
            # Artist: pure Jaccard (bidirectional — both sides matter for artist names).
            title_overlap = _title_match(target_title, yt_title)
            artist_overlap = _token_overlap(target_artist, yt_artist) if yt_artist else 0.0

            # Title must contain enough of the target.
            if title_overlap < 0.35:
                continue
            # Allow weak artist overlap only when the title match is very strong.
            if artist_overlap < 0.2 and title_overlap < 0.75:
                continue

            score = 0.55 * artist_overlap + 0.45 * title_overlap
            if "official" in yt_title or "original" in yt_title:
                score = min(1.0, score + 0.05)

            scored.append((score, r))

        scored.sort(key=lambda p: p[0], reverse=True)
        return scored

    def _get_ytm_client(self) -> Any:
        """Lazy-init the ytmusicapi client. Deferred import saves 300ms boot."""
        if self._ytm_client is not None:
            return self._ytm_client
        try:
            from ytmusicapi import YTMusic
        except ImportError as e:
            raise DiscoveryError(
                "ytmusicapi is not installed. Check requirements.txt."
            ) from e
        # No auth file — unauthenticated search works fine for our needs
        # and avoids shipping a user auth flow for the MVP.
        self._ytm_client = YTMusic()
        return self._ytm_client

    # ── Generic retry helper ──

    def _with_backoff(
        self,
        fn: Callable[[], Any],
        *,
        what: str,
        max_attempts: int = 4,
    ) -> Any:
        """Exponential backoff: 1s → 2s → 4s. Only for _RetryableError."""
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return fn()
            except _RetryableError as e:
                last_exc = e
                if attempt == max_attempts:
                    break
                self._log.info(
                    "%s retryable error (attempt %d/%d): %s — sleeping %.1fs",
                    what, attempt, max_attempts, e, delay,
                )
                self._stats.recent_errors.append(
                    (time.time(), what, str(e)),
                )
                time.sleep(delay)
                delay *= 2
            except DiscoveryError:
                raise
            except Exception as e:
                raise DiscoveryError(f"{what} unexpected error: {e}") from e

        raise DiscoveryThrottledError(
            f"{what} failed after {max_attempts} attempts: {last_exc}"
        )

    # ── Session ──

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": self.USER_AGENT})
        return s


# ─── Private helpers ─────────────────────────────────────────────────

class _RetryableError(Exception):
    """Internal marker for transient failures — never leaks out."""


def _parse_retry_after(raw: Optional[str]) -> float:
    """
    Retry-After can be either seconds (int) or an HTTP-date. We only
    need the seconds form for Discogs. Defaults to 60s if unparseable.
    """
    if not raw:
        return 60.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 60.0


_NORM_RE = re.compile(r"[^\w\s]+", re.UNICODE)


def _norm(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return _NORM_RE.sub(" ", s.lower()).strip()


def _extract_primary_artist(artist: str) -> str:
    """
    Distil a Discogs compound credit string to a clean searchable name.

    Handles three Discogs conventions:
      "Artist*"              — asterisk marks name disambiguation, strip it
      "A / B"                — slash separates collaborators, take lead only
      "Composer, Orchestra…" — very long ensemble credits, take composer part
    """
    # Strip Discogs disambiguation asterisks
    artist = artist.replace("*", "").strip()
    # Slash-separated credits: take only the first (lead) artist
    if " / " in artist:
        artist = artist.split(" / ")[0].strip()
    # Extremely long credit strings (orchestras, ensembles) — take only the
    # portion before the first comma so the YTM query stays meaningful.
    if len(artist) > 40 and "," in artist:
        artist = artist.split(",")[0].strip()
    return artist or "Unknown Artist"


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on tokenized strings. Returns 0..1."""
    at = set(a.split())
    bt = set(b.split())
    if not at or not bt:
        return 0.0
    return len(at & bt) / len(at | bt)


def _title_match(target: str, candidate: str) -> float:
    """
    Precision-weighted title match designed to tolerate YouTube suffixes
    like '(Remastered 2019)', '(Mono)', year stamps, and subtitle strings
    appended to classical works.

    Short titles (≤2 words) use pure precision — every target word must
    appear in the candidate, but extra words in the candidate are OK.
    Longer titles blend toward Jaccard to stay strict against unrelated content.
    """
    at = set(target.split())
    bc = set(candidate.split())
    if not at or not bc:
        return 0.0
    intersection = len(at & bc)
    precision = intersection / len(at)          # how much of target is in candidate
    jaccard = intersection / len(at | bc)       # symmetric overlap

    # Weight: target len 1 → pure precision; len 5+ → pure Jaccard
    jaccard_weight = min(1.0, (len(at) - 1) / 4.0)
    return precision * (1 - jaccard_weight) + jaccard * jaccard_weight


def _ytm_duration_seconds(r: dict[str, Any]) -> Optional[int]:
    """
    ytmusicapi surfaces duration as either 'duration_seconds' (int)
    or 'duration' ('M:SS' string) depending on the endpoint. Handle both.
    """
    secs = r.get("duration_seconds")
    if isinstance(secs, int):
        return secs
    raw = r.get("duration")
    if isinstance(raw, str) and ":" in raw:
        try:
            parts = [int(p) for p in raw.split(":")]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        except ValueError:
            return None
    return None