#!/usr/bin/env python3
"""Render the current track as a 400x600 frame for the panel.

Cover art fills the top, track info sits along the bottom. Text is drawn in
index space at full size and never dithered - on six inks dithered type turns to
mush, so it stays pure black on white while only the artwork gets diffused.

  render.py --preview out.png                    whatever is playing
  render.py --demo "Air" "La femme d'argent" --preview out.png
  render.py --watch                              follow playback, push each track
"""
import argparse, io, json, os, subprocess, sys, time, urllib.request
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engines
import panel
import server as np_server

W, H = panel.WIDTH, panel.HEIGHT
ART = 400                      # cover art is square and full width
BLACK, WHITE = 0, 1            # palette indices

FONTS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
FONTS_R = ["/System/Library/Fonts/Supplemental/Arial.ttf",
           "/System/Library/Fonts/Helvetica.ttc"]
# Arial covers Latin, Greek and Cyrillic only, so CJK titles render as .notdef
# boxes. Pillow cannot fall back per glyph, so pick a face per string from the
# scripts it actually uses.
FONT_UNICODE = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
FONT_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def _needs_unicode(text):
    for ch in text or "":
        o = ord(ch)
        if o < 0x0400:            # Latin, Latin-1, Latin Extended, Greek
            continue
        if 0x0400 <= o <= 0x04FF:  # Cyrillic
            continue
        if o in (0x2018, 0x2019, 0x201C, 0x201D, 0x2013, 0x2014, 0x2026):
            continue
        return True
    return False


def font(size, bold=True, text=None):
    paths = list(FONTS if bold else FONTS_R)
    if text is not None and _needs_unicode(text):
        paths = [FONT_CJK, FONT_UNICODE] + paths
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_token(draw, tok, fnt, width):
    """Hard-break a token too wide to fit a line on its own - a URL, or a
    run-together CJK title, which has no space to wrap at."""
    if draw.textlength(tok, font=fnt) <= width:
        return [tok]
    out, cur = [], ""
    for ch in tok:
        if cur and draw.textlength(cur + ch, font=fnt) > width:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def wrap(draw, text, fnt, width, max_lines):
    """Greedy wrap, ellipsising when text is actually dropped.

    Truncation is tracked directly rather than inferred from total text width:
    ragged-right lines each stop short of `width`, so a title can lose its last
    words while the total still measures under any threshold.
    """
    if not text:
        return []
    words, lines, cur = text.split(), [], ""
    truncated = False
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=fnt) <= width:
            cur = t
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if len(lines) >= max_lines:
            truncated = True
            break
        pieces = _fit_token(draw, w, fnt, width)
        for piece in pieces[:-1]:
            if len(lines) >= max_lines:
                truncated = True
                break
            lines.append(piece)
        if truncated:
            break
        cur = pieces[-1]
    if cur:
        if len(lines) < max_lines:
            lines.append(cur)
        else:
            truncated = True
    if truncated and lines:
        last = lines[-1]
        while last and draw.textlength(last + "\u2026", font=fnt) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "\u2026"
    return lines[:max_lines]


def fetch_cover(url, b64=None):
    """Deezer's 1000x1000 art, falling back to MediaRemote's embedded thumbnail.

    That thumbnail is only ~150x150, but upscaled it still beats a blank card
    and is the only artwork available for anything the catalogue misses.
    """
    if not url:
        if b64:
            try:
                import base64
                return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            except Exception:
                return None
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "reframe"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception:
        return None


def cover_art(track, size=ART):
    """The source cover, fitted, undithered - the left-hand side of a comparison."""
    cover = fetch_cover(track.get("cover"), track.get("artwork_b64"))
    if cover is None:
        return None
    return panel.fit(cover, size, size, mode="crop")


