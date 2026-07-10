"""Local Protomaps extraction and raster-renderer lifecycle.

The public Protomaps daily build is a PMTiles vector archive.  This module
extracts the small subset needed by a Plai tile plan, then runs the full
TileServer GL image on loopback so :mod:`tile_downloader` can consume an
ordinary XYZ raster URL.

No public raster tile server is contacted by this backend.  The only remote
map request is made by the ``pmtiles extract`` CLI against a Protomaps archive
whose terms permit extracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


BUILDS_METADATA_URL = "https://build-metadata.protomaps.dev/builds.json"
BUILDS_BASE_URL = "https://build.protomaps.com/"
DEFAULT_DOCKER_IMAGE = "maptiler/tileserver-gl:v5.6.0"
TILESET_MAJOR = 4
PMTILES_HEADER_SIZE = 127
WEB_MERCATOR_LIMIT = 85.05112878


class ProtomapsError(RuntimeError):
    """Raised when a Protomaps extract or its local renderer cannot start."""


@dataclass(frozen=True)
class BuildInfo:
    """Resolved Protomaps build metadata safe to include in a manifest."""

    key: str
    version: str | None
    uploaded: str | None
    url: str


@dataclass(frozen=True)
class _ExtractPiece:
    label: str
    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float] | None = None


Fetch = Callable[..., Any]
Run = Callable[..., Any]


def redact_url(url: str) -> str:
    """Return a URL that cannot disclose user info, query values, or fragments."""

    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return "<redacted-url>"
    if not parsed.scheme or not parsed.netloc:
        return url

    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    try:
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    if port is not None:
        netloc = f"{netloc}:{port}"
    if parsed.username is not None or parsed.password is not None:
        netloc = f"<redacted>@{netloc}"

    query = "<redacted>" if parsed.query else ""
    fragment = "<redacted>" if parsed.fragment else ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, query, fragment)
    )


def _default_fetch(url: str, *, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, */*;q=0.1",
            "User-Agent": "Plai-Offline-Map-Packager/2.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _value_from_plan(plan: object | None, names: Iterable[str]) -> Any:
    if plan is None:
        return None
    for name in names:
        if isinstance(plan, Mapping) and name in plan:
            return plan[name]
        if hasattr(plan, name):
            return getattr(plan, name)
    return None


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _looks_like_bbox(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray))
        and len(value) == 4
        and all(_is_number(item) for item in value)
    )


def _bbox_from_object(value: object) -> tuple[float, float, float, float]:
    if _looks_like_bbox(value):
        sequence = value  # type: ignore[assignment]
        return tuple(float(item) for item in sequence)  # type: ignore[return-value]

    name_sets = (
        ("west", "south", "east", "north"),
        ("min_lon", "min_lat", "max_lon", "max_lat"),
        ("lon_min", "lat_min", "lon_max", "lat_max"),
    )
    for names in name_sets:
        if isinstance(value, Mapping) and all(name in value for name in names):
            return tuple(float(value[name]) for name in names)  # type: ignore[return-value]
        if all(hasattr(value, name) for name in names):
            return tuple(float(getattr(value, name)) for name in names)  # type: ignore[return-value]
    raise ProtomapsError(
        "Each regional bound must be (west, south, east, north)."
    )


def _normalise_bounds(
    bounds: object | None,
) -> tuple[tuple[float, float, float, float], ...]:
    if bounds is None:
        return ()

    if _looks_like_bbox(bounds) or isinstance(bounds, Mapping):
        raw_bounds = [_bbox_from_object(bounds)]
    else:
        if isinstance(bounds, (str, bytes, bytearray)):
            raise ProtomapsError(
                "Regional bounds must be numeric (west, south, east, north) tuples."
            )
        try:
            raw_bounds = [_bbox_from_object(item) for item in bounds]  # type: ignore[union-attr]
        except TypeError as error:
            raise ProtomapsError(
                "Regional bounds must be numeric (west, south, east, north) tuples."
            ) from error

    result: list[tuple[float, float, float, float]] = []
    for west, south, east, north in raw_bounds:
        coordinates = (west, south, east, north)
        if not all(math.isfinite(item) for item in coordinates):
            raise ProtomapsError("Regional bounds must contain finite coordinates.")
        if not -180.0 <= west <= 180.0 or not -180.0 <= east <= 180.0:
            raise ProtomapsError("Regional longitude bounds must be between -180 and 180.")
        if not -WEB_MERCATOR_LIMIT <= south <= WEB_MERCATOR_LIMIT:
            raise ProtomapsError(
                f"South latitude must be within Web Mercator ({-WEB_MERCATOR_LIMIT} to {WEB_MERCATOR_LIMIT})."
            )
        if not -WEB_MERCATOR_LIMIT <= north <= WEB_MERCATOR_LIMIT:
            raise ProtomapsError(
                f"North latitude must be within Web Mercator ({-WEB_MERCATOR_LIMIT} to {WEB_MERCATOR_LIMIT})."
            )
        if south >= north:
            raise ProtomapsError("Regional bounds must have south < north.")
        if west == east:
            raise ProtomapsError("Regional bounds must have a non-zero longitude span.")

        # Accept a single conventional antimeridian-crossing bound in addition
        # to the already-split tuple-of-tuples emitted by the tile planner.
        pieces = (
            ((west, south, 180.0, north), (-180.0, south, east, north))
            if west > east
            else ((west, south, east, north),)
        )
        for piece in pieces:
            if piece not in result:
                result.append(piece)
    return tuple(result)


class ProtomapsSession:
    """Prepare a Protomaps extract and a loopback TileServer GL renderer.

    ``bounds`` may be one bbox, an iterable of bboxes, or the one/two split
    bounds produced for an antimeridian-crossing tile plan.  The optional
    ``plan`` form exists for callers that keep these values in a dataclass;
    explicit keyword arguments take precedence.

    ``fetch``, ``run``, ``which``, ``sleep``, and ``monotonic`` are injectable
    to make all network/process orchestration deterministic in unit tests.
    """

    def __init__(
        self,
        plan: object | None = None,
        *,
        bounds: object | None = None,
        min_zoom: int | None = None,
        global_zoom: int | None = None,
        max_zoom: int | None = None,
        style: str = "dark",
        cache_dir: str | os.PathLike[str] = ".cache/plai-protomaps",
        pmtiles_bin: str = "pmtiles",
        docker_bin: str = "docker",
        docker_image: str = DEFAULT_DOCKER_IMAGE,
        port: int = 8080,
        build_url: str | None = None,
        key: str | None = None,
        fetch: Fetch | None = None,
        run: Run | None = None,
        which: Callable[[str], str | None] | None = None,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
        ready_timeout: float = 60.0,
        ready_interval: float = 0.25,
    ) -> None:
        if bounds is None:
            bounds = _value_from_plan(
                plan, ("regional_bounds", "bounds", "extract_bounds")
            )
        if min_zoom is None:
            min_zoom = _value_from_plan(plan, ("min_zoom",))
        if global_zoom is None:
            global_zoom = _value_from_plan(plan, ("global_zoom",))
        if max_zoom is None:
            max_zoom = _value_from_plan(plan, ("max_zoom",))

        self.bounds = _normalise_bounds(bounds)
        self.min_zoom = self._coerce_zoom("min_zoom", min_zoom)
        self.global_zoom = self._coerce_zoom(
            "global_zoom", global_zoom, allow_minus_one=True
        )
        self.max_zoom = self._coerce_zoom("max_zoom", max_zoom)
        if self.min_zoom > self.max_zoom:
            raise ProtomapsError("min_zoom must be less than or equal to max_zoom.")

        regional_min = max(self.min_zoom, self.global_zoom + 1)
        if regional_min <= self.max_zoom and not self.bounds:
            raise ProtomapsError(
                "Regional bounds are required for zooms above global_zoom."
            )

        if style not in {"osm", "dark"}:
            raise ProtomapsError("Protomaps style must be either 'osm' or 'dark'.")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ProtomapsError("TileServer GL port must be between 0 and 65535.")
        if not docker_image.strip():
            raise ProtomapsError("A TileServer GL Docker image is required.")
        if not pmtiles_bin.strip() or not docker_bin.strip():
            raise ProtomapsError("The pmtiles and docker executable names cannot be empty.")
        if ready_timeout <= 0 or ready_interval <= 0:
            raise ProtomapsError("Renderer readiness timeout and interval must be positive.")
        if build_url is not None and not build_url.strip():
            raise ProtomapsError("build_url cannot be empty.")
        if key is not None and not key.strip():
            raise ProtomapsError("Protomaps build key cannot be empty.")

        self.style = style
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.pmtiles_bin = pmtiles_bin
        self.docker_bin = docker_bin
        self.docker_image = docker_image
        self.port = port
        self.key = key
        self.ready_timeout = float(ready_timeout)
        self.ready_interval = float(ready_interval)

        self._requested_build_url = build_url
        self._raw_build_url: str | None = None
        self._fetch = fetch or _default_fetch
        self._run = run or subprocess.run
        self._which = which or shutil.which
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

        self.build_key: str | None = None
        self.build_version: str | None = None
        self.build_uploaded: str | None = None
        self.build_identity_sha256: str | None = None
        self.style_sha256: str | None = None
        self.archive_path: Path | None = None
        self.config_path: Path | None = None
        self.container_name: str | None = None
        self._url_template: str | None = None
        self._prepared = False
        self._dry_run = False

    @staticmethod
    def _coerce_zoom(
        name: str, value: object, *, allow_minus_one: bool = False
    ) -> int:
        minimum = -1 if allow_minus_one else 0
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtomapsError(f"{name} must be an integer.")
        if not minimum <= value <= 30:
            raise ProtomapsError(f"{name} must be between {minimum} and 30.")
        return value

    @property
    def build_url(self) -> str | None:
        """The resolved build URL with credentials and query values redacted."""

        return redact_url(self._raw_build_url) if self._raw_build_url else None

    @property
    def build_info(self) -> BuildInfo | None:
        if self.build_key is None or self.build_url is None:
            return None
        return BuildInfo(
            key=self.build_key,
            version=self.build_version,
            uploaded=self.build_uploaded,
            url=self.build_url,
        )

    @property
    def url_template(self) -> str:
        if self._url_template is None:
            raise ProtomapsError(
                "The renderer URL is unavailable until prepare() succeeds."
            )
        return self._url_template

    def __enter__(self) -> "ProtomapsSession":
        return self.prepare()

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def prepare(self, dry_run: bool = False) -> "ProtomapsSession":
        """Resolve, extract, configure, and start the local renderer once."""

        if self._prepared:
            if dry_run or not self._dry_run:
                return self
            # It is harmless to turn a plan-only session into a real session
            # later.  This is useful to interactive callers even though the
            # command-line flow constructs a fresh session after dry-run.
            self._prepared = False
            self._dry_run = False
        if dry_run:
            # CLI dry-runs must be usable before installing Docker/pmtiles and
            # must never turn into a hidden network operation.
            self._dry_run = True
            self._prepared = True
            return self

        self._verify_style_asset()
        self._verify_binary(self.pmtiles_bin, "pmtiles CLI")
        self._verify_binary(self.docker_bin, "Docker CLI")
        self._resolve_build()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.archive_path = self._prepare_extract()
        self.config_path = self._write_tileserver_config()

        try:
            self._start_renderer()
            self._wait_for_renderer()
        except BaseException:
            self.close()
            raise

        self._prepared = True
        return self

    def close(self) -> None:
        """Force-stop and remove the managed container, if one was started."""

        name = self.container_name
        self.container_name = None
        self._url_template = None
        self._prepared = False
        self._dry_run = False
        if name is None:
            return
        self._run_command(
            [self.docker_bin, "rm", "--force", name],
            action="stop TileServer GL",
            check=False,
        )

    def _verify_binary(self, executable: str, description: str) -> None:
        if os.sep in executable or (os.altsep and os.altsep in executable):
            candidate = Path(executable).expanduser()
            present = candidate.is_file() and os.access(candidate, os.X_OK)
        else:
            present = self._which(executable) is not None
        if not present:
            raise ProtomapsError(
                f"{description} executable '{executable}' was not found or is not executable."
            )

    def _selected_style_path(self) -> Path:
        filename = "light.json" if self.style == "osm" else "dark.json"
        return Path(__file__).resolve().parent / "tileserver" / "styles" / filename

    def _verify_style_asset(self) -> None:
        style_path = self._selected_style_path()
        try:
            payload = style_path.read_bytes()
        except OSError as error:
            raise ProtomapsError(
                f"Vendored TileServer GL style file is unavailable: {style_path}"
            ) from error
        try:
            parsed = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtomapsError(
                f"Vendored TileServer GL style is invalid JSON: {style_path}"
            ) from error
        if not isinstance(parsed, Mapping) or parsed.get("version") != 8:
            raise ProtomapsError(
                f"Vendored TileServer GL style is not a Mapbox Style v8 document: {style_path}"
            )
        sources = parsed.get("sources")
        protomaps_source = (
            sources.get("protomaps") if isinstance(sources, Mapping) else None
        )
        if not isinstance(protomaps_source, Mapping) or (
            protomaps_source.get("type") != "vector"
            or protomaps_source.get("url") != "pmtiles://{protomaps}"
        ):
            raise ProtomapsError(
                f"Vendored style must use the configured local Protomaps source: {style_path}"
            )
        self.style_sha256 = hashlib.sha256(payload).hexdigest()

    def _resolve_build(self) -> None:
        if self._requested_build_url is not None:
            raw_url = self._requested_build_url
            if self.key:
                if "{key}" in raw_url:
                    raw_url = raw_url.replace(
                        "{key}", urllib.parse.quote(self.key, safe="")
                    )
                elif raw_url.endswith("/"):
                    raw_url = urllib.parse.urljoin(
                        raw_url, urllib.parse.quote(self.key, safe="/")
                    )
                else:
                    raise ProtomapsError(
                        "When build_url and key are both provided, build_url must end in '/' or contain '{key}'."
                    )
            self._set_resolved_build(raw_url, self.key)
            return

        if self.key:
            raw_url = urllib.parse.urljoin(
                BUILDS_BASE_URL, urllib.parse.quote(self.key, safe="/")
            )
            self._set_resolved_build(raw_url, self.key)
            return

        try:
            payload = self._fetch_json(BUILDS_METADATA_URL)
        except Exception as error:
            if isinstance(error, ProtomapsError):
                raise
            raise ProtomapsError(
                f"Could not read the Protomaps build index at {redact_url(BUILDS_METADATA_URL)}: {error}"
            ) from error

        if isinstance(payload, Mapping):
            payload = payload.get("builds")
        if not isinstance(payload, list):
            raise ProtomapsError("The Protomaps build index was not a JSON array.")

        candidates: list[Mapping[str, Any]] = []
        for entry in payload:
            if not isinstance(entry, Mapping):
                continue
            version = str(entry.get("version", ""))
            key = entry.get("key")
            if re.match(rf"^{TILESET_MAJOR}(?:\.|$)", version) and isinstance(
                key, str
            ) and key:
                candidates.append(entry)
        if not candidates:
            raise ProtomapsError(
                f"No tileset-major-{TILESET_MAJOR} build was present in the Protomaps build index."
            )

        latest = max(
            candidates,
            key=lambda entry: (
                str(entry.get("uploaded", "")), str(entry.get("key", ""))
            ),
        )
        build_key = str(latest["key"])
        raw_url = urllib.parse.urljoin(
            BUILDS_BASE_URL, urllib.parse.quote(build_key, safe="/")
        )
        self._set_resolved_build(raw_url, build_key)
        self.build_version = str(latest.get("version") or "") or None
        self.build_uploaded = str(latest.get("uploaded") or "") or None

    def _set_resolved_build(self, raw_url: str, key: str | None) -> None:
        parsed = urllib.parse.urlsplit(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ProtomapsError("Protomaps build_url must be an HTTP or HTTPS URL.")
        path_key = Path(urllib.parse.unquote(parsed.path)).name
        self.build_key = key or path_key or "custom.pmtiles"
        self._raw_build_url = raw_url
        self.build_identity_sha256 = hashlib.sha256(raw_url.encode("utf-8")).hexdigest()

    def _fetch_json(self, url: str) -> Any:
        try:
            try:
                response = self._fetch(url, timeout=30.0)
            except TypeError:
                # A small one-argument lambda is convenient in unit tests.
                response = self._fetch(url)

            if isinstance(response, (Mapping, list)):
                return response
            if isinstance(response, str):
                raw = response.encode("utf-8")
            elif isinstance(response, (bytes, bytearray)):
                raw = bytes(response)
            elif hasattr(response, "read"):
                raw = response.read()
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            else:
                raise TypeError("fetch returned neither bytes, JSON, nor a readable response")
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            raise ProtomapsError(
                f"Request to {redact_url(url)} failed: {self._redact_text(str(error))}"
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise ProtomapsError(
                f"Invalid JSON returned by {redact_url(url)}: {self._redact_text(str(error))}"
            ) from error

    def _extract_pieces(self) -> list[_ExtractPiece]:
        pieces: list[_ExtractPiece] = []
        global_max = min(self.global_zoom, self.max_zoom)
        if self.min_zoom <= global_max:
            pieces.append(
                _ExtractPiece("global", self.min_zoom, global_max, None)
            )

        regional_min = max(self.min_zoom, self.global_zoom + 1)
        if regional_min <= self.max_zoom:
            for index, bounds in enumerate(self.bounds):
                pieces.append(
                    _ExtractPiece(
                        f"region-{index + 1}",
                        regional_min,
                        self.max_zoom,
                        bounds,
                    )
                )
        if not pieces:
            raise ProtomapsError("The zoom plan did not produce any extract ranges.")
        return pieces

    def _prepare_extract(self) -> Path:
        if self._raw_build_url is None or self.build_key is None:
            raise ProtomapsError("A Protomaps build must be resolved before extraction.")

        pieces = self._extract_pieces()
        fingerprint_payload = {
            "source": self._raw_build_url,
            "build_key": self.build_key,
            "pieces": [
                {
                    "label": piece.label,
                    "min_zoom": piece.min_zoom,
                    "max_zoom": piece.max_zoom,
                    "bounds": piece.bounds,
                }
                for piece in pieces
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:16]
        build_slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", self.build_key).strip("-.")
        build_slug = build_slug.removesuffix(".pmtiles") or "custom"
        stem = f"protomaps-v{TILESET_MAJOR}-{build_slug}-{fingerprint}"
        final_path = self.cache_dir / f"{stem}.pmtiles"

        lock_path = self.cache_dir / f".{stem}.lock"
        try:
            lock_fd = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError as error:
            raise ProtomapsError(
                "Another Protomaps extraction is using this cache entry; if no "
                f"job is running, remove the stale lock {lock_path}."
            ) from error
        try:
            os.write(lock_fd, f"pid={os.getpid()}\n".encode("ascii"))
            return self._prepare_extract_locked(pieces, stem, final_path)
        finally:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except OSError:
                pass

    def _prepare_extract_locked(
        self, pieces: Sequence[_ExtractPiece], stem: str, final_path: Path
    ) -> Path:

        if self._verified_pmtiles(final_path):
            return final_path
        if final_path.exists():
            final_path.unlink()

        if len(pieces) == 1:
            self._extract_piece(pieces[0], final_path)
            return final_path

        piece_paths: list[Path] = []
        for piece in pieces:
            piece_path = self.cache_dir / f"{stem}-{piece.label}.pmtiles"
            self._extract_piece(piece, piece_path)
            piece_paths.append(piece_path)

        temporary = final_path.with_name(f"{final_path.stem}.part.pmtiles")
        if temporary.exists():
            temporary.unlink()
        command = [
            self.pmtiles_bin,
            "merge",
            *(str(path) for path in piece_paths),
            str(temporary),
        ]
        self._run_command(command, action="merge Protomaps extracts")
        self._promote_pmtiles(temporary, final_path, "merged Protomaps extract")
        return final_path

    def _extract_piece(self, piece: _ExtractPiece, destination: Path) -> None:
        if self._verified_pmtiles(destination):
            return
        if destination.exists():
            destination.unlink()
        temporary = destination.with_name(f"{destination.stem}.part.pmtiles")
        if temporary.exists():
            temporary.unlink()

        command = [
            self.pmtiles_bin,
            "extract",
            self._raw_build_url or "",
            str(temporary),
            f"--minzoom={piece.min_zoom}",
            f"--maxzoom={piece.max_zoom}",
        ]
        if piece.bounds is not None:
            command.append(f"--bbox={self._format_bbox(piece.bounds)}")
        self._run_command(
            command,
            action=f"extract Protomaps {piece.label} zooms {piece.min_zoom}-{piece.max_zoom}",
        )
        self._promote_pmtiles(temporary, destination, f"Protomaps {piece.label} extract")

    @staticmethod
    def _format_bbox(bounds: tuple[float, float, float, float]) -> str:
        return ",".join(format(value, ".12g") for value in bounds)

    @staticmethod
    def _valid_pmtiles(path: Path) -> bool:
        try:
            if path.stat().st_size < PMTILES_HEADER_SIZE:
                return False
            with path.open("rb") as archive:
                header = archive.read(8)
            return header[:7] == b"PMTiles" and header[7] == 3
        except (OSError, IndexError):
            return False

    def _verified_pmtiles(self, path: Path) -> bool:
        """Check the header and PMTiles directory/header integrity."""
        if not self._valid_pmtiles(path):
            return False
        result = self._run_command(
            [self.pmtiles_bin, "verify", str(path)],
            action=f"verify PMTiles archive {path.name}",
            check=False,
        )
        return int(getattr(result, "returncode", 0) or 0) == 0

    def _promote_pmtiles(self, temporary: Path, destination: Path, label: str) -> None:
        if not self._verified_pmtiles(temporary):
            if temporary.exists():
                temporary.unlink()
            raise ProtomapsError(
                f"The {label} command succeeded but did not create a valid, "
                "verified PMTiles v3 archive."
            )
        os.replace(temporary, destination)

    def _write_tileserver_config(self) -> Path:
        if self.archive_path is None:
            raise ProtomapsError("The PMTiles archive is unavailable.")

        selected_style = self._selected_style_path().name
        runtime_dir = self.cache_dir / "tileserver"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = hashlib.sha256(
            f"{self.archive_path}\0{selected_style}".encode("utf-8")
        ).hexdigest()[:16]
        config_path = runtime_dir / f"config-{fingerprint}.json"
        config = {
            "options": {
                "paths": {
                    "root": "/data",
                    "styles": "styles",
                    "pmtiles": "pmtiles",
                },
                "formatOptions": {"jpeg": {"quality": 75}},
                "serveAllStyles": False,
            },
            "styles": {
                "plai": {
                    "style": selected_style,
                    "serve_rendered": True,
                    "serve_data": False,
                    "tilejson": {"format": "jpg"},
                }
            },
            "data": {
                "protomaps": {
                    "pmtiles": self.archive_path.name,
                }
            },
        }
        encoded = (json.dumps(config, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if not config_path.is_file() or config_path.read_bytes() != encoded:
            temporary = config_path.with_name(
                f".{config_path.name}.{uuid.uuid4().hex}.part"
            )
            try:
                temporary.write_bytes(encoded)
                os.replace(temporary, config_path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        return config_path

    def _start_renderer(self) -> None:
        if self.archive_path is None or self.config_path is None:
            raise ProtomapsError("Extract and config must exist before starting TileServer GL.")

        styles_dir = Path(__file__).resolve().parent / "tileserver" / "styles"
        name = f"plai-protomaps-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        port_mapping = (
            "127.0.0.1::8080"
            if self.port == 0
            else f"127.0.0.1:{self.port}:8080"
        )
        command = [
            self.docker_bin,
            "run",
            "--detach",
            "--rm",
            "--name",
            name,
            "--publish",
            port_mapping,
            "--mount",
            f"type=bind,source={self.cache_dir},target=/data/pmtiles,readonly",
            "--mount",
            f"type=bind,source={styles_dir},target=/data/styles,readonly",
            "--mount",
            f"type=bind,source={self.config_path},target=/data/config.json,readonly",
            self.docker_image,
            "--config",
            "/data/config.json",
        ]
        self.container_name = name
        try:
            self._run_command(command, action="start TileServer GL")
            actual_port = self.port or self._docker_published_port(name)
        except BaseException:
            self.close()
            raise
        self.port = actual_port
        self._url_template = (
            f"http://127.0.0.1:{actual_port}/styles/plai/256/{{z}}/{{x}}/{{y}}.jpg"
        )

    def _docker_published_port(self, name: str) -> int:
        result = self._run_command(
            [self.docker_bin, "port", name, "8080/tcp"],
            action="discover TileServer GL port",
        )
        output = self._result_text(result, "stdout").strip()
        match = re.search(r":(\d+)\s*$", output)
        if not match:
            raise ProtomapsError(
                "Docker did not report the loopback port published for TileServer GL."
            )
        port = int(match.group(1))
        if not 1 <= port <= 65535:
            raise ProtomapsError("Docker reported an invalid TileServer GL port.")
        return port

    def _wait_for_renderer(self) -> None:
        if self._url_template is None:
            raise ProtomapsError("TileServer GL has not been started.")
        ready_url = f"http://127.0.0.1:{self.port}/styles/plai.json"
        deadline = self._monotonic() + self.ready_timeout
        max_attempts = max(2, math.ceil(self.ready_timeout / self.ready_interval) + 1)
        last_error: Exception | None = None

        for _attempt in range(max_attempts):
            try:
                try:
                    response = self._fetch(ready_url, timeout=2.0)
                except TypeError:
                    response = self._fetch(ready_url)
                status = getattr(response, "status", 200)
                if status is not None and not 200 <= int(status) < 300:
                    raise ProtomapsError(
                        f"renderer readiness endpoint returned HTTP {status}"
                    )
                if hasattr(response, "read"):
                    response.read()
                    close = getattr(response, "close", None)
                    if callable(close):
                        close()
                return
            except Exception as error:  # server startup failures are transient
                last_error = error
            if self._monotonic() >= deadline:
                break
            self._sleep(self.ready_interval)

        logs = ""
        if self.container_name:
            result = self._run_command(
                [self.docker_bin, "logs", "--tail", "40", self.container_name],
                action="read TileServer GL logs",
                check=False,
            )
            logs = self._result_text(result, "stderr") or self._result_text(
                result, "stdout"
            )
        detail = self._redact_text(str(last_error)) if last_error else "no response"
        if logs.strip():
            detail = f"{detail}; container log: {self._redact_text(logs.strip())[-2000:]}"
        raise ProtomapsError(
            f"TileServer GL did not become ready at {redact_url(ready_url)} within {self.ready_timeout:g}s: {detail}"
        )

    def _run_command(
        self,
        command: list[str],
        *,
        action: str,
        check: bool = True,
    ) -> Any:
        try:
            result = self._run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as error:
            raise ProtomapsError(
                f"Could not {action}: executable '{command[0]}' was not found."
            ) from error
        except OSError as error:
            raise ProtomapsError(
                f"Could not {action}: {self._redact_text(str(error))}"
            ) from error

        return_code = int(getattr(result, "returncode", 0) or 0)
        if check and return_code != 0:
            stderr = self._result_text(result, "stderr").strip()
            stdout = self._result_text(result, "stdout").strip()
            detail = stderr or stdout or f"exit status {return_code}"
            command_text = " ".join(self._redact_argument(item) for item in command)
            raise ProtomapsError(
                f"Could not {action} ({command_text}): {self._redact_text(detail)}"
            )
        return result

    @staticmethod
    def _result_text(result: Any, attribute: str) -> str:
        value = getattr(result, attribute, "")
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value or "")

    def _redact_argument(self, argument: str) -> str:
        if "://" in argument:
            return redact_url(argument)
        return argument

    def _redact_text(self, text: str) -> str:
        if self._raw_build_url:
            text = text.replace(self._raw_build_url, redact_url(self._raw_build_url))
        if self._requested_build_url:
            text = text.replace(
                self._requested_build_url, redact_url(self._requested_build_url)
            )
        return text


__all__ = [
    "BUILDS_METADATA_URL",
    "DEFAULT_DOCKER_IMAGE",
    "BuildInfo",
    "ProtomapsError",
    "ProtomapsSession",
    "redact_url",
]
