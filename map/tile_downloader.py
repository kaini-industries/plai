#!/usr/bin/env python3
"""Planning, sources, validation, and packaging for Plai offline maps.

The public OpenStreetMap, CARTO, and OpenTopoMap tile services are deliberately
not sources in this module: their public endpoints do not authorize building
offline tile packs.  Callers must use a local/licensed raster endpoint, an
authorized raster MBTiles archive, or the Protomaps workflow in
``protomaps_backend``.
"""

from __future__ import annotations

import email.utils
import hashlib
import http.client
import io
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol, Sequence


WEB_MERCATOR_MAX_LAT = 85.05112878
FIRMWARE_MIN_ZOOM = 1
FIRMWARE_MAX_ZOOM = 15
FIRMWARE_STYLES = ("osm", "dark", "voyager", "topo")
TILE_SIZE = 256
DEFAULT_USER_AGENT = (
    "Plai-Tile-Packager/2.0 (+https://github.com/kaini-industries/plai)"
)
DEFAULT_ATTRIBUTION = "Map data © OpenStreetMap contributors"
DEFAULT_TERMS_URL = "https://www.openstreetmap.org/copyright"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ESTIMATED_JPEG_BYTES = 12 * 1024

_ENV_PLACEHOLDER = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_TILE_TEMP_FILE = re.compile(
    r"^\.\d+\.jpg\.[A-Za-z0-9_-]+\.part$", re.IGNORECASE
)
_KNOWN_PUBLIC_TILE_HOSTS = (
    "tile.openstreetmap.org",
    "basemaps.cartocdn.com",
    "tile.opentopomap.org",
)


class TilePackError(RuntimeError):
    """Base exception for a tile-pack job."""


class ConfigurationError(TilePackError):
    """The requested job is invalid or unsafe."""


class PolicyError(ConfigurationError):
    """The requested endpoint is known not to permit this workflow."""


class TileSourceError(TilePackError):
    """A source could not provide a usable tile."""


class TileMissingError(TileSourceError):
    """A requested coordinate is absent from the selected source."""


class AccessBlockedError(TileSourceError):
    """Authentication or provider policy rejected the request."""


def _require_pillow():
    try:
        from PIL import Image, ImageEnhance, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - exercised in a subprocess test
        raise ConfigurationError(
            "Pillow is required for firmware JPEG output; install "
            "map/requirements.txt"
        ) from exc
    return Image, ImageEnhance, UnidentifiedImageError


def normalize_longitude(lon: float) -> float:
    """Normalize longitude to [-180, 180), retaining -180 at the boundary."""
    normalized = (lon + 180.0) % 360.0 - 180.0
    if math.isclose(normalized, 180.0):
        return -180.0
    return normalized


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Return the XYZ tile containing a Web-Mercator coordinate."""
    lat = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))
    lon = max(-180.0, min(180.0, lon))
    n = 1 << zoom
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    if x == n:
        x = n - 1
    lat_rad = math.radians(lat)
    y_float = (
        1.0
        - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi
    ) / 2.0 * n
    y = int(math.floor(y_float))
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_to_latlon(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Return the latitude/longitude of an XYZ tile's north-west corner."""
    n = 1 << zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_km = 6371.0088
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlat = lat2_rad - lat1_rad
    dlon = math.radians(normalize_longitude(lon2 - lon1))
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * radius_km * math.asin(min(1.0, math.sqrt(a)))


def _unwrap_near(lon: float, reference: float) -> float:
    return reference + normalize_longitude(lon - reference)


def _tile_intersects_radius(
    tile: "TileCoord", center_lat: float, center_lon: float, radius_km: float
) -> bool:
    if radius_km == 0:
        return tile == TileCoord(tile.z, *latlon_to_tile(center_lat, center_lon, tile.z))

    north, west = tile_to_latlon(tile.x, tile.y, tile.z)
    south, east = tile_to_latlon(tile.x + 1, tile.y + 1, tile.z)
    west = _unwrap_near(west, center_lon)
    east = _unwrap_near(east, center_lon)
    if east <= west:
        east += 360.0
    center_unwrapped = _unwrap_near(center_lon, (west + east) / 2.0)
    nearest_lon = min(max(center_unwrapped, west), east)
    nearest_lat = min(max(center_lat, south), north)
    return _haversine_km(center_lat, center_lon, nearest_lat, nearest_lon) <= radius_km


def _radius_bounds(
    center_lat: float, center_lon: float, radius_km: float
) -> tuple[tuple[float, float, float, float], ...]:
    """Return one or two non-wrapping WGS84 boxes covering the radius."""
    # PMTiles bbox extracts require a non-degenerate area. A zero-radius plan
    # still means "the tile containing this point", so give the backend a tiny
    # box around the point instead of an invalid west==east/south==north bbox.
    minimum_delta = 1e-7 if radius_km == 0 else 0.0
    lat_delta = max(radius_km / 110.574, minimum_delta)
    south = max(-WEB_MERCATOR_MAX_LAT, center_lat - lat_delta)
    north = min(WEB_MERCATOR_MAX_LAT, center_lat + lat_delta)

    cos_lat = max(1e-9, abs(math.cos(math.radians(center_lat))))
    lon_delta = max(radius_km / (111.320 * cos_lat), minimum_delta)
    if lon_delta >= 180.0:
        return ((-180.0, south, 180.0, north),)

    west_raw = center_lon - lon_delta
    east_raw = center_lon + lon_delta
    if west_raw < -180.0:
        return (
            (-180.0, south, east_raw, north),
            (west_raw + 360.0, south, 180.0, north),
        )
    if east_raw > 180.0:
        return (
            (west_raw, south, 180.0, north),
            (-180.0, south, east_raw - 360.0, north),
        )
    return ((west_raw, south, east_raw, north),)


