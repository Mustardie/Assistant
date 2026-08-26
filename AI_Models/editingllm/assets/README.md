# Asset library

Local files only. Nothing here is downloaded, and nothing in this
folder is ever modified -- trimming, gain and fades are applied to the
*placed clip* in Premiere, never to your source file.

## Folders

- **`music/`** — Full tracks and beds. Anything long enough to sit under a section.
- **`sfx/`** — Short one-shots: impacts, pops, whooshes, stings. Under about three seconds.
- **`ambience/`** — Loopable room tone and atmosphere: wind, cave drips, rain, crowd.
- **`callout/`** — Arrows, circles, labels and other point-at-this graphics. PNG with transparency.
- **`titles/`** — Title and chapter card backgrounds, lower-third plates, end cards.
- **`transitions/`** — Whoosh-and-wipe overlays and their sounds, when they belong together.

Subfolders are fine and are read as tags: a file in
`sfx/impacts/heavy/` picks up `impacts` and `heavy` automatically.

## Supported files

| Kind | Extensions |
|---|---|
| audio | `.wav` `.mp3` `.m4a` `.aac` `.flac` `.ogg` |
| image | `.png` `.jpg` `.jpeg` `.webp` |
| video | `.mp4` `.mov` `.webm` |
| Motion Graphics | `.mogrt` (indexed, but placed as a marker only) |

## Sidecar metadata

Optional. Put `<filename>.asset.json` next to a file to describe it:

```
impact_boom.wav
impact_boom.asset.json
```

See `example.asset.json` in this folder for every field. All of them
are optional. A sidecar that will not parse does not break indexing —
the asset is marked `needs_review` and left out of automatic
placement until you fix it.

## Naming

Filenames are read for tags, so descriptive names do most of the work
on their own:

- `whoosh_fast_01.wav` → tags `whoosh`, `fast`
- `tension_bed_loop.wav` → tags `tension`, `bed`, `loop` (and marked
  loopable, because the name says so)
- `arrow_red.png` → tags `arrow`, `red`

## Safety

`safe_for_auto: false` in a sidecar takes a file out of automatic
placement entirely — it stays indexed and searchable, and the system
will leave a marker naming it instead of using it. That is the switch
to reach for when a sound is right but you want to place it yourself.
