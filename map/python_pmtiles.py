"""Bounded in-process extraction of vector PMTiles archives.

The public Protomaps basemap is a very large remote PMTiles archive.  This
module reads only the directories and tile payload ranges needed by a map
plan, then writes a small local PMTiles archive for TileServer GL.  A server
that ignores Range requests is rejected before its response body is read.
"""

from __future__ import annotations

import concurrent.futures
import gzip
import http.client
import importlib.metadata
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pmtiles.reader import all_tiles
from pmtiles.tile import (
    Compression,
    TileType,
    deserialize_directory,
    deserialize_header,
    find_tile,
    tileid_to_zxy,
    zxy_to_tileid,
)
from pmtiles.writer import Writer


HEADER_SIZE = 127
ROOT_BLOCK_SIZE = 16 * 1024
MAX_DIRECTORY_DEPTH = 4
MAX_COALESCED_BYTES = 8 * 1024 * 1024
MAX_OVERFETCH_RATIO = 0.05
MAX_SELECTED_TILE_IDS = 2_000_000
MAX_METADATA_BYTES = 4 * 1024 * 1024
MAX_METADATA_DECOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_DIRECTORY_DECOMPRESSED_BYTES = 16 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 500_000
WEB_MERCATOR_LIMIT = 85.05112878
USER_AGENT = "Plai-Offline-Map-Packager/2.0"
_CONTENT_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$", re.IGNORECASE)


class PythonPmtilesError(RuntimeError):
    """Raised when a remote archive cannot be safely extracted."""


@dataclass(frozen=True)
class ExtractionPiece:
    """One inclusive zoom interval and optional non-wrapping WGS84 bbox."""

    min_zoom: int
    max_zoom: int
    bounds: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """Non-secret extraction statistics suitable for manifest provenance."""

    package_version: str
    source_size: int
    etag: str | None
    last_modified: str | None
    selected_tiles: int
    written_tiles: int
    downloaded_bytes: int
    request_count: int


@dataclass(frozen=True)
class _TileReference:
    tile_id: int
    offset: int
    length: int


@dataclass(frozen=True)
class _RangeBatch:
    start: int
    end: int
    spans: tuple[tuple[int, int], ...]

    @property
    def length(self) -> int:
        return self.end - self.start


class _PayloadStore:
    """Disk-backed tile payload slices with bounded process memory use."""

    def __init__(self) -> None:
        self._file = tempfile.TemporaryFile()
        self._index: dict[tuple[int, int], tuple[int, int]] = {}

    def add(self, batch: _RangeBatch, block: bytes) -> None:
        position = self._file.seek(0, os.SEEK_END)
        self._file.write(block)
        for offset, length in batch.spans:
            relative = offset - batch.start
            if relative < 0 or relative + length > len(block):
                raise PythonPmtilesError("A coalesced tile range was truncated.")
            self._index[(offset, length)] = (position + relative, length)

    def get(self, span: tuple[int, int]) -> bytes:
        position, length = self._index[span]
        self._file.seek(position)
        payload = self._file.read(length)
        if len(payload) != length:
            raise PythonPmtilesError("A spooled tile payload was truncated.")
        return payload

    def close(self) -> None:
        self._file.close()


def _redact_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
    except (TypeError, ValueError):
        return "<redacted-url>"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "<redacted-url>"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    netloc = f"{host}:{port}" if port is not None else host
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


