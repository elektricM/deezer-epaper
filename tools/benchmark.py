#!/usr/bin/env python3
"""Standardised measurement of what each rendering setting does.

Protocol
--------
Fixed cover set, fixed baseline, one parameter swept at a time, four metrics
reported for every point. Two rules that make the numbers comparable:

1. The SIMULATION palette is held constant. Only what the matcher aims at is
   varied. Simulating each candidate with its own palette compares two different
   images and tells you nothing.
2. Hue error is measured only where the source has real chroma, since hue is
   undefined for a grey, and is weighted by that chroma.

Metrics
-------
hue     mean hue-angle error in degrees, chroma-weighted, after an eye-model
        blur. Catches a cyan rendering as green - which a colour-share or mean
        dE metric will not.
chroma  output chroma / source chroma after blur. Below 1 is washed out, above
        1 is oversaturated.
flat    share of DETAILED source regions that came out as a flat 5x5 patch.
        Restricted to places the source itself varies, because a cover with a
        genuinely solid black background is not posterised - counting those
        made a near-black cover read as 74% flat and swamped every comparison.
dE      mean CIEDE2000 after blur. Overall accuracy; weakest of the four,
        because it rewards flat patches of the locally correct average.

    python3 tools/benchmark.py --covers covers/ --sweep saturate
    python3 tools/benchmark.py --covers covers/ --all
"""
import argparse, os, sys, glob
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import panel, colour

# Everything is measured against this, so a change in the matcher is not
# confused with a change in what the preview is drawn with.
SIM = panel.PAL_MUTED
BLUR = 1.0
CHROMA_FLOOR = 12.0          # below this the source has no meaningful hue
DETAIL_FLOOR = 6.0           # local luma std below this and the source is flat too

BASELINE = dict(method="floyd", blend=0.0, brightness=1.0, contrast=1.0,
                saturate=1.0, neutral=0.0, black_point=0.0, weight=1.0)

SWEEPS = {
    "blend":       [0.0, 0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0],
    "saturate":    [0.6, 0.8, 1.0, 1.2, 1.4, 1.7, 2.0, 2.5],
    "brightness":  [0.8, 0.9, 1.0, 1.1, 1.2, 1.4],
    "contrast":    [0.7, 0.85, 1.0, 1.15, 1.3, 1.6],
    "neutral":     [0.0, 1.0, 2.0, 3.0, 5.0, 8.0],
    "black_point": [0.0, 0.02, 0.04, 0.06, 0.10, 0.15],
    "method":      ["nearest", "bayer", "floyd", "atkinson", "jarvis", "sierra"],
}


def prep(src, brightness=1.0, contrast=1.0, saturate=1.0, black_point=0.0, **_):
    a = src.astype(np.float32)
    if black_point > 0:
        L = (a * panel.LUMA).sum(-1, keepdims=True)
        a = np.where(L <= black_point * 255.0, 0.0, a)
    if brightness != 1.0:
        a = np.clip(a * brightness, 0, 255)
    if contrast != 1.0:
        a = np.clip(128.0 + (a - 128.0) * contrast, 0, 255)
    if saturate != 1.0:
        l = (a * panel.LUMA).sum(-1, keepdims=True)
        a = np.clip(l + (a - l) * saturate, 0, 255)
    return a


def render(src, **cfg):
    c = dict(BASELINE, **cfg)
    match_pal = panel.palette_blend(c["blend"])
    idx = panel.dither(prep(src, **c), c["method"], match_pal,
                       gamut=False, weight=c["weight"], neutral=c["neutral"])
    return idx


