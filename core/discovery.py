"""
core/discovery.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Gem Discovery (Discogs + YouTube Music matcher)

The "Dig" button workflow:
    1. User picks Decade, Country, Genre/Style in the UI.
    2. Query Discogs for master releases matching the filters.
    3. Rank by community stats (want/have ratio + absolute haves)
       and pick a random high-rated master the user hasn't been
       shown before.
    4. Query ytmusicapi for the exact audio match, preferring
       official album versions over live/remixes.
    5. Return an enriched suggestion ready to be queued.

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
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import requests

from core.database import DiscoveryRecord, VaultDatabase


# ─── Public types ────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class DiscoveryFilters:
    """User-selected filters from the Digital Crate tab."""
    decade: Optional[int] = None           # e.g. 1970 = "the 70s"
    country: Optional[str] = None          # Discogs country string, e.g. "Brazil"
    genre: Optional[str] = None            # Discogs top-level genre, e.g. "Jazz"
    style: Optional[str] = None            # Discogs style, e.g. "Bossa Nova"
    min_have: int = 50                     # min community `have` count


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
    # returns 50 per page; 4 pages = 200 candidates, plenty to pick
    # from without burning the rate budget on a single click.
    MAX_SEARCH_PAGES = 4

    # How many Discogs candidates to try when the first YT match fails.
    MAX_YT_MATCH_ATTEMPTS = 5

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
        Main entry point. Given filters, return one fully-resolved
        DiscoverySuggestion. The caller (pipeline or UI) is responsible
        for recording the discovery into the DB once the user acts on it.

        Raises:
            DiscoveryConfigError     — missing token
            NoResultsError           — no Discogs masters match
            NoYouTubeMatchError      — Discogs matches found but none resolvable
            DiscoveryThrottledError  — rate limited beyond our ability to wait
        """
        if not self._token:
            raise DiscoveryConfigError(
                "Discogs API token is required. Add one in Settings."
            )

        self._log.info("Dig started: %s", filters)

        candidates = self._search_discogs(filters)
        if not candidates:
            raise NoResultsError(
                "No Discogs masters matched the filters. Try widening them."
            )

        # Filter out previously-suggested masters.
        fresh = [c for c in candidates
                 if not self._db.is_already_suggested(c.master_id)]
        if not fresh:
            # If everything has been seen, fall back to the full list —
            # the user clicked Dig, they should get *something*.
            self._log.info(
                "All %d candidates already suggested; reusing pool.",
                len(candidates),
            )
            fresh = candidates

        # Weighted pick: desirability-weighted, but still randomized so
        # repeated clicks feel exploratory rather than deterministic.
        ranked = sorted(fresh, key=lambda c: c.desirability, reverse=True)
        top_pool = ranked[:max(10, len(ranked) // 4)]
        self._rng.shuffle(top_pool)

        # Try candidates in order until a YT match resolves.
        last_error: Optional[Exception] = None
        for i, cand in enumerate(top_pool[:self.MAX_YT_MATCH_ATTEMPTS]):
            try:
                suggestion = self._match_youtube(cand)
                self._log.info(
                    "Dig resolved on attempt %d: %s — %s (YT score %.2f)",
                    i + 1, cand.artist, cand.title, suggestion.match_score,
                )
                # Record now so a second Dig doesn't re-surface this one
                # even if the user never queues it.
                self._db.record_discovery(DiscoveryRecord(
                    discogs_master_id=cand.master_id,
                    discogs_release_id=cand.release_id,
                    artist=cand.artist,
                    title=cand.title,
                    year=cand.year,
                    country=cand.country,
                    genre=(cand.genres[0] if cand.genres else None),
                    style=(cand.styles[0] if cand.styles else None),
                ))
                return suggestion
            except NoYouTubeMatchError as e:
                last_error = e
                self._log.debug(
                    "No YT match for %s — %s: %s",
                    cand.artist, cand.title, e,
                )
                continue

        raise NoYouTubeMatchError(
            f"Tried {self.MAX_YT_MATCH_ATTEMPTS} Discogs candidates; "
            f"none resolved on YouTube Music. Last error: {last_error}"
        )

    # ── Discogs ──

    def _search_discogs(
        self, filters: DiscoveryFilters,
    ) -> list[DiscogsCandidate]:
        """Paginated Discogs search with filter-to-param translation."""
        params: dict[str, Any] = {
            "type": "master",
            "per_page": 50,
            "format": "album",       # skip singles — digger bias is LPs
        }
        if filters.decade is not None:
            # Discogs supports range syntax in the year param: "1970-1979"
            params["year"] = f"{filters.decade}-{filters.decade + 9}"
        if filters.country:
            params["country"] = filters.country
        if filters.genre:
            params["genre"] = filters.genre
        if filters.style:
            params["style"] = filters.style

        candidates: list[DiscogsCandidate] = []
        for page in range(1, self.MAX_SEARCH_PAGES + 1):
            params["page"] = page
            data = self._discogs_get("/database/search", params)
            results = data.get("results") or []
            if not results:
                break

            for r in results:
                cand = self._result_to_candidate(r)
                if cand is None:
                    continue
                if cand.have < filters.min_have:
                    continue
                candidates.append(cand)

            # Honor pagination-provided total if present
            pagination = data.get("pagination") or {}
            if page >= int(pagination.get("pages") or 0):
                break

        self._log.debug(
            "Discogs yielded %d candidates (filters=%s)",
            len(candidates), filters,
        )
        return candidates

    @staticmethod
    def _result_to_candidate(r: dict[str, Any]) -> Optional[DiscogsCandidate]:
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
        """Search YTM for the canonical album-version of `cand`."""
        ytm = self._get_ytm_client()

        query = f"{cand.artist} {cand.title} album version"

        def _do() -> list[dict[str, Any]]:
            waited = self._ytm_limiter.acquire()
            self._stats.ytm_rate_waits += waited
            self._stats.ytm_requests += 1
            with self._ytm_lock:
                # filter="songs" forces YTM to return proper music entries
                # (skips user-uploaded covers, live sets, topic channels).
                return ytm.search(query=query, filter="songs", limit=15)

        try:
            results = self._with_backoff(_do, what="YTM search")
        except DiscoveryError as e:
            raise NoYouTubeMatchError(f"YTM search failed: {e}") from e

        if not results:
            raise NoYouTubeMatchError(f"YTM returned zero hits for {query!r}")

        scored = self._score_ytm_results(results, cand)
        if not scored:
            raise NoYouTubeMatchError(
                f"No YTM result passed quality threshold for {cand.artist} — {cand.title}"
            )

        best_score, best = scored[0]
        video_id = best.get("videoId")
        if not video_id:
            raise NoYouTubeMatchError("Top YTM match has no videoId")

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

    @staticmethod
    def _score_ytm_results(
        results: list[dict[str, Any]], cand: DiscogsCandidate,
    ) -> list[tuple[float, dict[str, Any]]]:
        """
        Rank YTM results by fit to the Discogs candidate. Returns
        [(score, result), ...] sorted descending, excluding obviously
        bad matches (live/remix/cover).
        """
        target_artist = _norm(cand.artist)
        target_title = _norm(cand.title)

        scored: list[tuple[float, dict[str, Any]]] = []
        for r in results:
            yt_title = _norm(r.get("title") or "")
            yt_artist = _norm(
                ", ".join(a.get("name", "") for a in (r.get("artists") or []))
            )

            if not yt_title or not yt_artist:
                continue

            # Hard filters: skip anything that smells like a live/remix/cover
            # unless the Discogs title itself contains those tokens.
            banned = ("live", "remix", "cover", "karaoke", "reaction", "tutorial")
            if any(b in yt_title for b in banned) and not any(
                b in target_title for b in banned
            ):
                continue

            artist_overlap = _token_overlap(target_artist, yt_artist)
            title_overlap = _token_overlap(target_title, yt_title)

            # Require meaningful overlap on both axes.
            if artist_overlap < 0.5 or title_overlap < 0.5:
                continue

            # Score: weighted avg, small bonus for exact album-version hits
            score = 0.55 * artist_overlap + 0.45 * title_overlap
            if "album version" in yt_title or "official" in yt_title:
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


def _token_overlap(a: str, b: str) -> float:
    """
    Jaccard similarity on tokenized strings. Returns 0..1.
    Used for fuzzy artist/title matching between Discogs & YTM.
    """
    at = set(a.split())
    bt = set(b.split())
    if not at or not bt:
        return 0.0
    return len(at & bt) / len(at | bt)


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