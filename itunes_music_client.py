"""iTunes release discovery through the public Search and Lookup APIs.

The iTunes API needs no credentials, so this module holds no OAuth code at all.
A ``429`` from Apple is a per-IP request rate limit rather than a hard quota, so
it is retried with a bounded backoff instead of pausing the scan.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional

import requests
from requests import Response
from requests.exceptions import RequestException

LOGGER = logging.getLogger(__name__)

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
PLATFORM_ITUNES = "itunes"
ITUNES_PREFIX = "it_"

TRANSIENT_HTTP_STATUSES = {403, 408, 425, 429, 500, 502, 503, 504}
STATUS_KEYS = ("status", "log", "details")


class MusicServiceError(RuntimeError):
    """A service failed in a way that should not abort the scan."""


def strip_prefix(raw_id: Any) -> str:
    """Return the bare iTunes artist ID with any prefix removed."""
    value = str(raw_id or "").strip()
    if value.startswith(ITUNES_PREFIX):
        return value[len(ITUNES_PREFIX) :]
    return value


class iTunesMusicEngine:
    """Discover recently released albums, singles and features on iTunes."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        recent_days: int = 5,
        max_retries: int = 3,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            }
        )
        self.timeout = timeout
        self.recent_days = max(1, int(recent_days))
        self.max_retries = max(1, int(max_retries))
        self._artist_cache: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------
    # General helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_html(text: str) -> str:
        """Return a compact, safe-to-log summary of an HTML/error response."""
        without_tags = re.sub(r"<[^>]+>", " ", text or "")
        return " ".join(without_tags.split())[:180]

    @staticmethod
    def _normalise_text(value: Any) -> str:
        """Normalise text for equality comparisons without relying on substrings."""
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(char for char in text if not unicodedata.combining(char))
        return re.sub(r"[^a-z0-9]+", "", text.casefold())

    @staticmethod
    def _clean_display_title(title: Any) -> str:
        """Drop the store's collection-type suffix from a shown title."""
        return (
            re.sub(
                r"\s*[-–—]\s*(?:single|ep|album)\s*$", "", str(title or ""), flags=re.I
            ).strip()
            or str(title or "").strip()
        )

    @classmethod
    def _normalise_title(cls, title: Any) -> str:
        """Normalise harmless store-specific title decorations for deduplication."""
        text = str(title or "")
        text = re.sub(
            r"\s*[\(\[\{][^\]\)\}]*\b(?:feat(?:uring)?|ft\.)\b[^\]\)\}]*[\]\)\}]",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*[-–—]\s*(?:single|ep)\s*$", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\s*[\(\[\{]\s*(?:deluxe|expanded|anniversary|remaster(?:ed)?|"
            r"special)\s+(?:edition|version)?\s*[\]\)\}]\s*$",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return cls._normalise_text(text)

    @staticmethod
    def _parse_release_date(value: Any) -> Optional[date]:
        """Parse only day-precision dates, which are required by the lookback rule."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str):
            return None
        match = re.match(r"^(\d{4}-\d{2}-\d{2})", value.strip())
        if not match:
            return None
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None

    def _is_recent(self, release_date: Any) -> bool:
        parsed = self._parse_release_date(release_date)
        if not parsed:
            return False
        today = date.today()
        return today - timedelta(days=self.recent_days) <= parsed <= today

    @staticmethod
    def _format_date(value: Any) -> Optional[str]:
        parsed = iTunesMusicEngine._parse_release_date(value)
        return parsed.isoformat() if parsed else None

    @staticmethod
    def _error_from_response(response: Response) -> str:
        body = iTunesMusicEngine._clean_html(response.text)
        return f"HTTP {response.status_code}" + (f": {body}" if body else "")

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Response:
        """Make a bounded request, retrying transient failures and rate limits."""
        last_error: Optional[str] = None
        request_kwargs = dict(kwargs)
        request_kwargs.setdefault("timeout", self.timeout)
        request_kwargs.setdefault("params", params)

        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, **request_kwargs)
            except RequestException as exc:
                last_error = f"connection error: {str(exc)[:160]}"
            else:
                if response.status_code == 200:
                    return response
                last_error = self._error_from_response(response)
                if response.status_code not in TRANSIENT_HTTP_STATUSES:
                    break

                if response.status_code == 429:
                    if attempt == self.max_retries - 1:
                        break
                    try:
                        wait = min(float(response.headers.get("Retry-After", 0)), 30.0)
                    except (TypeError, ValueError):
                        wait = 5.0 * (attempt + 1)
                    LOGGER.info(
                        "iTunes rate limit hit; pausing %.1fs before retry %d",
                        wait,
                        attempt + 2,
                    )
                    time.sleep(wait)
                    continue

                retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        min(float(retry_after), 10.0)
                        if retry_after
                        else 0.5 * (2**attempt)
                    )
                except ValueError:
                    delay = 0.5 * (2**attempt)
                if response.status_code == 403:
                    delay = max(delay, 2.0 * (2**attempt))
                time.sleep(min(delay, 15.0))
                continue

            if attempt < self.max_retries - 1:
                time.sleep(0.5 * (2**attempt))

        raise MusicServiceError(last_error or "request failed without an error message")

    @staticmethod
    def _json(response: Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise MusicServiceError("iTunes returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise MusicServiceError("iTunes returned an unexpected JSON payload")
        return payload

    # ------------------------------------------------------------------
    # Strict false-positive filtering and deduplication
    # ------------------------------------------------------------------
    @staticmethod
    def _looks_like_non_release(title: str) -> bool:
        """Reject catalogue entries that are not the artist's own new material.

        Third-party versions of a record — remixes, mashups, bootlegs, reworks,
        sped-up/slowed copies — carry the original artist's name but are not a new
        release, so they are dropped by title. Sets, compilations and live audio
        are dropped for the same reason.
        """
        folded = str(title or "").casefold()
        patterns = (
            # Third-party versions and re-uploads.
            r"\bremix(?:ed)?\b",
            r"\bremixes\b",
            r"\bmashup\b",
            r"\bbootleg\b",
            r"\brework(?:ed)?\b",
            r"\bdub\b",
            r"\bvip\b",
            r"\bedit(?:ed)?\b",
            r"\bflips?\b",
            r"\b(?:slowed|sped\s*up|reverb|8d|bass[ _-]?boosted)\b",
            r"\btype\s*beat\b",
            r"\bcover(?:ed)?\b",
            r"\bremaster(?:ed)?\b",
            # Sets, compilations and live audio.
            r"\bvarious artists\b",
            r"\bcompilation\b",
            r"\bcontinuous\s+(?:mix|set)\b",
            r"\b(?:total|mega|non[ _-]?stop)\s+mix\b",
            r"\bdj\s*(?:mix|set)\b",
            r"\b(?:mix|set)\s+by\s+dj\b",
            r"\b(?:live|concert)\s+(?:at|from|in)\b",
            r"\blive\s+(?:set|session|performance)\b",
            r"\bsoundcheck\b",
            r"\bkaraoke\b",
            r"\btribute\b",
            r"\binstrumental\b",
        )
        return any(re.search(pattern, folded) for pattern in patterns)

    # Credits arrive as display strings, so "A, B & C", "A feat. B" and
    # "A x B" all have to be split before the tracked artist can be located.
    CREDIT_SPLIT_PATTERN = re.compile(
        r"\s*(?:,|;|&|/|\+|\bx\b|\band\b|\bfeat(?:uring)?\.?\b|\bft\.?\b|\bwith\b"
        r"|\bvs\.?\b|\bversus\b)\s*",
        flags=re.IGNORECASE,
    )

    # iTunes reports only the lead act on a track, so guests are recovered from a
    # "(feat. ...)" suffix on the track or collection title.
    FEAT_SUFFIX_PATTERN = re.compile(
        r"[\(\[\{]\s*(?:feat(?:uring)?|ft)\.?\s*([^)\]\}]+)[\)\]\}]",
        flags=re.IGNORECASE,
    )

    @classmethod
    def _credit_names(cls, credited_artists: Iterable[Any]) -> list[str]:
        """Split raw credit strings into individual, de-duplicated artist names."""
        names: list[str] = []
        for credit in credited_artists:
            raw = str(credit or "").strip()
            if not raw:
                continue
            for part in cls.CREDIT_SPLIT_PATTERN.split(raw):
                name = part.strip(" \t-–—")
                if name and name not in names:
                    names.append(name)
        return names

    @classmethod
    def _feat_credit_names(cls, title: Any) -> list[str]:
        """Return the guests named in a ``(feat. X)`` style title suffix."""
        names: list[str] = []
        for match in cls.FEAT_SUFFIX_PATTERN.finditer(str(title or "")):
            for part in cls.CREDIT_SPLIT_PATTERN.split(match.group(1)):
                name = part.strip(" \t-–—")
                if name and name not in names:
                    names.append(name)
        return names

    @classmethod
    def _credit_index(cls, artist_name: str, credits: list[str]) -> Optional[int]:
        """Return where the tracked artist sits in the credit list, or ``None``.

        The position decides how the release is reported: index 0 is the act's
        own record, anything later is a feature on someone else's.
        """
        target = cls._normalise_text(artist_name)
        if not target:
            return None
        for index, credit in enumerate(credits):
            if cls._normalise_text(credit) == target:
                return index
        return None

    @staticmethod
    def _is_various_artists(credits: Iterable[str]) -> bool:
        """A store 'Various Artists' credit can never be a real collaboration."""
        return any(
            iTunesMusicEngine._normalise_text(credit).startswith("variousartists")
            for credit in credits
        )

    def _make_release(
        self,
        *,
        source_id: str,
        artist_name: str,
        credited_artists: Iterable[Any],
        name: Any,
        release_type: Any,
        release_date: Any,
        url: Any,
    ) -> Optional[dict[str, Any]]:
        """Build one alert record, or ``None`` when the entry must be dropped."""
        title = str(name or "").strip()
        release_day = self._format_date(release_date)
        kind = str(release_type or "").casefold()
        credits = self._credit_names(credited_artists)
        credit_index = self._credit_index(artist_name, credits)

        if (
            not source_id
            or not title
            or not release_day
            or not self._is_recent(release_day)
            or credit_index is None
            or self._is_various_artists(credits)
            or self._looks_like_non_release(title)
        ):
            return None

        clean_title = self._normalise_title(title)
        if not clean_title:
            return None

        # The first credit is the act the release is sold as; every later credit
        # is a guest, which for the tracked artist means a feature appearance.
        is_feature = credit_index > 0
        primary_artist = credits[0] if credits else artist_name
        others = list(credits[1:])

        # A deterministic content identity prevents duplicates on future rescans.
        material = (
            f"{self._normalise_text(primary_artist)}|{clean_title}|{release_day}"
        ).encode("utf-8")
        dedup_key = "release_" + hashlib.sha256(material).hexdigest()[:24]
        return {
            "id": dedup_key,
            "dedup_key": dedup_key,
            "source_ids": [source_id],
            "platforms": [PLATFORM_ITUNES],
            "name": self._clean_display_title(title),
            "type": "feature" if is_feature else (
                "album" if kind == "album" else "single"
            ),
            "release_date": release_day,
            "url": str(url or ""),
            "artist_name": primary_artist,
            "credited_artists": others,
            "is_feature": is_feature,
        }

    def _merge_releases(
        self, releases: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Collapse same-title/same-date records into one alert."""
        merged: dict[str, dict[str, Any]] = {}
        for release in releases:
            key = str(release["dedup_key"])
            existing = merged.get(key)
            if not existing:
                merged[key] = dict(release)
                continue
            for source_id in release.get("source_ids", []):
                if source_id not in existing["source_ids"]:
                    existing["source_ids"].append(source_id)
            if release.get("type") == "album":
                existing["type"] = "album"

        return sorted(
            merged.values(),
            key=lambda release: (release["release_date"], release["name"].casefold()),
            reverse=True,
        )

    # ------------------------------------------------------------------
    # Public, unauthenticated iTunes endpoints
    # ------------------------------------------------------------------
    @property
    def itunes_country(self) -> str:
        return os.getenv("ITUNES_COUNTRY", "US").strip() or "US"

    def _get(self, url: str, *, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """Call an iTunes endpoint with the shared retry/backoff policy."""
        request_params = dict(params or {})
        request_params.setdefault("country", self.itunes_country)
        response = self._request("GET", url, params=request_params)
        return self._json(response)

    @staticmethod
    def _id_from_query(query: str) -> Optional[str]:
        """Extract an iTunes/Apple Music artist ID from a pasted URL."""
        text = str(query or "")
        if not re.search(r"(?:music\.apple\.com|itunes\.apple\.com)/", text, re.I):
            return None
        # Apple URLs end with /artist/<slug>/<numericId>, sometimes followed by
        # a track query such as ?i=12345 which must be ignored.
        match = re.search(r"/artist/[^/?#]+/(\d+)", text, re.I)
        return match.group(1) if match else None

    def _itunes_artist(self, artist_id: str) -> tuple[str, str]:
        """Resolve an iTunes artist ID to ``(id, name)`` via the lookup API."""
        raw_id = strip_prefix(artist_id)
        if not raw_id:
            raise MusicServiceError("An iTunes artist ID is required")
        cache_key = f"it|{raw_id}"
        cached = self._artist_cache.get(cache_key)
        if cached:
            return cached

        payload = self._get(
            ITUNES_LOOKUP_URL,
            params={"id": raw_id, "entity": "musicArtist"},
        )
        results = payload.get("results", [])
        if not isinstance(results, list) or not results:
            raise MusicServiceError("iTunes could not resolve that artist")
        first = results[0] if isinstance(results[0], dict) else {}
        resolved_id = str(first.get("artistId") or raw_id)
        resolved_name = str(first.get("artistName") or "").strip()
        if not resolved_name:
            raise MusicServiceError("iTunes returned no name for that artist")
        self._artist_cache[cache_key] = (resolved_id, resolved_name)
        return resolved_id, resolved_name

    def search_itunes_artist(
        self, query: str
    ) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Search iTunes for an artist by name. Returns ``(artist, error)``."""
        term = str(query or "").strip()
        if not term:
            return None, "Empty query"
        try:
            payload = self._get(
                ITUNES_SEARCH_URL,
                params={"term": term, "entity": "musicArtist", "limit": 25},
            )
        except MusicServiceError as exc:
            LOGGER.info("iTunes artist search failed for %r: %s", term, exc)
            return None, str(exc)

        results = payload.get("results", [])
        if not isinstance(results, list):
            return None, "iTunes returned an unexpected payload"

        artists = [r for r in results if isinstance(r, dict) and r.get("artistId")]
        if not artists:
            return None, "No matching artist found on iTunes"
        target = self._normalise_text(term)
        exact = next(
            (
                item
                for item in artists
                if self._normalise_text(item.get("artistName")) == target
            ),
            None,
        )
        item = exact or artists[0]
        return (
            {
                "id": str(item.get("artistId")),
                "name": str(item.get("artistName") or "").strip(),
                "genre": str(item.get("primaryGenreName") or ""),
            },
            None,
        )

    def _album_releases(
        self, itunes_id: str, artist_name: str
    ) -> list[dict[str, Any]]:
        """Return the artist's own albums, EPs and singles released recently.

        Only collections owned by the artist are taken from this pass; a guest
        appearance is a track on somebody else's collection and is reported by
        ``_song_releases`` instead, which knows the real credit order.
        """
        try:
            payload = self._get(
                ITUNES_LOOKUP_URL,
                params={
                    "id": itunes_id,
                    "entity": "album",
                    "limit": 200,
                    "sort": "recent",
                },
            )
        except MusicServiceError as exc:
            LOGGER.warning("iTunes album scan failed for %s: %s", artist_name, exc)
            return []

        results = payload.get("results", [])
        if not isinstance(results, list):
            return []

        releases: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict) or not item.get("collectionId"):
                continue
            if str(item.get("artistId") or "") != itunes_id:
                continue
            collection_type = str(item.get("collectionType") or "").casefold()
            if collection_type not in {"album", "single", "ep"}:
                continue
            collection_name = item.get("collectionName")
            release = self._make_release(
                source_id=f"{ITUNES_PREFIX}{item.get('collectionId', '')}",
                artist_name=artist_name,
                credited_artists=[
                    str(item.get("artistName") or ""),
                    *self._feat_credit_names(collection_name),
                ],
                name=collection_name,
                release_type=collection_type,
                release_date=item.get("releaseDate"),
                url=item.get("collectionViewUrl", ""),
            )
            if release:
                releases.append(release)
        return releases

    def _song_releases(
        self, itunes_id: str, artist_name: str
    ) -> list[dict[str, Any]]:
        """Return recent tracks where the artist is a guest on someone else's record.

        Apple's lookup returns the track's lead act as ``artistName``, so a track
        whose lead act is not the tracked artist is a feature appearance and is
        reported as one.
        """
        try:
            payload = self._get(
                ITUNES_LOOKUP_URL,
                params={
                    "id": itunes_id,
                    "entity": "song",
                    "limit": 200,
                    "sort": "recent",
                },
            )
        except MusicServiceError as exc:
            LOGGER.warning("iTunes song scan failed for %s: %s", artist_name, exc)
            return []

        results = payload.get("results", [])
        if not isinstance(results, list):
            return []

        releases: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict) or not item.get("trackId"):
                continue
            release = self._make_release(
                source_id=f"{ITUNES_PREFIX}{item.get('trackId', '')}",
                artist_name=artist_name,
                credited_artists=[
                    str(item.get("artistName") or ""),
                    *self._feat_credit_names(item.get("trackName")),
                ],
                name=item.get("trackName"),
                release_type="single",
                release_date=item.get("releaseDate"),
                url=item.get("trackViewUrl", ""),
            )
            if release and release["is_feature"]:
                releases.append(release)
        return releases

    def _releases(self, artist_id: str) -> list[dict[str, Any]]:
        """Return recent own collections plus recent guest appearances."""
        raw_id = strip_prefix(artist_id)
        if not raw_id:
            return []
        itunes_id, artist_name = self._itunes_artist(raw_id)
        return self._album_releases(itunes_id, artist_name) + self._song_releases(
            itunes_id, artist_name
        )

    # ------------------------------------------------------------------
    # Public API used by itunes_bot.py
    # ------------------------------------------------------------------
    @staticmethod
    def detect_platform(query: str) -> Optional[str]:
        """Return ``'itunes'`` when the query is an Apple/iTunes link."""
        text = str(query or "")
        if re.search(r"(?:music\.apple\.com|itunes\.apple\.com)/", text, re.I):
            return PLATFORM_ITUNES
        return None

    def get_artist_info(self, query: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        """Resolve an artist on iTunes. Returns ``(artist_dict, error)``."""
        query = str(query or "").strip()
        if not query:
            return None, "Empty query"
        itunes_id = self._id_from_query(query)
        if itunes_id:
            try:
                resolved_id, name = self._itunes_artist(itunes_id)
                return {"id": resolved_id, "name": name}, None
            except MusicServiceError as exc:
                return None, str(exc)
        return self.search_itunes_artist(query)

    def check_itunes_releases(
        self, artist_id: str, artist_name: str = ""
    ) -> list[dict[str, Any]]:
        """Return recent iTunes releases for an artist, newest first."""
        if not artist_id:
            return []
        try:
            return self._merge_releases(self._releases(artist_id))
        except MusicServiceError as exc:
            LOGGER.warning(
                "iTunes release scan failed for %s: %s",
                artist_name or artist_id,
                exc,
            )
            return []
        except Exception:
            LOGGER.exception(
                "Unexpected iTunes release scan failure for %s",
                artist_name or artist_id,
            )
            return []

    @staticmethod
    def _status(status: str, message: str) -> dict[str, str]:
        """Return the exact schema consumed by itunes_bot.py's /status formatter."""
        return {"status": status, "log": message, "details": message}

    def get_status_report(self) -> dict[str, dict[str, str]]:
        """Report whether the public iTunes API is reachable."""
        try:
            self._get(
                ITUNES_SEARCH_URL,
                params={"term": "test", "entity": "musicArtist", "limit": 1},
            )
        except MusicServiceError as exc:
            report = self._status("ERROR", str(exc))
        else:
            report = self._status(
                "ONLINE", "Public iTunes API reachable (no authentication required)"
            )
        return {
            PLATFORM_ITUNES: {key: str(report.get(key, "")) for key in STATUS_KEYS}
        }