@dataclass(frozen=True, order=True)
class TileCoord:
    z: int
    x: int
    y: int


@dataclass(frozen=True)
class TilePlan:
    center_lat: float
    center_lon: float
    radius_km: float
    min_zoom: int
    global_zoom: int
    max_zoom: int
    regional_bounds: tuple[tuple[float, float, float, float], ...]
    tiles: tuple[TileCoord, ...]
    counts_by_zoom: Mapping[int, int]

    @property
    def total_tiles(self) -> int:
        return len(self.tiles)

    @property
    def estimated_bytes(self) -> int:
        return self.total_tiles * ESTIMATED_JPEG_BYTES

    def __iter__(self) -> Iterator[TileCoord]:
        return iter(self.tiles)

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": {"lat": self.center_lat, "lon": self.center_lon},
            "radius_km": self.radius_km,
            "min_zoom": self.min_zoom,
            "global_zoom": self.global_zoom,
            "max_zoom": self.max_zoom,
            "regional_bounds": [list(bounds) for bounds in self.regional_bounds],
            "counts_by_zoom": {
                str(zoom): count for zoom, count in self.counts_by_zoom.items()
            },
            "total_tiles": self.total_tiles,
        }


def _validate_plan_inputs(
    center_lat: float,
    center_lon: float,
    radius_km: float,
    min_zoom: int,
    global_zoom: int,
    max_zoom: int,
) -> None:
    values = (center_lat, center_lon, radius_km)
    if not all(math.isfinite(value) for value in values):
        raise ConfigurationError("latitude, longitude, and radius must be finite")
    if not -WEB_MERCATOR_MAX_LAT <= center_lat <= WEB_MERCATOR_MAX_LAT:
        raise ConfigurationError(
            f"latitude must be within ±{WEB_MERCATOR_MAX_LAT:.8f} for Web Mercator"
        )
    if not -180.0 <= center_lon <= 180.0:
        raise ConfigurationError("longitude must be between -180 and 180")
    if radius_km < 0:
        raise ConfigurationError("radius must be non-negative")
    if not FIRMWARE_MIN_ZOOM <= min_zoom <= FIRMWARE_MAX_ZOOM:
        raise ConfigurationError("min zoom must be between 1 and 15")
    if not FIRMWARE_MIN_ZOOM <= max_zoom <= FIRMWARE_MAX_ZOOM:
        raise ConfigurationError("max zoom must be between 1 and 15")
    if min_zoom > max_zoom:
        raise ConfigurationError("min zoom cannot exceed max zoom")
    if not 0 <= global_zoom <= FIRMWARE_MAX_ZOOM:
        raise ConfigurationError("global zoom must be between 0 and 15")


def build_tile_plan(
    center_lat: float,
    center_lon: float,
    radius_km: float,
    min_zoom: int = 2,
    global_zoom: int = 5,
    max_zoom: int = 12,
    *,
    hard_limit: int | None = None,
) -> TilePlan:
    """Build a deterministic, deduplicated tile plan before source access."""
    _validate_plan_inputs(
        center_lat, center_lon, radius_km, min_zoom, global_zoom, max_zoom
    )
    center_lon = normalize_longitude(center_lon)
    bounds = _radius_bounds(center_lat, center_lon, radius_km)
    tiles: set[TileCoord] = set()

    def add(tile: TileCoord) -> None:
        tiles.add(tile)
        if hard_limit is not None and len(tiles) > hard_limit:
            raise ConfigurationError(
                f"tile plan exceeds the hard limit of {hard_limit:,} tiles"
            )

    global_end = min(global_zoom, max_zoom)
    if min_zoom <= global_end:
        for zoom in range(min_zoom, global_end + 1):
            n = 1 << zoom
            for x in range(n):
                for y in range(n):
                    add(TileCoord(zoom, x, y))

    regional_start = max(min_zoom, global_zoom + 1)
    for zoom in range(regional_start, max_zoom + 1):
        n = 1 << zoom
        for west, south, east, north in bounds:
            x_min, y_max = latlon_to_tile(south, west, zoom)
            x_max, y_min = latlon_to_tile(north, east, zoom)
            if east >= 180.0:
                x_max = n - 1
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    tile = TileCoord(zoom, x % n, y)
                    if _tile_intersects_radius(
                        tile, center_lat, center_lon, radius_km
                    ):
                        add(tile)

    ordered = tuple(sorted(tiles))
    counts: dict[int, int] = {}
    for tile in ordered:
        counts[tile.z] = counts.get(tile.z, 0) + 1
    return TilePlan(
        center_lat=center_lat,
        center_lon=center_lon,
        radius_km=radius_km,
        min_zoom=min_zoom,
        global_zoom=global_zoom,
        max_zoom=max_zoom,
        regional_bounds=bounds,
        tiles=ordered,
        counts_by_zoom=counts,
    )