def _latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    n = 1 << zoom
    lon = min(180.0, max(-180.0, lon))
    lat = min(WEB_MERCATOR_LIMIT, max(-WEB_MERCATOR_LIMIT, lat))
    x = int(math.floor((lon + 180.0) / 360.0 * n))
    if lon >= 180.0:
        x = n - 1
    lat_radians = math.radians(lat)
    y_value = (
        1.0
        - math.log(math.tan(lat_radians) + 1.0 / math.cos(lat_radians))
        / math.pi
    ) / 2.0 * n
    y = int(math.floor(y_value))
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _normalise_piece(piece: ExtractionPiece) -> tuple[ExtractionPiece, ...]:
    if (
        not isinstance(piece.min_zoom, int)
        or isinstance(piece.min_zoom, bool)
        or not isinstance(piece.max_zoom, int)
        or isinstance(piece.max_zoom, bool)
        or not 0 <= piece.min_zoom <= piece.max_zoom <= 30
    ):
        raise PythonPmtilesError("Extraction zooms must satisfy 0 <= min <= max <= 30.")
    if piece.bounds is None:
        return (piece,)
    try:
        west, south, east, north = (float(value) for value in piece.bounds)
    except (TypeError, ValueError) as error:
        raise PythonPmtilesError(
            "Extraction bounds must be (west, south, east, north)."
        ) from error
    if not all(math.isfinite(value) for value in (west, south, east, north)):
        raise PythonPmtilesError("Extraction bounds must be finite.")
    if not -180.0 <= west <= 180.0 or not -180.0 <= east <= 180.0:
        raise PythonPmtilesError("Extraction longitudes must be between -180 and 180.")
    if not -WEB_MERCATOR_LIMIT <= south < north <= WEB_MERCATOR_LIMIT:
        raise PythonPmtilesError("Extraction latitudes are outside Web Mercator.")
    if west == east:
        raise PythonPmtilesError("Extraction bounds must have a non-zero width.")
    if west < east:
        return (ExtractionPiece(piece.min_zoom, piece.max_zoom, (west, south, east, north)),)
    return (
        ExtractionPiece(piece.min_zoom, piece.max_zoom, (west, south, 180.0, north)),
        ExtractionPiece(piece.min_zoom, piece.max_zoom, (-180.0, south, east, north)),
    )


def _selected_tile_ids(pieces: Sequence[ExtractionPiece]) -> tuple[int, ...]:
    selected: set[int] = set()

    def add(tile_id: int) -> None:
        selected.add(tile_id)
        if len(selected) > MAX_SELECTED_TILE_IDS:
            raise PythonPmtilesError(
                f"Extraction selects more than {MAX_SELECTED_TILE_IDS:,} source tiles."
            )

    for original in pieces:
        for piece in _normalise_piece(original):
            for zoom in range(piece.min_zoom, piece.max_zoom + 1):
                n = 1 << zoom
                if piece.bounds is None:
                    for x in range(n):
                        for y in range(n):
                            add(zxy_to_tileid(zoom, x, y))
                else:
                    west, south, east, north = piece.bounds
                    x_min, y_max = _latlon_to_tile(south, west, zoom)
                    x_max, y_min = _latlon_to_tile(north, east, zoom)
                    if east >= 180.0:
                        x_max = n - 1
                    for x in range(x_min, x_max + 1):
                        for y in range(y_min, y_max + 1):
                            add(zxy_to_tileid(zoom, x, y))
    if not selected:
        raise PythonPmtilesError("The extraction plan selected no source tile IDs.")
    return tuple(sorted(selected))


