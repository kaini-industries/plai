from __future__ import annotations

import http.client
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from PIL import Image

TEST_DIR = Path(__file__).resolve().parent
MAP_DIR = TEST_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

import tile_downloader as tiles  # noqa: E402
from support import create_raster_mbtiles, image_bytes  # noqa: E402


class MemorySource:
    """Small in-memory source used to test packaging without a socket."""

    name = "memory-source"
    attribution = "© Offline Test Map contributors"
    terms_url = "https://example.test/terms"

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requests: list[tiles.TileCoord] = []

    def fetch(self, tile: tiles.TileCoord) -> bytes:
        self.requests.append(tile)
        return self.payload

    def provenance(self) -> Mapping[str, Any]:
        return {"kind": "memory", "name": self.name}

    def fingerprint_data(self) -> Mapping[str, Any]:
        return {"kind": "memory", "fixture": "solid-rgba"}

    def close(self) -> None:
        return


class BlockingSource(MemorySource):
    def fetch(self, tile: tiles.TileCoord) -> bytes:
        self.requests.append(tile)
        raise tiles.AccessBlockedError("fixture denied access")


def one_tile_plan(tile: tiles.TileCoord = tiles.TileCoord(1, 0, 0)) -> tiles.TilePlan:
    return tiles.TilePlan(
        center_lat=0.0,
        center_lon=0.0,
        radius_km=0.0,
        min_zoom=tile.z,
        global_zoom=tile.z,
        max_zoom=tile.z,
        regional_bounds=((-180.0, -85.0, 180.0, 85.0),),
        tiles=(tile,),
        counts_by_zoom={tile.z: 1},
    )


class PlanningTests(unittest.TestCase):
    def test_plan_includes_global_levels_and_regional_higher_zoom(self) -> None:
        plan = tiles.build_tile_plan(
            10.0,
            10.0,
            1.0,
            min_zoom=1,
            global_zoom=2,
            max_zoom=3,
        )

        self.assertEqual(plan.counts_by_zoom[1], 4)
        self.assertEqual(plan.counts_by_zoom[2], 16)
        self.assertGreater(plan.counts_by_zoom[3], 0)
        self.assertLess(plan.counts_by_zoom[3], 64)

    def test_radius_crossing_antimeridian_wraps_without_duplicates(self) -> None:
        plan = tiles.build_tile_plan(
            0.0,
            179.8,
            100.0,
            min_zoom=1,
            global_zoom=1,
            max_zoom=3,
        )
        regional = [tile for tile in plan if tile.z == 3]

        self.assertEqual(len(plan.regional_bounds), 2)
        self.assertEqual({tile.x for tile in regional}, {0, 7})
        self.assertEqual(
            len(regional),
            len({(tile.z, tile.x, tile.y) for tile in regional}),
        )

    def test_zero_radius_has_non_degenerate_extract_bounds(self) -> None:
        plan = tiles.build_tile_plan(
            40.0,
            -74.0,
            0.0,
            min_zoom=1,
            global_zoom=1,
            max_zoom=2,
        )

        self.assertEqual(plan.counts_by_zoom[2], 1)
        for west, south, east, north in plan.regional_bounds:
            self.assertLess(west, east)
            self.assertLess(south, north)