class TileSource(Protocol):
    name: str
    attribution: str
    terms_url: str

    def fetch(self, tile: TileCoord) -> bytes:
        ...

    def provenance(self) -> Mapping[str, Any]:
        ...

    def fingerprint_data(self) -> Mapping[str, Any]:
        ...

    def close(self) -> None:
        ...


def _host_is_known_public(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in _KNOWN_PUBLIC_TILE_HOSTS)


def validate_http_tile_template(url_template: str) -> None:
    """Validate an XYZ URL without opening it or expanding secret values."""
    for placeholder in ("{z}", "{x}", "{y}"):
        if placeholder not in url_template:
            raise ConfigurationError(
                f"tile URL must contain {placeholder}; got "
                f"{_redact_url_template(url_template)!r}"
            )
    parsed = urllib.parse.urlparse(url_template)
    if parsed.scheme not in ("http", "https"):
        raise ConfigurationError("tile URL must use http or https")
    if _host_is_known_public(parsed.hostname):
        raise PolicyError(
            f"{parsed.hostname} does not authorize offline tile packs; use "
            "Protomaps, raster MBTiles, or a licensed/self-hosted endpoint"
        )


def _redact_url_template(url_template: str) -> str:
    """Remove credentials, query values, and fragments from recorded URLs."""
    try:
        parsed = urllib.parse.urlsplit(url_template)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parsed.port
    except (TypeError, ValueError):
        return "<redacted-url>"
    netloc = hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    if parsed.username is not None or parsed.password is not None:
        netloc = f"<redacted>@{netloc}"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            "<redacted>" if parsed.query else "",
            "<redacted>" if parsed.fragment else "",
        )
    )


