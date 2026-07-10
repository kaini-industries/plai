from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest import mock

TEST_DIR = Path(__file__).resolve().parent
MAP_DIR = TEST_DIR.parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

import download_osm_tiles as cli  # noqa: E402
import tile_downloader as tiles  # noqa: E402
from support import create_raster_mbtiles, image_bytes  # noqa: E402


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], payload: bytes) -> None:
        self.status = status
        self.headers = headers
        self.payload = payload

    def read(self, limit: int = -1) -> bytes:
        return self.payload if limit < 0 else self.payload[:limit]

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        return


class SequenceOpener:
    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]]) -> None:
        self.responses = list(responses)
        self.requests: list[str] = []

    def __call__(self, request: Any, **_kwargs: Any) -> FakeResponse:
        self.requests.append(request.full_url)
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        status, headers, payload = self.responses.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                payload.decode("utf-8", errors="replace"),
                headers,
                None,
            )
        return FakeResponse(status, headers, payload)


def source_factory(opener: SequenceOpener):
    def build(*args: Any, **kwargs: Any) -> tiles.HttpTileSource:
        kwargs["opener"] = opener
        kwargs["sleeper"] = lambda _seconds: None
        return tiles.HttpTileSource(*args, **kwargs)

    return build


class DownloadCliTests(unittest.TestCase):
    def run_cli(self, args: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = cli.run(args)
        return code, stream.getvalue()

    @staticmethod
    def plan_args(output: Path) -> list[str]:
        return [
            "--lat",
            "0",
            "--lon",
            "0",
            "--radius",
            "1",
            "--min-zoom",
            "1",
            "--global-zoom",
            "1",
            "--max-zoom",
            "1",
            "--max-tiles",
            "10",
            "--output",
            str(output),
        ]

    def test_protomaps_dry_run_starts_no_source_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            args = [
                "--source",
                "protomaps",
                "--style",
                "dark",
                "--dry-run",
                *self.plan_args(output),
            ]
            with mock.patch.object(
                cli, "_open_source", side_effect=AssertionError("source opened")
            ):
                code, text = self.run_cli(args)

            self.assertEqual(code, 0, text)
            self.assertIn("Dry run complete", text)
            self.assertFalse(output.exists())

    def test_public_osm_endpoint_is_rejected_before_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "osm"
            args = [
                "--source",
                "local",
                "--tile-url",
                "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
                "--attribution",
                "© OpenStreetMap contributors",
                *self.plan_args(output),
            ]
            with mock.patch.object(
                cli, "_open_source", side_effect=AssertionError("source opened")
            ):
                code, text = self.run_cli(args)

            self.assertEqual(code, 2, text)
            self.assertIn("does not authorize offline tile packs", text)
            self.assertFalse(output.exists())

    def test_tile_ceiling_is_enforced_before_source_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            args = [
                "--source",
                "protomaps",
                "--style",
                "dark",
                "--max-tiles",
                "1",
                *self.plan_args(output),
            ]
            # plan_args supplies max-tiles=10 later; replace its final value.
            last_limit = len(args) - 1 - args[::-1].index("--max-tiles")
            args[last_limit + 1] = "1"
            with mock.patch.object(
                cli, "_open_source", side_effect=AssertionError("source opened")
            ):
                code, text = self.run_cli(args)

            self.assertEqual(code, 2, text)
            self.assertIn("hard limit of 1", text)
            self.assertFalse(output.exists())

    def test_local_source_builds_and_resumes_complete_firmware_pack(self) -> None:
        payload = image_bytes()
        opener = SequenceOpener(
            [(200, {"Content-Type": "image/png"}, payload)] * 4
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "voyager"
            args = [
                "--source",
                "local",
                "--style",
                "voyager",
                "--tile-url",
                "https://licensed.example.test/{z}/{x}/{y}.png",
                "--attribution",
                "© Authorized Offline Tiles",
                "--terms-url",
                "https://example.test/terms",
                *self.plan_args(output),
            ]

            with mock.patch.object(
                cli, "HttpTileSource", new=source_factory(opener)
            ):
                first_code, first_text = self.run_cli(args)
                second_code, second_text = self.run_cli(args)

            self.assertEqual(first_code, 0, first_text)
            self.assertEqual(second_code, 0, second_text)
            self.assertEqual(len(opener.requests), 4)
            self.assertEqual(len(list(output.rglob("*.jpg"))), 4)
            manifest = json.loads((output / "tileset.json").read_text())
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["counts"]["cached"], 4)
            self.assertIn(
                "© Authorized Offline Tiles",
                (output / "ATTRIBUTION.txt").read_text(),
            )

    def test_403_is_fatal_even_when_retries_are_configured(self) -> None:
        opener = SequenceOpener(
            [(403, {"Content-Type": "text/plain"}, b"Access blocked")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            args = [
                "--source",
                "local",
                "--style",
                "dark",
                "--tile-url",
                "https://licensed.example.test/{z}/{x}/{y}.png",
                "--attribution",
                "© Authorized Offline Tiles",
                "--retries",
                "5",
                *self.plan_args(output),
            ]

            with mock.patch.object(
                cli, "HttpTileSource", new=source_factory(opener)
            ):
                code, text = self.run_cli(args)

            self.assertEqual(code, 2, text)
            self.assertEqual(len(opener.requests), 1)
            manifest = json.loads((output / "tileset.json").read_text())
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["status"], "blocked")

    def test_mbtiles_source_builds_complete_pack(self) -> None:
        payload = image_bytes()
        rows = [
            (1, x, 1 - xyz_y, payload)
            for x in range(2)
            for xyz_y in range(2)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = create_raster_mbtiles(root / "fixture.mbtiles", rows)
            output = root / "topo"
            args = [
                "--source",
                "mbtiles",
                "--style",
                "topo",
                "--mbtiles",
                str(archive),
                *self.plan_args(output),
            ]

            code, text = self.run_cli(args)

            self.assertEqual(code, 0, text)
            self.assertEqual(len(list(output.rglob("*.jpg"))), 4)
            manifest = json.loads((output / "tileset.json").read_text())
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["source"]["kind"], "raster-mbtiles")

    def test_bad_image_configuration_is_rejected_before_source_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "dark"
            args = [
                "--source",
                "protomaps",
                "--style",
                "dark",
                "--background",
                "not-a-color",
                *self.plan_args(output),
            ]
            with mock.patch.object(
                cli, "_open_source", side_effect=AssertionError("source opened")
            ):
                code, text = self.run_cli(args)

            self.assertEqual(code, 2, text)
            self.assertIn("invalid background color", text)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
