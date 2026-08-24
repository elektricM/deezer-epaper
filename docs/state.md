# Where things stand

Written at the end of a long session, so the next one does not start by
rediscovering all of this.

## What works right now

- Track source: macOS MediaRemote, no permission needed. Falls back to reading
  Deezer's own window (needs Accessibility) when another app holds the system
  audio session, which macOS limits to one at a time.
- Panel: fetches frames over WiFi, long-polls, redraws about a second after a
  track change. The refresh itself is ~20.5 s and is the panel, not the code.
- Menu bar app spawns the server, shows state, and toggles Wi-Fi serving and
  Deezer-only vs any player.
- `/tune` gives a live side-by-side with hand-editable ink values and a switch
  per processing stage.

## The live baseline

Kept in `.baseline/` in the working copy. Settings that were in use:

    method     floyd
    pigments   030203,ffffff,fdfc04,fa0404,0006ff,02f703   (blend ~0.05)
    brightness 1.05
    contrast   1.00
    saturate   2.20
    neutral    off
    black pt   off

These were arrived at by eye on the actual panel and beat every default that was
proposed from measurement alone. Treat them as the thing to beat.

## Settings, and what is actually known

`tools/benchmark.py` sweeps each parameter over the cover set;
`tools/contactsheet.py` renders the same sweeps as labelled grids. **Use the
sheets to decide and the numbers to explain.** Ranking settings by a metric
repeatedly produced choices that looked worse on the glass.

Metrics, in rough order of usefulness:

- **hue** - chroma-weighted hue-angle error. Catches a cyan subject resolving to
  green, which nothing else here notices.
- **detail** - correlation of the blurred a\*/b\* channels against the source,
  restricted to chromatic regions. Catches a pale cyan logo vanishing into white.
- **flat** - share of *detailed* source regions that came out as a flat patch.
  Restricted to regions the source itself varies in, otherwise a near-black
  cover reads as 74% flat and swamps everything.
- **chroma** - output/source chroma ratio.
- **dE** - weakest. Rewards flat patches of the locally correct average.

Findings that held up:

- Blend does two separate things: hue discrimination (best 0.2-0.4) and ink
  luminance (higher blend lets pale saturated detail survive at all). It is
  genuinely image-dependent - a dark cover gains detail 0.46 to 0.84 going
  0.0 to 1.0, while a bright one loses it 0.91 to 0.81.
- Jarvis beats floyd on both mean and worst-case hue error. Atkinson is worse on
  both and flattens more.
- Neutral protection is a no-op below about blend 0.4 - with pure primaries a
  mid grey sits nearly equidistant from all six inks, so nothing competes for it.
- Saturation helps most covers and badly hurts magenta and cyan, which need two
  inks at once that the palette cannot mix brightly enough.

Corrections made to earlier claims in this repo's history, all from measuring
the wrong thing:

- CIEDE2000 is wrong for palette matching - it rates pure magenta nearer blue
  than red at these distances. Caught only because one cover was pink; the mean
  over the others was fine.
- "Saturation above 1.2 loses colour" was wrong. Coloured-ink share rises. What
  actually degrades is hue on specific covers.
- "Keep blend near 0" was wrong. Pure primaries are the worst setting on the
  hardest cover.
- Pimoroni Inky's blend of 0.6 does not transfer - different panel.

## Open

- Settings are global. Different covers genuinely want different treatment, and
  upstream reframe stores them per photo. This is the obvious next thing.
- The battery has not been retested since the firmware fixes.
- The ink values are still estimates by eye. A colorimeter would put the whole
  palette question on a real footing and is the single highest-value measurement
  left.
