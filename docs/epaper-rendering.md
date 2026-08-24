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


## Measured settings reference

Produced by `tools/benchmark.py` over 11 covers spanning dark, bright, neutral,
skin, and specific hues (cyan, magenta, orange, blue). Protocol: the simulation
palette is held constant so only the matcher varies, one parameter is swept at a
time from a documented baseline, and both the mean and the **worst cover** are
reported - a mean over a varied set hides a single colour category failing
completely, which is how several wrong conclusions were reached before this
harness existed.

Hue error is the primary metric: chroma-weighted mean hue-angle error in CIELAB
after an eye-model blur, measured only where the source has real chroma. It
catches a cyan subject rendering as green, which neither mean dE nor
coloured-ink share will.

### Kernel

| method | hue mean | hue worst | flat | chroma |
|---|---|---|---|---|
| jarvis | **20.7°** | **30.5°** | 1.2 % | 0.34 |
| sierra | 20.9° | 36.4° | 0.5 % | 0.43 |
| floyd | 21.0° | 36.8° | 0.5 % | 0.42 |
| atkinson | 22.6° | 42.3° | 6.4 % | 0.30 |
| bayer | 45.2° | 77.4° | 22.4 % | 0.26 |
| nearest | 52.0° | 77.7° | 42.5 % | 0.33 |

Jarvis wins on both mean and worst case. Atkinson's discarded quarter of the
error costs hue accuracy and flattens more than it helps.

### Palette blend

| blend | hue mean | hue worst | flat mean | flat worst | chroma |
|---|---|---|---|---|---|
| 0.0 | 21.0° | 36.8° | 0.5 % | 3.8 % | 0.42 |
| 0.2 | **17.6°** | **25.4°** | 0.9 % | 3.4 % | 0.56 |
| 0.3 | 17.0° | 25.3° | 1.6 % | 7.3 % | 0.61 |
| 0.4 | 16.7° | 25.0° | 2.6 % | 8.9 % | 0.64 |
| 0.6 | 18.6° | 28.0° | 8.1 % | 23.0 % | 0.70 |
| 1.0 | 20.4° | 33.8° | 13.7 % | 47.8 % | 0.86 |

**0.2 to 0.3.** Hue error is worse in *both* directions from there: pure
primaries are the worst setting of all on the hardest cover (36.8°), and past
0.4 the inks sit close enough together that hue discrimination collapses - a
cyan subject resolves to green at 0.6. Flat patches climb monotonically, so 0.2
is the safer end of the range.

### Saturation

| saturate | hue mean | hue worst | flat | chroma |
|---|---|---|---|---|
| 0.8 | 21.5° | **36.3°** | 1.1 % | 0.35 |
| 1.0 | 21.0° | 36.8° | 1.0 % | 0.42 |
| 1.4 | 20.9° | 43.4° | 0.7 % | 0.56 |
| 2.0 | 20.7° | 48.7° | 1.0 % | 0.69 |
| 2.5 | **20.4°** | 50.7° | 1.6 % | 0.78 |

The mean and the worst case point in opposite directions, and the worst case is
the one that matters. Raising saturation improves most covers slightly while
pushing specific hues outside the reachable gamut, where the residual cannot
resolve and the diffusion settles on the wrong ink. A magenta cover goes from
36.8° to 50.7°. Cyan is the other vulnerable hue: it needs high G and high B at
once, blue and green average to a dark teal, and past about 1.8 the error runs
away to green.

### Brightness, contrast, black point

All three leave hue roughly alone and trade flat patches. Worst-case hue prefers
brightness 0.9 and contrast 0.85; both degrade above 1.15. Black point barely
touches hue and is worth using only on covers with large near-black areas, where
it stops the diffusion scattering bright dots.

### Neutral protection

**A no-op below about blend 0.4.** With pure primaries a mid grey sits nearly
equidistant from all six inks, so nothing competes with black and white for it
and the penalty never fires - the sweep is identical from 0 to 8. It only
matters once the blend brings a chromatic ink close to the greys.

### Recommended, and its limits

`jarvis`, blend 0.25, everything else off: hue 17.5° mean / 25.4° worst against
21.0° / 36.8° for plain floyd at blend 0. But it is better on 6 of 11 covers and
worse on 5, so it is a better default, not a universal answer. Covers genuinely
differ; that is what the tuning page is for.

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