def render(track, method="floyd", saturate=1.0, contrast=1.0,
           chroma=None, gamut=0.0, palette="ideal", pal_rgb=None,
           art_only=False, neutral=0.0, black_point=0.0, brightness=1.0,
           blend=0.0):
    """Return (index array, complete).

    complete is False when the track HAD a cover URL but fetching it failed, so
    the caller can decline to treat a degraded frame as final and retry later.
    """
    idx = np.full((H, W), WHITE, dtype=np.uint8)

    cover = fetch_cover(track.get("cover"), track.get("artwork_b64"))
    if cover is not None:
        sq = panel.fit(cover, ART, ART, mode="crop")
        arr = np.asarray(sq, dtype=np.float32)
        if black_point > 0:
            # Crush near-blacks before dithering. A background just above the
            # darkest ink keeps a live residual, and diffusion scatters stray
            # bright dots across it. Raising the black ink instead would hide
            # that at the cost of every shadow detail above it.
            lum = (arr * panel.LUMA).sum(axis=-1, keepdims=True)
            arr = np.where(lum <= black_point * 255.0, 0.0, arr)
        if brightness != 1.0:
            # Straight multiply, distinct from contrast: lifts the whole
            # picture, which a reflective 30:1 panel often wants.
            arr = np.clip(arr * brightness, 0, 255)
        if contrast != 1.0:
            # Pivot on mid grey rather than the image mean, so the same setting
            # behaves the same on a dark cover and a light one.
            arr = np.clip(128.0 + (arr - 128.0) * contrast, 0, 255)
        if saturate != 1.0:
            l = (arr * panel.LUMA).sum(axis=-1, keepdims=True)
            arr = np.clip(l + (arr - l) * saturate, 0, 255)
        # A hand-tuned palette wins over the named presets.
        pal = (np.asarray(pal_rgb, np.float32) if pal_rgb is not None
               else panel.PALETTES.get(palette, panel.PAL_IDEAL))
        # The tone stages above run whichever method is selected, so moving a
        # slider means the same thing across all of them and the comparison
        # stays a comparison of dithering rather than of whole pipelines.
        idx[0:ART, 0:W] = panel.dither(arr, method, pal,
                                       gamut=gamut > 0, gamut_strength=gamut,
                                       weight=chroma, neutral=neutral,
                                       blend=blend)
        complete = True
        if art_only:
            return idx[0:ART, 0:W], True
    else:
        # No override: the frame is already white, so a missing cover degrades
        # to a clean typographic card.
        complete = track.get("cover") is None and not track.get("artwork_b64")

    # --- info panel along the bottom, drawn in index space so it stays crisp
    band = Image.new("P", (W, H - ART), WHITE)
    band.putpalette(_flat_palette())
    d = ImageDraw.Draw(band)

    y = 18
    f_title = font(27, True, track.get("title") or "")
    for line in wrap(d, track.get("title") or "", f_title, W - 36, 2):
        d.text((18, y), line, font=f_title, fill=BLACK)
        y += 31
    y += 5

    f_art = font(20, False, track.get("artist") or "")
    for line in wrap(d, track.get("artist") or "", f_art, W - 36, 1):
        d.text((18, y), line, font=f_art, fill=BLACK)
        y += 25

    alb = track.get("album") or ""
    if alb and y < (H - ART) - 26:
        f_alb = font(15, False, alb)
        d.line([18, y + 6, W - 18, y + 6], fill=BLACK)
        y += 15
        for line in wrap(d, alb, f_alb, W - 100, 1):
            d.text((18, y), line, font=f_alb, fill=BLACK)
        dur = track.get("duration")
        if dur:
            txt = "%d:%02d" % (dur // 60, dur % 60)
            d.text((W - 18 - d.textlength(txt, font=f_alb), y), txt,
                   font=f_alb, fill=BLACK)

    idx[ART:H, 0:W] = np.asarray(band, dtype=np.uint8)
    return idx, complete


def _flat_palette():
    flat = []
    for rgb in panel.PAL_IDEAL.astype(int).tolist():
        flat.extend(rgb)
    return flat + [0] * (768 - len(flat))


def preview(idx, path):
    panel.simulate(idx).save(path)
    return path


def push(idx, project=None, force=False, interval=0):
    """Legacy USB path: compile the image into the firmware and re-flash.

    Superseded by the WiFi firmware, which fetches frames from /frame.bin.
    Kept for bringing up a panel with no working network.

    interval is 0: no manufacturer documents a minimum between refreshes. The
    widely repeated 180 s is reseller boilerplate. A refresh takes ~22 s and
    blocks, so updates are self-limiting. Pass interval=N to reinstate a wait.
    """
    project = project or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "firmware")
    stamp = os.path.join(project, ".last-refresh")
    if interval and not force and os.path.exists(stamp):
        age = time.time() - os.path.getmtime(stamp)
        if age < interval:
            return False, "cooling down, %d s left" % int(interval - age)

    panel.emit({"BMP_1": panel.pack(idx)}, tool="server/render.py")
    with open(os.path.join(project, "src", "main_usb.cpp"), "w") as f:
        f.write(MAIN_CPP)
    pio = os.path.expanduser("~/.platformio/penv/bin/pio")
    p = subprocess.run([pio, "run", "-e", "usb", "-t", "upload"], cwd=project,
                       capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout)[-300:]
    open(stamp, "w").close()
    return True, "flashed"