class _HttpRangeSource:
    def __init__(
        self,
        url: str,
        *,
        timeout: float,
        retries: int,
        opener: object | None,
        sleep: Callable[[float], None],
    ) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PythonPmtilesError("The PMTiles source must be an HTTP or HTTPS URL.")
        self.url = url
        self.safe_url = _redact_url(url)
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or urllib.request.build_opener()
        self.sleep = sleep
        self.total_size: int | None = None
        self.etag: str | None = None
        self.last_modified: str | None = None
        self.request_count = 0
        self.downloaded_bytes = 0
        self._lock = threading.Lock()

    def _open(self, request: urllib.request.Request) -> Any:
        method = getattr(self.opener, "open", None)
        if callable(method):
            return method(request, timeout=self.timeout)
        if callable(self.opener):
            return self.opener(request, timeout=self.timeout)
        raise TypeError("HTTP opener is not callable")

    @staticmethod
    def _retry_after(headers: Mapping[str, str] | Any) -> float | None:
        value = headers.get("Retry-After") if headers is not None else None
        if not value:
            return None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(seconds, 30.0)) if math.isfinite(seconds) else None

    def read(self, offset: int, length: int) -> bytes:
        if offset < 0 or length <= 0:
            raise PythonPmtilesError("Invalid PMTiles byte range requested.")
        with self._lock:
            known_size = self.total_size
        if known_size is not None:
            if offset >= known_size:
                raise PythonPmtilesError("PMTiles byte range starts beyond the archive.")
            length = min(length, known_size - offset)
        end = offset + length - 1

        for attempt in range(self.retries + 1):
            with self._lock:
                etag = self.etag
                modified = self.last_modified
            headers = {
                "Accept": "application/vnd.pmtiles, application/octet-stream",
                "Accept-Encoding": "identity",
                "Range": f"bytes={offset}-{end}",
                "User-Agent": USER_AGENT,
            }
            if etag and not etag.startswith("W/"):
                headers["If-Match"] = etag
            elif modified:
                headers["If-Unmodified-Since"] = modified
            request = urllib.request.Request(self.url, headers=headers)
            try:
                response = self._open(request)
                try:
                    raw_status = getattr(response, "status", None)
                    if raw_status is None:
                        raw_status = response.getcode()
                    status = int(raw_status)
                    if status != 206:
                        raise PythonPmtilesError(
                            f"Range request to {self.safe_url} returned HTTP {status}; "
                            "refusing a possible full-archive download."
                        )
                    content_range = response.headers.get("Content-Range", "")
                    match = _CONTENT_RANGE.fullmatch(content_range.strip())
                    if match is None:
                        raise PythonPmtilesError(
                            f"Range response from {self.safe_url} had an invalid Content-Range."
                        )
                    actual_start, actual_end, total = (
                        int(value) for value in match.groups()
                    )
                    expected_end = min(end, total - 1)
                    if (
                        actual_start != offset
                        or actual_end != expected_end
                        or total <= 0
                        or actual_end < actual_start
                    ):
                        raise PythonPmtilesError(
                            f"Range response from {self.safe_url} did not match the request."
                        )
                    expected_length = actual_end - actual_start + 1
                    declared_length = response.headers.get("Content-Length")
                    if declared_length is not None and int(declared_length) != expected_length:
                        raise PythonPmtilesError(
                            f"Range response from {self.safe_url} declared the wrong length."
                        )
                    response_etag = response.headers.get("ETag")
                    response_modified = response.headers.get("Last-Modified")
                    with self._lock:
                        if self.total_size is not None and self.total_size != total:
                            raise PythonPmtilesError(
                                "The PMTiles source size changed during extraction."
                            )
                        if self.etag and response_etag and self.etag != response_etag:
                            raise PythonPmtilesError(
                                "The PMTiles source ETag changed during extraction."
                            )
                        strong_etag = self.etag and not self.etag.startswith("W/")
                        if (
                            not strong_etag
                            and self.last_modified
                            and response_modified
                            and self.last_modified != response_modified
                        ):
                            raise PythonPmtilesError(
                                "The PMTiles source modification time changed during extraction."
                            )
                        self.total_size = total
                        if self.etag is None and response_etag:
                            self.etag = response_etag
                        if self.last_modified is None and response_modified:
                            self.last_modified = response_modified
                        strong_etag = self.etag and not self.etag.startswith("W/")
                        if not strong_etag and not self.last_modified:
                            raise PythonPmtilesError(
                                "The PMTiles source did not provide a stable ETag or "
                                "Last-Modified validator."
                            )
                    body = response.read(expected_length + 1)
                    if len(body) < expected_length:
                        raise http.client.IncompleteRead(
                            body, expected_length - len(body)
                        )
                    if len(body) > expected_length:
                        raise PythonPmtilesError(
                            f"Range response from {self.safe_url} exceeded its declared length."
                        )
                    with self._lock:
                        self.request_count += 1
                        self.downloaded_bytes += len(body)
                    return body
                finally:
                    response.close()
            except urllib.error.HTTPError as error:
                try:
                    status = int(error.code)
                    retryable = status == 429 or 500 <= status <= 599
                    if not retryable or attempt >= self.retries:
                        raise PythonPmtilesError(
                            f"Range request to {self.safe_url} failed with HTTP {status}."
                        ) from None
                    delay = self._retry_after(error.headers)
                finally:
                    error.close()
            except PythonPmtilesError:
                raise
            except (
                urllib.error.URLError,
                http.client.IncompleteRead,
                TimeoutError,
                OSError,
                ValueError,
            ) as error:
                if attempt >= self.retries:
                    raise PythonPmtilesError(
                        f"Range request to {self.safe_url} failed: {type(error).__name__}."
                    ) from None
                delay = None
            self.sleep(delay if delay is not None else min(0.5 * (2**attempt), 4.0))
        raise AssertionError("unreachable")


