"""
core/database.py
──────────────────────────────────────────────────────────────────────
Crate Digger — SQLite Data Access Layer

The ONLY module in the codebase allowed to touch vault.db. Every other
module (pipeline, discovery, queue_manager, UI) receives a VaultDatabase
instance via dependency injection.

Concurrency model:
  • WAL mode enforced on every connection. Readers (UI) never block
    writers (worker threads), and vice versa.
  • Thread-local connections. sqlite3 connections are NOT thread-safe;
    attempting to share one across threads produces cryptic "SQLite
    objects created in a thread can only be used in that same thread"
    errors intermittently under load. We give each thread its own
    connection, lazily created on first use and reused thereafter.
  • Single writer lock. SQLite serializes writes anyway; holding a
    Python-side RLock around write operations keeps our own logging
    clean and prevents spurious "database is locked" retries when
    multiple worker threads happen to race on INSERT.
  • busy_timeout=5000ms. Even with WAL, a checkpoint briefly locks
    the DB; the timeout absorbs that without surfacing errors.

Data model invariants:
  • The filesystem is the source of truth. Any row whose `file_path`
    no longer exists on disk is stale and flagged by `reconcile()`.
  • FTS5 is a denormalized index, kept in sync via triggers defined
    in the schema bootstrap. Searches go through the FTS view; never
    LIKE-scan the main table.
  • Schema version lives in the `app_metadata` table. Future migrations
    branch on the stored value in `_migrate()`.

Zero UI awareness. Zero network. Zero business logic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# ─── Schema ──────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_metadata (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path           TEXT    NOT NULL UNIQUE,
    file_size_bytes     INTEGER,
    checksum_sha256     TEXT,
    artist              TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    album               TEXT,
    genre               TEXT,
    style               TEXT,
    country             TEXT,
    year                INTEGER,
    decade              INTEGER,
    duration_seconds    REAL,
    bpm                 REAL,
    bpm_confidence      REAL,
    musical_key         TEXT,
    camelot_key         TEXT,
    key_confidence      REAL,
    artwork_embedded    INTEGER NOT NULL DEFAULT 0 CHECK (artwork_embedded IN (0,1)),
    stems_separated     INTEGER NOT NULL DEFAULT 0 CHECK (stems_separated IN (0,1)),
    stems_path          TEXT,
    source_url          TEXT    NOT NULL,
    source_platform     TEXT    NOT NULL CHECK (source_platform IN
                            ('youtube','youtube_music','manual','discogs_dig')),
    discogs_master_id   INTEGER,
    discogs_release_id  INTEGER,
    date_added          TEXT    NOT NULL,
    date_modified       TEXT    NOT NULL,
    last_exported_at    TEXT,
    export_count        INTEGER NOT NULL DEFAULT 0,
    rating              INTEGER CHECK (rating BETWEEN 0 AND 5),
    notes               TEXT,
    tags                TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_artist   ON tracks(artist COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_tracks_title    ON tracks(title  COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_tracks_genre    ON tracks(genre);
CREATE INDEX IF NOT EXISTS idx_tracks_bpm      ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_key      ON tracks(camelot_key);
CREATE INDEX IF NOT EXISTS idx_tracks_decade   ON tracks(decade);
CREATE INDEX IF NOT EXISTS idx_tracks_added    ON tracks(date_added DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
    artist, title, album, genre, style, notes, tags,
    content='tracks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS tracks_ai AFTER INSERT ON tracks BEGIN
    INSERT INTO tracks_fts(rowid, artist, title, album, genre, style, notes, tags)
    VALUES (new.id, new.artist, new.title, new.album, new.genre, new.style, new.notes, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS tracks_ad AFTER DELETE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, artist, title, album, genre, style, notes, tags)
    VALUES ('delete', old.id, old.artist, old.title, old.album, old.genre, old.style, old.notes, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS tracks_au AFTER UPDATE ON tracks BEGIN
    INSERT INTO tracks_fts(tracks_fts, rowid, artist, title, album, genre, style, notes, tags)
    VALUES ('delete', old.id, old.artist, old.title, old.album, old.genre, old.style, old.notes, old.tags);
    INSERT INTO tracks_fts(rowid, artist, title, album, genre, style, notes, tags)
    VALUES (new.id, new.artist, new.title, new.album, new.genre, new.style, new.notes, new.tags);
END;

CREATE TABLE IF NOT EXISTS queue_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url          TEXT    NOT NULL,
    display_name        TEXT,
    status              TEXT    NOT NULL CHECK (status IN
                            ('pending','downloading','analyzing',
                             'tagging','separating_stems','complete','failed','cancelled')),
    progress_pct        REAL    NOT NULL DEFAULT 0,
    current_stage       TEXT,
    error_message       TEXT,
    track_id            INTEGER,
    enable_stems        INTEGER NOT NULL DEFAULT 0 CHECK (enable_stems IN (0,1)),
    created_at          TEXT    NOT NULL,
    started_at          TEXT,
    completed_at        TEXT,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_status   ON queue_jobs(status);
CREATE INDEX IF NOT EXISTS idx_queue_created  ON queue_jobs(created_at DESC);

CREATE TABLE IF NOT EXISTS discovery_history (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    discogs_master_id   INTEGER NOT NULL,
    discogs_release_id  INTEGER,
    artist              TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    year                INTEGER,
    decade              INTEGER,
    country             TEXT,
    genre               TEXT,
    style               TEXT,
    suggested_at        TEXT    NOT NULL,
    was_queued          INTEGER NOT NULL DEFAULT 0 CHECK (was_queued IN (0,1)),
    track_id            INTEGER,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_discovery_master
    ON discovery_history(discogs_master_id);
CREATE INDEX IF NOT EXISTS idx_discovery_suggested
    ON discovery_history(suggested_at DESC);

CREATE TABLE IF NOT EXISTS mpc_exports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id            INTEGER NOT NULL,
    destination_path    TEXT    NOT NULL,
    destination_device  TEXT,
    wav_size_bytes      INTEGER,
    exported_at         TEXT    NOT NULL,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_exports_track     ON mpc_exports(track_id);
CREATE INDEX IF NOT EXISTS idx_exports_exported  ON mpc_exports(exported_at DESC);

CREATE TABLE IF NOT EXISTS crates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    description         TEXT,
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS crate_tracks (
    crate_id            INTEGER NOT NULL,
    track_id            INTEGER NOT NULL,
    added_at            TEXT    NOT NULL,
    PRIMARY KEY (crate_id, track_id),
    FOREIGN KEY (crate_id) REFERENCES crates(id) ON DELETE CASCADE,
    FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_crate_tracks_crate ON crate_tracks(crate_id);
CREATE INDEX IF NOT EXISTS idx_crate_tracks_track ON crate_tracks(track_id);
"""


