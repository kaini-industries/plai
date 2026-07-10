#!/usr/bin/env python3
"""Build policy-compliant offline JPEG tile packs for Plai firmware.

The default workflow downloads an ODbL Protomaps build, extracts only the
requested zooms/region, and renders it through a loopback-only TileServer GL
container.  Authorized XYZ endpoints and existing raster MBTiles archives are
also supported.  Public community tile servers are intentionally rejected:
they are interactive map services, not bulk-download APIs.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import re
import sys
from pathlib import Path
from typing import Sequence

try:
    from protomaps_backend import (
        DEFAULT_DOCKER_IMAGE,
        ProtomapsError,
        ProtomapsSession,
    )
    from tile_downloader import (
        DEFAULT_TERMS_URL,
        DEFAULT_USER_AGENT,
        FIRMWARE_STYLES,
        AccessBlockedError,
        ConfigurationError,
        HttpTileSource,
        PolicyError,
        RasterMbtilesSource,
        TilePackError,
        TilePackager,
        TilePlan,
        build_tile_plan,
        format_bytes,
        print_plan,
        validate_http_tile_template,
        validate_output_configuration,
    )
except ImportError:  # pragma: no cover - supports ``python -m map...``
    from .protomaps_backend import (
        DEFAULT_DOCKER_IMAGE,
        ProtomapsError,
        ProtomapsSession,
    )
    from .tile_downloader import (
        DEFAULT_TERMS_URL,
        DEFAULT_USER_AGENT,
        FIRMWARE_STYLES,
        AccessBlockedError,
        ConfigurationError,
        HttpTileSource,
        PolicyError,
        RasterMbtilesSource,
        TilePackError,
        TilePackager,
        TilePlan,
        build_tile_plan,
        format_bytes,
        print_plan,
        validate_http_tile_template,
        validate_output_configuration,
    )


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "plai-map"
DEFAULT_MAX_TILES = 25_000
PROTOMAPS_ATTRIBUTION = (
    "Map data © OpenStreetMap contributors; map design © Protomaps"
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a firmware-ready offline JPEG map pack",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Public OpenStreetMap, CARTO, and OpenTopoMap tile endpoints are "
            "not bulk-download sources. Use Protomaps, an authorized/self-hosted "
            "XYZ endpoint, or a licensed raster MBTiles archive."
        ),
    )
    parser.add_argument(
        "--source",
        choices=("protomaps", "local", "mbtiles"),
        default="protomaps",
        help="input source workflow",
    )
    parser.add_argument("--lat", type=float, required=True, help="center latitude")
    parser.add_argument("--lon", type=float, required=True, help="center longitude")
    parser.add_argument(
        "--radius", type=float, default=50.0, help="regional high-zoom radius in km"
    )
    parser.add_argument("--min-zoom", type=int, default=2, help="lowest zoom")
    parser.add_argument(
        "--global-zoom",
        type=int,
        default=5,
        help="download complete global levels through this zoom",
    )
    parser.add_argument("--max-zoom", type=int, default=12, help="highest zoom")
    parser.add_argument(
        "--style",
        choices=FIRMWARE_STYLES,
        default="osm",
        help="firmware style directory; Protomaps supports osm and dark",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="output style directory (default: map/<style>)",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan only; do no I/O")
    parser.add_argument(
        "--max-tiles",
        type=int,
        default=DEFAULT_MAX_TILES,
        help="safety ceiling checked before source access",
    )
    parser.add_argument(
        "--allow-large-download",
        action="store_true",
        help="acknowledge and bypass --max-tiles",
    )

    image = parser.add_argument_group("firmware JPEG output")
    image.add_argument("--contrast", type=float, default=1.0)
    image.add_argument("--brightness", type=float, default=1.0)
    image.add_argument("--saturation", type=float, default=1.0)
    image.add_argument(
        "--background", default="#ffffff", help="matte color for transparent pixels"
    )
    image.add_argument("--jpeg-quality", type=int, default=75)
    image.add_argument(
        "--force",
        action="store_true",
        help="clear existing numeric tile trees and rebuild this pack",
    )
    image.add_argument(
        "--no-verify-cache",
        action="store_true",
        help="skip full cached-JPEG decode while still checking format metadata",
    )
    image.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=3,
        help="abort after this many adjacent source failures",
    )

    local = parser.add_argument_group("authorized local/licensed XYZ source")
    local.add_argument(
        "--tile-url", help="XYZ URL containing {z}, {x}, and {y} placeholders"
    )
    local.add_argument(
        "--attribution", help="required source credit (or MBTiles metadata fallback)"
    )
    local.add_argument("--terms-url", default="", help="source license/terms URL")
    local.add_argument(
        "--header-env",
        action="append",
        default=[],
        metavar="HEADER=ENV_VAR",
        help="read an HTTP header value from an environment variable; repeatable",
    )
    local.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    local.add_argument("--timeout", type=float, default=15.0, help="HTTP timeout seconds")
    local.add_argument("--retries", type=int, default=2, help="bounded HTTP retries")
    local.add_argument(
        "--request-delay",
        type=float,
        default=0.0,
        help="minimum delay between authorized endpoint requests",
    )

    mbtiles = parser.add_argument_group("raster MBTiles source")
    mbtiles.add_argument("--mbtiles", type=Path, help="existing raster MBTiles file")

    protomaps = parser.add_argument_group("Protomaps renderer")
    protomaps.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="PMTiles/cache directory"
    )
    protomaps.add_argument(
        "--protomaps-build-url",
        help="specific PMTiles build URL (otherwise resolve latest compatible build)",
    )
    protomaps.add_argument(
        "--protomaps-key",
        help="build key substituted into {key} in --protomaps-build-url",
    )
    protomaps.add_argument(
        "--pmtiles-extractor",
        choices=("python", "cli"),
        default="python",
        help="PMTiles extraction implementation; cli requires the external binary",
    )
    protomaps.add_argument(
        "--pmtiles-bin",
        default="pmtiles",
        help="pmtiles executable used only with --pmtiles-extractor cli",
    )
    protomaps.add_argument("--docker-bin", default="docker")
    protomaps.add_argument("--tileserver-image", default=DEFAULT_DOCKER_IMAGE)
    protomaps.add_argument(
        "--tileserver-port",
        type=int,
        default=18080,
        help="loopback port for the temporary renderer",
    )
    return parser


def _header_environment(items: Sequence[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in items:
        name, separator, environment_name = item.partition("=")
        if not separator or not _HEADER_NAME.fullmatch(name):
            raise ConfigurationError(
                f"invalid --header-env {item!r}; expected HEADER=ENV_VAR"
            )
        if not _ENV_NAME.fullmatch(environment_name):
            raise ConfigurationError(
                f"invalid environment variable in --header-env {item!r}"
            )
        lowered = name.lower()
        if any(existing.lower() == lowered for existing in headers):
            raise ConfigurationError(f"duplicate HTTP header {name!r}")
        headers[name] = "{env:" + environment_name + "}"
    return headers


def _validate_args(args: argparse.Namespace) -> dict[str, str]:
    if args.max_tiles < 1:
        raise ConfigurationError("--max-tiles must be positive")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ConfigurationError("--timeout must be positive")
    if args.retries < 0:
        raise ConfigurationError("--retries cannot be negative")
    if not math.isfinite(args.request_delay) or args.request_delay < 0:
        raise ConfigurationError("--request-delay cannot be negative")
    if not 1 <= args.tileserver_port <= 65535:
        raise ConfigurationError("--tileserver-port must be between 1 and 65535")
    for name in ("contrast", "brightness", "saturation"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise ConfigurationError(f"--{name} must be positive")
    if not 1 <= args.jpeg_quality <= 95:
        raise ConfigurationError("--jpeg-quality must be between 1 and 95")
    if args.max_consecutive_errors < 1:
        raise ConfigurationError("--max-consecutive-errors must be positive")

    headers = _header_environment(args.header_env)
    if args.source == "protomaps":
        if args.style not in {"osm", "dark"}:
            raise ConfigurationError(
                "the Protomaps renderer supports --style osm or dark; use an "
                "authorized local/MBTiles source for voyager or topo"
            )
    elif args.source == "local":
        if not args.tile_url:
            raise ConfigurationError("--source local requires --tile-url")
        if not args.attribution or not args.attribution.strip():
            raise ConfigurationError("--source local requires --attribution")
        validate_http_tile_template(args.tile_url)
    elif args.source == "mbtiles" and args.mbtiles is None:
        raise ConfigurationError("--source mbtiles requires --mbtiles")
    return headers


def _output_path(args: argparse.Namespace) -> Path:
    return (args.output or SCRIPT_DIR / args.style).expanduser().resolve()


def _print_job(args: argparse.Namespace, plan: TilePlan, output: Path) -> None:
    print("Plai offline map packager")
    print(f"  source: {args.source}")
    print(f"  style: {args.style}")
    print(
        f"  center/radius: {plan.center_lat:.6f}, {plan.center_lon:.6f} / "
        f"{plan.radius_km:g} km"
    )
    print(f"  output: {output}")
    if args.source == "protomaps":
        detail = (
            "in-process pmtiles library"
            if args.pmtiles_extractor == "python"
            else f"external {args.pmtiles_bin!r} executable"
        )
        print(f"  PMTiles extractor: {args.pmtiles_extractor} ({detail})")
    print_plan(plan)
    if args.allow_large_download and plan.total_tiles > args.max_tiles:
        print(
            f"WARNING: --allow-large-download bypassed the {args.max_tiles:,}-tile ceiling"
        )


def _protomaps_source(
    args: argparse.Namespace,
    plan: TilePlan,
    stack: contextlib.ExitStack,
) -> HttpTileSource:
    session = ProtomapsSession(
        bounds=plan.regional_bounds,
        min_zoom=plan.min_zoom,
        global_zoom=plan.global_zoom,
        max_zoom=plan.max_zoom,
        style=args.style,
        cache_dir=args.cache_dir,
        pmtiles_extractor=args.pmtiles_extractor,
        pmtiles_bin=args.pmtiles_bin,
        docker_bin=args.docker_bin,
        docker_image=args.tileserver_image,
        port=args.tileserver_port,
        build_url=args.protomaps_build_url,
        key=args.protomaps_key,
    )
    stack.callback(session.close)
    session.prepare()
    provenance = {
        "workflow": "protomaps",
        "build_key": session.build_key,
        "build_version": session.build_version,
        "build_url": session.build_url,
        "archive": session.archive_path.name if session.archive_path else None,
        "extractor": session.extractor_provenance,
        "renderer_image": args.tileserver_image,
    }
    source = HttpTileSource(
        session.url_template,
        name="protomaps-loopback-renderer",
        attribution=PROTOMAPS_ATTRIBUTION,
        terms_url=DEFAULT_TERMS_URL,
        timeout=args.timeout,
        retries=args.retries,
        request_delay=args.request_delay,
        extra_provenance=provenance,
        fingerprint_override={
            "kind": "protomaps",
            "build_key": session.build_key,
            "build_version": session.build_version,
            "build_url": session.build_url,
            "build_identity_sha256": session.build_identity_sha256,
            "style_sha256": session.style_sha256,
            "renderer_image": args.tileserver_image,
        },
    )
    stack.callback(source.close)
    return source


def _open_source(
    args: argparse.Namespace,
    plan: TilePlan,
    headers: dict[str, str],
    stack: contextlib.ExitStack,
):
    if args.source == "protomaps":
        return _protomaps_source(args, plan, stack)
    if args.source == "local":
        source = HttpTileSource(
            args.tile_url,
            name="authorized-local-xyz",
            attribution=args.attribution,
            terms_url=args.terms_url,
            headers=headers,
            user_agent=args.user_agent,
            timeout=args.timeout,
            retries=args.retries,
            request_delay=args.request_delay,
        )
        stack.callback(source.close)
        return source
    source = RasterMbtilesSource(
        args.mbtiles,
        attribution=args.attribution,
        terms_url=args.terms_url,
    )
    stack.callback(source.close)
    return source


def _print_summary(summary, output: Path) -> None:
    print("Summary:")
    print(f"  planned: {summary.planned:,}")
    print(f"  downloaded: {summary.downloaded:,}")
    print(f"  cached: {summary.cached:,}")
    print(f"  converted legacy PNGs: {summary.converted:,}")
    print(f"  failed: {summary.failed:,}")
    print(f"  newly written: {format_bytes(summary.bytes_written)}")
    print(f"  output: {output}")
    if summary.errors:
        print("  first source errors:")
        for error in summary.errors[:5]:
            print(f"    - {error}")
    if summary.complete:
        print(
            f"Copy the '{output.name}' directory to /sdcard/map/{output.name}/ "
            "and keep ATTRIBUTION.txt with it."
        )


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        headers = _validate_args(args)
        hard_limit = None if args.allow_large_download else args.max_tiles
        plan = build_tile_plan(
            args.lat,
            args.lon,
            args.radius,
            min_zoom=args.min_zoom,
            global_zoom=args.global_zoom,
            max_zoom=args.max_zoom,
            hard_limit=hard_limit,
        )
        output = _output_path(args)
        _print_job(args, plan, output)
        if args.dry_run:
            if args.source == "protomaps":
                print(
                    "  Prerequisites were not checked during dry-run; the real "
                    "run requires Docker and "
                    + (
                        "pmtiles>=3.7,<4 from map/requirements.txt."
                        if args.pmtiles_extractor == "python"
                        else f"the {args.pmtiles_bin!r} pmtiles executable."
                    )
                )
            print(
                "Dry run complete; no network, process, or filesystem writes "
                "performed."
            )
            return 0

        validate_output_configuration(
            style=args.style,
            contrast=args.contrast,
            brightness=args.brightness,
            saturation=args.saturation,
            background=args.background,
            jpeg_quality=args.jpeg_quality,
            max_consecutive_errors=args.max_consecutive_errors,
        )

        with contextlib.ExitStack() as stack:
            source = _open_source(args, plan, headers, stack)
            packager = TilePackager(
                plan,
                source,
                output,
                style=args.style,
                contrast=args.contrast,
                brightness=args.brightness,
                saturation=args.saturation,
                background=args.background,
                jpeg_quality=args.jpeg_quality,
                force=args.force,
                verify_cache=not args.no_verify_cache,
                max_consecutive_errors=args.max_consecutive_errors,
            )
            summary = packager.run()
        _print_summary(summary, output)
        return 0 if summary.complete else 1
    except (ConfigurationError, PolicyError, AccessBlockedError, ProtomapsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted; the incomplete manifest prevents firmware from using "
            "the pack.",
            file=sys.stderr,
        )
        return 130
    except TilePackError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