def _validate_header(header: Mapping[str, Any], total_size: int) -> None:
    if header.get("version") != 3:
        raise PythonPmtilesError("Only PMTiles v3 archives are supported.")
    if header.get("internal_compression") != Compression.GZIP:
        raise PythonPmtilesError("Only gzip-compressed PMTiles directories are supported.")
    if header.get("tile_type") != TileType.MVT:
        raise PythonPmtilesError("The Protomaps source must contain MVT vector tiles.")
    if not 0 <= int(header.get("min_zoom", -1)) <= int(header.get("max_zoom", -1)) <= 30:
        raise PythonPmtilesError("The PMTiles source has invalid zoom metadata.")

    sections = (
        ("root", int(header["root_offset"]), int(header["root_length"])),
        ("metadata", int(header["metadata_offset"]), int(header["metadata_length"])),
        (
            "leaf directory",
            int(header["leaf_directory_offset"]),
            int(header["leaf_directory_length"]),
        ),
        ("tile data", int(header["tile_data_offset"]), int(header["tile_data_length"])),
    )
    for name, offset, length in sections:
        if offset < HEADER_SIZE or length < 0 or offset + length > total_size:
            raise PythonPmtilesError(f"The PMTiles {name} section is outside the archive.")
    root_end = int(header["root_offset"]) + int(header["root_length"])
    if int(header["root_length"]) <= 0 or root_end > min(ROOT_BLOCK_SIZE, total_size):
        raise PythonPmtilesError("The PMTiles root directory is outside its 16 KiB block.")
    nonempty = [(name, offset, offset + length) for name, offset, length in sections if length]
    for index, (left_name, left_start, left_end) in enumerate(nonempty):
        for right_name, right_start, right_end in nonempty[index + 1 :]:
            if max(left_start, right_start) < min(left_end, right_end):
                raise PythonPmtilesError(
                    f"The PMTiles {left_name} and {right_name} sections overlap."
                )


def _bounded_gzip_decompress(payload: bytes, limit: int, label: str) -> bytes:
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        value = decompressor.decompress(payload, limit + 1)
    except zlib.error as error:
        raise PythonPmtilesError(f"The PMTiles {label} gzip stream is invalid.") from error
    if (
        len(value) > limit
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
    ):
        raise PythonPmtilesError(
            f"The PMTiles {label} gzip stream is truncated or expands beyond its limit."
        )
    return value


def _directory_entries(payload: bytes, *, label: str) -> list[Any]:
    try:
        raw = _bounded_gzip_decompress(
            payload, MAX_DIRECTORY_DECOMPRESSED_BYTES, f"{label} directory"
        )
        entry_count = 0
        shift = 0
        for byte in raw[:10]:
            entry_count |= (byte & 0x7F) << shift
            if byte < 0x80:
                break
            shift += 7
        else:
            raise PythonPmtilesError(
                f"The PMTiles {label} directory has an invalid entry count."
            )
        if entry_count > MAX_DIRECTORY_ENTRIES:
            raise PythonPmtilesError(
                f"The PMTiles {label} directory contains too many entries."
            )
        entries = deserialize_directory(gzip.compress(raw, mtime=0))
    except Exception as error:
        raise PythonPmtilesError(f"The PMTiles {label} directory is invalid.") from error
    if len(entries) != entry_count:
        raise PythonPmtilesError(f"The PMTiles {label} directory count is invalid.")
    previous = -1
    for entry in entries:
        if (
            entry.tile_id <= previous
            or entry.offset < 0
            or entry.length <= 0
            or entry.run_length < 0
        ):
            raise PythonPmtilesError(f"The PMTiles {label} directory is malformed.")
        previous = entry.tile_id
    return entries


def _decode_metadata(payload: bytes, compression: Compression) -> Mapping[str, Any]:
    try:
        if compression == Compression.GZIP:
            payload = _bounded_gzip_decompress(
                payload, MAX_METADATA_DECOMPRESSED_BYTES, "metadata"
            )
        metadata = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PythonPmtilesError("The PMTiles metadata is invalid.") from error
    if not isinstance(metadata, Mapping):
        raise PythonPmtilesError("The PMTiles metadata must be a JSON object.")
    return metadata


