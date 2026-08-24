"""sRGB, CIELAB, CIEDE2000 and a low-pass eye model.

Used for evaluating dithering quality, not for palette matching - see _lut()
in panel.py for why CIEDE2000 is the wrong tool for that.
"""
import numpy as np

_M_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                       [0.2126729, 0.7151522, 0.0721750],
                       [0.0193339, 0.1191920, 0.9503041]])
_WP_D65 = np.array([0.95047, 1.00000, 1.08883])


def srgb_to_linear(a):
    """0..255 gamma-encoded sRGB -> 0..1 linear light."""
    a = np.asarray(a, np.float64) / 255.0
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(a):
    a = np.clip(np.asarray(a, np.float64), 0.0, 1.0)
    return 255.0 * np.where(a <= 0.0031308, a * 12.92,
                            1.055 * a ** (1 / 2.4) - 0.055)


def linear_to_lab(lin):
    # BLAS raises spurious FP flags on its SIMD path for large arrays even when
    # every value is finite. Verified bit-identical to a chunked product that
    # does not trip them - the flags need ignoring, not the data clamping.
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        xyz = np.asarray(lin, np.float64) @ _M_RGB2XYZ.T / _WP_D65
    e, k = 216 / 24389, 24389 / 27
    f = np.where(xyz > e, np.cbrt(np.maximum(xyz, 0)), (k * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16,
                     500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])], axis=-1)


def srgb_to_lab(rgb):
    return linear_to_lab(srgb_to_linear(rgb))


def de2000(lab1, lab2):
    """CIEDE2000. Vectorised over any leading shape."""
    l1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    l2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    Cb7 = Cb ** 7
    G = 0.5 * (1 - np.sqrt(Cb7 / (Cb7 + 25.0 ** 7 + 1e-30)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = l2 - l1, C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0.0, dh)
    dHp = 2 * np.sqrt(np.maximum(C1p * C2p, 0)) * np.sin(np.radians(dh) / 2)
    Lbp, Cbp = (l1 + l2) / 2, (C1p + C2p) / 2
    hsum, hdiff = h1p + h2p, np.abs(h1p - h2p)
    hbp = np.where(C1p * C2p == 0, hsum,
          np.where(hdiff <= 180, hsum / 2,
          np.where(hsum < 360, (hsum + 360) / 2, (hsum - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30))
           + 0.24 * np.cos(np.radians(2 * hbp))
           + 0.32 * np.cos(np.radians(3 * hbp + 6))
           - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Cbp7 = Cbp ** 7
    Rt = (-np.sin(np.radians(2 * (30 * np.exp(-((hbp - 275) / 25) ** 2))))
          * 2 * np.sqrt(Cbp7 / (Cbp7 + 25.0 ** 7 + 1e-30)))
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                   + Rt * (dCp / Sc) * (dHp / Sh))


def blur_linear(rgb, sigma):
    """Low-pass in linear light, the only space where averaging is physical.
    Blurring gamma-encoded values makes a dithered mid-grey read too dark."""
    lin = srgb_to_linear(rgb)
    if sigma <= 0:
        return lin
    r = max(1, int(np.ceil(3 * sigma)))
    x = np.arange(-r, r + 1)
    k = np.exp(-x * x / (2 * sigma * sigma))
    k /= k.sum()
    out = lin
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (r, r)
        p = np.pad(out, pad, mode="edge")
        out = np.apply_along_axis(lambda m: np.convolve(m, k, "valid"), axis, p)
    return out


def perceptual_error(src_rgb, out_rgb, sigma=1.0):
    """Mean CIEDE2000 after modelling the eye's spatial integration.

    Do not score halftoning per pixel (sigma=0): dithering trades per-pixel
    accuracy for correct local averages, so a per-pixel metric ranks "no
    dithering at all" first. Even with the blur, treat the number as weak
    evidence - it prefers flat patches of the locally-correct average colour
    over speckle that looks better on the glass.
    """
    a = blur_linear(src_rgb, sigma)
    b = blur_linear(out_rgb, sigma)
    return float(de2000(linear_to_lab(a), linear_to_lab(b)).mean())
