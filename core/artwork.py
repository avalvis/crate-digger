"""
core/artwork.py
──────────────────────────────────────────────────────────────────────
Crate Digger — Artwork Fetcher & Processor

Fetches the highest-resolution YouTube thumbnail, normalizes it to a
perfect 1:1 square, and returns an in-memory JPEG buffer ready for
`core.metadata` to embed into the .m4a `covr` atom.

Design contract:
  • Never raise unhandled exceptions to the caller — always either
    return bytes or raise a typed ArtworkError subclass.
  • Never transcode or resize *up*. Center-crop preserves pixels;
    scaling down is only done when the source exceeds the embed cap.
  • Pure in-memory pipeline. No temp files, no disk writes. The
    pipeline orchestrator decides whether to persist the bytes.
  • Handles every edge case the YouTube thumbnail API throws at us:
    black bars, letterboxing, already-square maxres, RGBA PNGs,
    palette images, broken CDN responses, truncated streams.

This module has zero ties to `ui/` and zero awareness of yt-dlp —
it takes a URL (or raw bytes) and returns bytes. That's it.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional

import requests
from PIL import Image, ImageFile, UnidentifiedImageError

# YouTube's CDN occasionally serves thumbnails with a trailing byte
# that Pillow considers "truncated." Rather than fail on a perfectly
# good 99.9%-complete JPEG, we let Pillow finish the decode. This is
# the standard Pillow idiom for dealing with real-world web imagery.
ImageFile.LOAD_TRUNCATED_IMAGES = True


# ─── Public types ────────────────────────────────────────────────────

@dataclass(slots=True, frozen=True)
class ArtworkResult:
    """
    Everything `core.metadata` needs to embed the cover. `data` is a
    JPEG byte buffer — always JPEG, always square, always <= max_size
    on each side.
    """
    data: bytes
    width: int
    height: int
    source_url: Optional[str]       # None if built from raw input bytes
    was_cropped: bool               # True if source wasn't already square
    was_downscaled: bool            # True if source exceeded max_size


# ─── Public exceptions ───────────────────────────────────────────────

class ArtworkError(Exception):
    """Base class for all errors surfaced by the artwork module."""


class ArtworkFetchError(ArtworkError):
    """HTTP/network failure fetching the thumbnail URL."""


class ArtworkDecodeError(ArtworkError):
    """Bytes were fetched but could not be decoded as an image."""


class ArtworkProcessingError(ArtworkError):
    """Image decoded but processing (crop/convert/encode) failed."""


# ─── Tunables ────────────────────────────────────────────────────────

# YouTube's documented thumbnail ladder, from best to worst. `maxresdefault`
# is only populated for videos above a minimum source resolution; the
# others are always available. We try them in order when a high-res URL
# 404s (common for older uploads / unlisted videos).
_YT_THUMB_LADDER: tuple[str, ...] = (
    "maxresdefault.jpg",
    "sddefault.jpg",
    "hqdefault.jpg",
    "mqdefault.jpg",
    "default.jpg",
)

# Cover art spec sanity caps. Apple MP4 `covr` atoms have no strict
# size limit but embedders commonly choke past ~5MB, and DJ software
# (Serato/Rekordbox) benefits from sensible dimensions. 1400px is the
# de-facto standard (matches Apple Music cover spec).
_MAX_EMBED_DIMENSION = 1400
_MIN_SENSIBLE_DIMENSION = 200   # below this the image is useless as cover art
_JPEG_QUALITY = 92              # visually transparent; keeps files <1MB

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_REQUEST_TIMEOUT = (5, 15)      # (connect, read) seconds
_MAX_RESPONSE_BYTES = 25 * 1024 * 1024   # 25MB hard cap — defensive


# ─── Public API ──────────────────────────────────────────────────────

class ArtworkProcessor:
    """
    Facade over requests + Pillow. Stateless aside from the injected
    logger and HTTP session. Safe to share across worker threads.
    """

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        session: Optional[requests.Session] = None,
        *,
        max_dimension: int = _MAX_EMBED_DIMENSION,
        jpeg_quality: int = _JPEG_QUALITY,
    ) -> None:
        self._log = logger or logging.getLogger("cratedigger.artwork")
        self._session = session or self._build_session()
        self._max_dim = max_dimension
        self._quality = jpeg_quality

    # ── Primary entry points ──

    def fetch_and_process(self, thumbnail_url: str) -> ArtworkResult:
        """
        Download `thumbnail_url` and return a square JPEG ready for
        embedding. If the URL 404s and it's a recognizable YouTube
        thumbnail URL, fall back through the quality ladder.
        """
        raw = self._fetch_with_fallback(thumbnail_url)
        result = self._process(raw, source_url=thumbnail_url)
        return result

    def process_bytes(self, raw: bytes) -> ArtworkResult:
        """
        Process already-fetched image bytes. Used when the caller has
        their own image source (e.g. a user-provided file in a future
        'override artwork' feature).
        """
        return self._process(raw, source_url=None)

    # ── HTTP ──

    @staticmethod
    def _build_session() -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "image/avif,image/webp,image/jpeg,image/png,*/*;q=0.8",
        })
        return s

    def _fetch_with_fallback(self, url: str) -> bytes:
        """
        Fetch `url`. If it 404s and looks like a YouTube thumbnail URL,
        walk down the quality ladder until something responds.
        """
        try:
            return self._fetch_one(url)
        except ArtworkFetchError as primary_err:
            ladder = _derive_yt_fallback_urls(url)
            if not ladder:
                raise
            self._log.warning(
                "Primary thumbnail failed (%s); trying %d fallback(s).",
                primary_err, len(ladder),
            )
            last_err: Exception = primary_err
            for candidate in ladder:
                try:
                    data = self._fetch_one(candidate)
                    self._log.info("Thumbnail fallback succeeded: %s", candidate)
                    return data
                except ArtworkFetchError as e:
                    last_err = e
                    continue
            raise ArtworkFetchError(
                f"All thumbnail candidates failed. Last error: {last_err}"
            ) from last_err

    def _fetch_one(self, url: str) -> bytes:
        try:
            resp = self._session.get(
                url, timeout=_REQUEST_TIMEOUT, stream=True, allow_redirects=True,
            )
        except requests.RequestException as e:
            raise ArtworkFetchError(f"Network error fetching {url}: {e}") from e

        try:
            if resp.status_code >= 400:
                raise ArtworkFetchError(
                    f"HTTP {resp.status_code} fetching {url}"
                )

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                # Not fatal on its own — some CDNs mislabel — but worth logging.
                self._log.debug(
                    "Unexpected Content-Type %r for %s", content_type, url,
                )

            # Bounded read prevents a malicious/misconfigured CDN from
            # streaming gigabytes into memory.
            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > _MAX_RESPONSE_BYTES:
                    raise ArtworkFetchError(
                        f"Response exceeded {_MAX_RESPONSE_BYTES} bytes; aborting."
                    )

            if not buf:
                raise ArtworkFetchError(f"Empty response body from {url}")
            return bytes(buf)
        finally:
            resp.close()

    # ── Image processing ──

    def _process(self, raw: bytes, *, source_url: Optional[str]) -> ArtworkResult:
        """Decode → validate → normalize mode → square-crop → downscale → encode JPEG."""
        # Decode
        try:
            img = Image.open(io.BytesIO(raw))
            img.load()                          # force full decode now, not lazily
        except UnidentifiedImageError as e:
            raise ArtworkDecodeError(f"Bytes are not a recognizable image: {e}") from e
        except (OSError, ValueError) as e:
            raise ArtworkDecodeError(f"Image decode failed: {e}") from e

        try:
            # Apply EXIF orientation before measuring dimensions —
            # otherwise a portrait-rotated source crops off the wrong axis.
            img = _apply_exif_transpose(img)

            w, h = img.size
            if w <= 0 or h <= 0:
                raise ArtworkProcessingError(
                    f"Degenerate image dimensions: {w}x{h}"
                )
            if min(w, h) < _MIN_SENSIBLE_DIMENSION:
                # Not fatal — we'll still embed it — but flag it. A 120×90
                # thumbnail beats no thumbnail, even if it looks soft.
                self._log.warning(
                    "Low-resolution artwork (%dx%d); embedding anyway.", w, h,
                )

            img = _normalize_mode(img)

            cropped, was_cropped = _square_center_crop(img)

            # Downscale only — never upscale. Upscaling pads bytes without
            # adding detail and violates the 'lossless passthrough' spirit.
            was_downscaled = False
            side = cropped.size[0]              # square, so width == height
            if side > self._max_dim:
                cropped = cropped.resize(
                    (self._max_dim, self._max_dim),
                    resample=Image.Resampling.LANCZOS,
                )
                was_downscaled = True

            out = _encode_jpeg(cropped, quality=self._quality)

            self._log.debug(
                "Artwork processed: src=%dx%d → %dx%d (cropped=%s downscaled=%s) %d bytes",
                w, h, cropped.size[0], cropped.size[1],
                was_cropped, was_downscaled, len(out),
            )
            return ArtworkResult(
                data=out,
                width=cropped.size[0],
                height=cropped.size[1],
                source_url=source_url,
                was_cropped=was_cropped,
                was_downscaled=was_downscaled,
            )
        except ArtworkError:
            raise
        except Exception as e:
            # Defensive catch-all — spec says this module must never
            # leak raw tracebacks to the caller.
            raise ArtworkProcessingError(f"Unexpected processing error: {e}") from e
        finally:
            try:
                img.close()
            except Exception:
                pass


# ─── Helpers (pure functions, unit-test friendly) ────────────────────

def _apply_exif_transpose(img: Image.Image) -> Image.Image:
    """Respect EXIF Orientation tag; no-op if absent or malformed."""
    try:
        from PIL import ImageOps
        return ImageOps.exif_transpose(img) or img
    except Exception:
        # EXIF data can be arbitrarily broken in the wild. A failed
        # transpose is never a reason to fail the whole pipeline.
        return img


def _normalize_mode(img: Image.Image) -> Image.Image:
    """
    Convert any color mode to RGB for JPEG encoding:
      • RGBA / LA / P with transparency → composite onto black background
        (DJ software shows cover art on dark surfaces; black looks better
        than white for the typical album-cover aesthetic).
      • P (palette) without alpha → direct convert to RGB.
      • L / 1 (grayscale / bitmap) → convert to RGB.
      • CMYK → convert to RGB.
      • RGB → pass through.
    """
    mode = img.mode

    if mode == "RGB":
        return img

    if mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (0, 0, 0))
        # img.split()[-1] is the alpha channel for both RGBA and LA
        alpha = img.split()[-1]
        rgb = img.convert("RGB")
        background.paste(rgb, mask=alpha)
        return background

    if mode == "P":
        # Palette image — check for transparency in the palette.
        if "transparency" in img.info:
            return _normalize_mode(img.convert("RGBA"))
        return img.convert("RGB")

    # L, 1, CMYK, I, F, YCbCr, etc. — straight conversion is safe.
    return img.convert("RGB")


def _square_center_crop(img: Image.Image) -> tuple[Image.Image, bool]:
    """
    Return (cropped, was_cropped). If the image is already square
    (within 1px tolerance to absorb odd-dimension sources), pass
    it through unchanged.
    """
    w, h = img.size
    if abs(w - h) <= 1:
        # Already square (or off-by-one due to odd source). Skip crop
        # entirely to preserve every pixel and avoid re-encode artifacts.
        return img, False

    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    right = left + side
    bottom = top + side

    # Pillow's .crop() returns a view by default; calling .copy() forces
    # a materialized image so subsequent operations don't trigger lazy
    # re-decodes against a closed file handle.
    cropped = img.crop((left, top, right, bottom)).copy()
    return cropped, True


def _encode_jpeg(img: Image.Image, *, quality: int) -> bytes:
    """Encode as baseline JPEG with broad decoder compatibility."""
    buf = io.BytesIO()
    try:
        img.save(
            buf,
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=False,   # baseline JPEG — widest decoder support
            subsampling=2,       # 4:2:0 — standard for photographic content
        )
    except OSError as e:
        raise ArtworkProcessingError(f"JPEG encode failed: {e}") from e
    return buf.getvalue()


def _derive_yt_fallback_urls(url: str) -> list[str]:
    """
    Given a YouTube thumbnail URL, return a fallback list walking down
    the quality ladder from *below* the current level. Returns [] if
    the URL isn't a recognizable YouTube thumbnail.

    Example: passing an i.ytimg.com ".../maxresdefault.jpg" returns
    ['sddefault.jpg', 'hqdefault.jpg', 'mqdefault.jpg', 'default.jpg']
    URLs at the same path. Non-YouTube URLs return [].
    """
    if "ytimg.com" not in url and "youtube.com" not in url:
        return []

    # Find which rung of the ladder this URL is on.
    current_index: Optional[int] = None
    for i, fname in enumerate(_YT_THUMB_LADDER):
        if fname in url:
            current_index = i
            break
    if current_index is None:
        return []

    # Also try the .webp variants that YouTube's newer CDN serves —
    # some videos only have webp available at the higher rungs.
    candidates: list[str] = []
    for fname in _YT_THUMB_LADDER[current_index + 1:]:
        candidates.append(url.replace(_YT_THUMB_LADDER[current_index], fname))

    return candidates