class HttpSourceTests(unittest.TestCase):
    def test_403_is_fail_fast_and_does_not_expose_environment_secrets(self) -> None:
        opened_urls: list[str] = []

        class ForbiddenResponse:
            status = 403
            headers: Mapping[str, str] = {}

            def __enter__(self) -> "ForbiddenResponse":
                return self

            def __exit__(self, *_exc_info: object) -> None:
                return

        def forbidden(request: Any, **_kwargs: Any) -> Any:
            opened_urls.append(request.full_url)
            return ForbiddenResponse()

        source = tiles.HttpTileSource(
            "https://tiles.example.test/{z}/{x}/{y}.png?token={env:TILE_TOKEN}",
            attribution="© Authorized Example Tiles",
            headers={"Authorization": "Bearer {env:AUTH_TOKEN}"},
            retries=5,
            environ={
                "TILE_TOKEN": "url-secret-value",
                "AUTH_TOKEN": "header-secret-value",
            },
            opener=forbidden,
            sleeper=lambda _seconds: None,
        )

        with self.assertRaises(tiles.AccessBlockedError) as caught:
            source.fetch(tiles.TileCoord(1, 0, 0))

        self.assertEqual(len(opened_urls), 1)
        public_text = json.dumps(
            {
                "error": str(caught.exception),
                "provenance": source.provenance(),
                "fingerprint": source.fingerprint_data(),
            }
        )
        self.assertNotIn("url-secret-value", public_text)
        self.assertNotIn("header-secret-value", public_text)
        self.assertIn("TILE_TOKEN", public_text)
        self.assertIn("AUTH_TOKEN", public_text)

    def test_known_public_tile_hosts_are_rejected_before_access(self) -> None:
        with self.assertRaises(tiles.PolicyError):
            tiles.HttpTileSource(
                "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                attribution="© OpenStreetMap contributors",
            )

    def test_authorized_http_source_requires_attribution(self) -> None:
        with self.assertRaises(tiles.ConfigurationError):
            tiles.HttpTileSource(
                "https://tiles.example.test/{z}/{x}/{y}.png",
                attribution="",
            )

    def test_literal_query_credentials_are_redacted_from_provenance(self) -> None:
        source = tiles.HttpTileSource(
            "https://tiles.example.test/{z}/{x}/{y}.png?token=literal-secret",
            attribution="© Authorized Example Tiles",
        )

        public_text = json.dumps(
            {"provenance": source.provenance(), "fingerprint": source.fingerprint_data()}
        )
        self.assertNotIn("literal-secret", public_text)
        self.assertIn("<redacted>", public_text)

    def test_fingerprint_override_ignores_loopback_transport(self) -> None:
        identity = {"kind": "protomaps", "build_key": "20260710.pmtiles"}
        first = tiles.HttpTileSource(
            "http://127.0.0.1:18080/{z}/{x}/{y}.jpg",
            attribution="© OpenStreetMap contributors",
            fingerprint_override=identity,
        )
        second = tiles.HttpTileSource(
            "http://127.0.0.1:18081/{z}/{x}/{y}.jpg",
            attribution="© OpenStreetMap contributors",
            fingerprint_override=identity,
        )

        self.assertEqual(first.fingerprint_data(), second.fingerprint_data())

    def test_query_configuration_changes_source_fingerprint_without_disclosure(self) -> None:
        first = tiles.HttpTileSource(
            "https://tiles.example.test/render?layer=streets&z={z}&x={x}&y={y}",
            attribution="© Authorized Example Tiles",
        )
        second = tiles.HttpTileSource(
            "https://tiles.example.test/render?layer=terrain&z={z}&x={x}&y={y}",
            attribution="© Authorized Example Tiles",
        )

        self.assertNotEqual(first.fingerprint_data(), second.fingerprint_data())
        public_text = json.dumps(first.fingerprint_data())
        self.assertNotIn("streets", public_text)

    def test_cross_origin_redirect_is_rejected_before_following(self) -> None:
        handler = tiles._SameOriginRedirectHandler()
        request = urllib.request.Request(
            "https://licensed.example.test/1/0/0.png"
        )

        with self.assertRaises(tiles.AccessBlockedError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://tile.openstreetmap.org/1/0/0.png",
            )

    def test_expanded_public_host_is_rejected(self) -> None:
        with self.assertRaises(tiles.PolicyError):
            tiles.HttpTileSource(
                "https://{env:TILE_HOST}/{z}/{x}/{y}.png",
                attribution="© OpenStreetMap contributors",
                environ={"TILE_HOST": "tile.openstreetmap.org"},
            )

    def test_invalid_url_error_cannot_disclose_expanded_secret(self) -> None:
        def invalid_url(_request: Any, **_kwargs: Any) -> Any:
            raise http.client.InvalidURL("URL contains secret value")

        source = tiles.HttpTileSource(
            "https://tiles.example.test/{z}/{x}/{y}.png?token={env:TOKEN}",
            attribution="© Authorized Example Tiles",
            environ={"TOKEN": "secret value"},
            opener=invalid_url,
            retries=0,
        )

        with self.assertRaises(tiles.TileSourceError) as caught:
            source.fetch(tiles.TileCoord(1, 0, 0))
        self.assertNotIn("secret value", str(caught.exception))
        self.assertIn("InvalidURL", str(caught.exception))


class MBTilesTests(unittest.TestCase):
    def test_raster_mbtiles_converts_xyz_y_to_tms_row(self) -> None:
        north_tile = image_bytes(color=(220, 20, 20, 255))
        south_tile = image_bytes(color=(20, 220, 20, 255))
        with tempfile.TemporaryDirectory() as temporary:
            database = create_raster_mbtiles(
                Path(temporary) / "fixture.mbtiles",
                (
                    (1, 0, 1, north_tile),  # XYZ y=0 maps to TMS row 1.
                    (1, 0, 0, south_tile),  # XYZ y=1 maps to TMS row 0.
                ),
            )

            with tiles.RasterMbtilesSource(database) as source:
                self.assertEqual(source.fetch(tiles.TileCoord(1, 0, 0)), north_tile)
                self.assertEqual(source.fetch(tiles.TileCoord(1, 0, 1)), south_tile)


