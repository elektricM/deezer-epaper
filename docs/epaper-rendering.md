# Rendering images on 6-colour e-paper (E Ink Spectra 6)

Measurements taken while getting album art onto a Waveshare 4in0e. Everything
numeric here was measured on that panel unless a source is cited.

## The constraint

Six inks, one per pixel: black, white, yellow, red, blue, green. No greys, no
intermediate tones. Contrast ratio is about **30:1** (E Ink's figure for
Spectra 6) against 1000:1 or better for a monitor, and it is reflective, so
apparent contrast falls further in dim light.

Everything else follows from that: all intermediate colour and all tonality
comes from **spatial averaging** - putting different inks next to each other and
letting the eye mix them.

## What two inks average to

A 50/50 dither of each pair, computed in linear light (the only space where
averaging is physical):

| pair | result | | pair | result |
|---|---|---|---|---|
| black + white | mid grey | | white + red | pink |
| black + yellow | olive | | white + blue | pale blue |
| black + red | dark red | | white + green | mint |
| black + blue | navy | | yellow + red | orange |
| black + green | forest | | yellow + green | lime |
| white + yellow | cream | | red + blue | magenta |
| red + green | olive | | blue + green | teal |

Pink, orange, magenta and teal are the ones that matter in practice - they are
common in photographs and none of them is an ink.

## The central finding: the matching palette is not the display palette

They serve different purposes and using one set of numbers for both is what
made our output posterise.

- **The matching palette** is what the quantiser aims at. Exaggerating it forces
  the dithering to mix.
- **The display palette** is what the preview simulates with. It should be the
  real measured ink appearance, so the preview is honest.

Measured on one cover, changing only the matching palette:

| matching palette | ink separation | flat 5x5 patches | mean quant error |
|---|---|---|---|
| measured (real ink appearance) | 180 | **18.4 %** | 71 |
| half | 250 | 8.2 % | 101 |
| ideal (pure primaries) | 329 | **4.1 %** | 146 |

Why: error diffusion only switches ink when accumulated error crosses a decision
boundary. Matching against the real, desaturated inks means a blue sky is
*already close* to the blue ink, the residual is small, and the quantiser stays
on blue for a long run - a flat patch. Matching against a saturated primary the
panel cannot actually make leaves a large residual everywhere, so the quantiser
is forced to alternate, and the alternation is what produces intermediate
colour. Being "wrong" in that direction is what makes dithering work.


## Shadows and midtones are two separate mechanisms

"Measured versus ideal" looks like one choice but is two independent effects
that happen to move together in the presets. Separating them gets both.

**1. Shadow behaviour is set by the luma of the darkest ink.** The measured
black sits at luma 48. Any source pixel darker than that leaves a negative
residual that clamps, so the quantiser picks black every time and shadows come
out perfectly clean - but every shadow detail above that level is flattened into
the same black. The ideal black is luma 0, so a near-black background (luma ~11)
sits *above* it, keeps a live residual, and error diffusion scatters bright and
coloured dots across it. Measured on a cover whose background is 80 % near
black: **6 stray dots against 4125**.

The fix is not a raised black ink, it is an explicit **black point** - crush
everything below a threshold to solid black before dithering. Same clean
blacks, without flattening the shadows that sit above it:

| black point | stray dots in a near-black background |
|---|---|
| 0.00 | 6348 (4.94 %) |
| 0.06 | 213 (0.17 %) |
| 0.10 | 5 (0.00 %) |

**2. Midtone mixing is set by the spread between inks**, as described above.

So: take the ideal palette's spread for live midtones, and add a black point for
clean shadows. They are orthogonal.

## Neutrals need protecting separately from hue

A silver object resolving to blue is a third, unrelated failure. Against the
measured palette, neutral mid grey (125,125,125) is **nearer to the blue ink
(74) than to black (133) or white (158)**. The blue ink is a desaturated
mid-blue and simply sits where the greys are.

A flat chroma weight ("hue priority") fixes it and breaks everything else,
because penalising hue error everywhere forbids the hue-wrong pairings that
produce every non-ink colour. Scale the penalty by **the source pixel's own
chroma** instead: full penalty where the source is near-neutral, tapering to
none by about chroma 60. At K=3, mid grey stops resolving to a colour while hot
pink, orange, sky blue, skin and deep red all pick exactly the same inks as with
no protection at all.

## Palette blend: keep it low, and do not import the number

Upstream reframe blends continuously between muted real-ink values and pure
primaries, defaulting to 0.6 - an idea it takes from Pimoroni's Inky library.
The parameterisation is right and better than choosing between fixed palettes.
**The 0.6 is not transferable**: Inky is a 7-colour ACeP panel with different
inks. Measured on Spectra 6 across seven covers, flat 5x5 patches by blend:

