#!/usr/bin/env python3
"""Render parameter sweeps as contact sheets you can look at.

Numbers rank settings; they do not tell you which one looks right. This renders
every cover against every value of a setting into one labelled grid, so the
choice can be made by eye and the metrics used only to explain it afterwards.

    python3 tools/contactsheet.py --sweep blend
    python3 tools/contactsheet.py --sweep saturate --cell 200
    python3 tools/contactsheet.py --compare            # named configs side by side
    python3 tools/contactsheet.py --all                # every sweep
"""
import argparse, os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "server"))
import panel
from benchmark import load_covers, render, metrics, SIM, SWEEPS, BASELINE

PAD, LABEL_W, HEAD_H = 6, 96, 26
BG, FG, DIM = (18, 18, 20), (235, 235, 232), (150, 150, 156)

# Named configurations worth comparing directly.
CONFIGS = {
    "plain floyd":      dict(method="floyd", blend=0.0),
    "baseline (live)":  dict(method="floyd", blend=0.05, brightness=1.05, saturate=2.2),
    "jarvis 0.25":      dict(method="jarvis", blend=0.25),
    "jarvis 0.25 +sat": dict(method="jarvis", blend=0.25, saturate=1.6),
    "atkinson 0.25":    dict(method="atkinson", blend=0.25),
}


def _font(size):
    for p in ("/System/Library/Fonts/Supplemental/Arial.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def sheet(covers, columns, render_fn, title, cell=180, annotate=True):
    """columns: list of (label, kwargs). One row per cover."""
    f_lab, f_head = _font(12), _font(13)
    w = LABEL_W + len(columns) * (cell + PAD) + PAD
    h = HEAD_H + len(covers) * (cell + PAD) + PAD + 22
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.text((PAD, 6), title, font=f_head, fill=FG)

    for ci, (label, _) in enumerate(columns):
        x = LABEL_W + ci * (cell + PAD)
        d.text((x + 2, HEAD_H + 4), str(label), font=f_lab, fill=DIM)

    for ri, (name, src) in enumerate(covers):
        y = HEAD_H + 22 + ri * (cell + PAD)
        d.text((PAD, y + cell // 2 - 7), name[:14], font=f_lab, fill=DIM)
        for ci, (_, kw) in enumerate(columns):
            idx = render_fn(src, **kw)
            tile = Image.fromarray(SIM[idx].astype(np.uint8)).resize(
                (cell, cell), Image.NEAREST)
            x = LABEL_W + ci * (cell + PAD)
            img.paste(tile, (x, y))
            if annotate:
                m = metrics(src, idx)
                d.rectangle([x, y + cell - 15, x + cell, y + cell], fill=(0, 0, 0))
                d.text((x + 3, y + cell - 14),
                       "hue %.0f°  det %.2f" % (m["hue"], m["detail"]),
                       font=_font(10), fill=(200, 200, 200))
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--covers", default=os.path.join(HERE, "..", "covers"))
    ap.add_argument("--out", default=os.path.join(HERE, "..", "sheets"))
    ap.add_argument("--sweep")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--cell", type=int, default=180)
    ap.add_argument("--size", type=int, default=280)
    a = ap.parse_args()

    covers = load_covers(a.covers, a.size)
    if not covers:
        print("no covers in %s - run covers/fetch.py" % a.covers); return 1
    os.makedirs(a.out, exist_ok=True)

    jobs = []
    if a.compare or a.all:
        jobs.append(("configs", [(k, v) for k, v in CONFIGS.items()],
                     "named configurations"))
    names = list(SWEEPS) if a.all else ([a.sweep] if a.sweep else [])
    for n in names:
        jobs.append((n, [(v, {n: v}) for v in SWEEPS[n]],
                     "%s   (everything else at baseline)" % n))

    if not jobs:
        jobs.append(("configs", [(k, v) for k, v in CONFIGS.items()],
                     "named configurations"))

    for tag, cols, title in jobs:
        img = sheet(covers, cols, render, title, cell=a.cell)
        path = os.path.join(a.out, "%s.png" % tag)
        img.save(path)
        print("%-12s %s  (%dx%d)" % (tag, path, img.width, img.height))
    return 0


if __name__ == "__main__":
    sys.exit(main())