MAIN_CPP = '''#include <Arduino.h>
#include <Preferences.h>
#include "EPD_4in0e.h"
#include "ImageData.h"

// Written by server/render.py. Skips the refresh when the panel already
// shows this frame, so a power cycle costs nothing.

#define IMG_BYTES (EPD_4IN0E_WIDTH * EPD_4IN0E_HEIGHT / 2)

static uint32_t crc32(const uint8_t *d, size_t n, uint32_t crc) {
  crc = ~crc;
  for (size_t i = 0; i < n; i++) {
    crc ^= d[i];
    for (int k = 0; k < 8; k++)
      crc = (crc >> 1) ^ (0xEDB88320 & (-(int32_t)(crc & 1)));
  }
  return ~crc;
}

void setup() {
  Serial.begin(115200);
  delay(800);
  uint32_t want = crc32((const uint8_t *)BMP_1, IMG_BYTES, 0);
  Preferences prefs;
  bool nvs = prefs.begin("reframe", false);
  if (nvs && prefs.isKey("shown") && prefs.getUInt("shown") == want) {
    Serial.println("already showing this track");
    prefs.end();
    return;
  }
  DEV_Module_Init();
  EPD_4IN0E_Init();
  unsigned long t0 = millis();
  EPD_4IN0E_Display((UBYTE *)BMP_1);
  Serial.print("refresh ms: "); Serial.println(millis() - t0);
  EPD_4IN0E_Sleep();
  if (!EPD_4IN0E_timed_out && nvs) prefs.putUInt("shown", want);
  prefs.end();
  DEV_Module_Exit();
  Serial.println("done");
}

void loop() {}
'''


def watch(method, saturate, every=10, interval=0, port=8766):
    """Follow playback and draw each new track.

    Reads the track over HTTP from the running server rather than in-process:
    macOS grants Accessibility per binary, and the grant belongs to the app
    bundle, so an in-process call from a terminal never sees a track.

    A track already on the glass is never redrawn, a frame that rendered without
    its artwork is retried rather than treated as final, and failures back off.
    """
    import urllib.error
    url = "http://127.0.0.1:%d/now" % port
    shown = None
    fails = 0
    next_try = 0.0
    last_err = None
    print("watching %s - ctrl-c to stop." % url)
    while True:
        try:
            if time.time() >= next_try:
                try:
                    with urllib.request.urlopen(url, timeout=8) as r:
                        st = json.loads(r.read().decode())
                except Exception as e:
                    st = {"state": "app not reachable: %s" % e}

                # "held" is a real state: still the last Deezer track, we just
                # do not currently own the system audio session.
                track = st.get("track") if st.get("state") in ("playing", "paused", "held") else None
                if track is None and st.get("state") not in ("playing", "idle", "held"):
                    if st.get("state") != last_err:
                        last_err = st.get("state")
                        print("%s  %s" % (time.strftime("%H:%M:%S"), last_err))
                elif track:
                    last_err = None

                if track and track.get("id") != shown:
                    # Check the interval before rendering, not inside push():
                    # render() costs a dither plus a cover download.
                    if interval and _cooling(interval):
                        next_try = time.time() + 5
                    else:
                        idx, complete = render(track, method, saturate)
                        ok, msg = push(idx, force=True)
                        if ok:
                            if complete:
                                shown = track.get("id")
                            fails = 0
                            print("%s  ->  %s / %s%s" % (
                                time.strftime("%H:%M:%S"), track.get("artist"),
                                track.get("title"),
                                "" if complete else "  (no artwork, will retry)"))
                        else:
                            fails += 1
                            backoff = min(600, 10 * (3 ** min(fails - 1, 4)))
                            next_try = time.time() + backoff
                            print("%s  push failed (%d): %s - retrying in %ds" % (
                                time.strftime("%H:%M:%S"), fails, msg, backoff))
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        except Exception as e:
            print("%s  %s" % (time.strftime("%H:%M:%S"), e))
        try:
            time.sleep(every)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0


def _cooling(interval):
    stamp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "firmware", ".last-refresh")
    if not os.path.exists(stamp):
        return False
    return (time.time() - os.path.getmtime(stamp)) < interval


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--demo", nargs=2, metavar=("ARTIST", "TITLE"))
    ap.add_argument("--preview", metavar="PNG")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the interval")
    ap.add_argument("--interval", type=int, default=0,
                    help="seconds to wait between refreshes (default 0 - no "
                         "manufacturer minimum is documented)")
    ap.add_argument("--port", type=int, default=8766,
                    help="port of the running Now Playing server")
    ap.add_argument("--watch", action="store_true",
                    help="follow your music and push each new track")
    ap.add_argument("--method", default="floyd", choices=panel.METHODS)
    # Saturation raises coloured-ink share on nearly every cover; dark
    # low-chroma sources in particular want a lot of it. Tune per image.
    ap.add_argument("--saturate", type=float, default=1.0)
    a = ap.parse_args()

    if a.watch:
        return watch(a.method, a.saturate, interval=a.interval, port=a.port)

    if a.demo:
        track = np_server.deezer_enrich(a.demo[0], a.demo[1])
    else:
        np_server.load_cfg()
        st = np_server.wait_for_track(timeout=12)
        if not st.get("track"):
            print("nothing playing (%s) %s" % (st.get("state"), st.get("detail", "")))
            return 1
        track = st["track"]

    print("%s — %s" % (track.get("artist"), track.get("title")))
    idx, _ = render(track, a.method, a.saturate)
    print("score: %s" % panel.score(idx)["verdict"])
    if a.preview:
        print("preview: %s" % preview(idx, a.preview))
    if a.push:
        ok, msg = push(idx, force=a.force, interval=a.interval)
        print("push: %s" % msg)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
