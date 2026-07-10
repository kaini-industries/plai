from __future__ import annotations

import hashlib
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
MAP_DIR = TEST_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

from pmtiles.reader import MemorySource, Reader, all_tiles  # noqa: E402
from pmtiles.tile import TileType, tileid_to_zxy  # noqa: E402

from python_pmtiles import (  # noqa: E402
    ExtractionPiece,
    PythonPmtilesError,
    PythonPmtilesExtractor,
)
from support import (  # noqa: E402
    RangeServer,
    create_leaf_gzip_bomb_pmtiles,
    create_metadata_gzip_bomb_pmtiles,
    create_vector_pmtiles,
)


def archive_tiles(path: Path) -> dict[tuple[int, int, int], bytes]:
    payload = path.read_bytes()
    return dict(all_tiles(MemorySource(payload)))


def archive_reader(path: Path) -> Reader:
    return Reader(MemorySource(path.read_bytes()))


def high_entropy_payload(index: int, size: int = 2048) -> bytes:
    return hashlib.shake_256(f"tile-{index}".encode("ascii")).digest(size)


class PythonPmtilesExtractorTests(unittest.TestCase):
    def extractor(self, *, workers: int = 4) -> PythonPmtilesExtractor:
        return PythonPmtilesExtractor(
            timeout=2.0,
            retries=0,
            workers=workers,
            sleep=lambda _seconds: None,
        )

    def test_global_regional_antimeridian_and_sparse_tiles_round_trip(self) -> None:
        coordinates_and_labels = [
            (0, 0, 0, b"global-z0"),
            (1, 0, 0, b"global-north-west"),
            (1, 1, 1, b"global-south-east"),
            (2, 1, 1, b"regional-center"),
            (2, 0, 1, b"antimeridian-west"),
            (2, 3, 1, b"antimeridian-east"),
            (2, 2, 1, b"outside-regions"),
            (3, 4, 4, b"wrong-zoom-sparse-tile"),
        ]
        # Keep the source larger than the 16 KiB directory bootstrap request,
        # so this integration case also proves subsequent If-Match behavior.
        rows = [
            (z, x, y, label + high_entropy_payload(index, 4096))
            for index, (z, x, y, label) in enumerate(coordinates_and_labels)
        ]
        expected_coordinates = {
            (0, 0, 0),
            (1, 0, 0),
            (1, 1, 1),
            (2, 1, 1),
            (2, 0, 1),
            (2, 3, 1),
        }
        pieces = (
            ExtractionPiece(min_zoom=0, max_zoom=1),
            ExtractionPiece(
                min_zoom=2,
                max_zoom=2,
                bounds=(-89.0, 1.0, -1.0, 60.0),
            ),
            ExtractionPiece(
                min_zoom=2,
                max_zoom=2,
                bounds=(-179.0, 1.0, -170.0, 60.0),
            ),
            ExtractionPiece(
                min_zoom=2,
                max_zoom=2,
                bounds=(170.0, 1.0, 179.0, 60.0),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(root / "source.pmtiles", rows)
            source_tiles = archive_tiles(source)
            destination = root / "subset.pmtiles"

            with RangeServer(source.read_bytes()) as server:
                result = self.extractor().extract(server.url, destination, pieces)
                requests = list(server.requests)

            self.assertTrue(destination.is_file())
            self.assertTrue(self.extractor().validate(destination))
            self.assertEqual(result.etag, '"fixture-v1"')
            self.assertEqual(result.request_count, len(requests))
            self.assertEqual(result.selected_tiles, 8)
            self.assertEqual(result.written_tiles, len(expected_coordinates))
            actual = archive_tiles(destination)
            self.assertEqual(set(actual), expected_coordinates)
            self.assertEqual(
                actual,
                {
                    coordinate: source_tiles[coordinate]
                    for coordinate in expected_coordinates
                },
            )
            reader = archive_reader(destination)
            self.assertEqual(reader.header()["tile_type"], TileType.MVT)
            self.assertEqual(
                reader.metadata()["vector_layers"][0]["id"], "fixture"
            )
            self.assertGreater(len(requests), 1)
            self.assertIsNone(requests[0].if_match)
            self.assertTrue(all(request.range_header for request in requests))
            self.assertTrue(
                all(request.accept_encoding == "identity" for request in requests)
            )
            self.assertTrue(
                all(request.if_match == '"fixture-v1"' for request in requests[1:])
            )
            self.assertEqual(list(root.glob("*part*")), [])

    def test_crossing_antimeridian_piece_and_overlap_are_deduplicated(self) -> None:
        rows = [
            (2, 0, 1, b"west" + high_entropy_payload(100, 8192)),
            (2, 3, 1, b"east" + high_entropy_payload(101, 8192)),
            (2, 2, 1, b"outside" + high_entropy_payload(102, 8192)),
        ]
        pieces = (
            ExtractionPiece(
                min_zoom=2,
                max_zoom=2,
                bounds=(170.0, 1.0, -170.0, 60.0),
            ),
            # This is wholly inside the east half of the crossing piece and
            # must not duplicate its selected tile ID or output entry.
            ExtractionPiece(
                min_zoom=2,
                max_zoom=2,
                bounds=(175.0, 5.0, 179.0, 55.0),
            ),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(root / "source.pmtiles", rows)
            destination = root / "subset.pmtiles"
            with RangeServer(source.read_bytes()) as server:
                result = self.extractor().extract(server.url, destination, pieces)

            self.assertEqual(result.selected_tiles, 2)
            self.assertEqual(result.written_tiles, 2)
            self.assertEqual(set(archive_tiles(destination)), {(2, 0, 1), (2, 3, 1)})

    def test_many_sparse_tiles_use_leaf_directories_and_bounded_range_count(self) -> None:
        # Random sparse IDs and variable payload sizes keep the directory from
        # gzip-compressing into a tiny root.  This exercises leaf traversal as
        # well as the 8 MiB coalesced-read ceiling with a modest (~12 MB)
        # fixture.
        zoom = 9
        first_tile_id = ((1 << (zoom * 2)) - 1) // 3
        population = 1 << (zoom * 2)
        rng = random.Random(0x504D5449)
        tile_ids = sorted(rng.sample(range(first_tile_id, first_tile_id + population), 30_000))
        rows = [
            (
                *tileid_to_zxy(tile_id),
                high_entropy_payload(index, 256 + (index % 257)),
            )
            for index, tile_id in enumerate(tile_ids)
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(root / "leaf-source.pmtiles", rows)
            self.assertGreater(source.stat().st_size, 8 * 1024 * 1024)
            source_header = archive_reader(source).header()
            self.assertGreater(source_header["leaf_directory_length"], 0)
            destination = root / "leaf-subset.pmtiles"

            with RangeServer(source.read_bytes()) as server:
                self.extractor(workers=4).extract(
                    server.url,
                    destination,
                    (ExtractionPiece(min_zoom=zoom, max_zoom=zoom),),
                )
                requests = list(server.requests)

            output_header = archive_reader(destination).header()
            self.assertEqual(output_header["addressed_tiles_count"], len(rows))
            self.assertGreater(output_header["leaf_directory_length"], 0)
            self.assertEqual(len(archive_tiles(destination)), len(rows))
            self.assertLess(len(requests), 100)
            self.assertTrue(all(request.range_header for request in requests))

            spans = []
            for request in requests:
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.range_header or "")
                self.assertIsNotNone(match)
                assert match is not None
                spans.append(int(match.group(2)) - int(match.group(1)) + 1)
            self.assertTrue(
                any(span > 4096 for span in spans),
                "tile data was not coalesced into useful range requests",
            )
            self.assertLessEqual(
                max(spans),
                8 * 1024 * 1024,
                "a coalesced HTTP response exceeded the bounded-read ceiling",
            )

    def test_rejects_server_that_ignores_range_and_returns_200(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(
                root / "source.pmtiles", [(0, 0, 0, b"fixture")]
            )
            destination = root / "subset.pmtiles"
            with RangeServer(source.read_bytes(), serve_full_response=True) as server:
                with self.assertRaises(PythonPmtilesError):
                    self.extractor().extract(
                        server.url,
                        destination,
                        (ExtractionPiece(min_zoom=0, max_zoom=0),),
                    )

            self.assertFalse(destination.exists())

    def test_rejects_malformed_content_range_and_short_response_body(self) -> None:
        for fault in ("malformed-content-range", "short-body"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = create_vector_pmtiles(
                    root / "source.pmtiles", [(0, 0, 0, b"fixture")]
                )
                destination = root / "subset.pmtiles"
                options = (
                    {"malformed_content_range": True}
                    if fault == "malformed-content-range"
                    else {"short_body": True}
                )
                with RangeServer(source.read_bytes(), **options) as server:
                    with self.assertRaises(PythonPmtilesError):
                        self.extractor().extract(
                            server.url,
                            destination,
                            (ExtractionPiece(min_zoom=0, max_zoom=0),),
                        )

                self.assertFalse(destination.exists())

    def test_etag_mutation_and_explicit_412_abort_extraction(self) -> None:
        rows = [
            (5, index % 32, index // 32, high_entropy_payload(index))
            for index in range(128)
        ]
        for options in (
            {"mutate_etag_after": 1},
            {"force_412_after": 1},
        ):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = create_vector_pmtiles(root / "source.pmtiles", rows)
                destination = root / "subset.pmtiles"
                with RangeServer(source.read_bytes(), **options) as server:
                    with self.assertRaises(PythonPmtilesError):
                        self.extractor().extract(
                            server.url,
                            destination,
                            (ExtractionPiece(min_zoom=5, max_zoom=5),),
                        )
                    requests = list(server.requests)

                self.assertFalse(destination.exists())
                self.assertGreater(len(requests), 1)
                self.assertIsNone(requests[0].if_match)
                self.assertTrue(
                    any(request.if_match == '"fixture-v1"' for request in requests[1:])
                )
                self.assertTrue(any(request.status == 412 for request in requests))

    def test_missing_or_weak_only_validator_is_rejected(self) -> None:
        validators = (None, 'W/"fixture-v1"')
        for etag in validators:
            with self.subTest(etag=etag), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = create_vector_pmtiles(
                    root / "source.pmtiles", [(0, 0, 0, b"fixture")]
                )
                destination = root / "subset.pmtiles"
                with RangeServer(
                    source.read_bytes(), etag=etag, last_modified=None
                ) as server:
                    with self.assertRaises(PythonPmtilesError) as raised:
                        self.extractor().extract(
                            server.url,
                            destination,
                            (ExtractionPiece(min_zoom=0, max_zoom=0),),
                        )

                self.assertIn("stable ETag", str(raised.exception))
                self.assertFalse(destination.exists())
                self.assertEqual(len(server.requests), 1)

    def test_initial_truncation_retries_with_learned_etag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(
                root / "source.pmtiles", [(0, 0, 0, b"fixture")]
            )
            destination = root / "subset.pmtiles"
            extractor = PythonPmtilesExtractor(
                timeout=2.0,
                retries=1,
                workers=1,
                sleep=lambda _seconds: None,
            )
            with RangeServer(
                source.read_bytes(), truncate_requests={1}
            ) as server:
                result = extractor.extract(
                    server.url,
                    destination,
                    (ExtractionPiece(min_zoom=0, max_zoom=0),),
                )
                requests = list(server.requests)

            self.assertTrue(extractor.validate(destination))
            self.assertEqual(result.etag, '"fixture-v1"')
            self.assertGreaterEqual(len(requests), 2)
            self.assertEqual(result.request_count, len(requests) - 1)
            self.assertEqual(requests[0].range_header, requests[1].range_header)
            self.assertIsNone(requests[0].if_match)
            self.assertEqual(requests[1].if_match, '"fixture-v1"')
            self.assertTrue(
                all(request.if_match == '"fixture-v1"' for request in requests[1:])
            )

    def test_error_redacts_query_secret(self) -> None:
        secret = "super-secret-token-value"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(
                root / "source.pmtiles", [(0, 0, 0, b"fixture")]
            )
            destination = root / "subset.pmtiles"
            with RangeServer(source.read_bytes(), serve_full_response=True) as server:
                source_url = f"{server.url}?access_token={secret}"
                with self.assertRaises(PythonPmtilesError) as raised:
                    self.extractor().extract(
                        source_url,
                        destination,
                        (ExtractionPiece(min_zoom=0, max_zoom=0),),
                    )

            self.assertNotIn(secret, str(raised.exception))
            self.assertFalse(destination.exists())

    def test_corrupt_source_is_rejected_without_promotion(self) -> None:
        corruptions = {
            "bad-magic": lambda payload: b"X" + payload[1:],
            "root-outside-file": self._root_outside_file,
        }
        for label, corrupt in corruptions.items():
            with self.subTest(corruption=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = create_vector_pmtiles(
                    root / "source.pmtiles", [(0, 0, 0, b"fixture")]
                )
                destination = root / "subset.pmtiles"
                with RangeServer(corrupt(source.read_bytes())) as server:
                    with self.assertRaises(PythonPmtilesError):
                        self.extractor().extract(
                            server.url,
                            destination,
                            (ExtractionPiece(min_zoom=0, max_zoom=0),),
                        )

                self.assertFalse(destination.exists())
                self.assertEqual(list(root.glob("*part*")), [])

    def test_metadata_and_leaf_directory_gzip_bombs_are_rejected(self) -> None:
        expanded_size = 16 * 1024 * 1024 + 1
        fixtures = (
            ("metadata", create_metadata_gzip_bomb_pmtiles),
            ("leaf", create_leaf_gzip_bomb_pmtiles),
        )
        for label, build in fixtures:
            with self.subTest(section=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = build(root / f"{label}-bomb.pmtiles", expanded_size)
                destination = root / "subset.pmtiles"
                with RangeServer(source.read_bytes()) as server:
                    with self.assertRaises(PythonPmtilesError):
                        self.extractor().extract(
                            server.url,
                            destination,
                            (ExtractionPiece(min_zoom=0, max_zoom=0),),
                        )

                self.assertFalse(destination.exists())
                self.assertEqual(list(root.glob("*part*")), [])

    def test_single_tile_larger_than_range_ceiling_is_rejected(self) -> None:
        oversized = high_entropy_payload(200, 8 * 1024 * 1024 + 1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(
                root / "oversized.pmtiles", [(0, 0, 0, oversized)]
            )
            destination = root / "subset.pmtiles"
            with RangeServer(source.read_bytes()) as server:
                with self.assertRaises(PythonPmtilesError) as raised:
                    self.extractor().extract(
                        server.url,
                        destination,
                        (ExtractionPiece(min_zoom=0, max_zoom=0),),
                    )
                requests = list(server.requests)

            self.assertIn("tile payload exceeds", str(raised.exception))
            self.assertFalse(destination.exists())
            self.assertTrue(requests)
            for request in requests:
                match = re.fullmatch(r"bytes=(\d+)-(\d+)", request.range_header or "")
                self.assertIsNotNone(match)
                assert match is not None
                self.assertLessEqual(
                    int(match.group(2)) - int(match.group(1)) + 1,
                    8 * 1024 * 1024,
                )

    def test_validate_rejects_truncation_and_header_count_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = create_vector_pmtiles(
                root / "valid.pmtiles",
                [
                    (0, 0, 0, b"first"),
                    (1, 1, 1, b"second"),
                ],
            )
            extractor = self.extractor()
            self.assertTrue(extractor.validate(valid))

            truncated = root / "truncated.pmtiles"
            truncated.write_bytes(valid.read_bytes()[:-1])
            self.assertFalse(extractor.validate(truncated))

            wrong_count = bytearray(valid.read_bytes())
            addressed_tiles = int.from_bytes(wrong_count[72:80], "little")
            wrong_count[72:80] = (addressed_tiles + 1).to_bytes(8, "little")
            count_path = root / "wrong-count.pmtiles"
            count_path.write_bytes(wrong_count)
            self.assertFalse(extractor.validate(count_path))
            self.assertFalse(extractor.validate(root / "missing.pmtiles"))

    def test_failure_preserves_existing_destination_and_cleans_temporary_file(self) -> None:
        sentinel = b"existing-cache-must-survive"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_vector_pmtiles(
                root / "source.pmtiles", [(0, 0, 0, b"fixture")]
            )
            destination = root / "subset.pmtiles"
            destination.write_bytes(sentinel)
            corrupt = b"not-a-pmtiles-archive" + source.read_bytes()[23:]

            with RangeServer(corrupt) as server:
                with self.assertRaises(PythonPmtilesError):
                    self.extractor().extract(
                        server.url,
                        destination,
                        (ExtractionPiece(min_zoom=0, max_zoom=0),),
                    )

            self.assertEqual(destination.read_bytes(), sentinel)
            self.assertEqual(list(root.glob("*part*")), [])

    @staticmethod
    def _root_outside_file(payload: bytes) -> bytes:
        corrupted = bytearray(payload)
        corrupted[8:16] = (len(payload) + 1024).to_bytes(8, "little")
        return bytes(corrupted)


if __name__ == "__main__":
    unittest.main()