class _DirectoryResolver:
    def __init__(
        self,
        source: _HttpRangeSource,
        header: Mapping[str, Any],
        bootstrap: bytes,
    ) -> None:
        self.source = source
        self.header = header
        self.cache: dict[tuple[int, int], list[Any]] = {}
        root_offset = int(header["root_offset"])
        root_length = int(header["root_length"])
        root_bytes = bootstrap[root_offset : root_offset + root_length]
        if len(root_bytes) != root_length:
            root_bytes = source.read(root_offset, root_length)
        self.root_key = (root_offset, root_length)
        self.cache[self.root_key] = _directory_entries(root_bytes, label="root")

    def _directory(self, offset: int, length: int, depth: int) -> list[Any]:
        if length > MAX_COALESCED_BYTES:
            raise PythonPmtilesError("A PMTiles leaf directory is unreasonably large.")
        key = (offset, length)
        if key not in self.cache:
            self.cache[key] = _directory_entries(
                self.source.read(offset, length), label=f"leaf depth {depth}"
            )
        return self.cache[key]

    def locate(self, tile_id: int) -> _TileReference | None:
        offset, length = self.root_key
        for depth in range(MAX_DIRECTORY_DEPTH):
            entry = find_tile(self._directory(offset, length, depth), tile_id)
            if entry is None:
                return None
            if entry.run_length > 0:
                absolute = int(self.header["tile_data_offset"]) + entry.offset
                tile_start = int(self.header["tile_data_offset"])
                tile_end = tile_start + int(self.header["tile_data_length"])
                if absolute < tile_start or absolute + entry.length > tile_end:
                    raise PythonPmtilesError("A PMTiles tile entry is outside tile data.")
                return _TileReference(tile_id, absolute, entry.length)
            offset = int(self.header["leaf_directory_offset"]) + entry.offset
            length = entry.length
            leaf_start = int(self.header["leaf_directory_offset"])
            leaf_end = leaf_start + int(self.header["leaf_directory_length"])
            if offset < leaf_start or offset + length > leaf_end:
                raise PythonPmtilesError("A PMTiles leaf entry is outside leaf data.")
        raise PythonPmtilesError("The PMTiles directory exceeds the supported depth.")


def _coalesce_ranges(spans: Iterable[tuple[int, int]]) -> tuple[_RangeBatch, ...]:
    unique = sorted(set(spans))
    if not unique:
        return ()
    batches: list[_RangeBatch] = []
    current: list[tuple[int, int]] = []
    start = end = data_bytes = 0
    for offset, length in unique:
        if length <= 0:
            raise PythonPmtilesError("A PMTiles tile entry has an invalid length.")
        if length > MAX_COALESCED_BYTES:
            raise PythonPmtilesError("A PMTiles tile payload exceeds the range limit.")
        span_end = offset + length
        if not current:
            current = [(offset, length)]
            start, end, data_bytes = offset, span_end, length
            continue
        candidate_end = max(end, span_end)
        candidate_data = data_bytes + max(0, span_end - max(offset, start))
        candidate_span = candidate_end - start
        overfetch = max(0, candidate_span - candidate_data)
        may_merge = (
            offset <= end
            or (
                candidate_span <= MAX_COALESCED_BYTES
                and overfetch <= candidate_span * MAX_OVERFETCH_RATIO
            )
        )
        if may_merge and candidate_span <= MAX_COALESCED_BYTES:
            current.append((offset, length))
            end = candidate_end
            data_bytes = candidate_data
        else:
            batches.append(_RangeBatch(start, end, tuple(current)))
            current = [(offset, length)]
            start, end, data_bytes = offset, span_end, length
    batches.append(_RangeBatch(start, end, tuple(current)))
    return tuple(batches)