# ─── Public dataclasses ──────────────────────────────────────────────


@dataclass(slots=True)
class TrackRecord:
    """Denormalized track row. Fields marked Optional can be None in DB."""

    id: Optional[int] = None
    file_path: str = ""
    file_size_bytes: Optional[int] = None
    checksum_sha256: Optional[str] = None
    artist: str = ""
    title: str = ""
    album: Optional[str] = None
    genre: Optional[str] = None
    style: Optional[str] = None
    country: Optional[str] = None
    year: Optional[int] = None
    decade: Optional[int] = None
    duration_seconds: Optional[float] = None
    bpm: Optional[float] = None
    bpm_confidence: Optional[float] = None
    musical_key: Optional[str] = None
    camelot_key: Optional[str] = None
    key_confidence: Optional[float] = None
    artwork_embedded: bool = False
    stems_separated: bool = False
    stems_path: Optional[str] = None
    source_url: str = ""
    source_platform: str = "manual"
    discogs_master_id: Optional[int] = None
    discogs_release_id: Optional[int] = None
    date_added: Optional[str] = None
    date_modified: Optional[str] = None
    last_exported_at: Optional[str] = None
    export_count: int = 0
    rating: Optional[int] = None
    notes: Optional[str] = None
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class QueueJobRecord:
    id: Optional[int] = None
    source_url: str = ""
    display_name: Optional[str] = None
    status: str = "pending"
    progress_pct: float = 0.0
    current_stage: Optional[str] = None
    error_message: Optional[str] = None
    track_id: Optional[int] = None
    enable_stems: bool = False
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass(slots=True)
class DiscoveryRecord:
    id: Optional[int] = None
    discogs_master_id: int = 0
    discogs_release_id: Optional[int] = None
    artist: str = ""
    title: str = ""
    year: Optional[int] = None
    decade: Optional[int] = None
    country: Optional[str] = None
    genre: Optional[str] = None
    style: Optional[str] = None
    suggested_at: Optional[str] = None
    was_queued: bool = False
    track_id: Optional[int] = None


@dataclass(slots=True)
class ExportRecord:
    id: Optional[int] = None
    track_id: int = 0
    destination_path: str = ""
    destination_device: Optional[str] = None
    wav_size_bytes: Optional[int] = None
    exported_at: Optional[str] = None


@dataclass(slots=True)
class CrateRecord:
    """A user-defined grouping of tracks (project / mood / kit)."""
    id: Optional[int] = None
    name: str = ""
    description: Optional[str] = None
    created_at: Optional[str] = None
    track_count: int = 0


@dataclass(slots=True, frozen=True)
class DuplicateGroup:
    """A cluster of tracks that appear to be duplicates of each other."""
    key: str                      # what they share (checksum or artist|title)
    reason: str                   # "checksum" | "artist+title"
    tracks: tuple["TrackRecord", ...] = ()


@dataclass(slots=True, frozen=True)
class TrackFilter:
    """Search / filter criteria for the Vault tab. All optional."""

    query: Optional[str] = None  # FTS5 free-text search
    genre: Optional[str] = None
    decade: Optional[int] = None
    min_bpm: Optional[float] = None
    max_bpm: Optional[float] = None
    camelot_key: Optional[str] = None
    has_stems: Optional[bool] = None
    min_rating: Optional[int] = None
    tag: Optional[str] = None
    crate_id: Optional[int] = None
    limit: int = 500
    offset: int = 0
    order_by: str = "date_added"  # whitelist-checked in build_query
    order_desc: bool = True