def _format_xyz_template(url_template: str, tile: "TileCoord") -> str:
    """Expand only XYZ fields, leaving any other literal braces untouched."""
    return (
        url_template.replace("{z}", str(tile.z))
        .replace("{x}", str(tile.x))
        .replace("{y}", str(tile.y))
    )


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _display_origin(url: str) -> str:
    scheme, host, port = _url_origin(url)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 443 if scheme == "https" else 80
    port_text = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{host}{port_text}"


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent credentials and policy checks from being bypassed by redirects."""

    def redirect_request(
        self,
        request,
        file_pointer,
        code,
        message,
        headers,
        new_url,
    ):
        if _url_origin(request.full_url) != _url_origin(new_url):
            raise AccessBlockedError(
                "source attempted a cross-origin redirect to "
                f"{_display_origin(new_url)}"
            )
        return super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )


_POLICY_HTTP_OPENER = urllib.request.build_opener(_SameOriginRedirectHandler())


def _open_with_policy_redirects(request, *, timeout: float):
    return _POLICY_HTTP_OPENER.open(request, timeout=timeout)


def _expand_env_template(
    value: str, environ: Mapping[str, str], *, url_encode: bool = False
) -> tuple[str, tuple[str, ...]]:
    names: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environ or not environ[name]:
            raise ConfigurationError(f"required environment variable {name} is unset")
        names.append(name)
        expanded = environ[name]
        return urllib.parse.quote(expanded, safe="") if url_encode else expanded

    return _ENV_PLACEHOLDER.sub(replace, value), tuple(names)


def _retry_after_seconds(value: str | None, now: Callable[[], float]) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpTileSource:
    """Authorized XYZ raster endpoint with bounded, policy-aware retries."""

    def __init__(
        self,
        url_template: str,
        *,
        name: str = "local-http",
        attribution: str,
        terms_url: str = "",
        headers: Mapping[str, str] | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 15.0,
        retries: int = 2,
        request_delay: float = 0.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        environ: Mapping[str, str] | None = None,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        wall_time: Callable[[], float] = time.time,
        extra_provenance: Mapping[str, Any] | None = None,
        fingerprint_override: Mapping[str, Any] | None = None,
    ) -> None:
        if not attribution.strip():
            raise ConfigurationError("an HTTP tile source requires attribution")
        validate_http_tile_template(url_template)
        if retries < 0:
            raise ConfigurationError("retries cannot be negative")
        if (
            not math.isfinite(timeout)
            or not math.isfinite(request_delay)
            or timeout <= 0
            or request_delay < 0
        ):
            raise ConfigurationError("timeout must be positive and delay non-negative")
        if max_response_bytes < 1:
            raise ConfigurationError("maximum response size must be positive")

        environment = os.environ if environ is None else environ
        request_configuration = {
            "url_template": url_template,
            "headers": sorted((str(name), str(value)) for name, value in (headers or {}).items()),
        }
        self._request_configuration_sha256 = hashlib.sha256(
            json.dumps(
                request_configuration, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        expanded_url, secret_names = _expand_env_template(
            url_template, environment, url_encode=True
        )
        # Re-check the concrete host after expansion so an environment
        # placeholder cannot bypass the public-provider policy guard.
        validate_http_tile_template(expanded_url)
        expanded_headers: dict[str, str] = {}
        header_secret_names: list[str] = []
        for header_name, header_value in (headers or {}).items():
            expanded, names = _expand_env_template(header_value, environment)
            expanded_headers[header_name] = expanded
            header_secret_names.extend(names)
        expanded_headers.setdefault("User-Agent", user_agent)
        expanded_headers.setdefault("Accept", "image/png,image/jpeg,image/webp,*/*;q=0.1")

        self.name = name
        self.attribution = attribution.strip()
        self.terms_url = terms_url.strip()
        self.url_template = expanded_url
        self.safe_url_template = _redact_url_template(url_template)
        self.headers = expanded_headers
        self.timeout = timeout
        self.retries = retries
        self.request_delay = request_delay
        self.max_response_bytes = max_response_bytes
        self._opener = opener or _open_with_policy_redirects
        self._sleep = sleeper
        self._monotonic = monotonic
        self._wall_time = wall_time
        self._last_request_at: float | None = None
        self.request_count = 0
        self._secret_env_names = tuple(sorted(set(secret_names + tuple(header_secret_names))))
        self._extra_provenance = dict(extra_provenance or {})
        self._fingerprint_override = (
            dict(fingerprint_override) if fingerprint_override is not None else None
        )

    def _rate_limit(self) -> None:
        if self.request_delay <= 0 or self._last_request_at is None:
            return
        remaining = self.request_delay - (self._monotonic() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def _request(self, url: str):
        self._rate_limit()
        request = urllib.request.Request(url, headers=self.headers)
        self.request_count += 1
        self._last_request_at = self._monotonic()
        return self._opener(request, timeout=self.timeout)

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        parsed = _retry_after_seconds(retry_after, self._wall_time)
        if parsed is not None:
            return min(parsed, 60.0)
        return min(2.0**attempt, 30.0)

    def fetch(self, tile: TileCoord) -> bytes:
        url = _format_xyz_template(self.url_template, tile)
        safe_url = _format_xyz_template(self.safe_url_template, tile)
        for attempt in range(self.retries + 1):
            try:
                with self._request(url) as response:
                    get_final_url = getattr(response, "geturl", None)
                    if callable(get_final_url):
                        final_url = str(get_final_url())
                        if _url_origin(url) != _url_origin(final_url):
                            raise AccessBlockedError(
                                "source response came from a different origin: "
                                f"{_display_origin(final_url)}"
                            )
                    status_value = getattr(response, "status", None)
                    status = int(
                        status_value
                        if status_value is not None
                        else response.getcode()
                    )
                    headers = response.headers
                    blocked = headers.get("x-blocked")
                    if blocked:
                        raise AccessBlockedError(
                            f"source blocked {safe_url}: {blocked}"
                        )
                    if status in (401, 403):
                        raise AccessBlockedError(
                            f"source returned HTTP {status} for {safe_url}"
                        )
                    if status == 429:
                        if attempt >= self.retries:
                            raise TileSourceError(
                                f"source rate limit persisted for {safe_url}"
                            )
                        self._sleep(
                            self._retry_delay(attempt, headers.get("Retry-After"))
                        )
                        continue
                    if status == 404 or status == 204:
                        raise TileMissingError(f"tile is absent: {safe_url}")
                    if 500 <= status <= 599:
                        if attempt >= self.retries:
                            raise TileSourceError(
                                f"source returned HTTP {status} for {safe_url}"
                            )
                        self._sleep(self._retry_delay(attempt))
                        continue
                    if status < 200 or status >= 300:
                        raise TileSourceError(
                            f"source returned HTTP {status} for {safe_url}"
                        )
                    content_type = headers.get("Content-Type", "").lower()
                    if content_type and not content_type.startswith("image/"):
                        raise TileSourceError(
                            f"source returned {content_type!r}, not an image, for {safe_url}"
                        )
                    payload = response.read(self.max_response_bytes + 1)
                    if len(payload) > self.max_response_bytes:
                        raise TileSourceError(
                            f"tile response exceeded {self.max_response_bytes} bytes"
                        )
                    if not payload:
                        raise TileSourceError(f"source returned an empty tile: {safe_url}")
                    return payload
            except urllib.error.HTTPError as exc:
                blocked = exc.headers.get("x-blocked") if exc.headers else None
                if exc.code in (401, 403) or blocked:
                    detail = f": {blocked}" if blocked else ""
                    raise AccessBlockedError(
                        f"source returned HTTP {exc.code} for {safe_url}{detail}"
                    ) from None
                if exc.code == 404:
                    raise TileMissingError(f"tile is absent: {safe_url}") from None
                if exc.code == 429 or 500 <= exc.code <= 599:
                    if attempt < self.retries:
                        retry_after = (
                            exc.headers.get("Retry-After") if exc.headers else None
                        )
                        self._sleep(self._retry_delay(attempt, retry_after))
                        continue
                raise TileSourceError(
                    f"source returned HTTP {exc.code} for {safe_url}"
                ) from None
            except AccessBlockedError:
                raise
            except TileSourceError:
                raise
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                TimeoutError,
                OSError,
                ValueError,
            ) as exc:
                if attempt < self.retries:
                    self._sleep(self._retry_delay(attempt))
                    continue
                raise TileSourceError(
                    f"request failed after {self.retries + 1} attempts for "
                    f"{safe_url} ({type(exc).__name__})"
                ) from None
        raise AssertionError("unreachable retry loop")

    def provenance(self) -> Mapping[str, Any]:
        return {
            "kind": "http",
            "name": self.name,
            "endpoint": self.safe_url_template,
            "secret_environment_variables": list(self._secret_env_names),
            **self._extra_provenance,
        }

    def fingerprint_data(self) -> Mapping[str, Any]:
        if self._fingerprint_override is not None:
            return dict(self._fingerprint_override)
        return {
            "kind": "http",
            "name": self.name,
            "endpoint": self.safe_url_template,
            "headers": sorted(name.lower() for name in self.headers if name.lower() != "authorization"),
            "request_configuration_sha256": self._request_configuration_sha256,
            **self._extra_provenance,
        }

    def close(self) -> None:
        return


class RasterMbtilesSource:
    """Read image tiles from an existing, authorized raster MBTiles archive."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        attribution: str | None = None,
        terms_url: str = "",
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise ConfigurationError(f"MBTiles archive does not exist: {self.path}")
        self._connection = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
        try:
            metadata_rows = self._connection.execute(
                "SELECT name, value FROM metadata"
            ).fetchall()
            self.metadata = {str(name): str(value) for name, value in metadata_rows}
            columns = {
                row[1]
                for row in self._connection.execute("PRAGMA table_info(tiles)").fetchall()
            }
        except sqlite3.DatabaseError as exc:
            self._connection.close()
            raise ConfigurationError(f"invalid MBTiles archive: {self.path.name}") from exc
        required_columns = {"zoom_level", "tile_column", "tile_row", "tile_data"}
        if not required_columns.issubset(columns):
            self._connection.close()
            raise ConfigurationError("MBTiles archive lacks the required tiles schema")
        tile_format = self.metadata.get("format", "").lower()
        if tile_format in ("pbf", "mvt", "application/x-protobuf"):
            self._connection.close()
            raise ConfigurationError(
                "vector MBTiles cannot be converted by Pillow; render it through "
                "TileServer GL and use --source local"
            )
        if tile_format and tile_format not in ("png", "jpg", "jpeg", "webp"):
            self._connection.close()
            raise ConfigurationError(f"unsupported MBTiles raster format: {tile_format}")
        selected_attribution = attribution or self.metadata.get("attribution", "")
        if not selected_attribution.strip():
            self._connection.close()
            raise ConfigurationError(
                "MBTiles has no attribution metadata; provide --attribution"
            )
        self.name = self.metadata.get("name", self.path.stem)
        self.attribution = selected_attribution.strip()
        self.terms_url = (terms_url or self.metadata.get("license", "")).strip()
        self.request_count = 0

    def fetch(self, tile: TileCoord) -> bytes:
        tms_y = (1 << tile.z) - 1 - tile.y
        row = self._connection.execute(
            """
            SELECT tile_data FROM tiles
            WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?
            """,
            (tile.z, tile.x, tms_y),
        ).fetchone()
        self.request_count += 1
        if row is None:
            raise TileMissingError(
                f"MBTiles does not contain {tile.z}/{tile.x}/{tile.y}"
            )
        payload = bytes(row[0])
        if not payload:
            raise TileSourceError(
                f"MBTiles contains an empty tile at {tile.z}/{tile.x}/{tile.y}"
            )
        return payload

    def provenance(self) -> Mapping[str, Any]:
        stat = self.path.stat()
        return {
            "kind": "raster-mbtiles",
            "name": self.name,
            "archive": self.path.name,
            "format": self.metadata.get("format", "unknown"),
            "size": stat.st_size,
        }

    def fingerprint_data(self) -> Mapping[str, Any]:
        stat = self.path.stat()
        return {
            "kind": "raster-mbtiles",
            "path": str(self.path),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "RasterMbtilesSource":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass
class PackSummary:
    planned: int
    downloaded: int = 0
    cached: int = 0
    converted: int = 0
    failed: int = 0
    bytes_written: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.failed == 0 and self.downloaded + self.cached + self.converted == self.planned

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["complete"] = self.complete
        return result


def validate_output_configuration(
    *,
    style: str,
    contrast: float = 1.0,
    brightness: float = 1.0,
    saturation: float = 1.0,
    background: str = "#ffffff",
    jpeg_quality: int = 75,
    max_consecutive_errors: int = 3,
):
    """Validate image dependencies/options before opening an expensive source."""
    if style not in FIRMWARE_STYLES:
        raise ConfigurationError(f"style must be one of {', '.join(FIRMWARE_STYLES)}")
    for name, value in (
        ("contrast", contrast),
        ("brightness", brightness),
        ("saturation", saturation),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ConfigurationError(f"{name} must be a positive finite number")
    if not 1 <= jpeg_quality <= 95:
        raise ConfigurationError("JPEG quality must be between 1 and 95")
    if max_consecutive_errors < 1:
        raise ConfigurationError("max consecutive errors must be positive")

    image, image_enhance, unidentified_image = _require_pillow()
    try:
        background_rgb = image.new("RGB", (1, 1), background).getpixel((0, 0))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid background color: {background}") from exc
    return image, image_enhance, unidentified_image, background_rgb


class TilePackager:
    """Convert planned source images into the firmware's JPEG directory tree."""

    def __init__(
        self,
        plan: TilePlan,
        source: TileSource,
        output_dir: str | os.PathLike[str],
        *,
        style: str,
        contrast: float = 1.0,
        brightness: float = 1.0,
        saturation: float = 1.0,
        background: str = "#ffffff",
        jpeg_quality: int = 75,
        force: bool = False,
        verify_cache: bool = True,
        max_consecutive_errors: int = 3,
        progress: Callable[[str], None] = print,
    ) -> None:
        (
            self._image,
            self._image_enhance,
            self._unidentified_image,
            self._background_rgb,
        ) = validate_output_configuration(
            style=style,
            contrast=contrast,
            brightness=brightness,
            saturation=saturation,
            background=background,
            jpeg_quality=jpeg_quality,
            max_consecutive_errors=max_consecutive_errors,
        )
        self.plan = plan
        self.source = source
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.style = style
        self.contrast = contrast
        self.brightness = brightness
        self.saturation = saturation
        self.background = background
        self.jpeg_quality = jpeg_quality
        self.force = force
        self.verify_cache = verify_cache
        self.max_consecutive_errors = max_consecutive_errors
        self.progress = progress

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / "tileset.json"

    @property
    def attribution_path(self) -> Path:
        return self.output_dir / "ATTRIBUTION.txt"

    def _tile_path(self, tile: TileCoord) -> Path:
        return self.output_dir / str(tile.z) / str(tile.x) / f"{tile.y}.jpg"

    def _legacy_png_path(self, tile: TileCoord) -> Path:
        return self.output_dir / str(tile.z) / str(tile.x) / f"{tile.y}.png"

    def _valid_cached_jpeg(self, path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", self._image.DecompressionBombWarning)
                if self.verify_cache:
                    with self._image.open(path) as image:
                        image.verify()
                with self._image.open(path) as image:
                    return (
                        image.format == "JPEG"
                        and image.size == (TILE_SIZE, TILE_SIZE)
                        and image.mode == "RGB"
                        and not image.info.get("progressive")
                        and not image.info.get("progression")
                    )
        except (
            OSError,
            ValueError,
            SyntaxError,
            Warning,
            self._image.DecompressionBombError,
            self._unidentified_image,
        ):
            return False

    def _atomic_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".part", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _write_jpeg(self, payload: bytes, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", self._image.DecompressionBombWarning)
                with self._image.open(io.BytesIO(payload)) as image:
                    if image.size != (TILE_SIZE, TILE_SIZE):
                        raise TileSourceError(
                            f"tile image is {image.size[0]}x{image.size[1]}, expected 256x256"
                        )
                    image.load()
                    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                        rgba = image.convert("RGBA")
                        background = self._image.new("RGBA", image.size, self._background_rgb + (255,))
                        background.alpha_composite(rgba)
                        image = background.convert("RGB")
                    else:
                        image = image.convert("RGB")
                    if self.contrast != 1.0:
                        image = self._image_enhance.Contrast(image).enhance(self.contrast)
                    if self.brightness != 1.0:
                        image = self._image_enhance.Brightness(image).enhance(self.brightness)
                    if self.saturation != 1.0:
                        image = self._image_enhance.Color(image).enhance(self.saturation)
                    fd, temp_name = tempfile.mkstemp(
                        prefix=f".{path.name}.", suffix=".part", dir=path.parent
                    )
                    os.close(fd)
                    try:
                        image.save(
                            temp_name,
                            format="JPEG",
                            quality=self.jpeg_quality,
                            progressive=False,
                            optimize=False,
                            subsampling=2,
                        )
                        with open(temp_name, "rb") as handle:
                            os.fsync(handle.fileno())
                        size = os.path.getsize(temp_name)
                        os.replace(temp_name, path)
                        return size
                    finally:
                        try:
                            os.unlink(temp_name)
                        except FileNotFoundError:
                            pass
        except TileSourceError:
            raise
        except (
            OSError,
            ValueError,
            SyntaxError,
            Warning,
            self._image.DecompressionBombError,
            self._unidentified_image,
        ) as exc:
            raise TileSourceError(f"source returned an invalid raster image: {exc}") from exc

    def _source_fingerprint(self) -> str:
        data = {
            "source": self.source.fingerprint_data(),
            "style": self.style,
            "output": {
                "format": "jpeg",
                "size": TILE_SIZE,
                "quality": self.jpeg_quality,
                "contrast": self.contrast,
                "brightness": self.brightness,
                "saturation": self.saturation,
                "background": self.background,
            },
        }
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _check_existing_fingerprint(self, fingerprint: str) -> None:
        if self.force:
            return
        has_tiles = self._has_tile_artifacts()
        if not self.manifest_path.is_file():
            if has_tiles:
                raise ConfigurationError(
                    f"{self.output_dir} contains tiles without provenance; choose "
                    "another output directory or use --force to rebuild it"
                )
            return
        try:
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if has_tiles:
                raise ConfigurationError(
                    f"{self.output_dir} contains tiles but its manifest is invalid; "
                    "choose another output directory or use --force"
                )
            return
        if not isinstance(existing, Mapping):
            if has_tiles:
                raise ConfigurationError(
                    f"{self.output_dir} contains tiles but its manifest is not "
                    "a JSON object; choose another output directory or use --force"
                )
            return
        prior = existing.get("source_fingerprint")
        if not isinstance(prior, str) or not prior:
            if has_tiles:
                raise ConfigurationError(
                    f"{self.output_dir} has no usable source fingerprint; choose "
                    "another output directory or use --force"
                )
            return
        if prior != fingerprint:
            raise ConfigurationError(
                f"{self.output_dir} contains tiles from a different source or image "
                "configuration; choose another output directory or use --force"
            )

    def _has_tile_artifacts(self) -> bool:
        if not self.output_dir.is_dir():
            return False
        for zoom_dir in self.output_dir.iterdir():
            if (
                zoom_dir.is_symlink()
                or not zoom_dir.is_dir()
                or not zoom_dir.name.isdecimal()
            ):
                continue
            if any(
                path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                for path in zoom_dir.rglob("*")
            ):
                return True
        return False

    def _validate_tile_tree_paths(self) -> None:
        """Reject symlinks that could move reads/writes outside the pack root."""
        if not self.output_dir.is_dir():
            return
        for zoom_dir in self.output_dir.iterdir():
            if not zoom_dir.name.isdecimal():
                continue
            if zoom_dir.is_symlink():
                raise ConfigurationError(
                    f"tile zoom path cannot be a symlink: {zoom_dir}"
                )
            if not zoom_dir.is_dir():
                continue
            for x_dir in zoom_dir.iterdir():
                if not x_dir.name.isdecimal():
                    continue
                if x_dir.is_symlink():
                    raise ConfigurationError(
                        f"tile x path cannot be a symlink: {x_dir}"
                    )
                if not x_dir.is_dir():
                    continue
                for tile_path in x_dir.iterdir():
                    if tile_path.is_symlink():
                        raise ConfigurationError(
                            f"tile path cannot be a symlink: {tile_path}"
                        )

    def _validate_force_target(self) -> None:
        if not self.output_dir.is_dir():
            return

        managed = False
        if self.manifest_path.is_file():
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = None
            if isinstance(manifest, Mapping):
                generator = manifest.get("generator")
                managed = (
                    isinstance(generator, Mapping)
                    and generator.get("name") == "Plai offline map tile packager"
                    and manifest.get("style") == self.style
                )

        numeric_directories = [
            path for path in self.output_dir.iterdir() if path.name.isdecimal()
        ]
        if not numeric_directories:
            return
        if not managed and self.output_dir.name != self.style:
            raise ConfigurationError(
                "--force refuses to clear numeric directories in an unowned "
                f"path not named '{self.style}': {self.output_dir}"
            )

        for zoom_dir in numeric_directories:
            if (
                zoom_dir.is_symlink()
                or not zoom_dir.is_dir()
                or not FIRMWARE_MIN_ZOOM
                <= int(zoom_dir.name)
                <= FIRMWARE_MAX_ZOOM
            ):
                raise ConfigurationError(
                    f"--force found a non-tile numeric path: {zoom_dir}"
                )
            for x_dir in zoom_dir.iterdir():
                if (
                    not x_dir.name.isdecimal()
                    or x_dir.is_symlink()
                    or not x_dir.is_dir()
                ):
                    raise ConfigurationError(
                        f"--force found unexpected content in a tile tree: {x_dir}"
                    )
                for tile_path in x_dir.iterdir():
                    valid_tile = (
                        tile_path.is_file()
                        and tile_path.stem.isdecimal()
                        and tile_path.suffix.lower() in {".jpg", ".jpeg", ".png"}
                    )
                    valid_temporary = tile_path.is_file() and _TILE_TEMP_FILE.fullmatch(
                        tile_path.name
                    )
                    if tile_path.is_symlink() or not (valid_tile or valid_temporary):
                        raise ConfigurationError(
                            "--force found unexpected content in a tile tree: "
                            f"{tile_path}"
                        )

    def _clear_tile_trees_for_force(self) -> None:
        if not self.output_dir.is_dir():
            return
        for path in self.output_dir.iterdir():
            if path.is_dir() and path.name.isdecimal():
                shutil.rmtree(path)

    def _manifest(
        self, summary: PackSummary, fingerprint: str, *, status: str
    ) -> dict[str, Any]:
        # Keep `complete` first: firmware intentionally reads only the first KiB.
        return {
            "complete": summary.complete and status == "complete",
            "status": status,
            "generator": {
                "name": "Plai offline map tile packager",
                "version": 2,
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "style": self.style,
            "attribution": self.source.attribution,
            "terms_url": self.source.terms_url,
            "source_fingerprint": fingerprint,
            "source": dict(self.source.provenance()),
            "output": {
                "format": "jpeg",
                "tile_size": TILE_SIZE,
                "quality": self.jpeg_quality,
                "contrast": self.contrast,
                "brightness": self.brightness,
                "saturation": self.saturation,
                "background": self.background,
            },
            "plan": self.plan.to_dict(),
            "counts": summary.to_dict(),
        }

    def _write_manifest(
        self, summary: PackSummary, fingerprint: str, *, status: str
    ) -> None:
        self._atomic_text(
            self.manifest_path,
            json.dumps(
                self._manifest(summary, fingerprint, status=status),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )

    def _write_attribution(self) -> None:
        lines = [self.source.attribution.strip()]
        if self.source.terms_url:
            lines.append(f"License/terms: {self.source.terms_url}")
        lines.append("Packaged for Plai; keep this file with the map tiles.")
        self._atomic_text(self.attribution_path, "\n".join(lines) + "\n")

    def _clean_stale_parts(self) -> None:
        if not self.output_dir.exists():
            return
        candidates = list(self.output_dir.glob(".tileset.json.*.part"))
        candidates.extend(self.output_dir.glob(".ATTRIBUTION.txt.*.part"))
        for zoom_dir in self.output_dir.iterdir():
            if (
                zoom_dir.is_symlink()
                or not zoom_dir.is_dir()
                or not zoom_dir.name.isdecimal()
            ):
                continue
            for x_dir in zoom_dir.iterdir():
                if (
                    not x_dir.is_symlink()
                    and x_dir.is_dir()
                    and x_dir.name.isdecimal()
                ):
                    candidates.extend(
                        path
                        for path in x_dir.glob(".*.part")
                        if _TILE_TEMP_FILE.fullmatch(path.name)
                    )
        for path in candidates:
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            except OSError:
                continue

    def _prune_unplanned_tiles(self) -> None:
        planned = {(tile.z, tile.x, tile.y) for tile in self.plan}
        if not self.output_dir.is_dir():
            return
        for zoom_dir in self.output_dir.iterdir():
            if not zoom_dir.name.isdecimal():
                continue
            if zoom_dir.is_symlink():
                raise ConfigurationError(
                    f"tile zoom path cannot be a symlink: {zoom_dir}"
                )
            if not zoom_dir.is_dir():
                continue
            zoom = int(zoom_dir.name)
            for x_dir in list(zoom_dir.iterdir()):
                if not x_dir.name.isdecimal():
                    continue
                if x_dir.is_symlink():
                    raise ConfigurationError(
                        f"tile x path cannot be a symlink: {x_dir}"
                    )
                if not x_dir.is_dir():
                    continue
                x = int(x_dir.name)
                for path in list(x_dir.iterdir()):
                    if path.is_symlink():
                        raise ConfigurationError(
                            f"tile path cannot be a symlink: {path}"
                        )
                    if (
                        not path.is_file()
                        or not path.stem.isdecimal()
                        or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}
                    ):
                        continue
                    coordinate = (zoom, x, int(path.stem))
                    if coordinate not in planned or path.suffix.lower() != ".jpg":
                        path.unlink()
                try:
                    x_dir.rmdir()
                except OSError:
                    pass
            try:
                zoom_dir.rmdir()
            except OSError:
                pass

    def run(self) -> PackSummary:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.output_dir / ".plai-packager.lock"
        try:
            lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise ConfigurationError(
                f"another packager is using {self.output_dir}; if no job is "
                f"running, remove the stale lock {lock_path}"
            ) from exc
        try:
            os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
            return self._run_locked()
        finally:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except OSError:
                self.progress(f"WARNING: could not remove packager lock {lock_path}")

    def _run_locked(self) -> PackSummary:
        summary = PackSummary(planned=self.plan.total_tiles)
        fingerprint = self._source_fingerprint()
        self._validate_tile_tree_paths()
        if self.force:
            self._validate_force_target()
        if self.force:
            # Publish fail-closed state before removing any previously complete
            # pack. An interrupt during cleanup can never leave a stale
            # complete=true manifest beside missing/mixed tiles.
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._write_manifest(summary, fingerprint, status="rebuilding")
            self._clear_tile_trees_for_force()
        self._check_existing_fingerprint(fingerprint)
        self._clean_stale_parts()
        self._write_attribution()
        self._write_manifest(summary, fingerprint, status="running")
        consecutive_errors = 0

        try:
            current_zoom: int | None = None
            for tile in self.plan:
                if tile.z != current_zoom:
                    current_zoom = tile.z
                    self.progress(
                        f"Zoom {tile.z}: {self.plan.counts_by_zoom[tile.z]:,} planned"
                    )
                output_path = self._tile_path(tile)
                if not self.force and self._valid_cached_jpeg(output_path):
                    summary.cached += 1
                    consecutive_errors = 0
                    continue

                legacy_png = self._legacy_png_path(tile)
                if not self.force and legacy_png.is_file():
                    try:
                        summary.bytes_written += self._write_jpeg(
                            legacy_png.read_bytes(), output_path
                        )
                        summary.converted += 1
                        consecutive_errors = 0
                        continue
                    except (OSError, TileSourceError):
                        pass

                try:
                    payload = self.source.fetch(tile)
                    summary.bytes_written += self._write_jpeg(payload, output_path)
                    summary.downloaded += 1
                    consecutive_errors = 0
                except AccessBlockedError:
                    summary.failed += 1
                    self._write_manifest(summary, fingerprint, status="blocked")
                    raise
                except TileSourceError as exc:
                    summary.failed += 1
                    consecutive_errors += 1
                    message = f"{tile.z}/{tile.x}/{tile.y}: {exc}"
                    if len(summary.errors) < 20:
                        summary.errors.append(message)
                    self.progress(f"FAILED {message}")
                    if consecutive_errors >= self.max_consecutive_errors:
                        self._write_manifest(summary, fingerprint, status="failed")
                        raise TilePackError(
                            f"aborting after {consecutive_errors} consecutive source errors"
                        ) from exc
            if summary.failed == 0:
                self._prune_unplanned_tiles()
            status = "complete" if summary.failed == 0 else "partial"
            self._write_manifest(summary, fingerprint, status=status)
            return summary
        except KeyboardInterrupt:
            self._write_manifest(summary, fingerprint, status="interrupted")
            raise
        except Exception:
            if self.manifest_path.is_file():
                try:
                    current = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = {}
                if isinstance(current, Mapping) and current.get("status") == "running":
                    self._write_manifest(summary, fingerprint, status="failed")
            raise


def format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    raise AssertionError("unreachable")


def print_plan(plan: TilePlan, output: Callable[[str], None] = print) -> None:
    output("Tile plan:")
    for zoom, count in plan.counts_by_zoom.items():
        phase = "global" if zoom <= plan.global_zoom else "regional"
        output(f"  z{zoom:02d}: {count:>8,} tiles ({phase})")
    output(f"  total: {plan.total_tiles:,} tiles")
    output(f"  estimated JPEG storage: {format_bytes(plan.estimated_bytes)}")
