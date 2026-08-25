"""Independent dithering engines, so different approaches can be compared.

Each engine takes an RGB image and returns hardware palette indices for the
Spectra 6. They are deliberately NOT refactored into a shared core: the point
is that they are separate implementations that can be judged against each
other on the glass. Sharing code between them would quietly make them agree.

  native    the hand-written error diffusion in panel.py. Serpentine scan,
            error accumulated in an extended [-64, 320] range, a 64-level
            lookup table for matching, and a choice of kernels.

  reframe   a port of upstream reframe (github.com/PatrickJnr/reframe), which
            hands the whole job to PIL's quantize(). Different in every
            respect that matters: 8-bit integer error, no serpentine, PIL's
            own error distribution, and matching in PIL's internal space.

Both end at the same six inks, so the difference you see is entirely the
method.
"""
import numpy as np
from PIL import Image, ImageEnhance

# ---------------------------------------------------------------- reframe
#
# Upstream's two palettes, verbatim. Note the names are the wrong way round in
# the original: DESATURATED_PALETTE holds the PURE primaries and
# SATURATED_PALETTE holds the muted measurements of the real inks. The names
# are kept so this stays diffable against upstream, but read the values.
#
# Order here is upstream's own: black, white, green, blue, red, yellow.
_PURE = [                      # upstream calls this DESATURATED_PALETTE
    [0, 0, 0],                 # black
    [255, 255, 255],           # white
    [0, 255, 0],               # green
    [0, 0, 255],               # blue
    [255, 0, 0],               # red
    [255, 255, 0],             # yellow
]
_MUTED = [                     # upstream calls this SATURATED_PALETTE
    [57, 48, 57],              # black
    [255, 255, 255],           # white
    [40, 91, 58],              # green
    [0, 128, 255],             # blue
    [156, 72, 75],             # red
    [208, 190, 71],            # yellow
]

# Reorders the two lists above into the panel's own index order, with a black
# at index 4 because the controller has no ink there. Upstream's own line.
_ORDER = [0, 1, 5, 4, 0, 3, 2]


def reframe_palette(muted=0.0):
    """Flat RGB list in hardware order.

    muted = 0 is the pure primaries, 1 is the measured real inks. Upstream
    calls this argument `saturation` and defaults it to 0.6, which - because
    of the swapped names - actually means 60% MUTED. Renamed here because the
    original reads as the opposite of what it does.
    """
    out = []
    for i in _ORDER:
        for ch in range(3):
            out.append(int(_MUTED[i][ch] * muted + _PURE[i][ch] * (1.0 - muted)))
    return out


def reframe(img, muted=0.0, brightness=1.0, colour=1.0, **_):
    """Upstream reframe's dithering, as it actually runs there.

    The whole job goes to PIL: brightness and saturation through ImageEnhance,
    then quantize() against a fixed palette with Floyd-Steinberg. PIL diffuses
    in 8-bit integers, scans strictly left to right, and clamps to 0-255 - all
    different from the native engine, which is the point of having both.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if colour != 1.0:
        img = ImageEnhance.Color(img).enhance(colour)

    pal = reframe_palette(muted)
    pimg = Image.new("P", (1, 1))
    pimg.putpalette(pal + [0, 0, 0] * (256 - len(pal) // 3))
    out = img.quantize(palette=pimg, dither=Image.FLOYDSTEINBERG)

    idx = np.frombuffer(out.tobytes("raw"), dtype=np.uint8).reshape(
        out.size[1], out.size[0]).copy()
    # Palette slot 4 is the duplicate black, and everything from 7 up is
    # padding; fold both onto black so nothing can address a missing ink.
    idx[idx == 4] = 0
    idx[idx > 6] = 0
    # Panel index 5 is green in this codebase but slot 6 upstream, and slot 5
    # is blue. Translate to the indices the packer expects.
    trans = np.array([0, 1, 2, 3, 0, 4, 5], np.uint8)
    return trans[idx]


def reframe_ordered(img, muted=0.0, brightness=1.0, colour=1.0,
                    bayer_size=4, threshold_scale=1.0, **_):
    """Upstream's other mode: Bayer thresholds sized from the palette spacing.

    Ordered dithering has no feedback loop, so it cannot drift the way error
    diffusion can, and it produces a regular texture rather than a scattered
    one. Kept as a third opinion.
    """
    if img.mode != "RGB":
        img = img.convert("RGB")
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
    if colour != 1.0:
        img = ImageEnhance.Color(img).enhance(colour)

    n = max(2, int(bayer_size))
    m = np.array([[0]])
    while m.shape[0] < n:                      # recursive Bayer construction
        k = m.shape[0]
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    m = m[:n, :n] / float(n * n) - 0.5

    arr = np.asarray(img, np.float32)
    h, w, _ = arr.shape
    tile = np.tile(m, (h // n + 1, w // n + 1))[:h, :w][..., None]

    flat = reframe_palette(muted)
    pal = np.array(flat, np.float32).reshape(-1, 3)[:7]
    # Spacing between neighbouring ink levels sets how far a threshold may
    # push a pixel; too large and the texture swamps the picture.
    spread = float(np.median(np.diff(np.sort(pal[:, 0])))) or 64.0
    arr = np.clip(arr + tile * spread * threshold_scale, 0, 255)

    d = ((arr[:, :, None, :] - pal[None, None, :, :]) ** 2).sum(-1)
    idx = d.argmin(-1).astype(np.uint8)
    idx[idx == 4] = 0
    trans = np.array([0, 1, 2, 3, 0, 4, 5], np.uint8)
    return trans[idx]


ENGINES = {
    "reframe": reframe,
    "reframe_ordered": reframe_ordered,
}