# ─── Exceptions ──────────────────────────────────────────────────────


class DatabaseError(Exception):
    """Base class for DAO errors."""


class DatabaseSchemaError(DatabaseError):
    """Schema bootstrap / migration failure."""


class RecordNotFoundError(DatabaseError):
    """No row matched the given selector."""


# ─── The DAO ─────────────────────────────────────────────────────────


class VaultDatabase:
    """
    Thread-safe SQLite DAO for vault.db. One instance per app.
    Workers and the UI both hold a reference; connections are
    lazily opened per thread.
    """

    # Whitelist for ORDER BY — prevents SQL injection through UI sort clicks.
    _ORDERABLE_COLUMNS: frozenset[str] = frozenset(
        {
            "date_added",
            "date_modified",
            "artist",
            "title",
            "bpm",
            "camelot_key",
            "genre",
            "decade",
            "year",
            "rating",
        }
    )

    def __init__(
        self,
        db_path: Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = logger or logging.getLogger("cratedigger.db")

        # Thread-local storage for per-thread connections. sqlite3
        # connections can't be safely shared across threads.
        self._tls = threading.local()

        # Coarse-grained write lock. SQLite serializes writes anyway,
        # but this avoids thrashy retry loops at the sqlite3 layer
        # when multiple worker threads INSERT simultaneously.
        self._write_lock = threading.RLock()

        # Track open connections so close() can tear them all down
        # cleanly at app shutdown. WeakSet avoids keeping dead threads
        # alive just to hold onto their connections.
        self._all_connections: list[sqlite3.Connection] = []
        self._conn_registry_lock = threading.Lock()

        self._bootstrap()

    # ─── Lifecycle ──────────────────────────────────────────────────

    def _bootstrap(self) -> None:
        """Create schema if missing, then run any pending migrations."""
        try:
            with self._writing() as conn:
                conn.executescript(_SCHEMA_SQL)
                current = self._get_schema_version(conn)
                if current is None:
                    # Fresh install — stamp schema and app version.
                    conn.execute(
                        "INSERT OR REPLACE INTO app_metadata(key,value) VALUES(?,?)",
                        ("schema_version", str(SCHEMA_VERSION)),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO app_metadata(key,value) VALUES(?,?)",
                        ("created_at", _utc_now_iso()),
                    )
                    self._log.info("Initialized vault.db schema v%d", SCHEMA_VERSION)
                elif current < SCHEMA_VERSION:
                    self._migrate(conn, current, SCHEMA_VERSION)
                elif current > SCHEMA_VERSION:
                    raise DatabaseSchemaError(
                        f"vault.db schema v{current} is newer than app's "
                        f"v{SCHEMA_VERSION}. Upgrade the app or restore an "
                        f"older backup."
                    )
        except sqlite3.Error as e:
            raise DatabaseSchemaError(f"Schema bootstrap failed: {e}") from e

    def _migrate(
        self,
        conn: sqlite3.Connection,
        from_v: int,
        to_v: int,
    ) -> None:
        """Run sequential migrations. Extended in future schema versions."""
        self._log.info("Migrating vault.db from v%d to v%d", from_v, to_v)
        # Placeholder for future migrations — e.g.:
        # if from_v < 2: conn.executescript("ALTER TABLE tracks ADD COLUMN ...")
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key,value) VALUES(?,?)",
            ("schema_version", str(to_v)),
        )

    @staticmethod
    def _get_schema_version(conn: sqlite3.Connection) -> Optional[int]:
        row = conn.execute(
            "SELECT value FROM app_metadata WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else None

    def close(self) -> None:
        """Close all per-thread connections. Called at app shutdown."""
        with self._conn_registry_lock:
            for c in self._all_connections:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all_connections.clear()
        # Clear any connection on the current thread too
        if hasattr(self._tls, "conn"):
            delattr(self._tls, "conn")

    # ─── Connection management ──────────────────────────────────────

    def _connection(self) -> sqlite3.Connection:
        """Return (and lazily create) the current thread's connection."""
        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            return conn

        conn = sqlite3.connect(
            str(self._db_path),
            timeout=10.0,
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit mode — we manage txns explicitly
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row

        # WAL mode and friends. Applied to every connection because
        # PRAGMA journal_mode is global-but-per-connection-verified:
        # the first WAL set persists, but each connection still needs
        # foreign_keys and busy_timeout set individually.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe; ~5x faster than FULL
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")  # 5s wait before raising locked
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-20000")  # ~20MB cache per connection

        # Verify WAL actually engaged. On some network filesystems it
        # silently falls back to 'journal' mode — we want to know.
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            self._log.warning(
                "WAL mode not engaged (got %r). Concurrent reads/writes "
                "may contend. Is vault.db on a network drive?",
                mode,
            )

        self._tls.conn = conn
        with self._conn_registry_lock:
            self._all_connections.append(conn)
        return conn

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """Context manager for read-only operations. No lock needed — WAL."""
        conn = self._connection()
        try:
            yield conn
        except sqlite3.Error as e:
            raise DatabaseError(f"Read failed: {e}") from e

    @contextmanager
    def _writing(self) -> Iterator[sqlite3.Connection]:
        """Context manager for write operations. Transaction-wrapped."""
        conn = self._connection()
        with self._write_lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                if conn.in_transaction:
                    conn.commit()
            except sqlite3.Error as e:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                except sqlite3.Error:
                    pass
                raise DatabaseError(f"Write failed: {e}") from e
            except Exception:
                try:
                    if conn.in_transaction:
                        conn.rollback()
                except sqlite3.Error:
                    pass
                raise

    # ─── Tracks CRUD ────────────────────────────────────────────────

    def upsert_track(self, track: TrackRecord) -> int:
        """
        Insert or update a track. Returns the track's id.
        Uses file_path as the natural key — moving a file and re-adding
        produces a new row (correct — the old path is gone).
        """
        now = _utc_now_iso()
        if track.date_added is None:
            track.date_added = now
        track.date_modified = now

        tags_json = json.dumps(track.tags or [], ensure_ascii=False)

        # Derive decade from year if caller didn't
        if track.year is not None and track.decade is None:
            track.decade = (int(track.year) // 10) * 10

        params = {
            "file_path": track.file_path,
            "file_size_bytes": track.file_size_bytes,
            "checksum_sha256": track.checksum_sha256,
            "artist": track.artist,
            "title": track.title,
            "album": track.album,
            "genre": track.genre,
            "style": track.style,
            "country": track.country,
            "year": track.year,
            "decade": track.decade,
            "duration_seconds": track.duration_seconds,
            "bpm": track.bpm,
            "bpm_confidence": track.bpm_confidence,
            "musical_key": track.musical_key,
            "camelot_key": track.camelot_key,
            "key_confidence": track.key_confidence,
            "artwork_embedded": int(track.artwork_embedded),
            "stems_separated": int(track.stems_separated),
            "stems_path": track.stems_path,
            "source_url": track.source_url,
            "source_platform": track.source_platform,
            "discogs_master_id": track.discogs_master_id,
            "discogs_release_id": track.discogs_release_id,
            "date_added": track.date_added,
            "date_modified": track.date_modified,
            "rating": track.rating,
            "notes": track.notes,
            "tags": tags_json,
        }

        sql = """
            INSERT INTO tracks (
                file_path, file_size_bytes, checksum_sha256, artist, title,
                album, genre, style, country, year, decade, duration_seconds,
                bpm, bpm_confidence, musical_key, camelot_key, key_confidence,
                artwork_embedded, stems_separated, stems_path, source_url,
                source_platform, discogs_master_id, discogs_release_id,
                date_added, date_modified, rating, notes, tags
            ) VALUES (
                :file_path, :file_size_bytes, :checksum_sha256, :artist, :title,
                :album, :genre, :style, :country, :year, :decade, :duration_seconds,
                :bpm, :bpm_confidence, :musical_key, :camelot_key, :key_confidence,
                :artwork_embedded, :stems_separated, :stems_path, :source_url,
                :source_platform, :discogs_master_id, :discogs_release_id,
                :date_added, :date_modified, :rating, :notes, :tags
            )
            ON CONFLICT(file_path) DO UPDATE SET
                file_size_bytes   = excluded.file_size_bytes,
                checksum_sha256   = excluded.checksum_sha256,
                artist            = excluded.artist,
                title             = excluded.title,
                album             = excluded.album,
                genre             = excluded.genre,
                style             = excluded.style,
                country           = excluded.country,
                year              = excluded.year,
                decade            = excluded.decade,
                duration_seconds  = excluded.duration_seconds,
                bpm               = excluded.bpm,
                bpm_confidence    = excluded.bpm_confidence,
                musical_key       = excluded.musical_key,
                camelot_key       = excluded.camelot_key,
                key_confidence    = excluded.key_confidence,
                artwork_embedded  = excluded.artwork_embedded,
                stems_separated   = excluded.stems_separated,
                stems_path        = excluded.stems_path,
                source_url        = excluded.source_url,
                source_platform   = excluded.source_platform,
                discogs_master_id = excluded.discogs_master_id,
                discogs_release_id= excluded.discogs_release_id,
                date_modified     = excluded.date_modified,
                rating            = excluded.rating,
                notes             = excluded.notes,
                tags              = excluded.tags
        """
        with self._writing() as conn:
            cur = conn.execute(sql, params)
            if cur.lastrowid and cur.rowcount == 1:
                # True insert (INSERT, not ON CONFLICT update)
                track.id = cur.lastrowid
            else:
                row = conn.execute(
                    "SELECT id FROM tracks WHERE file_path=?",
                    (track.file_path,),
                ).fetchone()
                track.id = int(row["id"]) if row else None
        assert track.id is not None, "upsert should always yield an id"
        return track.id

    def get_track(self, track_id: int) -> TrackRecord:
        with self._reading() as conn:
            row = conn.execute(
                "SELECT * FROM tracks WHERE id=?",
                (track_id,),
            ).fetchone()
        if not row:
            raise RecordNotFoundError(f"No track with id={track_id}")
        return _row_to_track(row)

    def get_track_by_path(self, file_path: str) -> Optional[TrackRecord]:
        with self._reading() as conn:
            row = conn.execute(
                "SELECT * FROM tracks WHERE file_path=?",
                (file_path,),
            ).fetchone()
        return _row_to_track(row) if row else None

    def list_tracks(self, filt: TrackFilter) -> list[TrackRecord]:
        sql, params = self._build_list_query(filt)
        with self._reading() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_track(r) for r in rows]

    def count_tracks(self, filt: Optional[TrackFilter] = None) -> int:
        filt = filt or TrackFilter(limit=1)
        # Reuse query builder for WHERE consistency; swap SELECT / drop LIMIT
        sql, params = self._build_list_query(filt, count_only=True)
        with self._reading() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def delete_track(self, track_id: int) -> None:
        with self._writing() as conn:
            cur = conn.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            if cur.rowcount == 0:
                raise RecordNotFoundError(f"No track with id={track_id}")

    def set_track_stems(self, track_id: int, stems_path: str) -> None:
        """Mark a track's stem separation complete and store its folder."""
        with self._writing() as conn:
            cur = conn.execute(
                """
                UPDATE tracks
                   SET stems_separated=1, stems_path=?, date_modified=?
                 WHERE id=?
                """,
                (stems_path, _utc_now_iso(), track_id),
            )
            if cur.rowcount == 0:
                raise RecordNotFoundError(f"No track with id={track_id}")

    def set_track_rating(self, track_id: int, rating: Optional[int]) -> None:
        """Set a 0–5 star rating (None clears). Bumps date_modified."""
        if rating is not None:
            rating = max(0, min(5, int(rating)))
        with self._writing() as conn:
            conn.execute(
                "UPDATE tracks SET rating=?, date_modified=? WHERE id=?",
                (rating, _utc_now_iso(), track_id),
            )

    def set_track_annotations(
        self,
        track_id: int,
        *,
        notes: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> None:
        """Update notes and/or tags for a track. Bumps date_modified."""
        sets: list[str] = []
        params: list[Any] = []
        if notes is not None:
            sets.append("notes=?")
            params.append(notes)
        if tags is not None:
            cleaned = [t.strip() for t in tags if t and t.strip()]
            sets.append("tags=?")
            params.append(json.dumps(cleaned, ensure_ascii=False))
        if not sets:
            return
        sets.append("date_modified=?")
        params.append(_utc_now_iso())
        params.append(track_id)
        with self._writing() as conn:
            conn.execute(
                f"UPDATE tracks SET {', '.join(sets)} WHERE id=?", params
            )

    def list_distinct_tags(self) -> list[str]:
        """Union of all tags across the library (for tag-filter dropdowns)."""
        tags: set[str] = set()
        with self._reading() as conn:
            for row in conn.execute(
                "SELECT tags FROM tracks WHERE tags IS NOT NULL AND tags != ''"
            ):
                try:
                    parsed = json.loads(row["tags"])
                    if isinstance(parsed, list):
                        tags.update(str(t) for t in parsed if t)
                except (json.JSONDecodeError, TypeError):
                    continue
        return sorted(tags, key=str.lower)

    # ─── Crates / collections ───────────────────────────────────────

    def create_crate(self, name: str, description: Optional[str] = None) -> int:
        """Create a crate; returns its id. Returns existing id if name taken."""
        name = (name or "").strip()
        if not name:
            raise DatabaseError("Crate name cannot be empty.")
        with self._writing() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO crates(name, description, created_at) "
                    "VALUES(?,?,?)",
                    (name, description, _utc_now_iso()),
                )
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT id FROM crates WHERE name=?", (name,)
                ).fetchone()
                return int(row["id"])

    def list_crates(self) -> list[CrateRecord]:
        with self._reading() as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.name, c.description, c.created_at,
                       COUNT(ct.track_id) AS track_count
                  FROM crates c
                  LEFT JOIN crate_tracks ct ON ct.crate_id = c.id
                 GROUP BY c.id
                 ORDER BY c.name COLLATE NOCASE
                """
            ).fetchall()
        return [
            CrateRecord(
                id=r["id"], name=r["name"], description=r["description"],
                created_at=r["created_at"], track_count=r["track_count"] or 0,
            )
            for r in rows
        ]

    def add_tracks_to_crate(self, crate_id: int, track_ids: Iterable[int]) -> int:
        """Add tracks to a crate (idempotent). Returns count newly added."""
        now = _utc_now_iso()
        added = 0
        with self._writing() as conn:
            for tid in track_ids:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO crate_tracks(crate_id, track_id, "
                    "added_at) VALUES(?,?,?)",
                    (int(crate_id), int(tid), now),
                )
                added += cur.rowcount
        return added

    def remove_tracks_from_crate(
        self, crate_id: int, track_ids: Iterable[int]
    ) -> int:
        with self._writing() as conn:
            total = 0
            for tid in track_ids:
                cur = conn.execute(
                    "DELETE FROM crate_tracks WHERE crate_id=? AND track_id=?",
                    (int(crate_id), int(tid)),
                )
                total += cur.rowcount
        return total

    def delete_crate(self, crate_id: int) -> None:
        with self._writing() as conn:
            conn.execute("DELETE FROM crates WHERE id=?", (int(crate_id),))

    # ─── Duplicate detection ────────────────────────────────────────

    def find_duplicates(self) -> list[DuplicateGroup]:
        """
        Cluster likely-duplicate tracks. Two signals:
          1. Identical checksum_sha256 (byte-identical files).
          2. Same normalized artist+title (re-rips / alt uploads).
        Groups with a single member are omitted.
        """
        with self._reading() as conn:
            rows = conn.execute("SELECT * FROM tracks").fetchall()
        tracks = [_row_to_track(r) for r in rows]

        by_checksum: dict[str, list[TrackRecord]] = {}
        by_name: dict[str, list[TrackRecord]] = {}
        for tr in tracks:
            if tr.checksum_sha256:
                by_checksum.setdefault(tr.checksum_sha256, []).append(tr)
            key = f"{(tr.artist or '').strip().lower()}|{(tr.title or '').strip().lower()}"
            if key.strip("|"):
                by_name.setdefault(key, []).append(tr)

        groups: list[DuplicateGroup] = []
        seen_ids: set[int] = set()
        for checksum, members in by_checksum.items():
            if len(members) > 1:
                groups.append(DuplicateGroup(
                    key=checksum[:12], reason="checksum",
                    tracks=tuple(members)))
                seen_ids.update(m.id for m in members if m.id is not None)
        for key, members in by_name.items():
            if len(members) > 1:
                # Skip members already grouped by checksum.
                remaining = [m for m in members if m.id not in seen_ids]
                if len(remaining) > 1:
                    groups.append(DuplicateGroup(
                        key=key, reason="artist+title",
                        tracks=tuple(remaining)))
        return groups

    def _build_list_query(
        self,
        filt: TrackFilter,
        count_only: bool = False,
    ) -> tuple[str, list[Any]]:
        """Construct the parameterized SELECT. Whitelist-only column refs."""
        # FTS join only when a free-text query is present — saves a join
        # in the common "filter by genre only" case.
        clauses: list[str] = []
        params: list[Any] = []
        joins = ""

        if filt.query and filt.query.strip():
            joins = " JOIN tracks_fts ON tracks_fts.rowid = tracks.id "
            clauses.append("tracks_fts MATCH ?")
            params.append(_sanitize_fts_query(filt.query))

        if filt.genre:
            clauses.append("tracks.genre = ?")
            params.append(filt.genre)
        if filt.decade is not None:
            clauses.append("tracks.decade = ?")
            params.append(int(filt.decade))
        if filt.min_bpm is not None:
            clauses.append("tracks.bpm >= ?")
            params.append(float(filt.min_bpm))
        if filt.max_bpm is not None:
            clauses.append("tracks.bpm <= ?")
            params.append(float(filt.max_bpm))
        if filt.camelot_key:
            clauses.append("tracks.camelot_key = ?")
            params.append(filt.camelot_key)
        if filt.has_stems is not None:
            clauses.append("tracks.stems_separated = ?")
            params.append(1 if filt.has_stems else 0)
        if filt.min_rating is not None:
            clauses.append("tracks.rating >= ?")
            params.append(int(filt.min_rating))
        if filt.tag:
            # tags is a JSON array of strings; match the quoted token so
            # "jazz" doesn't match "jazzy". Cheap and index-free but fine
            # for the library sizes this app targets.
            clauses.append("tracks.tags LIKE ?")
            params.append(f'%"{filt.tag}"%')
        if filt.crate_id is not None:
            joins += (
                " JOIN crate_tracks ON crate_tracks.track_id = tracks.id "
                "AND crate_tracks.crate_id = ? "
            )
            # crate param must precede any WHERE params bound after joins;
            # insert it at the front of the params list to match SQL order.
            params.insert(0, int(filt.crate_id))

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        if count_only:
            return (f"SELECT COUNT(*) FROM tracks{joins}{where}", params)

        order_col = (
            filt.order_by if filt.order_by in self._ORDERABLE_COLUMNS else "date_added"
        )
        direction = "DESC" if filt.order_desc else "ASC"

        sql = (
            f"SELECT tracks.* FROM tracks{joins}{where} "
            f"ORDER BY tracks.{order_col} {direction}, tracks.id {direction} "
            f"LIMIT ? OFFSET ?"
        )
        params.extend([int(filt.limit), int(filt.offset)])
        return sql, params

    # ─── Queue job CRUD ─────────────────────────────────────────────

    def create_queue_job(self, job: QueueJobRecord) -> int:
        now = _utc_now_iso()
        if job.created_at is None:
            job.created_at = now
        with self._writing() as conn:
            cur = conn.execute(
                """
                INSERT INTO queue_jobs (
                    source_url, display_name, status, progress_pct,
                    current_stage, error_message, track_id, enable_stems,
                    created_at, started_at, completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job.source_url,
                    job.display_name,
                    job.status,
                    job.progress_pct,
                    job.current_stage,
                    job.error_message,
                    job.track_id,
                    int(job.enable_stems),
                    job.created_at,
                    job.started_at,
                    job.completed_at,
                ),
            )
            job.id = cur.lastrowid
        return int(job.id)

    def update_queue_job(
        self,
        job_id: int,
        *,
        status: Optional[str] = None,
        progress_pct: Optional[float] = None,
        current_stage: Optional[str] = None,
        error_message: Optional[str] = None,
        track_id: Optional[int] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        for col, val in (
            ("status", status),
            ("progress_pct", progress_pct),
            ("current_stage", current_stage),
            ("error_message", error_message),
            ("track_id", track_id),
            ("started_at", started_at),
            ("completed_at", completed_at),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if not sets:
            return
        params.append(job_id)
        with self._writing() as conn:
            conn.execute(
                f"UPDATE queue_jobs SET {', '.join(sets)} WHERE id=?",
                params,
            )

    def list_queue_jobs(
        self,
        statuses: Optional[Iterable[str]] = None,
        limit: int = 100,
    ) -> list[QueueJobRecord]:
        with self._reading() as conn:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = conn.execute(
                    f"SELECT * FROM queue_jobs WHERE status IN ({placeholders}) "
                    f"ORDER BY created_at DESC LIMIT ?",
                    (*statuses, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM queue_jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [_row_to_queue_job(r) for r in rows]

    def reset_stuck_jobs(self) -> int:
        """
        Called at app startup. Any job in an 'in-flight' status is
        stuck — the worker that owned it died. Mark as failed so the
        UI can show it clearly and the user can retry.
        """
        in_flight = ("downloading", "analyzing", "tagging", "separating_stems")
        with self._writing() as conn:
            cur = conn.execute(
                f"""
                UPDATE queue_jobs
                   SET status='failed',
                       error_message='Interrupted by app shutdown',
                       completed_at=?
                 WHERE status IN ({",".join("?" for _ in in_flight)})
                """,
                (_utc_now_iso(), *in_flight),
            )
            return cur.rowcount

    # ─── Discovery ──────────────────────────────────────────────────

    def record_discovery(self, rec: DiscoveryRecord) -> int:
        """
        Insert a discovery-history row. If the master_id already exists,
        return its id without modifying — prevents the Dig button from
        silently re-suggesting the same master.
        """
        if rec.suggested_at is None:
            rec.suggested_at = _utc_now_iso()
        if rec.year and rec.decade is None:
            rec.decade = (int(rec.year) // 10) * 10

        with self._writing() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO discovery_history (
                        discogs_master_id, discogs_release_id, artist, title,
                        year, decade, country, genre, style,
                        suggested_at, was_queued, track_id
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        rec.discogs_master_id,
                        rec.discogs_release_id,
                        rec.artist,
                        rec.title,
                        rec.year,
                        rec.decade,
                        rec.country,
                        rec.genre,
                        rec.style,
                        rec.suggested_at,
                        int(rec.was_queued),
                        rec.track_id,
                    ),
                )
                rec.id = cur.lastrowid
                return int(rec.id)
            except sqlite3.IntegrityError:
                # Already exists — look up the existing id.
                row = conn.execute(
                    "SELECT id FROM discovery_history WHERE discogs_master_id=?",
                    (rec.discogs_master_id,),
                ).fetchone()
                rec.id = int(row["id"])
                return rec.id

    def is_already_suggested(self, master_id: int) -> bool:
        with self._reading() as conn:
            row = conn.execute(
                "SELECT 1 FROM discovery_history WHERE discogs_master_id=? LIMIT 1",
                (master_id,),
            ).fetchone()
        return row is not None

    def list_recent_discoveries(self, limit: int = 100) -> list[DiscoveryRecord]:
        """Most-recent discovery-history rows for the 'Recent digs' browser."""
        with self._reading() as conn:
            rows = conn.execute(
                "SELECT * FROM discovery_history "
                "ORDER BY suggested_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [_row_to_discovery(r) for r in rows]

    def mark_discovery_queued(
        self,
        master_id: int,
        track_id: Optional[int] = None,
    ) -> None:
        with self._writing() as conn:
            conn.execute(
                """UPDATE discovery_history
                     SET was_queued=1, track_id=COALESCE(?, track_id)
                   WHERE discogs_master_id=?""",
                (track_id, master_id),
            )

    # ─── Exports ────────────────────────────────────────────────────

    def record_export(self, exp: ExportRecord) -> int:
        if exp.exported_at is None:
            exp.exported_at = _utc_now_iso()
        with self._writing() as conn:
            cur = conn.execute(
                """
                INSERT INTO mpc_exports (
                    track_id, destination_path, destination_device,
                    wav_size_bytes, exported_at
                ) VALUES (?,?,?,?,?)
                """,
                (
                    exp.track_id,
                    exp.destination_path,
                    exp.destination_device,
                    exp.wav_size_bytes,
                    exp.exported_at,
                ),
            )
            exp.id = cur.lastrowid

            # Denormalize last_exported_at and bump counter on the track row.
            conn.execute(
                """UPDATE tracks
                     SET last_exported_at=?, export_count = export_count + 1
                   WHERE id=?""",
                (exp.exported_at, exp.track_id),
            )
        return int(exp.id)

    # ─── Maintenance ────────────────────────────────────────────────

    def reconcile(self) -> dict[str, int]:
        """
        Verify every track row's file still exists on disk. Returns
        {'total': N, 'missing': M}. UI surfaces missing count and lets
        the user choose whether to delete stale rows.

        Intentionally read-only — removing rows is the user's decision.
        """
        total = 0
        missing = 0
        with self._reading() as conn:
            for row in conn.execute("SELECT id, file_path FROM tracks"):
                total += 1
                if not Path(row["file_path"]).exists():
                    missing += 1
                    self._log.warning(
                        "Stale track row: id=%d path=%s",
                        row["id"],
                        row["file_path"],
                    )
        return {"total": total, "missing": missing}

    def vacuum(self) -> None:
        """Compact the DB. Safe to run periodically — not on every shutdown."""
        # VACUUM must run outside a transaction.
        conn = self._connection()
        with self._write_lock:
            conn.execute("VACUUM")

    def checkpoint(self, mode: str = "PASSIVE") -> None:
        """Checkpoint the WAL into the main DB file."""
        conn = self._connection()
        conn.execute(f"PRAGMA wal_checkpoint({mode})")

    # ─── Metadata ───────────────────────────────────────────────────

    def get_meta(self, key: str) -> Optional[str]:
        with self._reading() as conn:
            row = conn.execute(
                "SELECT value FROM app_metadata WHERE key=?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._writing() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES(?,?)",
                (key, value),
            )


# ─── Row → dataclass helpers ─────────────────────────────────────────


def _row_to_track(row: sqlite3.Row) -> TrackRecord:
    tags_raw = row["tags"]
    try:
        tags = json.loads(tags_raw) if tags_raw else []
        if not isinstance(tags, list):
            tags = []
    except (json.JSONDecodeError, TypeError):
        tags = []

    return TrackRecord(
        id=row["id"],
        file_path=row["file_path"],
        file_size_bytes=row["file_size_bytes"],
        checksum_sha256=row["checksum_sha256"],
        artist=row["artist"],
        title=row["title"],
        album=row["album"],
        genre=row["genre"],
        style=row["style"],
        country=row["country"],
        year=row["year"],
        decade=row["decade"],
        duration_seconds=row["duration_seconds"],
        bpm=row["bpm"],
        bpm_confidence=row["bpm_confidence"],
        musical_key=row["musical_key"],
        camelot_key=row["camelot_key"],
        key_confidence=row["key_confidence"],
        artwork_embedded=bool(row["artwork_embedded"]),
        stems_separated=bool(row["stems_separated"]),
        stems_path=row["stems_path"],
        source_url=row["source_url"],
        source_platform=row["source_platform"],
        discogs_master_id=row["discogs_master_id"],
        discogs_release_id=row["discogs_release_id"],
        date_added=row["date_added"],
        date_modified=row["date_modified"],
        last_exported_at=row["last_exported_at"],
        export_count=row["export_count"] or 0,
        rating=row["rating"],
        notes=row["notes"],
        tags=tags,
    )


def _row_to_discovery(row: sqlite3.Row) -> DiscoveryRecord:
    return DiscoveryRecord(
        id=row["id"],
        discogs_master_id=row["discogs_master_id"],
        discogs_release_id=row["discogs_release_id"],
        artist=row["artist"],
        title=row["title"],
        year=row["year"],
        decade=row["decade"],
        country=row["country"],
        genre=row["genre"],
        style=row["style"],
        suggested_at=row["suggested_at"],
        was_queued=bool(row["was_queued"]),
        track_id=row["track_id"],
    )


def _row_to_queue_job(row: sqlite3.Row) -> QueueJobRecord:
    return QueueJobRecord(
        id=row["id"],
        source_url=row["source_url"],
        display_name=row["display_name"],
        status=row["status"],
        progress_pct=row["progress_pct"] or 0.0,
        current_stage=row["current_stage"],
        error_message=row["error_message"],
        track_id=row["track_id"],
        enable_stems=bool(row["enable_stems"]),
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


# ─── Utilities ───────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# FTS5 reserves a handful of operator characters. Stripping them makes
# naïve user input safe for MATCH without surprising users with "syntax
# error near ..." messages from SQLite when they type 'AC/DC' etc.
_FTS_RESERVED = str.maketrans({c: " " for c in '"*:^()'})


def _sanitize_fts_query(q: str) -> str:
    """
    Escape user-supplied search text for FTS5 MATCH. Quotes each token
    so SQLite treats operators as literals, and trailing-asterisks the
    last token for prefix search (what users expect from a search bar).
    """
    cleaned = q.translate(_FTS_RESERVED).strip()
    if not cleaned:
        return '""'
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return '""'
    # Quote each token; add prefix match on the last for live-search UX.
    quoted = [f'"{t}"' for t in tokens[:-1]]
    quoted.append(f'"{tokens[-1]}"*')
    return " ".join(quoted)
