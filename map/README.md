# Offline map tile packager

`download_osm_tiles.py` builds the 256 x 256 baseline JPEG tile tree used by
Plai. It packages tiles from a source that permits offline use; it does not
scrape public community tile servers.

## Install

Use Python 3 and install the image and PMTiles dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r map/requirements.txt
```

Before downloading data, inspect the plan and estimated tile count:

```bash
python3 map/download_osm_tiles.py \
  --source protomaps \
  --lat 40.7128 --lon -74.0060 --radius 50 \
  --style dark --dry-run
```

The default output is `map/<style>`. Copy that style directory to
`/sdcard/map/` so a tile is stored as
`/sdcard/map/<style>/<z>/<x>/<y>.jpg`.

## Sources and styles are different

`--source` says where tile data comes from. `--style` names the rendered look
and the directory selected by the firmware. The recognized directories are
`osm`, `dark`, `voyager`, and `topo`; Protomaps initially supplies `osm` (its
light theme) and `dark`, while `voyager` and `topo` require a licensed local or
MBTiles source. Changing a style does not grant permission to download from a
source.

The built-in sources are:

- `protomaps`: extracts an allowed Protomaps basemap and renders it locally.
- `local`: reads an authorized XYZ raster endpoint, normally on loopback or a
  server you operate.
- `mbtiles`: reads an existing raster MBTiles file without network
  access.

### Protomaps

The default extractor uses the Python `pmtiles` package installed by
`map/requirements.txt`; no separate `pmtiles` executable is needed. Install
Docker as well. The packager extracts only the requested bounds and zooms from
a [Protomaps daily basemap build][protomaps-builds], caches the extract, and
starts a pinned **full** TileServer GL container on loopback to render JPEG/PNG
tiles:

```bash
python3 map/download_osm_tiles.py \
  --source protomaps \
  --lat 40.7128 --lon -74.0060 --radius 50 \
  --min-zoom 2 --global-zoom 5 --max-zoom 12 \
  --style dark
```

TileServer GL Light cannot be used because it does not provide server-side
raster rendering. Full TileServer GL exposes rendered tiles at
`/styles/{id}/256/{z}/{x}/{y}.jpg`; the packager manages that local renderer and
the vendored Protomaps styles. Those styles reference the official hosted
Protomaps glyph and sprite assets, so the renderer needs internet access for
those assets on a cache miss. The extracted vector archive itself is cached.
Cached and newly created PMTiles extracts are structurally validated before
they are rendered or atomically promoted into the cache.

To use an installed [`pmtiles` CLI][pmtiles-cli] instead, opt in explicitly:

```bash
python3 map/download_osm_tiles.py \
  --source protomaps --pmtiles-extractor cli --pmtiles-bin pmtiles \
  --lat 40.7128 --lon -74.0060 --radius 50 --style dark
```

Python mode never searches for or invokes the CLI, and CLI mode never falls
back to Python. A missing Python package produces an installation hint for
`map/requirements.txt`; a missing CLI produces an executable-specific error.
`--dry-run` performs no imports that initialize an extractor, network access,
process launches, or filesystem writes; its plan identifies which extractor
would be used during the real run.

Daily build URLs are date-specific. For a reproducible pack, select a specific
compatible build instead of relying on the moving latest build, and keep the
generated `tileset.json` with the tiles. Use `--protomaps-build-url` for a
specific URL; `--protomaps-key` fills a `{key}` placeholder in that URL.
Extracted archives are cached under `~/.cache/plai-map` by default and can be
moved with `--cache-dir`. Local installations can also override
`--docker-bin`, `--tileserver-image`, and the loopback `--tileserver-port`.
`--pmtiles-bin` applies only to explicit CLI mode. Per-extract lock files
prevent concurrent processes from sharing a PMTiles temporary file; if a
process is killed without cleanup, the error message identifies the stale lock
that can be removed after confirming no extraction is active.

The selected extractor and its package/executable details are recorded as
provenance in `tileset.json`. Extractor choice is deliberately not part of the
semantic source fingerprint: Python and CLI extraction of the same build and
coverage safely share the same validated cache entry.

Every cached extract has an adjacent `.cache.json` sidecar. It records a schema
version, a SHA-256 identity for the (possibly credential-bearing) source URL,
the exact zoom/bounds pieces, archive byte size, and archive SHA-256. A missing
or mismatched sidecar makes the cache entry untrusted and triggers a fresh
atomic extraction; raw source credentials are never written to the sidecar.
Python extraction also records source size and SHA-256 hashes of any ETag or
Last-Modified validator as audit metadata, never their raw values. Cache reuse
remains network-free and does not treat those snapshots as a freshness check.

### Authorized local XYZ endpoint

Use `local` only with a tile service whose operator permits the intended volume
and offline storage. The URL must be an XYZ template containing `{z}`, `{x}`,
and `{y}`:

```bash
python3 map/download_osm_tiles.py \
  --source local \
  --tile-url 'http://127.0.0.1:8080/styles/basic/256/{z}/{x}/{y}.png' \
  --attribution '© Authorized Example Map contributors' \
  --lat 51.5074 --lon -0.1278 --radius 40 \
  --style voyager
