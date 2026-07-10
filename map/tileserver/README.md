# TileServer GL styles

`light.json` and `dark.json` are generated from
`@protomaps/basemaps` 5.7.2 for TileServer GL's `pmtiles://{protomaps}`
configured data source. They are
used only by the local TileServer GL container that rasterizes an extracted
Protomaps archive into the JPEG directory tree consumed by the device.

Regenerate them with:

```sh
generate_style light.json 'pmtiles://{protomaps}' light en
generate_style dark.json 'pmtiles://{protomaps}' dark en
```

The generated styles reference the official Protomaps v4 hosted glyph and
sprite assets. See `PROTOMAPS_LICENSE.md` for the style code and visual-design
licenses. Map data generated from the Protomaps basemap remains an ODbL
Produced Work and must visibly credit OpenStreetMap.
