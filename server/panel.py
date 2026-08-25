#!/usr/bin/env python3
"""Palette, dithering and packing for the Waveshare 4in0e (E Ink Spectra 6).

The panel has six inks and one per pixel, so every intermediate colour comes
from spatial dithering. Anything that describes the panel or converts an image
for it lives here so the web preview and the panel frame cannot drift apart.
"""
import os
import sys
import numpy as np
from PIL import Image, ImageOps

import colour

WIDTH, HEIGHT = 400, 600
PANEL_BYTES = WIDTH * HEIGHT // 2

# (4-bit code the panel expects, idealised sRGB, name).
# 0x4 and 0x7 are unused by the controller - see EPD_4in0e.h.
PALETTE = [
    (0x0, (0, 0, 0), "black"),
    (0x1, (255, 255, 255), "white"),
    (0x2, (255, 255, 0), "yellow"),
    (0x3, (255, 0, 0), "red"),
    (0x5, (0, 0, 255), "blue"),
    (0x6, (0, 255, 0), "green"),
]
CODES = [c for c, _, _ in PALETTE]
NAMES = [n for _, _, n in PALETTE]

# What the matcher aims at. IDEAL is pure primaries; MEASURED is roughly what
# the inks look like on the glass. Aiming at pure primaries leaves a large
# residual everywhere, which keeps the diffusion mixing and preserves detail;
# aiming at the real inks puts colours where they land but flattens areas into
# solid patches. palette_blend() interpolates between the two.
PAL_IDEAL = np.array([rgb for _, rgb, _ in PALETTE], dtype=np.float32)
PAL_MEASURED = np.array([[48, 48, 48], [216, 216, 208], [240, 224, 80],
                         [160, 32, 32], [80, 128, 184], [96, 128, 80]],
                        dtype=np.float32)
PAL_HALF = (PAL_IDEAL + PAL_MEASURED) / 2

# Muted ink values from Pimoroni's Inky library, reordered to the index order
# used here (their table runs black, white, green, blue, red, yellow). Measured
# against a shipping product, unlike PAL_MEASURED which is an eyeball estimate.
PAL_MUTED = np.array([[57, 48, 57],       # black
                      [255, 255, 255],    # white
                      [208, 190, 71],     # yellow
                      [156, 72, 75],      # red
                      [0, 128, 255],      # blue
                      [40, 91, 58]],      # green
                     dtype=np.float32)


def palette_blend(t, muted=None, pure=None):
    """Blend between pure primaries (t=0) and muted real-ink values (t=1).

    Keep t low. Measured across seven covers, flat 5x5 patches stay under about
    6% up to t=0.3 and climb steeply after: one magenta cover went 0% at 0.3 to
    26% at 0.6 to 52% at 1.0. Dark covers collapse earliest, by around 0.2.
    """
    muted = PAL_MUTED if muted is None else np.asarray(muted, np.float32)
    pure = PAL_IDEAL if pure is None else np.asarray(pure, np.float32)
    t = float(np.clip(t, 0.0, 1.0))
    return muted * t + pure * (1.0 - t)


PALETTES = {"ideal": PAL_IDEAL, "measured": PAL_MEASURED, "half": PAL_HALF,
            "muted": PAL_MUTED, "reframe": palette_blend(0.6)}

# (taps, divisor) where a tap is (dx, dy, weight).
#
# The divisor is NOT redundant with the sum of the taps. Atkinson's six unit taps
# sum to 6 but its divisor is 8: deliberately throwing away a quarter of the
# error is what gives Atkinson its extra contrast. Normalising by the tap sum
# instead silently turns it into an ordinary full-diffusion kernel.
KERNELS = {
    "floyd":    ([(1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)], 16),
    "atkinson": ([(1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)], 8),
    "jarvis":   ([(1, 0, 7), (2, 0, 5), (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5),
                  (2, 1, 3), (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1)], 48),
    "sierra":   ([(1, 0, 2), (-1, 1, 1), (0, 1, 1)], 4),
}
BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32) / 64.0 - 0.5

