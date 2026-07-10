from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

TEST_DIR = Path(__file__).resolve().parent
MAP_DIR = TEST_DIR.parent
if str(MAP_DIR) not in sys.path:
    sys.path.insert(0, str(MAP_DIR))

from protomaps_backend import ProtomapsSession  # noqa: E402


PMTILES_FIXTURE = b"PMTiles\x03" + bytes(119)


class ProtomapsBackendTests(unittest.TestCase):
    def test_prepare_extracts_region_starts_loopback_renderer_and_is_idempotent(self) -> None:
        commands: list[list[str]] = []
        fetched: list[str] = []

        def fake_which(name: str) -> str:
            return f"/test-bin/{name}"

        def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            command = [str(item) for item in argv]
            commands.append(command)
            if len(command) > 1 and command[1] == "extract":
                Path(command[3]).write_bytes(PMTILES_FIXTURE)
            elif len(command) > 1 and command[1] == "merge":
                Path(command[-1]).write_bytes(PMTILES_FIXTURE)
            stdout = "offline-test-container\n" if "--detach" in command else ""
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        def fake_fetch(url: str, **_kwargs: object) -> bytes:
            fetched.append(url)
            return b"{}"

        with tempfile.TemporaryDirectory() as temporary:
            session = ProtomapsSession(
                bounds=(-75.0, 39.0, -73.0, 41.0),
                min_zoom=1,
                global_zoom=2,
                max_zoom=4,
                style="dark",
                cache_dir=Path(temporary),
                pmtiles_extractor="cli",
                build_url="https://build.protomaps.com/20260518.pmtiles",
                run=fake_run,
                fetch=fake_fetch,
                which=fake_which,
                sleep=lambda _seconds: None,
                monotonic=lambda: 0.0,
            )

            prepared = session.prepare()
            command_count = len(commands)
            session.prepare()

            self.assertIs(prepared, session)
            self.assertEqual(len(commands), command_count)
            self.assertTrue(session.archive_path.is_file())
            self.assertTrue(session.archive_path.read_bytes().startswith(b"PMTiles\x03"))
            self.assertIn("{z}", session.url_template)
            self.assertIn("{x}", session.url_template)
            self.assertIn("{y}", session.url_template)
            self.assertTrue(any(command[1:2] == ["extract"] for command in commands))
            self.assertTrue(any(command[1:2] == ["verify"] for command in commands))
            self.assertTrue(any("--bbox=" in argument for command in commands for argument in command))
            self.assertTrue(any("--maxzoom=" in argument for command in commands for argument in command))
            self.assertTrue(any(command[1:3] == ["run", "--detach"] for command in commands))
            self.assertEqual(
                fetched,
                ["http://127.0.0.1:8080/styles/plai.json"],
            )
            self.assertEqual(list(Path(temporary).glob(".*.lock")), [])

            session.close()
            self.assertTrue(any(command[1:3] == ["rm", "--force"] for command in commands))

    def test_dry_run_performs_no_fetch_or_subprocess(self) -> None:
        def forbidden(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("dry run performed external work")

        with tempfile.TemporaryDirectory() as temporary:
            session = ProtomapsSession(
                bounds=(-1.0, -1.0, 1.0, 1.0),
                min_zoom=1,
                global_zoom=1,
                max_zoom=2,
                style="osm",
                cache_dir=Path(temporary),
                build_url="https://build.protomaps.com/20260518.pmtiles",
                run=forbidden,
                fetch=forbidden,
                which=forbidden,
            )

            self.assertIs(session.prepare(dry_run=True), session)
            session.close()

    def test_keyboard_interrupt_during_container_start_removes_container(self) -> None:
        commands: list[list[str]] = []

        def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            command = [str(item) for item in argv]
            commands.append(command)
            if len(command) > 1 and command[1] == "extract":
                Path(command[3]).write_bytes(PMTILES_FIXTURE)
            if command[1:3] == ["run", "--detach"]:
                raise KeyboardInterrupt
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            session = ProtomapsSession(
                bounds=(-75.0, 39.0, -73.0, 41.0),
                min_zoom=1,
                global_zoom=1,
                max_zoom=1,
                style="dark",
                cache_dir=Path(temporary),
                pmtiles_extractor="cli",
                build_url="https://build.protomaps.com/20260518.pmtiles",
                run=fake_run,
                fetch=lambda *_args, **_kwargs: b"{}",
                which=lambda name: f"/test-bin/{name}",
            )

            with self.assertRaises(KeyboardInterrupt):
                session.prepare()

        self.assertTrue(any(command[1:3] == ["rm", "--force"] for command in commands))

    def test_python_extractor_handles_all_pieces_without_pmtiles_cli(self) -> None:
        commands: list[list[str]] = []
        which_calls: list[str] = []

        class FakePythonExtractor:
            def __init__(self) -> None:
                self.calls: list[tuple[str, Path, tuple[object, ...]]] = []

            def extract(self, source: str, destination: Path, pieces) -> object:
                pieces = tuple(pieces)
                self.calls.append((source, Path(destination), pieces))
                Path(destination).write_bytes(PMTILES_FIXTURE)
                return SimpleNamespace(
                    package_version="3.7.0",
                    source_size=4096,
                    etag='"private-validator"',
                    last_modified="Fri, 10 Jul 2026 12:00:00 GMT",
                    selected_tiles=7,
                    written_tiles=6,
                    downloaded_bytes=2048,
                    request_count=3,
                )

            def validate(self, path: Path) -> bool:
                return Path(path).read_bytes().startswith(b"PMTiles\x03")

        extractor = FakePythonExtractor()

        def fake_which(name: str) -> str:
            which_calls.append(name)
            if name == "pmtiles":
                raise AssertionError("Python mode searched for the pmtiles CLI")
            return f"/test-bin/{name}"

        def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
            command = [str(item) for item in argv]
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temporary:
            session = ProtomapsSession(
                bounds=((-1.0, -1.0, 1.0, 1.0), (170.0, -1.0, 180.0, 1.0)),
                min_zoom=1,
                global_zoom=1,
                max_zoom=2,
                style="osm",
                cache_dir=Path(temporary),
                build_url="https://build.protomaps.com/20260518.pmtiles",
                python_extractor=extractor,
                run=fake_run,
                fetch=lambda *_args, **_kwargs: b"{}",
                which=fake_which,
                sleep=lambda _seconds: None,
                monotonic=lambda: 0.0,
            )

            session.prepare()
            self.assertEqual(len(extractor.calls), 1)
            _source, _destination, pieces = extractor.calls[0]
            self.assertEqual(len(pieces), 3)
            self.assertEqual(which_calls, ["docker"])
            self.assertFalse(any(command[0] == "pmtiles" for command in commands))
            self.assertEqual(session.extractor_provenance["mode"], "python")
            self.assertEqual(session.extractor_provenance["selected_tiles"], 7)
            sidecars = list(Path(temporary).glob("*.cache.json"))
            self.assertEqual(len(sidecars), 1)
            sidecar_text = sidecars[0].read_text(encoding="utf-8")
            self.assertNotIn("https://", sidecar_text)
            self.assertNotIn("private-validator", sidecar_text)
            self.assertIn("etag_sha256", sidecar_text)
            archive_path = session.archive_path
            session.close()

            # Extractor implementation is provenance, not semantic identity:
            # explicit CLI mode can reuse the validated Python-created entry.
            cli_session = ProtomapsSession(
                bounds=((-1.0, -1.0, 1.0, 1.0), (170.0, -1.0, 180.0, 1.0)),
                min_zoom=1,
                global_zoom=1,
                max_zoom=2,
                style="osm",
                cache_dir=Path(temporary),
                pmtiles_extractor="cli",
                build_url="https://build.protomaps.com/20260518.pmtiles",
                run=fake_run,
                fetch=lambda *_args, **_kwargs: b"{}",
                which=lambda name: f"/test-bin/{name}",
                sleep=lambda _seconds: None,
                monotonic=lambda: 0.0,
            )
            cli_session.prepare()
            self.assertEqual(cli_session.archive_path, archive_path)
            self.assertTrue(cli_session.extract_cache_hit)
            self.assertFalse(
                any(
                    command[0] == "pmtiles"
                    and command[1:2] in (["extract"], ["merge"])
                    for command in commands
                )
            )
            cli_session.close()

    def test_python_extractor_interrupt_cleans_owned_partial(self) -> None:
        class InterruptingExtractor:
            def extract(self, _source: str, destination: Path, _pieces) -> object:
                Path(destination).write_bytes(PMTILES_FIXTURE)
                raise KeyboardInterrupt

            def validate(self, _path: Path) -> bool:
                return True

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            session = ProtomapsSession(
                bounds=(-1.0, -1.0, 1.0, 1.0),
                min_zoom=1,
                global_zoom=1,
                max_zoom=1,
                style="osm",
                cache_dir=cache,
                build_url="https://build.protomaps.com/20260518.pmtiles",
                python_extractor=InterruptingExtractor(),
                run=lambda *_args, **_kwargs: SimpleNamespace(
                    returncode=0, stdout="", stderr=""
                ),
                fetch=lambda *_args, **_kwargs: b"{}",
                which=lambda name: f"/test-bin/{name}",
            )

            with self.assertRaises(KeyboardInterrupt):
                session.prepare()

            self.assertEqual(list(cache.glob("*.part.pmtiles")), [])
            self.assertEqual(list(cache.glob("*.pmtiles")), [])
            self.assertEqual(list(cache.glob(".*.lock")), [])

    def test_custom_query_is_hashed_into_private_build_identity(self) -> None:
        common = {
            "bounds": (-1.0, -1.0, 1.0, 1.0),
            "min_zoom": 1,
            "global_zoom": 1,
            "max_zoom": 1,
            "style": "osm",
        }
        first = ProtomapsSession(**common)
        second = ProtomapsSession(**common)
        first._set_resolved_build(
            "https://example.test/archive.pmtiles?dataset=streets", None
        )
        second._set_resolved_build(
            "https://example.test/archive.pmtiles?dataset=terrain", None
        )

        self.assertEqual(first.build_url, second.build_url)
        self.assertNotEqual(
            first.build_identity_sha256, second.build_identity_sha256
        )


if __name__ == "__main__":
    unittest.main()
