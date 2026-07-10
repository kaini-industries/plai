"""Small, deterministic fixtures shared by the offline map tests."""

from __future__ import annotations

import gzip
import io
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image
from pmtiles.tile import (
    Compression,
    Entry,
    TileType,
    serialize_directory,
    serialize_header,
    zxy_to_tileid,
)
from pmtiles.writer import Writer


def image_bytes(
    *,
    image_format: str = "PNG",
    mode: str = "RGBA",
    color: tuple[int, ...] = (20, 80, 160, 128),
    size: tuple[int, int] = (256, 256),
) -> bytes:
    """Return a valid encoded image without reading any fixture files."""
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def create_raster_mbtiles(
    path: Path,
    tiles: Iterable[tuple[int, int, int, bytes]],
    *,
    image_format: str = "png",
) -> Path:
    """Create a minimal raster MBTiles database.

    ``tiles`` contains ``(zoom, x, tms_y, payload)`` rows. The explicit TMS row
    makes coordinate-inversion tests clear and prevents the fixture from
    accidentally duplicating the implementation under test.
    """
    database = sqlite3.connect(path)
    try:
        database.executescript(
            """
            CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE tiles (
                zoom_level INTEGER,
                tile_column INTEGER,
                tile_row INTEGER,
                tile_data BLOB,
                UNIQUE (zoom_level, tile_column, tile_row)
            );
            """
        )
        database.executemany(
            "INSERT INTO metadata(name, value) VALUES (?, ?)",
            (
                ("name", "offline-test"),
                ("format", image_format),
                ("type", "baselayer"),
                ("attribution", "© Offline Test Map contributors"),
            ),
        )
        database.executemany(
            """
            INSERT INTO tiles(zoom_level, tile_column, tile_row, tile_data)
            VALUES (?, ?, ?, ?)
            """,
            tiles,
        )
        database.commit()
    finally:
        database.close()
    return path


