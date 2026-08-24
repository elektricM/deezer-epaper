#!/usr/bin/env python3
"""Fetch the benchmark cover set from Deezer's public catalogue.

The covers are not in the repository - they are album art and not ours to
redistribute. This pulls them into covers/ on demand, for local benchmarking
only.

    python3 covers/fetch.py
"""
import json, os, sys, urllib.parse, urllib.request

# Chosen to span dark, bright, neutral, skin, and specific hues. If a lookup
# drifts to a different edition the benchmark still works; the numbers in
# docs/epaper-rendering.md were taken with these.
SET = [
    ("Daft Punk", "Random Access Memories", "ram"),        # near-black, silver
    ("Empire of the Sun", "Walking On A Dream", "eots"),    # busy midtones
    ("Doja Cat", "Hot Pink", "pink"),                       # magenta
    ("Tame Impala", "Currents", "currents"),                # high chroma
    ("Adele", "25", "skin"),                                # skin, low chroma
    ("Frank Ocean", "Blonde", "highkey"),                   # bright
    ("Nirvana", "Nevermind", "blue"),                       # one dominant hue
    ("Miles Davis", "Kind of Blue", "mono"),                # near-monochrome
    ("Amy Winehouse", "Back to Black", "dark"),             # dark
    ("Halsey", "Badlands", "halsey"),                       # cyan
    ("SIAMES", "Home", "siames"),                           # saturated red/magenta
]
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    for artist, album, tag in SET:
        out = os.path.join(HERE, tag + ".jpg")
        if os.path.exists(out):
            print("have %s" % tag); continue
        q = urllib.parse.urlencode(
            {"q": 'artist:"%s" album:"%s"' % (artist, album), "limit": 1})
        try:
            d = json.load(urllib.request.urlopen(
                "https://api.deezer.com/search/album?" + q, timeout=15))
        except Exception as e:
            print("error %s: %s" % (tag, e), file=sys.stderr); continue
        if not d.get("data"):
            print("not found: %s - %s" % (artist, album), file=sys.stderr); continue
        a = d["data"][0]
        urllib.request.urlretrieve(a.get("cover_xl") or a.get("cover_big"), out)
        print("%-9s %s" % (tag, a["title"][:48]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