| cover | 0.0 | 0.1 | 0.2 | 0.3 | 0.6 | 1.0 |
|---|---|---|---|---|---|---|
| SIAMES (magenta) | 0.0 % | 0.0 % | 0.0 % | 0.0 % | 26.0 % | 52.5 % |
| RAM (near-black) | 5.6 % | 26.8 % | 57.6 % | 74.1 % | 75.5 % | 75.9 % |
| Empire of the Sun | 2.2 % | 3.7 % | 4.8 % | 5.8 % | 8.3 % | 11.8 % |
| Hot Pink | 0.1 % | 0.1 % | 0.1 % | 0.1 % | 1.0 % | 10.9 % |

Keep it **under 0.3**. At 0.6 with brightness and saturation applied, one magenta
cover collapsed from a four-ink mix (red 60 %, white 24 %, blue 9 %, black 7 %)
to **87 % flat red**. Dark covers collapse earliest, by about 0.2, because the
muted black raises the floor and swallows the background.

## Metrics disagree with eyes here, and the eyes are right

On the same cover, matching against the real inks scored **dE 9.36** against
**16.76** for the exaggerated palette - and looked clearly worse: posterised,
flat, muddy. Every averaging metric tried (mean CIEDE2000, eye-model CIEDE2000
after a linear-light blur, local-contrast correlation) preferred the flat
version, because a flat patch of the locally-correct average colour is exactly
what an averaging metric rewards.

Perceived quality on six inks is driven by apparent colourfulness and the
absence of visible flat patches. **Do not tune this from a number.** Put the two
candidates side by side on the glass and look.

Related: dither speckle - including isolated bright and coloured dots in shadow
areas - reads as detail and texture, not noise. Suppressing it (Atkinson's 75 %
error propagation cuts stray shadow dots about 8x) makes images look cleaner in
a screenshot and worse on the wall.

## Choosing a matcher

The quantiser must be a nearest-neighbour search **in the same space the error
is accumulated in**. Error diffusion is a feedback loop driving the local
average towards the input, and it only settles if those agree. Matching in
CIELAB while diffusing in gamma-encoded sRGB stopped the loop converging and put
slabs of flat blue across a pink field.

**Do not use CIEDE2000 for palette matching.** It is calibrated for small
differences; its `S_C` term discounts chroma error as chroma rises, so over the
distances a six-ink palette actually works across it rates pure magenta closer
to blue than to red. Plain Euclidean distance in the working space behaves
sanely at these magnitudes.

## Pre-processing

- **Saturation helps, often a lot, and is image-dependent.** Measured across
  nine covers and both palettes, coloured-ink share rises with saturation in
  nine cases out of ten. Dark, low-chroma sources want the most - a near-black
  cover lit by a single blue light looked right only around 2.0-2.5 - while a
  bright busy cover needs none. Tune it per image rather than globally.
- **Global contrast compression to the panel's range costs more than it buys.**
  A hard linear remap into the reachable luminance range rescues detail at the
  extremes by flattening everything in between. Local contrast ("clarity",
  midtone local-contrast enhancement) is the better tool for a 30:1 display -
  see epdoptimize, which does this deliberately.
- Fit to native resolution before dithering, never after.

## Kernel notes

Serpentine scanning in all cases. Never normalise a kernel below its own
divisor: renormalising by the in-bounds tap sum amplifies at edges (on the
bottom row Floyd's single in-bounds tap would take 100 % of the error instead of
43.8 %, and the excess chains along the row). Atkinson's six unit taps sum to 6
against a divisor of 8 - discarding a quarter of the error is the point of it,
and normalising by the tap sum silently turns it into an ordinary kernel.

## Panel handling

- No minimum refresh interval is documented by anyone who makes these. The
  EL040EF1 datasheet specifies none; GxEPD2 has no such constant. The widely
  repeated 180 s is a reseller recommendation in boilerplate covering a whole
  product range. A refresh takes ~22 s and blocks, so updates are self-limiting.
- Good Display document the opposite rule: **refresh at least once every 24 h**
  or the image burns in.
- A full refresh is ~20.5 s and is the same cost for a partial window. There is
  no fast partial update on six inks; the full waveform is required.

## Sources

- E Ink Spectra 6 contrast ratio (30:1) - vendor specifications
- Good Display / Waveshare panel documentation and GxEPD2 driver source
- epdoptimize (paperlesspaper) - tone mapping, clarity, blue noise, edge
  preservation, and per-image "intent" presets
- Everything numeric above was measured on the 4in0e in this repo; the tuning
  page at `/tune` reproduces it.