def create_vector_pmtiles(
    path: Path,
    tiles: Iterable[tuple[int, int, int, bytes]],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Create a deterministic synthetic vector PMTiles v3 archive.

    Tile payloads are gzip-compressed to match the MVT compression declared in
    the header.  They intentionally need not contain real protobuf features:
    these tests exercise archive addressing and byte preservation, not vector
    rendering.
    """

    rows = sorted(
        (
            zxy_to_tileid(z, x, y),
            payload if payload.startswith(b"\x1f\x8b") else gzip.compress(payload),
        )
        for z, x, y, payload in tiles
    )
    if not rows:
        raise ValueError("A PMTiles fixture requires at least one tile.")

    fixture_metadata: dict[str, Any] = {
        "name": "plai-python-extractor-test",
        "format": "pbf",
        "attribution": "Synthetic test data",
        "vector_layers": [
            {
                "id": "fixture",
                "description": "Synthetic PMTiles extraction fixture",
                "minzoom": 0,
                "maxzoom": 15,
                "fields": {},
            }
        ],
    }
    if metadata:
        fixture_metadata.update(metadata)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as archive:
        writer = Writer(archive)
        for tile_id, payload in rows:
            writer.write_tile(tile_id, payload)
        writer.finalize(
            {
                "tile_compression": Compression.GZIP,
                "tile_type": TileType.MVT,
                "min_lon_e7": -1800000000,
                "min_lat_e7": -850511288,
                "max_lon_e7": 1800000000,
                "max_lat_e7": 850511288,
                "center_zoom": 0,
                "center_lon_e7": 0,
                "center_lat_e7": 0,
            },
            fixture_metadata,
        )
    return path


def _write_raw_vector_pmtiles(
    path: Path,
    *,
    root: bytes,
    metadata: bytes,
    leaves: bytes,
    tile_data: bytes,
) -> Path:
    """Write synthetic PMTiles sections with internally consistent offsets."""

    root_offset = 127
    metadata_offset = root_offset + len(root)
    leaf_offset = metadata_offset + len(metadata)
    tile_offset = leaf_offset + len(leaves)
    header = serialize_header(
        {
            "root_offset": root_offset,
            "root_length": len(root),
            "metadata_offset": metadata_offset,
            "metadata_length": len(metadata),
            "leaf_directory_offset": leaf_offset,
            "leaf_directory_length": len(leaves),
            "tile_data_offset": tile_offset,
            "tile_data_length": len(tile_data),
            "addressed_tiles_count": 1,
            "tile_entries_count": 1,
            "tile_contents_count": 1,
            "clustered": True,
            "internal_compression": Compression.GZIP,
            "tile_compression": Compression.GZIP,
            "tile_type": TileType.MVT,
            "min_zoom": 0,
            "max_zoom": 0,
            "min_lon_e7": -1800000000,
            "min_lat_e7": -850511288,
            "max_lon_e7": 1800000000,
            "max_lat_e7": 850511288,
            "center_zoom": 0,
            "center_lon_e7": 0,
            "center_lat_e7": 0,
        }
    )
    path.write_bytes(header + root + metadata + leaves + tile_data)
    return path


def create_metadata_gzip_bomb_pmtiles(path: Path, expanded_size: int) -> Path:
    """Create an archive whose metadata expands beyond a caller-supplied limit."""

    tile_data = gzip.compress(b"tile", mtime=0)
    root = serialize_directory([Entry(0, 0, len(tile_data), 1)])
    metadata = gzip.compress(b" " * expanded_size, mtime=0)
    return _write_raw_vector_pmtiles(
        path,
        root=root,
        metadata=metadata,
        leaves=b"",
        tile_data=tile_data,
    )


def create_leaf_gzip_bomb_pmtiles(path: Path, expanded_size: int) -> Path:
    """Create an archive whose selected leaf directory expands excessively."""

    leaf = gzip.compress(b"\0" * expanded_size, mtime=0)
    root = serialize_directory([Entry(0, 0, len(leaf), 0)])
    metadata = gzip.compress(
        json.dumps(
            {
                "name": "leaf-gzip-bomb-test",
                "format": "pbf",
                "vector_layers": [{"id": "fixture", "fields": {}}],
            }
        ).encode("utf-8"),
        mtime=0,
    )
    return _write_raw_vector_pmtiles(
        path,
        root=root,
        metadata=metadata,
        leaves=leaf,
        tile_data=gzip.compress(b"tile", mtime=0),
    )


@dataclass
class RangeRequest:
    """One request observed by :class:`RangeServer`."""

    path: str
    range_header: str | None
    if_match: str | None
    if_unmodified_since: str | None
    accept_encoding: str | None
    status: int | None = None


class RangeServer:
    """Loopback HTTP server with strict byte ranges and stable ETags.

    Fault toggles make protocol failures deterministic without mocking
    ``urllib`` internals.  ``mutate_etag_after=1`` changes the validator after
    the first request, for example, causing a correct second ``If-Match`` to
    receive HTTP 412.
    """

    _RANGE = re.compile(r"^bytes=(\d+)-(\d+)$")

    def __init__(
        self,
        payload: bytes,
        *,
        etag: str | None = '"fixture-v1"',
        last_modified: str | None = None,
        serve_full_response: bool = False,
        malformed_content_range: bool = False,
        short_body: bool = False,
        truncate_requests: Iterable[int] = (),
        mutate_etag_after: int | None = None,
        force_412_after: int | None = None,
    ) -> None:
        self.payload = payload
        self.etag = etag
        self.last_modified = last_modified
        self.serve_full_response = serve_full_response
        self.malformed_content_range = malformed_content_range
        self.short_body = short_body
        self.truncate_requests = frozenset(truncate_requests)
        self.mutate_etag_after = mutate_etag_after
        self.force_412_after = force_412_after
        self.requests: list[RangeRequest] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._httpd is None:
            raise RuntimeError("RangeServer is not running")
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/fixture.pmtiles"

    def __enter__(self) -> "RangeServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                owner._handle(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._httpd = None
        self._thread = None

    def _record(self, handler: BaseHTTPRequestHandler) -> tuple[RangeRequest, int]:
        request = RangeRequest(
            path=handler.path,
            range_header=handler.headers.get("Range"),
            if_match=handler.headers.get("If-Match"),
            if_unmodified_since=handler.headers.get("If-Unmodified-Since"),
            accept_encoding=handler.headers.get("Accept-Encoding"),
        )
        with self._lock:
            self.requests.append(request)
            return request, len(self.requests)

    def _send_empty(
        self,
        handler: BaseHTTPRequestHandler,
        request: RangeRequest,
        status: int,
    ) -> None:
        request.status = status
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.send_header("Connection", "close")
        handler.end_headers()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        request, request_number = self._record(handler)
        active_etag = self.etag
        if (
            self.mutate_etag_after is not None
            and request_number > self.mutate_etag_after
        ):
            active_etag = '"fixture-v2"'

        if (
            self.force_412_after is not None
            and request_number > self.force_412_after
        ) or (request.if_match is not None and request.if_match != active_etag):
            self._send_empty(handler, request, 412)
            return

        if self.serve_full_response:
            request.status = 200
            handler.send_response(200)
            handler.send_header("Content-Length", str(len(self.payload)))
            handler.send_header("Content-Type", "application/vnd.pmtiles")
            if active_etag is not None:
                handler.send_header("ETag", active_etag)
            if self.last_modified is not None:
                handler.send_header("Last-Modified", self.last_modified)
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(self.payload)
            return

        match = self._RANGE.fullmatch(request.range_header or "")
        if match is None:
            self._send_empty(handler, request, 416)
            return
        start, requested_end = (int(value) for value in match.groups())
        if start >= len(self.payload) or requested_end < start:
            request.status = 416
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{len(self.payload)}")
            handler.send_header("Content-Length", "0")
            handler.send_header("Connection", "close")
            handler.end_headers()
            return

        end = min(requested_end, len(self.payload) - 1)
        body = self.payload[start : end + 1]
        should_truncate = self.short_body or request_number in self.truncate_requests
        transmitted = body[:-1] if should_truncate and body else body
        content_range = (
            "bytes malformed"
            if self.malformed_content_range
            else f"bytes {start}-{end}/{len(self.payload)}"
        )

        request.status = 206
        handler.send_response(206)
        handler.send_header("Content-Type", "application/vnd.pmtiles")
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Range", content_range)
        handler.send_header("Content-Length", str(len(body)))
        if active_etag is not None:
            handler.send_header("ETag", active_etag)
        if self.last_modified is not None:
            handler.send_header("Last-Modified", self.last_modified)
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(transmitted)
