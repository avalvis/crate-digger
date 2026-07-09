"""
utils/config.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Configuration Manager

Persistent user preferences in JSON, with schema validation via
pydantic. Written atomically (via tmp + rename) so a crash mid-save
never leaves a half-written config file on disk.

Values that qualify as "sensitive" (currently: Discogs API token) are
obfuscated at rest using the OS keyring when available, with a plain
JSON fallback behind a warning log when the keyring is absent — e.g.
headless Linux. This is not security-grade secrecy; it's the bar for
"user's token isn't sitting in plaintext in a file a backup tool
might scoop up by accident."

Design contract:
  • Import-safe: constructing ConfigManager doesn't read the file.
    `load()` is explicit.
  • Defaults exist for every field, so a corrupted or missing config
    never blocks app boot.
  • Schema migration: every save stamps the schema version; a future
    field rename handles old configs gracefully.
  • Thread-safe: an RLock serializes reads and writes so the Settings
    tab can save from the Tk thread while the pipeline reads
    independently.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

# Schema version written into every saved config. Bump when you rename
# or remove a field; migrations branch on the stored value.
CONFIG_SCHEMA_VERSION = 1


# ─── Sensitive-value storage via OS keyring ──────────────────────────

# Keyring is optional; keeping the import lazy means headless environments
# without dbus/libsecret (CI, minimal Docker images) don't break app boot.
_KEYRING_SERVICE = "com.crate-digger.app"
_KEYRING_USER_DISCOGS_TOKEN = "discogs_token"
_KEYRING_USER_DEEPSEEK_KEY  = "deepseek_key"


def _keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        import keyring.errors  # noqa: F401

        return True
    except Exception:
        return False


def _keyring_get(user: str) -> Optional[str]:
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, user)
    except Exception:
        return None


def _keyring_set(user: str, value: str) -> bool:
    try:
        import keyring

        keyring.set_password(_KEYRING_SERVICE, user, value)
        return True
    except Exception:
        return False


def _keyring_delete(user: str) -> None:
    try:
        import keyring

        keyring.delete_password(_KEYRING_SERVICE, user)
    except Exception:
        pass


# ─── Schema ──────────────────────────────────────────────────────────


class GeneralConfig(BaseModel):
    """Top-level app preferences."""

    vault_root: str = Field(
        default_factory=lambda: str(Path.home() / "Music" / "CrateDigger_Vault"),
        description="Filesystem root of the Vault tree.",
    )
    staging_root: str = Field(
        default_factory=lambda: str(Path.home() / ".cratedigger" / "staging"),
        description="Scratch directory for in-progress pipeline jobs.",
    )
    concurrent_workers: int = Field(
        default=2,
        ge=1,
        le=8,
        description="Number of simultaneous pipeline jobs.",
    )
    enable_stems_by_default: bool = Field(
        default=False,
        description="Default state of the stems toggle on Manual Rip.",
    )
    use_ai_metadata: bool = Field(
        default=True,
        description=(
            "Use AI (DeepSeek) to extract artist/title from YouTube video "
            "titles when the track has no structured metadata."
        ),
    )
    vault_folder_scheme: str = Field(
        default="genre/bpm_key_artist_title",
        description=(
            "Folder-structure convention used when filing new tracks in the Vault. "
            "One of the keys listed in utils.paths.VAULT_FOLDER_SCHEMES."
        ),
    )
    has_deepseek_key: bool = Field(
        default=False,
        description="Whether a DeepSeek API key is stored in the OS keyring or plaintext fallback.",
    )
    mpc_samples_root: str = Field(
        default_factory=lambda: str(Path.home() / "Music" / "CrateDigger_MPC"),
        description=(
            "Destination root for the Digital Crate 'MPC Workflow' button — "
            "typically an MPC SD card path. Tracks sent here are split into "
            "stems and organized as <root>/<Artist - Title>/<stem>.wav, "
            "bypassing the Vault entirely."
        ),
    )
    mpc_export_max_concurrent: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Simultaneous MPC export jobs (demucs is CPU-heavy).",
    )


class DownloaderConfig(BaseModel):
    """yt-dlp / downloader preferences."""

    retries: int = Field(default=5, ge=0, le=20)
    fragment_retries: int = Field(default=5, ge=0, le=20)
    concurrent_fragments: int = Field(default=4, ge=1, le=16)


class StemsConfig(BaseModel):
    """Stem separation preferences."""

    model: str = Field(
        default="htdemucs_ft",
        description="Default demucs model.",
    )
    device: str = Field(
        default="auto",
        description="'auto' | 'cpu' | 'cuda' | 'mps'",
    )


class DiscoveryConfig(BaseModel):
    """Discogs + YTM discovery preferences. Token is NOT stored here."""

    # Reference to the keyring entry rather than the value itself. When
    # `True`, the loader fetches the token from the OS keyring.
    # `False` means the user hasn't entered one yet.
    has_token: bool = False
    default_min_have: int = Field(default=10, ge=1)
    # Ceiling on Discogs "have" count — records more common than this are
    # excluded as too mainstream to be a "gem". Discogs have/want counts
    # don't track pure chart popularity, but a record with thousands of
    # documented copies in circulation is definitionally not obscure.
    max_have: int = Field(default=3000, ge=1)

    # How many gems a single Dig surfaces into the reel.
    reel_size: int = Field(default=8, ge=1, le=24)

    # Tilt discovery toward sample-friendly genres/styles/eras. This only
    # re-weights the ranking — nothing is ever hard-excluded, so the
    # roulette can still surface anything.
    prioritize_samples: bool = True
    # 0.0 = pure Discogs desirability (want/have); 1.0 = strongly favor
    # sample-friendly genres/eras. 0.6 is a balanced default.
    sample_weight_intensity: float = Field(default=0.6, ge=0.0, le=1.0)

    # Include "Various Artists" compilations. Many breaks live on comps,
    # but they resolve poorly on YouTube Music, so it's off by default.
    allow_compilations: bool = False

    # Post-dig preview warmup (Digital Crate).
    preview_prefetch_enabled: bool = True
    preview_prefetch_concurrency: int = Field(default=2, ge=1, le=4)
    preview_prefetch_keep_decoded: bool = True


class UIConfig(BaseModel):
    """UI-state preferences."""

    window_width: int = Field(default=1280, ge=1120)
    window_height: int = Field(default=820, ge=720)
    window_maximized: bool = True
    last_active_tab: str = "manual_rip"
    # Playback volume for the in-app preview players (0.0..1.0).
    preview_volume: float = Field(default=0.85, ge=0.0, le=1.0)
    # Space for future vault-tab column sort/filter persistence.


class ExportConfig(BaseModel):
    """MPC / WAV export defaults."""

    sample_rate: int = Field(default=44100, ge=8000, le=192000)
    bit_depth: int = Field(default=16, ge=16, le=24)
    # Only 16 and 24 are supported by the exporter; validated at use time.


class AppConfig(BaseModel):
    """Root config schema."""

    schema_version: int = CONFIG_SCHEMA_VERSION
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    downloader: DownloaderConfig = Field(default_factory=DownloaderConfig)
    stems: StemsConfig = Field(default_factory=StemsConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    ui: UIConfig = Field(default_factory=UIConfig)


# ─── Public exceptions ───────────────────────────────────────────────


class ConfigError(Exception):
    """Base class for config failures."""


class ConfigLoadError(ConfigError):
    """Could not read or parse the config file."""


class ConfigSaveError(ConfigError):
    """Could not write the config file."""


# ─── Manager ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class ConfigSnapshot:
    """
    An immutable-feeling view of config at a moment in time. Tabs read
    this; they call `ConfigManager.update(...)` to mutate. Never mutate
    the snapshot directly — always go through the manager.
    """

    config: AppConfig
    discogs_token: Optional[str]   # materialized from keyring; None if unset
    deepseek_key: Optional[str]    # materialized from keyring; None if unset
    keyring_available: bool        # used by Settings UI to warn user


class ConfigManager:
    """
    Reads, writes, and protects user preferences. One instance per app.
    """

    def __init__(
        self,
        config_path: Path,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._path = Path(config_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._log = logger or logging.getLogger("cratedigger.config")

        self._lock = threading.RLock()
        self._config: AppConfig = AppConfig()  # defaults until load()
        self._token: Optional[str] = None
        self._deepseek_key: Optional[str] = None
        self._loaded = False

    @property
    def data_dir(self) -> Path:
        """Directory that holds config.json, vault.db, and cratedigger.log."""
        return self._path.parent

    # ── Lifecycle ──

    def load(self) -> ConfigSnapshot:
        """
        Read config from disk. If missing/corrupt, fall back to defaults
        and emit a warning. Always succeeds.
        """
        with self._lock:
            if self._path.exists():
                try:
                    raw = self._path.read_text(encoding="utf-8")
                    data = json.loads(raw) if raw.strip() else {}
                    self._config = self._parse_and_migrate(data)
                    self._log.info(
                        "Loaded config from %s (schema v%d)",
                        self._path,
                        self._config.schema_version,
                    )
                except (OSError, json.JSONDecodeError) as e:
                    self._log.warning(
                        "Could not read config (%s); using defaults. "
                        "Corrupted file moved to %s.bak",
                        e,
                        self._path,
                    )
                    self._quarantine_corrupt_file()
                    self._config = AppConfig()
                except ValidationError as e:
                    self._log.warning(
                        "Config failed schema validation (%s); using defaults.",
                        e,
                    )
                    self._quarantine_corrupt_file()
                    self._config = AppConfig()
            else:
                self._log.info(
                    "No config at %s — starting with defaults.",
                    self._path,
                )
                self._config = AppConfig()

            # Materialize credentials from the keyring (or leave None).
            self._token = self._load_token()
            self._deepseek_key = self._load_deepseek_key()
            self._loaded = True

            return self._snapshot()

    def snapshot(self) -> ConfigSnapshot:
        """Return the current state without re-reading from disk."""
        with self._lock:
            if not self._loaded:
                return self.load()
            return self._snapshot()

    def config_path(self) -> Path:
        """Absolute path to the JSON config file on disk."""
        return self._path

    # ── Mutation API ──

    def update_general(self, **fields: Any) -> ConfigSnapshot:
        return self._update_section("general", GeneralConfig, fields)

    def update_downloader(self, **fields: Any) -> ConfigSnapshot:
        return self._update_section("downloader", DownloaderConfig, fields)

    def update_stems(self, **fields: Any) -> ConfigSnapshot:
        return self._update_section("stems", StemsConfig, fields)

    def update_discovery(self, **fields: Any) -> ConfigSnapshot:
        return self._update_section("discovery", DiscoveryConfig, fields)

    def update_export(self, **fields: Any) -> ConfigSnapshot:
        return self._update_section("export", ExportConfig, fields)

    def update_ui(self, **fields: Any) -> ConfigSnapshot:
        return self._update_section("ui", UIConfig, fields)

    def set_discogs_token(self, token: Optional[str]) -> ConfigSnapshot:
        """
        Store or clear the Discogs API token. When `token` is a non-empty
        string, attempts keyring storage; on failure falls back to a
        warning log and stores in the JSON config with a 'keyring
        unavailable' note.
        """
        with self._lock:
            token = (token or "").strip() or None
            if token is None:
                _keyring_delete(_KEYRING_USER_DISCOGS_TOKEN)
                self._token = None
                self._config = self._config.model_copy(
                    update={
                        "discovery": self._config.discovery.model_copy(
                            update={"has_token": False},
                        ),
                    }
                )
                self._save()
                return self._snapshot()

            stored_in_keyring = _keyring_set(_KEYRING_USER_DISCOGS_TOKEN, token)
            if not stored_in_keyring:
                self._log.warning(
                    "OS keyring unavailable — storing Discogs token in "
                    "plaintext config. See docs for secrets-manager setup.",
                )
                # Fall back to config.json storage in a dedicated field
                # (not via AppConfig schema — manually written).
                self._fallback_write_plaintext_token(token)

            self._token = token
            self._config = self._config.model_copy(
                update={
                    "discovery": self._config.discovery.model_copy(
                        update={"has_token": True},
                    ),
                }
            )
            self._save()
            return self._snapshot()

    def set_deepseek_key(self, key: Optional[str]) -> ConfigSnapshot:
        """Store or clear the DeepSeek API key via keyring / plaintext fallback."""
        with self._lock:
            key = (key or "").strip() or None
            if key is None:
                _keyring_delete(_KEYRING_USER_DEEPSEEK_KEY)
                self._deepseek_key = None
                self._config = self._config.model_copy(
                    update={
                        "general": self._config.general.model_copy(
                            update={"has_deepseek_key": False},
                        ),
                    }
                )
                self._save()
                return self._snapshot()

            stored_in_keyring = _keyring_set(_KEYRING_USER_DEEPSEEK_KEY, key)
            if not stored_in_keyring:
                self._log.warning(
                    "OS keyring unavailable — storing DeepSeek key in "
                    "plaintext fallback.",
                )
                self._fallback_write_deepseek_key(key)

            self._deepseek_key = key
            self._config = self._config.model_copy(
                update={
                    "general": self._config.general.model_copy(
                        update={"has_deepseek_key": True},
                    ),
                }
            )
            self._save()
            return self._snapshot()

    # ── Internals ──

    def _update_section(
        self,
        section: str,
        model_cls: type[BaseModel],
        fields: dict[str, Any],
    ) -> ConfigSnapshot:
        """
        Merge `fields` into the named section, validate the result, save,
        and return a fresh snapshot. Invalid fields raise ValidationError.
        """
        with self._lock:
            current = getattr(self._config, section)
            try:
                merged = current.model_copy(update=fields)
                # Re-validate by round-tripping through the model. Ensures
                # defaults kick in and constraint checks (ge/le) fire.
                merged = model_cls.model_validate(merged.model_dump())
            except ValidationError as e:
                raise ConfigError(f"Invalid {section} config: {e}") from e
            self._config = self._config.model_copy(update={section: merged})
            self._save()
            return self._snapshot()

    def _snapshot(self) -> ConfigSnapshot:
        return ConfigSnapshot(
            config=self._config,
            discogs_token=self._token,
            deepseek_key=self._deepseek_key,
            keyring_available=_keyring_available(),
        )

    def _load_token(self) -> Optional[str]:
        """Try keyring first, then the plaintext fallback."""
        if self._config.discovery.has_token:
            token = _keyring_get(_KEYRING_USER_DISCOGS_TOKEN)
            if token:
                return token
            return self._fallback_read_plaintext_token()
        return None

    def _load_deepseek_key(self) -> Optional[str]:
        if self._config.general.has_deepseek_key:
            key = _keyring_get(_KEYRING_USER_DEEPSEEK_KEY)
            if key:
                return key
            return self._fallback_read_deepseek_key()
        return None

    # ── Atomic save ──

    def _save(self) -> None:
        """Write current config atomically. Holds the lock already."""
        try:
            payload = self._config.model_dump(mode="json")
            payload["schema_version"] = CONFIG_SCHEMA_VERSION

            # Write to a tmp sibling, fsync, then replace. This is the
            # same atomicity pattern the exporter uses for .partial WAVs.
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".config_",
                suffix=".tmp",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, str(self._path))
            finally:
                if Path(tmp_path).exists():
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        except OSError as e:
            raise ConfigSaveError(f"Could not save config: {e}") from e

    # ── Corrupt-file handling ──

    def _quarantine_corrupt_file(self) -> None:
        """Move a corrupt config aside so next save starts clean."""
        try:
            if self._path.exists():
                shutil.copy2(self._path, self._path.with_suffix(".json.bak"))
        except OSError:
            pass

    # ── Schema migration ──

    def _parse_and_migrate(self, data: dict[str, Any]) -> AppConfig:
        """
        Parse a raw dict into AppConfig, running migrations between
        schema versions if needed.
        """
        version = int(data.get("schema_version", 1))

        if version > CONFIG_SCHEMA_VERSION:
            raise ConfigLoadError(
                f"Config schema v{version} is newer than app's "
                f"v{CONFIG_SCHEMA_VERSION}. Upgrade the app.",
            )

        # Placeholder for future migrations:
        # if version < 2: data = self._migrate_v1_to_v2(data)
        # if version < 3: data = self._migrate_v2_to_v3(data)

        data["schema_version"] = CONFIG_SCHEMA_VERSION
        return AppConfig.model_validate(data)

    # ── Plaintext token fallback (no-keyring environments) ──

    def _fallback_write_plaintext_token(self, token: str) -> None:
        """
        When keyring is unavailable, stash the token in a sibling
        file (not the main config.json) so it's at least isolated.
        Still plaintext — we log a warning and document this clearly.
        """
        path = self._path.parent / ".discogs_token"
        try:
            path.write_text(token, encoding="utf-8")
            try:
                # POSIX: restrict to owner-only read/write (0600)
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as e:
            self._log.error("Could not write plaintext token fallback: %s", e)

    def _fallback_read_plaintext_token(self) -> Optional[str]:
        path = self._path.parent / ".discogs_token"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _fallback_write_deepseek_key(self, key: str) -> None:
        path = self._path.parent / ".deepseek_key"
        try:
            path.write_text(key, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as e:
            self._log.error("Could not write DeepSeek key fallback: %s", e)

    def _fallback_read_deepseek_key(self) -> Optional[str]:
        path = self._path.parent / ".deepseek_key"
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None
