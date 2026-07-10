"""Small, deterministic fixtures shared by the offline map tests."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from typing import Iterable

from PIL import Image


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