class PackagingTests(unittest.TestCase):
    def test_resume_reuses_valid_jpegs_and_repairs_a_corrupt_tile(self) -> None:
        plan = tiles.build_tile_plan(
            0.0,
            0.0,
            1.0,
            min_zoom=1,
            global_zoom=1,
            max_zoom=1,
        )
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"

            first = tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            ).run()
            request_count = len(source.requests)
            second = tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            ).run()

            self.assertEqual(first.downloaded, plan.total_tiles)
            self.assertEqual(second.cached, plan.total_tiles)
            self.assertEqual(len(source.requests), request_count)

            damaged_tile = plan.tiles[0]
            damaged_path = (
                output
                / str(damaged_tile.z)
                / str(damaged_tile.x)
                / f"{damaged_tile.y}.jpg"
            )
            damaged_path.write_bytes(b"not a jpeg")
            repaired = tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            ).run()

            self.assertEqual(repaired.downloaded, 1)
            self.assertEqual(repaired.cached, plan.total_tiles - 1)
            self.assertEqual(len(source.requests), request_count + 1)
            with Image.open(damaged_path) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (256, 256))
            self.assertEqual(list(output.rglob("*.part")), [])

    def test_legacy_rgba_png_is_converted_and_manifest_is_complete(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voyager"
            tiles.TilePackager(
                plan, source, output, style="voyager", progress=lambda _line: None
            ).run()
            source.requests.clear()
            legacy = output / "1" / "0" / "0.png"
            (output / "1" / "0" / "0.jpg").unlink()
            legacy.write_bytes(
                image_bytes(mode="RGBA", color=(0, 80, 200, 128))
            )

            summary = tiles.TilePackager(
                plan,
                source,
                output,
                style="voyager",
                background="#ffffff",
                progress=lambda _line: None,
            ).run()

            self.assertTrue(summary.complete)
            self.assertEqual(summary.converted, 1)
            self.assertEqual(source.requests, [])
            self.assertFalse(legacy.exists())
            with Image.open(output / "1" / "0" / "0.jpg") as rendered:
                self.assertEqual(rendered.format, "JPEG")
                self.assertEqual(rendered.mode, "RGB")
                self.assertEqual(rendered.size, (256, 256))

            manifest_text = (output / "tileset.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["counts"]["converted"], 1)
            self.assertTrue(manifest_text.startswith('{\n  "complete": true'))
            self.assertIn(
                source.attribution,
                (output / "ATTRIBUTION.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(list(output.rglob("*.part")), [])

    def test_unknown_tiles_are_refused_and_force_removes_stale_levels(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"
            unknown = output / "9" / "1" / "1.jpg"
            unknown.parent.mkdir(parents=True)
            unknown.write_bytes(image_bytes(image_format="JPEG", mode="RGB"))

            with self.assertRaises(tiles.ConfigurationError):
                tiles.TilePackager(
                    plan, source, output, style="osm", progress=lambda _line: None
                ).run()

            summary = tiles.TilePackager(
                plan,
                source,
                output,
                style="osm",
                force=True,
                progress=lambda _line: None,
            ).run()

            self.assertTrue(summary.complete)
            self.assertFalse((output / "9").exists())
            self.assertTrue((output / "1" / "0" / "0.jpg").is_file())

    def test_non_object_manifest_cannot_adopt_unknown_tiles(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"
            unknown = output / "1" / "0" / "0.jpg"
            unknown.parent.mkdir(parents=True)
            unknown.write_bytes(
                image_bytes(
                    image_format="JPEG", mode="RGB", color=(20, 80, 160)
                )
            )
            (output / "tileset.json").write_text("[]\n", encoding="utf-8")

            with self.assertRaises(tiles.ConfigurationError):
                tiles.TilePackager(
                    plan, source, output, style="osm", progress=lambda _line: None
                ).run()

    def test_force_refuses_unowned_numeric_directories(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "Pictures"
            unrelated = output / "2024" / "1" / "1.jpg"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(
                image_bytes(
                    image_format="JPEG", mode="RGB", color=(20, 80, 160)
                )
            )

            with self.assertRaises(tiles.ConfigurationError):
                tiles.TilePackager(
                    plan,
                    source,
                    output,
                    style="osm",
                    force=True,
                    progress=lambda _line: None,
                ).run()

            self.assertTrue(unrelated.is_file())
            self.assertFalse((output / ".plai-packager.lock").exists())

    def test_force_interrupt_publishes_incomplete_manifest_before_cleanup(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            tiles.TilePackager(
                plan, source, output, style="dark", progress=lambda _line: None
            ).run()

            with mock.patch.object(tiles.shutil, "rmtree", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    tiles.TilePackager(
                        plan,
                        source,
                        output,
                        style="dark",
                        force=True,
                        progress=lambda _line: None,
                    ).run()

            manifest = json.loads((output / "tileset.json").read_text())
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["status"], "rebuilding")
            self.assertFalse((output / ".plai-packager.lock").exists())

    def test_output_lock_blocks_concurrent_packager(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"
            output.mkdir()
            lock = output / ".plai-packager.lock"
            lock.write_text("pid=123\n")

            with self.assertRaises(tiles.ConfigurationError):
                tiles.TilePackager(
                    plan, source, output, style="osm", progress=lambda _line: None
                ).run()

            self.assertTrue(lock.is_file())

    def test_cleanup_is_scoped_and_complete_run_prunes_old_tiles(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"
            tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            ).run()
            stale_tile = output / "9" / "1" / "1.jpg"
            stale_tile.parent.mkdir(parents=True)
            stale_tile.write_bytes(
                image_bytes(
                    image_format="JPEG", mode="RGB", color=(20, 80, 160)
                )
            )
            unrelated_part = output / "notes" / "draft.part"
            unrelated_part.parent.mkdir()
            unrelated_part.write_text("keep")
            numeric_part = output / "1" / "2" / "important.part"
            numeric_part.parent.mkdir()
            numeric_part.write_text("keep")

            summary = tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            ).run()

            self.assertTrue(summary.complete)
            self.assertFalse(stale_tile.exists())
            self.assertTrue(unrelated_part.is_file())
            self.assertTrue(numeric_part.is_file())

    def test_numeric_symlink_is_rejected_without_touching_external_tiles(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "osm"
            tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            ).run()
            outside = root / "outside"
            victim = outside / "1" / "2.jpg"
            victim.parent.mkdir(parents=True)
            victim.write_bytes(
                image_bytes(
                    image_format="JPEG", mode="RGB", color=(20, 80, 160)
                )
            )
            (output / "9").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(tiles.ConfigurationError):
                tiles.TilePackager(
                    plan, source, output, style="osm", progress=lambda _line: None
                ).run()

            self.assertTrue(victim.is_file())
            self.assertFalse((output / ".plai-packager.lock").exists())

    def test_wrong_size_is_rejected_before_pixel_decode(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        payload = image_bytes(size=(512, 512))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"
            packager = tiles.TilePackager(
                plan, source, output, style="osm", progress=lambda _line: None
            )
            destination = output / "1" / "0" / "0.jpg"

            with mock.patch.object(
                Image.Image, "load", side_effect=AssertionError("decoded")
            ) as load:
                with self.assertRaisesRegex(tiles.TileSourceError, "512x512"):
                    packager._write_jpeg(payload, destination)
            load.assert_not_called()

    def test_progressive_and_cmyk_cached_jpegs_are_rebuilt(self) -> None:
        plan = one_tile_plan()
        source = MemorySource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            path = output / "1" / "0" / "0.jpg"
            tiles.TilePackager(
                plan, source, output, style="dark", progress=lambda _line: None
            ).run()

            Image.new("RGB", (256, 256), (1, 2, 3)).save(
                path, "JPEG", progressive=True
            )
            progressive = tiles.TilePackager(
                plan, source, output, style="dark", progress=lambda _line: None
            ).run()
            self.assertEqual(progressive.downloaded, 1)

            Image.new("CMYK", (256, 256), (1, 2, 3, 4)).save(path, "JPEG")
            cmyk = tiles.TilePackager(
                plan, source, output, style="dark", progress=lambda _line: None
            ).run()
            self.assertEqual(cmyk.downloaded, 1)
            with Image.open(path) as rebuilt:
                self.assertEqual(rebuilt.mode, "RGB")
                self.assertFalse(rebuilt.info.get("progressive"))

    def test_blocked_pack_leaves_atomic_valid_incomplete_manifest(self) -> None:
        plan = one_tile_plan()
        source = BlockingSource(image_bytes())
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            packager = tiles.TilePackager(
                plan, source, output, style="dark", progress=lambda _line: None
            )

            with self.assertRaises(tiles.AccessBlockedError):
                packager.run()

            manifest_text = (output / "tileset.json").read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["status"], "blocked")
            self.assertTrue(manifest_text.startswith('{\n  "complete": false'))
            self.assertEqual(list(output.rglob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