METHODS = ["nearest", "floyd", "atkinson", "jarvis", "sierra", "bayer"]

CLAMP_LO, CLAMP_HI = -64.0, 320.0
LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(_ROOT, "firmware", "src", "ImageData.cpp")


# ------------------------------------------------------------------ palette
def palette_image(pal=None):
    """Pillow 'P' image carrying the palette, for index-space drawing."""
    pal = PAL_IDEAL if pal is None else pal
    flat = []
    for rgb in np.asarray(pal, dtype=int).tolist():
        flat.extend(rgb)
    flat.extend([0] * (768 - len(flat)))
    p = Image.new("P", (1, 1))
    p.putpalette(flat)
    return p


def _luma(a):
    return (np.asarray(a, dtype=np.float32) * LUMA).sum(axis=-1)


# ------------------------------------------------------------------ fitting
def load(path):
    """Open an image, honouring EXIF orientation.

    PIL does not apply the Orientation tag on open, so a portrait phone shot
    would otherwise land sideways on the panel.
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def fit(img, w=WIDTH, h=HEIGHT, mode="crop"):
    """Bring an image to w x h without distorting it, rotating when the source
    orientation does not match the target's."""
    # Only when the target itself has an orientation. The art slot is square,
    # and (w > h) is False for a square, so without this guard every landscape
    # source was turned on its side.
    if w != h and (img.width > img.height) != (w > h):
        img = img.rotate(90, expand=True)
    sr, dr = img.width / img.height, w / h
    if mode == "pad":
        if sr > dr:
            nw, nh = w, max(1, round(w / sr))
        else:
            nh, nw = h, max(1, round(h * sr))
        img = img.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("RGB", (w, h), (255, 255, 255))
        canvas.paste(img, ((w - nw) // 2, (h - nh) // 2))
        return canvas
    if sr > dr:
        nh, nw = h, max(w, round(h * sr))
    else:
        nw, nh = w, max(h, round(w / sr))
    img = img.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return img.crop((l, t, l + w, t + h))


# ------------------------------------------------------------------ dithering
_LUT_BITS = 6                     # 64 levels per axis, 262 144 cells
_LUT_MAX = (1 << _LUT_BITS) - 1
_QUANT = (np.arange(256) * _LUT_MAX // 255).astype(np.int32).tolist()
_lut_cache = {}
_LUT_CACHE_MAX = 4                # entries; each is 256 KB
_LUT_CHUNK = 16384                # grid rows per pass when building one


# How much more a hue error costs than a brightness error, ON TOP of the bias
# the metric already carries. Note that 1.0 is NOT plain Euclidean distance:
# the luma/chroma split below charges a grey-axis error about a third of what
# Euclidean would, so the matcher is chroma-biased by construction. This knob
# scales the chroma term further. Raising it globally suppresses the mixing
# that produces intermediate colours - prefer the `neutral` argument, which
# acts only where the source is already near-neutral.
CHROMA_WEIGHT = 1.0

# Source colours below this chroma get the full neutral penalty; tapers above.
NEUTRAL_RANGE = 60.0


def _lut(pal, weight=None, neutral=0.0):
    """Nearest ink for every quantised sRGB colour.

    Distance is measured in the same gamma-encoded sRGB the error accumulates
    in. That matters: error diffusion is a feedback loop driving the local
    average towards the input, and it only converges when the quantiser is a
    nearest-neighbour search in the same space as the error. Matching in CIELAB
    while diffusing in sRGB does not converge and lays down flat slabs.

    Avoid CIEDE2000 here. It is calibrated for small differences and its S_C
    term discounts chroma error as chroma rises, so across the distances a
    six-ink palette spans it rates pure magenta nearer blue than red.

    The table is a speed device, about 8x faster than searching per pixel. It
    is an approximation, not an equivalence: colours are quantised to 64 levels
    per axis first, so around 1.6% of 8-bit colours land on a different ink
    than an exact search would pick - always an immediate neighbour. Error
    diffusion absorbs that, since the residual is fed back either way.
    """
    weight = CHROMA_WEIGHT if weight is None else weight
    key = (np.asarray(pal, np.float32).tobytes(), float(weight), float(neutral))
    hit = _lut_cache.get(key)
    if hit is not None:
        return hit
    n = 1 << _LUT_BITS
    axis = np.arange(n, dtype=np.float64) * (255.0 / _LUT_MAX)
    R, G, B = np.meshgrid(axis, axis, axis, indexing="ij")
    grid = np.stack([R, G, B], -1).reshape(-1, 3)
    pal = np.asarray(pal, np.float64)
    gl = (grid * LUMA).sum(-1, keepdims=True)
    gc = grid - gl                       # chroma = colour minus its own luma
    pl = (pal * LUMA).sum(-1, keepdims=True)
    pc = pal - pl
    # Neutral protection, scaled by the SOURCE colour's own chroma. A flat
    # chroma weight protects greys but also forbids the hue-wrong pairings that
    # produce every non-ink colour. Scaling by source chroma separates the two:
    # greys stop resolving to a colour while saturated hues are untouched.
    w = weight
    if neutral > 0:
        src_chroma = np.linalg.norm(gc, axis=-1, keepdims=True)
        w = weight + neutral * np.clip(1.0 - src_chroma / NEUTRAL_RANGE, 0.0, 1.0)
    # In one shot the (cells, inks, 3) difference alone is 37 MB of float64 and
    # the whole build peaks near 112 MB - for a 256 KB answer. Chunking the
    # rows holds the peak flat with no change to the result.
    idx = np.empty(grid.shape[0], np.uint8)
    for s in range(0, grid.shape[0], _LUT_CHUNK):
        e = s + _LUT_CHUNK
        wc = w[s:e] if np.ndim(w) else w
        d = ((gl[s:e] - pl.T) ** 2
             + wc * ((gc[s:e, None, :] - pc[None, :, :]) ** 2).sum(-1))
        idx[s:e] = d.argmin(1).astype(np.uint8)

    # Bounded: the tuning page makes a fresh key on every slider step and every
    # colour-picker keystroke, and each entry is 256 KB kept forever. A miss
    # costs about 30 ms, so a small cache loses nothing.
    if len(_lut_cache) >= _LUT_CACHE_MAX:
        _lut_cache.pop(next(iter(_lut_cache)))
    _lut_cache[key] = idx
    return idx


def gamut_map(arr, pal, strength=1.0):
    """Ease source luminance towards the panel's reachable range.

    A soft knee, not a linear remap: compressing the whole range costs contrast
    everywhere to rescue the two extremes. This leaves the midtones alone and
    bends only the last stretch at each end. strength=0 disables it.
    """
    if strength <= 0:
        return arr
    lo, hi = float(_luma(pal[0])), float(_luma(pal[1]))
    l = _luma(arr)
    target = lo + l * (hi - lo) / 255.0            # the old hard remap
    knee = 0.25 * 255.0
    t = np.clip(np.minimum(l, 255.0 - l) / knee, 0.0, 1.0)   # 0 at the extremes
    shift = (target - l) * (1.0 - t) * strength
    return arr + shift[..., None]


def dither(arr, method="floyd", pal=None, gamut=True, gamut_strength=1.0,
           weight=None, neutral=0.0):
    """arr: HxWx3 float RGB. Returns HxW uint8 palette indices."""
    pal = PAL_MEASURED if pal is None else pal
    buf = np.asarray(arr, dtype=np.float32).copy()
    if gamut:
        buf = gamut_map(buf, pal, gamut_strength)
    h, w, _ = buf.shape
    lut = _lut(pal, weight, neutral)

    # No sequential dependency in these two, so do the whole plane at once.
    if method in ("nearest", "bayer"):
        v = buf
        if method == "bayer":
            amp = 110.0
            tile = np.tile(BAYER8, (h // 8 + 1, w // 8 + 1))[:h, :w]
            v = v + (tile * amp)[..., None]
        q = np.asarray(_QUANT, np.int32)[np.clip(v, 0, 255).astype(np.int32)]
        flat = (q[..., 0] << (2 * _LUT_BITS)) | (q[..., 1] << _LUT_BITS) | q[..., 2]
        return lut[flat].reshape(h, w)

    ker, divisor = KERNELS[method]
    # Every shipped kernel's taps sum to at most its divisor, so the in-bounds
    # sum can never exceed it and the normalising denominator is just the
    # divisor. The loop used to rediscover that per pixel with a whole extra
    # pass over the kernel. If a future kernel breaks the assumption this
    # catches it at import rather than silently over-diffusing at the edges.
    assert sum(t[2] for t in ker) <= divisor, \
        "%s taps exceed its divisor; edge normalisation would need rethinking" % method

    kdx = np.array([t[0] for t in ker], np.int64)
    kdy = np.array([t[1] for t in ker], np.int64)
    kwt = np.array([t[2] for t in ker], np.float64)

    # float64 throughout, matching what .tolist() used to widen the float32
    # buffer to. The arithmetic below is bit-for-bit the same either way, which
    # is what lets the fast path and the fallback agree.
    px = buf.reshape(-1).astype(np.float64)
    palf = np.asarray(pal, np.float32).astype(np.float64)
    quant = np.asarray(_QUANT, np.int64)

    out = _run_diffuse(px, lut.astype(np.int64), palf, quant, _LUT_BITS,
                       float(CLAMP_LO), float(CLAMP_HI), h, w,
                       kdx, kdy, kwt, float(divisor))
    return out.astype(np.uint8).reshape(h, w)


def _run_diffuse(*args):
    """Diffuse with the compiled kernel, falling back to the interpreter.

    Compilation can fail at the first CALL rather than at import - a stale or
    unreadable on-disk cache raises there. Both paths produce the same bytes,
    so falling back costs only time, which is much better than failing to draw
    a frame. Demote once and stop trying.
    """
    global _diffuse, HAVE_NUMBA
    try:
        return _diffuse(*args)
    except Exception as e:
        if _diffuse is not _diffuse_py:
            sys.stderr.write("dither: numba unavailable (%s); "
                             "falling back to the interpreter\n" % e)
            _diffuse, HAVE_NUMBA = _diffuse_py, False
        return _diffuse_py(*args)


def _diffuse_py(px, lut, pal, quant, lut_bits, lo, hi, h, w,
                kdx, kdy, kwt, divisor):
    """Error diffusion, one pixel at a time. See _diffuse for why it is here.

    Strictly sequential: each pixel is quantised only after its neighbours have
    pushed their error into it, so there is nothing to vectorise. The clamp is
    applied per write and binds on a good fraction of them, which means even
    reordering the additions would change the picture.
    """
    out = np.zeros(h * w, np.uint8)
    stride = w * 3
    shift2 = 2 * lut_bits
    ntap = len(kwt)
    for y in range(h):
        rev = (y & 1) == 1
        base = y * stride
        for i in range(w):
            x = (w - 1 - i) if rev else i
            j = base + x * 3
            r = px[j]; g = px[j + 1]; b = px[j + 2]
            ir = 0 if r < 0 else (255 if r > 255 else int(r))
            ig = 0 if g < 0 else (255 if g > 255 else int(g))
            ib = 0 if b < 0 else (255 if b > 255 else int(b))
            k = lut[(quant[ir] << shift2) | (quant[ig] << lut_bits) | quant[ib]]
            out[y * w + x] = k
            er = r - pal[k, 0]; eg = g - pal[k, 1]; eb = b - pal[k, 2]
            for t in range(ntap):
                nx = x + (-kdx[t] if rev else kdx[t])
                ny = y + kdy[t]
                if 0 <= nx < w and 0 <= ny < h:
                    m = ny * stride + nx * 3
                    # Never normalise below the kernel's own divisor.
                    # Renormalising by the in-bounds sum would amplify at the
                    # edges - on the bottom row floyd's single remaining tap
                    # would take 100% of the error instead of 43.8% and chain
                    # it along the row. Capping discards the rest instead.
                    f = kwt[t] / divisor
                    v = px[m] + er * f
                    px[m] = lo if v < lo else (hi if v > hi else v)
                    v = px[m + 1] + eg * f
                    px[m + 1] = lo if v < lo else (hi if v > hi else v)
                    v = px[m + 2] + eb * f
                    px[m + 2] = lo if v < lo else (hi if v > hi else v)
    return out


# The loop above is the whole cost of a render - about 90% of it, and the one
# thing standing between a track change and the panel. numba compiles it to
# roughly the speed a C extension would reach, without a build step.
#
# fastmath stays off and every value stays float64 on purpose: that is what
# makes the compiled version produce the same bytes as the interpreter, so
# whether numba is installed cannot change what appears on the glass. The
# frame hashes in tests/baseline.py are what hold this to it.
try:
    from numba import njit as _njit
    _diffuse = _njit(cache=True, fastmath=False)(_diffuse_py)
    HAVE_NUMBA = True
except Exception:      # not installed, or the cache directory is unwritable
    _diffuse = _diffuse_py
    HAVE_NUMBA = False


def simulate(idx):
    """Simulate the panel using the given ink values."""
    return Image.fromarray(PAL_MEASURED[idx].astype(np.uint8))


# ------------------------------------------------------------------ output
def pack(idx):
    """Two pixels per byte, left pixel in the high nibble."""
    a = np.asarray(idx, dtype=np.uint8)
    h, w = a.shape
    if w % 2:
        raise ValueError("width must be even, got %d" % w)
    codes = np.array(CODES, dtype=np.uint8)[a]
    return bytes(((codes[:, 0::2] << 4) | codes[:, 1::2]).ravel())


def emit(arrays, out=None, source="", tool=""):
    """Write a C file. arrays: {symbol: bytes}. Defaults to the real firmware path."""
    out = out or DEFAULT_OUT
    with open(out, "w") as f:
        f.write("/* Generated by %s%s\n"
                " * Waveshare 4in0e, %dx%d, 6 colours, 4 bits per pixel.\n"
                " * Do not edit by hand, re-run the tool.\n */\n"
                '#include "ImageData.h"\n\n'
                % (tool or "tools/panel.py",
                   (" from " + os.path.basename(source)) if source else "",
                   WIDTH, HEIGHT))
        for name, data in arrays.items():
            f.write("const unsigned char %s[] = {\n" % name)
            for i in range(0, len(data), 16):
                f.write("    %s,\n" % ", ".join("0X%02X" % b for b in data[i:i + 16]))
            f.write("};\n\n")
    return out


# ------------------------------------------------------------------ scoring
def score(idx):
    """Rate a dithered result, scored on the quantised output rather than the
    source pixels' nearest inks - the two differ once dithering redistributes
    error."""
    n = np.bincount(np.asarray(idx).ravel(), minlength=len(PALETTE)).astype(float)
    tot = n.sum()
    chroma = float(n[2:].sum() / tot)
    p = n[n > 0] / tot
    spread = float(-(p * np.log(p)).sum() / np.log(len(PALETTE)))
    rich = chroma * (0.5 + 0.5 * spread)
    verdict = ("rich" if rich > 0.40 else "good" if rich > 0.28
               else "flat" if rich > 0.15 else "monotonous")
    return {"verdict": verdict, "chroma": chroma, "spread": spread,
            "rich": rich, "counts": dict(zip(NAMES, n.astype(int).tolist()))}