class PythonPmtilesExtractor:
    """Extract a bounded union of Protomaps tiles using only Python packages."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        retries: int = 2,
        workers: int = 4,
        opener: object | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise PythonPmtilesError("PMTiles timeout must be positive.")
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            raise PythonPmtilesError("PMTiles retries cannot be negative.")
        if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 16:
            raise PythonPmtilesError("PMTiles workers must be between 1 and 16.")
        self.timeout = float(timeout)
        self.retries = retries
        self.workers = workers
        self.opener = opener
        self.sleep = sleep or time.sleep

    @staticmethod
    def _metadata(
        source: _HttpRangeSource,
        header: Mapping[str, Any],
        bootstrap: bytes,
    ) -> Mapping[str, Any]:
        offset = int(header["metadata_offset"])
        length = int(header["metadata_length"])
        if length <= 0 or length > MAX_METADATA_BYTES:
            raise PythonPmtilesError("The PMTiles metadata section has an invalid size.")
        payload = bootstrap[offset : offset + length]
        if len(payload) != length:
            payload = source.read(offset, length)
        return _decode_metadata(payload, header["internal_compression"])

    def _fetch_payloads(
        self,
        source: _HttpRangeSource,
        references: Sequence[_TileReference],
    ) -> _PayloadStore:
        batches = _coalesce_ranges((item.offset, item.length) for item in references)
        store = _PayloadStore()

        def fetch(batch: _RangeBatch) -> tuple[_RangeBatch, bytes]:
            return batch, source.read(batch.start, batch.length)

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.workers
            ) as executor:
                remaining = iter(batches)
                pending: set[concurrent.futures.Future[tuple[_RangeBatch, bytes]]] = set()
                for _index in range(self.workers):
                    try:
                        pending.add(executor.submit(fetch, next(remaining)))
                    except StopIteration:
                        break
                while pending:
                    done, pending = concurrent.futures.wait(
                        pending, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for future in done:
                        batch, block = future.result()
                        store.add(batch, block)
                        try:
                            pending.add(executor.submit(fetch, next(remaining)))
                        except StopIteration:
                            pass
            return store
        except BaseException:
            store.close()
            raise

    @staticmethod
    def _payloads_match(
        path: Path,
        references: Sequence[_TileReference],
        payloads: _PayloadStore,
    ) -> bool:
        """Verify Writer de-duplication never changed a selected tile payload."""

        expected = {
            tileid_to_zxy(reference.tile_id): (
                reference.offset,
                reference.length,
            )
            for reference in references
        }
        try:
            size = path.stat().st_size
            with path.open("rb") as archive:
                def read(offset: int, length: int) -> bytes:
                    if offset < 0 or length < 0 or offset + length > size:
                        raise PythonPmtilesError("Extracted tile range is outside the file.")
                    archive.seek(offset)
                    value = archive.read(length)
                    if len(value) != length:
                        raise PythonPmtilesError("Extracted tile range was truncated.")
                    return value

                for coordinate, value in all_tiles(read):
                    span = expected.pop(coordinate, None)
                    if span is None or payloads.get(span) != value:
                        return False
            return not expected
        except Exception:
            return False

    def extract(
        self,
        source_url: str,
        destination: str | os.PathLike[str],
        pieces: Sequence[ExtractionPiece],
    ) -> ExtractionResult:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        selected_ids = _selected_tile_ids(tuple(pieces))
        source = _HttpRangeSource(
            source_url,
            timeout=self.timeout,
            retries=self.retries,
            opener=self.opener,
            sleep=self.sleep,
        )
        bootstrap = source.read(0, ROOT_BLOCK_SIZE)
        if len(bootstrap) < HEADER_SIZE or source.total_size is None:
            raise PythonPmtilesError("The PMTiles source is too short for a v3 header.")
        try:
            header = deserialize_header(bootstrap[:HEADER_SIZE])
        except Exception as error:
            raise PythonPmtilesError("The PMTiles source header is invalid.") from error
        _validate_header(header, source.total_size)
        metadata = self._metadata(source, header, bootstrap)
        resolver = _DirectoryResolver(source, header, bootstrap)
        references = tuple(
            reference
            for tile_id in selected_ids
            if (reference := resolver.locate(tile_id)) is not None
        )
        if not references:
            raise PythonPmtilesError("No selected tiles were present in the PMTiles source.")
        payloads = self._fetch_payloads(source, references)

        temporary = destination_path.with_name(
            f".{destination_path.name}.{uuid.uuid4().hex}.part"
        )
        try:
            with temporary.open("w+b") as output:
                writer = Writer(output)
                try:
                    for reference in references:
                        writer.write_tile(
                            reference.tile_id,
                            payloads.get((reference.offset, reference.length)),
                        )
                    writer.finalize(dict(header), dict(metadata))
                finally:
                    if not writer.tile_f.closed:
                        writer.tile_f.close()
                output.flush()
                os.fsync(output.fileno())
            if not self.validate(temporary):
                raise PythonPmtilesError("The extracted PMTiles archive failed validation.")
            if not self._payloads_match(temporary, references, payloads):
                raise PythonPmtilesError(
                    "The extracted PMTiles archive did not preserve every tile payload."
                )
            os.replace(temporary, destination_path)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            payloads.close()

        try:
            package_version = importlib.metadata.version("pmtiles")
        except importlib.metadata.PackageNotFoundError:
            package_version = "unknown"
        return ExtractionResult(
            package_version=package_version,
            source_size=source.total_size,
            etag=source.etag,
            last_modified=source.last_modified,
            selected_tiles=len(selected_ids),
            written_tiles=len(references),
            downloaded_bytes=source.downloaded_bytes,
            request_count=source.request_count,
        )

    @staticmethod
    def validate(path: str | os.PathLike[str]) -> bool:
        try:
            archive_path = Path(path)
            total_size = archive_path.stat().st_size
            if total_size < HEADER_SIZE:
                return False
            with archive_path.open("rb") as archive:
                def read(offset: int, length: int) -> bytes:
                    if offset < 0 or length < 0 or offset + length > total_size:
                        raise PythonPmtilesError("Local PMTiles range is outside the file.")
                    archive.seek(offset)
                    payload = archive.read(length)
                    if len(payload) != length:
                        raise PythonPmtilesError("Local PMTiles range was truncated.")
                    return payload

                header = deserialize_header(read(0, HEADER_SIZE))
                _validate_header(header, total_size)
                metadata_length = int(header["metadata_length"])
                if not 0 < metadata_length <= MAX_METADATA_BYTES:
                    return False
                metadata = _decode_metadata(
                    read(int(header["metadata_offset"]), metadata_length),
                    header["internal_compression"],
                )
                if not isinstance(metadata, Mapping):
                    return False

                seen_directories: set[tuple[int, int]] = set()
                content_spans: set[tuple[int, int]] = set()
                addressed = 0
                entry_count = 0
                min_id: int | None = None
                max_id: int | None = None

                def walk(offset: int, length: int, depth: int) -> None:
                    nonlocal addressed, entry_count, min_id, max_id
                    if depth >= MAX_DIRECTORY_DEPTH:
                        raise PythonPmtilesError("Local PMTiles directory is too deep.")
                    if not 0 < length <= MAX_COALESCED_BYTES:
                        raise PythonPmtilesError(
                            "Local PMTiles directory has an invalid size."
                        )
                    key = (offset, length)
                    if key in seen_directories:
                        raise PythonPmtilesError("Local PMTiles directory contains a cycle.")
                    seen_directories.add(key)
                    entries = _directory_entries(read(offset, length), label="local")
                    for entry in entries:
                        if entry.run_length == 0:
                            if not 0 < entry.length <= MAX_COALESCED_BYTES:
                                raise PythonPmtilesError(
                                    "Local PMTiles leaf has an invalid size."
                                )
                            leaf_offset = int(header["leaf_directory_offset"]) + entry.offset
                            leaf_start = int(header["leaf_directory_offset"])
                            leaf_end = leaf_start + int(header["leaf_directory_length"])
                            if leaf_offset < leaf_start or leaf_offset + entry.length > leaf_end:
                                raise PythonPmtilesError(
                                    "Local PMTiles leaf is outside its section."
                                )
                            walk(leaf_offset, entry.length, depth + 1)
                            continue
                        absolute = int(header["tile_data_offset"]) + entry.offset
                        tile_start = int(header["tile_data_offset"])
                        tile_end = tile_start + int(header["tile_data_length"])
                        if absolute < tile_start or absolute + entry.length > tile_end:
                            raise PythonPmtilesError("Local PMTiles tile is outside its section.")
                        addressed += entry.run_length
                        entry_count += 1
                        content_spans.add((entry.offset, entry.length))
                        first = entry.tile_id
                        last = entry.tile_id + entry.run_length - 1
                        min_id = first if min_id is None else min(min_id, first)
                        max_id = last if max_id is None else max(max_id, last)

                walk(int(header["root_offset"]), int(header["root_length"]), 0)
                if addressed <= 0 or min_id is None or max_id is None:
                    return False
                if addressed != int(header["addressed_tiles_count"]):
                    return False
                if entry_count != int(header["tile_entries_count"]):
                    return False
                if len(content_spans) != int(header["tile_contents_count"]):
                    return False
                if tileid_to_zxy(min_id)[0] != int(header["min_zoom"]):
                    return False
                if tileid_to_zxy(max_id)[0] != int(header["max_zoom"]):
                    return False
            return True
        except Exception:
            return False


__all__ = [
    "ExtractionPiece",
    "ExtractionResult",
    "PythonPmtilesError",
    "PythonPmtilesExtractor",
]