```

`--attribution` is required for an HTTP source and must match the provider's
license. Credentials belong in environment variables or the local service
configuration, not in committed URLs. URL templates can reference a secret as
`{env:PLAI_TILE_TOKEN}`; use `--header-env HEADER=ENVIRONMENT_VARIABLE` when a
provider expects a secret header. Only environment-variable names, never
secret values, are recorded in provenance. Secret URL values are percent-
encoded, the expanded host is checked again against prohibited public tile
services, and redirects are limited to the configured origin so credentials
cannot be forwarded to another host.

The downloader stops immediately on authentication/policy blocks, including
HTTP 401/403 and successful responses with an `x-blocked` header. It honors
`Retry-After` and bounds retries for transient 429 and 5xx responses.

### Raster MBTiles

An existing raster MBTiles archive is the simplest fully offline input:

```bash
python3 map/download_osm_tiles.py \
  --source mbtiles --mbtiles /path/to/authorized-map.mbtiles \
  --lat 35.6762 --lon 139.6503 --radius 60 \
  --style topo
```

The archive must contain PNG, JPEG, or WebP raster tiles. Vector MBTiles need a
local renderer first. MBTiles rows use TMS orientation; the packager performs
the TMS-to-XYZ row conversion.

## Planning, resuming, and output

Low zooms through `--global-zoom` are global. Higher zooms cover the requested
radius, including regions that cross the antimeridian. The command validates
Web Mercator bounds and zoom order within the firmware's supported zoom range
of 1 through 15, prints a per-zoom plan, and refuses plans over its safety limit
unless the limit is explicitly changed. Use `--dry-run` before large packs.

Downloads are written to a temporary `.part` file, validated as a 256 x 256 RGB
JPEG, and atomically moved into place. A rerun validates and reuses good cached
tiles; corrupt or incomplete tiles are replaced. Any partial failure returns a
nonzero status and leaves the manifest incomplete. After a successful run,
same-source tiles outside the new plan and superseded PNG/JPEG variants are
removed so the directory matches its manifest.

The packager only resumes a populated directory when its `tileset.json` has a
matching source/render fingerprint. It refuses unknown legacy tiles instead of
silently relabeling or mixing their provenance. To intentionally replace such
a directory, pass `--force`; that option removes every numeric zoom tree first,
so stale out-of-plan tiles cannot survive the rebuild. Before deletion it
publishes an incomplete manifest and verifies that an unowned directory is
named for the selected style and contains only a recognizable zoom/x/tile
tree. This prevents a broad `--output` path from turning `--force` into an
unrelated-data deletion. Non-tile files are left alone. A legacy PNG inside an
already fingerprinted pack is converted to the required JPEG format on the
next run.

An output-local `.plai-packager.lock` prevents two jobs from mixing tiles or
manifests. If a process is forcibly killed, confirm that no packager is running
before removing the stale lock named in the next run's error.

Each completed style directory contains:

- `{z}/{x}/{y}.jpg`: baseline RGB JPEG tiles at quality 75.
- `tileset.json`: source provenance, bounds, zooms, transformations, counts,
  and completion status.
- `ATTRIBUTION.txt`: attribution that must travel with the tile pack.

Keep the on-device attribution screen accessible. Protomaps basemap tiles are a
Produced Work of OpenStreetMap data and require visible OpenStreetMap
attribution; see the [Protomaps licensing guidance][protomaps-license] and
[OpenStreetMap attribution guidelines][osm-attribution].

## Public tile servers are not offline data sources

Do not configure this tool to prefetch or seed tiles from
`tile.openstreetmap.org`, CARTO public basemap endpoints, or OpenTopoMap. Their
public services are designed for interactive map viewing and do not authorize
bulk offline packs. The packager rejects known public endpoints. Changing a
User-Agent or hostname to evade a block is not supported.

There is intentionally no link to the old prebuilt Google Drive archive: its
source, license, build date, and redistribution permission were not documented.
Only distribute a pack when its provenance and redistribution terms are known.

## Tests

The test suite uses temporary directories, generated Pillow images, tiny SQLite
MBTiles fixtures, and fake HTTP responses. It never contacts a live tile
provider:

```bash
python3 -m unittest discover -s map/tests -v
```

[osm-attribution]: https://osmfoundation.org/wiki/Licence/Attribution_Guidelines
[pmtiles-cli]: https://docs.protomaps.com/pmtiles/cli
[protomaps-builds]: https://docs.protomaps.com/basemaps/downloads
[protomaps-license]: https://github.com/protomaps/basemaps#licensing-and-attribution-guidelines