def metrics(src, idx):
    out = SIM[idx]
    a = colour.linear_to_lab(colour.blur_linear(src, BLUR))
    b = colour.linear_to_lab(colour.blur_linear(out, BLUR))
    ca = np.hypot(a[..., 1], a[..., 2])
    cb = np.hypot(b[..., 1], b[..., 2])

    m = ca > CHROMA_FLOOR
    if m.sum() < 100:
        hue = float("nan")
    else:
        ha = np.degrees(np.arctan2(a[..., 2], a[..., 1]))
        hb = np.degrees(np.arctan2(b[..., 2], b[..., 1]))
        d = np.abs((hb - ha + 180) % 360 - 180)
        hue = float(np.average(d[m], weights=ca[m]))

    chroma = float(cb[m].mean() / max(ca[m].mean(), 1e-6)) if m.sum() else float("nan")
    de = float(colour.de2000(a, b).mean())

    r = 2
    h, w = idx.shape
    same = np.ones((h - 2 * r, w - 2 * r), bool)
    cc = idx[r:h - r, r:w - r]
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            same &= (idx[r + dy:h - r + dy, r + dx:w - r + dx] == cc)

    # Only where the source has something to lose.
    lum = (src * panel.LUMA).sum(-1)
    win = np.lib.stride_tricks.sliding_window_view(lum, (2 * r + 1, 2 * r + 1))
    detailed = win.std(axis=(-2, -1)) > DETAIL_FLOOR
    flat = float(same[detailed].mean()) if detailed.sum() > 100 else 0.0
    return dict(hue=hue, chroma=chroma, flat=flat, de=de)


def load_covers(path, size=280):
    files = sorted(glob.glob(os.path.join(path, "*.jpg")) +
                   glob.glob(os.path.join(path, "*.png")))
    out = []
    for f in files:
        try:
            im = panel.fit(panel.load(f), size, size, "crop")
            out.append((os.path.basename(f).rsplit(".", 1)[0],
                        np.asarray(im, np.float32)))
        except Exception as e:
            print("skip %s: %s" % (f, e), file=sys.stderr)
    return out


def sweep(covers, name, values=None):
    values = values or SWEEPS[name]
    print("\n%s  (baseline: %s)" % (
        name, ", ".join("%s=%s" % (k, v) for k, v in sorted(BASELINE.items())
                        if k != name)))
    print("%-10s %9s %9s %9s %9s %8s" % (
        name, "hue mean", "hue worst", "flat mean", "flat worst", "chroma"))
    rows = []
    for v in values:
        acc = [metrics(src, render(src, **{name: v})) for _, src in covers]
        agg = {k: float(np.nanmean([a[k] for a in acc])) for k in acc[0]}
        agg["hue_worst"] = float(np.nanmax([a["hue"] for a in acc]))
        agg["flat_worst"] = float(np.nanmax([a["flat"] for a in acc]))
        agg["worst_cover"] = covers[int(np.nanargmax([a["hue"] for a in acc]))][0]
        rows.append((v, agg))
        print("%-10s %8.1f\u00b0 %8.1f\u00b0 %8.1f%% %8.1f%% %8.2f" % (
            v, agg["hue"], agg["hue_worst"],
            100 * agg["flat"], 100 * agg["flat_worst"], agg["chroma"]))
    # Report on the WORST cover, not the mean. A mean over a varied set hides a
    # single colour category failing completely, which is how several wrong
    # conclusions got made here before this harness existed.
    best_mean = min(rows, key=lambda r: r[1]["hue"])[0]
    best_worst = min(rows, key=lambda r: r[1]["hue_worst"])[0]
    print("  best hue: %s by mean, %s by worst cover" % (best_mean, best_worst))
    if best_mean != best_worst:
        print("  (they disagree - trust the worst case)")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--covers", default="covers")
    ap.add_argument("--sweep")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--size", type=int, default=280)
    a = ap.parse_args()

    covers = load_covers(a.covers, a.size)
    if not covers:
        print("no covers found in %s" % a.covers); return 1
    print("%d covers: %s" % (len(covers), ", ".join(n for n, _ in covers)))
    print("simulated with a fixed palette; only the matcher varies")

    if a.all:
        for k in SWEEPS:
            sweep(covers, k)
    elif a.sweep:
        sweep(covers, a.sweep)
    else:
        print("\nbaseline:")
        for n, src in covers:
            m = metrics(src, render(src))
            print("  %-22s hue %5.1f°  chroma %.2f  flat %4.1f%%  dE %5.2f" % (
                n, m["hue"], m["chroma"], 100 * m["flat"], m["de"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
