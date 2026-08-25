#!/usr/bin/env python3
"""Now playing on Deezer, served locally and rendered for an e-paper panel.

    python3 server/server.py          then open http://localhost:8766

Endpoints:
    /                 now-playing page
    /tune             dithering controls with a live side-by-side preview
    /frame.bin        packed 4bpp frame the panel fetches over WiFi
    /frame.png        the same frame as an image
    /now, /config     JSON state and settings

The track comes from macOS itself (see mediaremote.py), with a fallback that
reads Deezer's own window when another app holds the system audio session.
Deezer's public catalogue supplies the 1000x1000 cover art; no authentication
is involved anywhere.
"""
import argparse, io, json, os, re, signal, subprocess, sys, threading, time, zlib
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mediaremote
import numpy as np
import engines
import panel
from PIL import Image

PORT = 8766
DEEZER = "https://api.deezer.com"
CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "config.json")

_art = {}          # track key -> enriched Deezer data, so we hit the API once
# Defaults are what looked right on the actual panel, not what scored best.
# Every "correction" this project accumulated - chroma weighting, gamut easing,
# pre-saturation, the measured palette - was switched off again after seeing it
# on the glass. Plain Floyd-Steinberg against the ideal palette won.
_cfg = {"follow_any": False, "serve_lan": False, "method": "jarvis",
        "saturate": 1.0, "contrast": 1.0, "chroma": 1.0, "gamut": 0.0,
        # From tools/benchmark.py over 11 covers: jarvis wins on both mean and
        # worst-case hue error, and blend 0.2-0.3 minimises it. Everything else
        # off - brightness, contrast and saturation all degrade the worst cover.
        "neutral": 0.0, "black_point": 0.0, "brightness": 1.0,
        "blend": 0.0, "saved": {}, "bindings": {},
        "palette": "ideal"}
# Deezer keeps playing while minimised, so a momentary "no window"
# must not blank the display - hold what we last resolved.
_last = {"track": None, "at": 0.0}


_cfg_lock = threading.Lock()


def load_cfg():
    try:
        with open(CONFIG) as f:
            _cfg.update(json.load(f))
    except Exception:
        pass


def save_cfg():
    """Write the config atomically.

    Truncating the real file first means a crash or two concurrent POSTs can
    leave it half written, and load_cfg() swallows the parse error and silently
    reverts to defaults. These values were arrived at by eye over eleven
    covers; they are the only state here that cannot be regenerated, so the
    write either lands whole or not at all.
    """
    tmp = CONFIG + ".tmp"
    with _cfg_lock:
        with open(tmp, "w") as f:
            json.dump(_cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, CONFIG)


def get_json(url, timeout=10):
    """GET JSON, and parse the body even on an HTTP error.

    Last.fm answers some invalid keys with 403 and a JSON error body rather than
    200 and an error object. urllib raises on 403, so without this a mistyped key
    surfaced as "could not reach last.fm" - the least useful message possible at
    exactly the moment the user is typing their key in.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "deezer-epaper"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            raise e
        if isinstance(body, dict) and "error" in body:
            return body        # a real API error, let the caller interpret it
        raise e


# ---------------------------------------------------------------- track source
# Bundle IDs we consider "Deezer". The desktop app is Electron; the web player
# reports whichever browser hosts it, so following the browser is opt-in via
# follow_any rather than guessed.
DEEZER_BUNDLES = ("com.deezer.",)

_watcher = None
_last = {"track": None, "at": 0.0, "state": None}
_frame_cache = {}
_frame_lock = threading.Lock()


_demo = {}


def _parse_pal(text):
    """Six comma-separated hex triplets into an RGB array, or None."""
    if not text:
        return None
    parts = [p.strip().lstrip("#") for p in text.split(",")]
    if len(parts) != len(panel.PALETTE):
        return None
    out = []
    for p in parts:
        if len(p) != 6:
            return None
        try:
            out.append([int(p[i:i + 2], 16) for i in (0, 2, 4)])
        except ValueError:
            return None
    return out


def _demo_track():
    """A varied cover to tune against when nothing is playing."""
    if "t" not in _demo:
        try:
            _demo["t"] = deezer_enrich("Tame Impala", "The Less I Know The Better")
        except Exception:
            _demo["t"] = None
    return _demo.get("t")


def _lan_ip():
    """This machine's LAN address, for printing the panel URL."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))     # TEST-NET-1: never actually sends
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


# Every setting a preset captures. Kept in one place so saving, applying and
# validating cannot drift apart - the old "presets" only carried the six
# pigment values, which is why a good look could not actually be saved.
PRESET_KEYS = ("method", "saturate", "contrast", "chroma", "gamut", "neutral",
               "black_point", "brightness", "blend", "palette", "pal")


def _album_key(track):
    """Identity a preset can be bound to.

    The album rather than the track: covers belong to records, and a setting
    chosen for one song is nearly always right for its neighbours.
    """
    if not track:
        return None
    a = (track.get("artist") or "").strip().lower()
    b = (track.get("album") or track.get("title") or "").strip().lower()
    return ("%s|%s" % (a, b)) if (a or b) else None


def _album_label(track):
    """Human-readable name for whatever a preset would bind to."""
    if not track:
        return None
    a = (track.get("artist") or "").strip()
    b = (track.get("album") or track.get("title") or "").strip()
    return (" - ".join(x for x in (a, b) if x)) or None


def settings_for(track):
    """The settings to render this track with.

    A preset bound to the album wins over the global settings, which is what
    makes per-cover tuning stick: one record can want a different treatment
    from the next, and the last thing tuned should not become the default for
    everything.
    """
    key = _album_key(track)
    name = (_cfg.get("bindings") or {}).get(key) if key else None
    preset = (_cfg.get("saved") or {}).get(name) if name else None
    out = {k: _cfg.get(k) for k in PRESET_KEYS}
    if preset:
        out.update({k: v for k, v in preset.items() if k in PRESET_KEYS})
        out["_preset"] = name
    return out


# Whether a rendered frame actually had its cover, keyed by ETag. Bounded:
# only the last few matter, and an unknown key is treated as complete.
_frame_complete = {}


def render_frame(track, want_png=False):
    """Packed frame plus its ETag, re-rendering only when the track changes."""
    st = settings_for(track)
    key = (track.get("id"), want_png) + tuple(st.get(k) for k in PRESET_KEYS)
    with _frame_lock:
        if _frame_cache.get("key") == key and _frame_cache.get("body"):
            return _frame_cache["body"], _frame_cache["etag"]
    import render as epd
    t0 = time.time()
    def _f(k, d):
        try:
            return float(st.get(k, d))
        except (TypeError, ValueError):
            return d
    idx, complete = epd.render(
        track,
        st.get("method") or "atkinson",
        saturate=_f("saturate", 1.0),
        contrast=_f("contrast", 1.0),
        chroma=_f("chroma", panel.CHROMA_WEIGHT),
        gamut=_f("gamut", 0.0),
        palette=st.get("palette") or "ideal",
        pal_rgb=_parse_pal(st.get("pal")),
        neutral=_f("neutral", 0.0),
        black_point=_f("black_point", 0.0),
        brightness=_f("brightness", 1.0),
        blend=_f("blend", 0.0))
    if want_png:
        buf = io.BytesIO()
        panel.simulate(idx).save(buf, "PNG")
        body = buf.getvalue()
    else:
        body = panel.pack(idx)
    etag = '"%08x"' % (zlib.crc32(body) & 0xFFFFFFFF)
    if len(_frame_complete) > 32:
        _frame_complete.clear()      # only the most recent frames matter
    _frame_complete[etag] = complete
    if complete:
        # Only cache a frame that has its artwork, so a CDN hiccup does not pin
        # a text-only card until the song changes.
        with _frame_lock:
            _frame_cache.update(key=key, body=body, etag=etag)
    if _cfg.get("verbose"):
        print("rendered %-38s %5.0f ms%s" % (
            (track.get("title") or "")[:38], 1000 * (time.time() - t0),
            "" if complete else "  (no artwork)"))
    return body, etag


def prerender_loop():
    """Render the next frame as soon as the track changes, keeping the cover
    download and the dither out of the panel's critical path."""
    w = source()
    seq = w.seq()
    last_window = False
    while True:
        try:
            # Same as the long poll: MediaRemote never signals when the track
            # comes from Deezer's window, so poll in that mode.
            seq = w.wait_for_change(seq, WINDOW_POLL if last_window else 60)
            st = current()
            last_window = _from_window(st)
            if st.get("track"):
                render_frame(st["track"])
        except Exception as e:
            if _cfg.get("verbose"):
                print("prerender: %s" % e)
            time.sleep(2)


def _ascii(s):
    """HTTP headers are latin-1; a Japanese title would otherwise throw."""
    return (s or "").encode("ascii", "replace").decode("ascii")[:120]
HOLD_SECONDS = 1800.0     # keep the last Deezer track on the glass this long


def source():
    global _watcher
    if _watcher is None:
        _watcher = mediaremote.Watcher().start()
    return _watcher


def _is_deezer(bundle):
    return bool(bundle) and any(bundle.startswith(p) for p in DEEZER_BUNDLES)


def current():
    """What is playing, as macOS itself reports it.

    Policy lives here rather than in the adapter. macOS exposes ONE active
    now-playing client, so when a browser video grabs the session Deezer becomes
    invisible even though it is still the thing this panel is for. Rather than
    show a YouTube title on the wall, we report that another app took the
    session and keep the last Deezer track. The adapter also lists every
    registered player, which is what makes "Deezer is idle" distinguishable
    from "Deezer is not running".
    """
    st = source().snapshot()
    state = st.get("state")

    if state in ("starting", "adapter exited", "adapter failed to start",
                 "adapter build failed"):
        return {"state": "starting", "source": "MediaRemote",
                "detail": st.get("detail", "")}

    bundle = st.get("bundleId") or ""
    players = st.get("players") or []
    deezer_present = (any(_is_deezer(p.get("bundleId")) for p in players)
                      or _deezer_app_running())
    follow_any = bool(_cfg.get("follow_any"))

    if state in ("playing", "paused") and (follow_any or _is_deezer(bundle)):
        track = enrich(st)
        _last.update(track=track, at=time.time(), state=state)
        return {"state": state, "source": st.get("appName") or "MediaRemote",
                "track": track,
                "elapsed": st.get("elapsed"), "duration": st.get("duration")}

    # Something else owns the audio session. If Deezer is running, ask Deezer
    # directly rather than reporting that we cannot see it.
    if state in ("playing", "paused") and bundle:
        if deezer_present and not follow_any:
            track, werr = track_from_window()
            if track:
                _last.update(track=track, at=time.time(), state="playing")
                return {"state": "playing", "source": "Deezer (window)",
                        "track": track}
            if werr == "accessibility":
                held = _held()
                out = {"state": "needs_accessibility",
                       "source": st.get("appName") or bundle,
                       "detail": "%s holds the audio session, so Deezer can only "
                                 "be read from its window - which needs "
                                 "Accessibility." % (st.get("appName") or bundle),
                       "deezer_running": True}
                if held:
                    out["track"] = held
                return out
        held = _held()
        out = {"state": "other", "source": st.get("appName") or bundle,
               "detail": "%s has the audio session" % (st.get("appName") or bundle),
               "deezer_running": deezer_present}
        if deezer_present and not follow_any:
            # Say WHY the Deezer window fallback did not answer, rather than
            # falling back to a message that implies it was never tried.
            raw, rerr = deezer_window_title()
            out["window_raw"] = raw
            out["window_error"] = rerr or "parsed but not corroborated"
        if held:
            out["track"], out["state"] = held, "held"
        return out

    held = _held()
    if held:
        return {"state": "held", "source": "Deezer", "track": held}
    return {"state": "idle", "source": "MediaRemote",
            "deezer_running": deezer_present}


def wait_for_track(timeout=12.0):
    """Block briefly until the adapter has something to say.

    The watcher is a subprocess plus an XPC round trip, so the first call after
    start-up legitimately returns "starting". One-shot callers - --push, --demo -
    would otherwise report "nothing playing" purely because they asked too soon.
    """
    deadline = time.time() + timeout
    st = current()
    while time.time() < deadline and st.get("state") == "starting":
        time.sleep(0.4)
        st = current()
    return st


# ---------------------------------------------------------------- deezer window
# macOS exposes a single now-playing client, so a browser holding that slot -
# even while paused - hides Deezer entirely. The private per-app MediaRemote
# calls (GetNowPlayingInfoForApp / ForClient / PlayerForClient) never fire their
# callbacks with any signature tried, so this reads Deezer's window title
# instead. Only consulted when Deezer is registered but does not own the
# session, and the parsed pair is discarded unless the catalogue confirms it.

_WINDOW_SCRIPT = (
    'tell application "System Events"\n'
    '  if not (exists process "Deezer") then return "__NOTRUNNING__"\n'
    '  tell process "Deezer"\n'
    '    if (count of windows) is 0 then return "__NOWINDOW__"\n'
    '    repeat with w in windows\n'
    '      set n to name of w\n'
    '      if n is not "" then return n\n'
    '    end repeat\n'
    '  end tell\n'
    '  return "__NOWINDOW__"\n'
    'end tell')


def _deezer_app_running():
    """Whether the Deezer app is running at all, regardless of MediaRemote.

    MediaRemote only lists a player once it has played something, so a freshly
    opened Deezer is invisible there. Without this, "Deezer is open but another
    app holds the session" is indistinguishable from "Deezer is not running",
    and the permission that would resolve it never gets asked for. Cached
    because it costs a process spawn.
    """
    now = time.time()
    with _proc_lock:
        if _proc_cache["at"] > now - DEEZER_PROC_TTL:
            return _proc_cache["val"]
    try:
        val = subprocess.run(["pgrep", "-x", "Deezer"],
                             capture_output=True, timeout=3).returncode == 0
    except Exception:
        val = False
    with _proc_lock:
        _proc_cache.update(at=time.time(), val=val)
    return val


DEEZER_PROC_TTL = 5.0
_proc_lock = threading.Lock()
_proc_cache = {"at": 0.0, "val": False}


def deezer_window_title():
    """Deezer's window title, or (None, reason). Counts windows first, since
    `front window` raises -1719 whenever Deezer is minimised or hidden.

    Memoised for a beat: the long poll and the prerender loop both ask on the
    same timer, so an uncached call meant two osascript processes and about
    150 ms of work every two seconds, for as long as a browser held the
    now-playing slot. The window cannot change faster than the poll notices
    anyway.
    """
    now = time.time()
    with _win_lock:
        if _win_cache["at"] > now - WINDOW_TITLE_TTL:
            return _win_cache["val"]
    val = _deezer_window_title_uncached()
    with _win_lock:
        _win_cache.update(at=time.time(), val=val)
    return val


WINDOW_TITLE_TTL = 1.5
_win_lock = threading.Lock()
_win_cache = {"at": 0.0, "val": (None, None)}


def _deezer_window_title_uncached():
    try:
        p = subprocess.run(["osascript", "-e", _WINDOW_SCRIPT],
                           capture_output=True, text=True, timeout=5)
    except Exception as e:
        return None, str(e)
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        if "assistive access" in err or "-1728" in err:
            return None, "accessibility"
        return None, (err.splitlines()[0] if err else "osascript failed")
    t = p.stdout.strip()
    if t == "__NOTRUNNING__":
        return None, "not running"
    if t == "__NOWINDOW__" or not t:
        return None, "no window"
    return t, None


def track_from_window():
    """A corroborated track from Deezer's window title, or None.

    The title is a hint, the catalogue is the judge. Both field orders are
    tried, since the format is undocumented and differs between builds.
    """
    raw, err = deezer_window_title()
    if not raw:
        return None, err
    s = raw.strip()
    for junk in (" - Deezer", " | Deezer", " — Deezer", "Deezer - ", "Deezer"):
        if s.endswith(junk):
            s = s[: -len(junk)].strip(" -|—")
        elif s.startswith(junk):
            s = s[len(junk):].strip(" -|—")
    s = s.strip()
    if not s:
        return None, "idle"
    parts = re.split(r"\s+[-–—]\s+", s, maxsplit=1)
    if len(parts) != 2:
        return None, "unrecognised title"
    a, b = parts[0].strip(), parts[1].strip()
    for artist, title in ((b, a), (a, b)):        # "Title - Artist" is the common one
        got = deezer_enrich(artist, title)
        if got.get("cover") and _plausible(got, artist, title):
            got = dict(got)
            got["id"] = "%s|%s|window" % (got.get("artist") or "", got.get("title") or "")
            got["window_raw"] = raw          # so a wrong match is diagnosable
            return got, None
    return None, "not found in catalogue"


WINDOW_POLL = 2.0     # seconds between window re-checks when MediaRemote is blind


def _from_window(st):
    return str((st or {}).get("source", "")).endswith("(window)")


def _held():
    if _last["track"] and time.time() - _last["at"] < HOLD_SECONDS:
        return _last["track"]
    return None


# ---------------------------------------------------------------- deezer art
# Successes cached indefinitely (catalogue metadata is static), failures only
# briefly, transport errors not at all - otherwise one network blip poisons a
# track for the life of the process.
_art = {}
_FAIL_TTL = 120.0


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _plausible(track, artist, title):
    """Deezer search always returns its best guess rather than nothing, so
    require the artist to correspond before trusting the result."""
    if not artist:
        return True
    a, b = _norm(track.get("artist")), _norm(artist)
    if not a or not b:
        return False
    ta, tb = set(a.split()), set(b.split())
    return bool(ta & tb) and (a in b or b in a or len(ta & tb) >= min(len(ta), len(tb)))


def deezer_enrich(artist, title, album=None):
    """Cover art and metadata from Deezer's public catalogue. No auth needed."""
    key = "%s|%s" % (artist or "", title or "")
    hit = _art.get(key)
    if hit is not None:
        data, at, ok = hit
        if ok or time.time() - at < _FAIL_TTL:
            return data

    fallback = {"title": title, "artist": artist, "album": album, "cover": None,
                "duration": None, "explicit": False, "link": None, "id": key}
    queries = []
    if artist and title:
        queries.append('artist:"%s" track:"%s"' % (artist, title))
        queries.append("%s %s" % (artist, title))
    elif title:
        queries.append(title)

    transport_error = False
    for q in queries:
        try:
            d = get_json(DEEZER + "/search?" + urllib.parse.urlencode(
                {"q": q, "limit": 5}))
        except Exception:
            transport_error = True      # NOT a catalogue miss - do not cache
            continue
        for t in (d.get("data") or []):
            alb, art = t.get("album") or {}, t.get("artist") or {}
            cand = {"title": t.get("title"), "artist": art.get("name"),
                    "album": alb.get("title"),
                    "cover": alb.get("cover_xl") or alb.get("cover_big"),
                    "duration": t.get("duration"),
                    "explicit": bool(t.get("explicit_lyrics")),
                    "link": t.get("link"), "id": t.get("id")}
            if _plausible(cand, artist, title):
                _art[key] = (cand, time.time(), True)
                return cand
    if not transport_error:
        _art[key] = (fallback, time.time(), False)
    return fallback


def enrich(st):
    """Merge the player's metadata with Deezer catalogue artwork.

    The player is authoritative for title/artist/album; the catalogue is
    consulted only for a 1000x1000 cover.
    """
    artist, title = st.get("artist"), st.get("title")
    album = st.get("album") or None
    dz = deezer_enrich(artist, title, album)
    track = {
        "title": title or dz.get("title"),
        "artist": artist or dz.get("artist"),
        "album": album or dz.get("album"),
        "cover": dz.get("cover"),
        "duration": int(st["duration"]) if st.get("duration") else dz.get("duration"),
        "explicit": dz.get("explicit", False),
        "link": dz.get("link"),
        # Identity for "is this already on the glass". Must include the title:
        # artworkId is per-album, so every track on one record would share it.
        # ContentItemIdentifier is avoided - regenerated on every query.
        "id": "%s|%s|%s" % (artist or "", title or "", st.get("artworkId") or ""),
    }
    if not track["cover"] and st.get("artwork"):
        track["artwork_b64"] = st["artwork"]   # small, but better than nothing
    return track


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/":
            self._html(PAGE)
        elif u.path == "/now":
            try:
                self._json(current())
            except Exception as e:
                self._json({"state": "error", "detail": str(e)}, 500)
        elif u.path in ("/frame.bin", "/frame.png"):
            self._frame(u)
        elif u.path == "/tune":
            self._html(TUNE_PAGE)
        elif u.path == "/tune/preview.png":
            self._tune_preview(u)
        elif u.path == "/tune/source.png":
            self._tune_source(u)
        elif u.path == "/config":
            self._json({"follow_any": bool(_cfg.get("follow_any")),
                        "serve_lan": bool(_cfg.get("serve_lan")),
                        "lan_ip": _lan_ip(),
                        "method": _cfg.get("method", "atkinson"),
                        "saturate": float(_cfg.get("saturate", 1.0)),
                        "contrast": float(_cfg.get("contrast", 1.0)),
                        "chroma": float(_cfg.get("chroma", panel.CHROMA_WEIGHT)),
                        "gamut": float(_cfg.get("gamut", 0.0)),
                        "neutral": float(_cfg.get("neutral", 0.0)),
                        "black_point": float(_cfg.get("black_point", 0.0)),
                        "brightness": float(_cfg.get("brightness", 1.0)),
                        "palette": _cfg.get("palette", "ideal"),
                        "pal": _cfg.get("pal", ""),
                        "presets": {k: ["%02x%02x%02x" % tuple(int(c) for c in row)
                                        for row in v]
                                    for k, v in panel.PALETTES.items()},
                        "names": panel.NAMES,
                        "methods": panel.METHODS,
                        "engines": sorted(engines.ENGINES),
                        "blend": float(_cfg.get("blend", 0.0)),
                        "saved": {k: v for k, v in
                                  (_cfg.get("saved") or {}).items()},
                        "album": _album_key((current() or {}).get("track")),
                        "album_label": _album_label((current() or {}).get("track")),
                        "bound": (_cfg.get("bindings") or {}).get(
                            _album_key((current() or {}).get("track"))),
                        "palettes": list(panel.PALETTES)})
        elif u.path == "/players":
            # Diagnostics: who is registered with macOS as a now-playing app.
            self._json({"players": (source().snapshot() or {}).get("players", [])})
        else:
            self._json({"error": "not found"}, 404)

    def _html(self, page):
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _tune_track(self):
        st = current()
        return st.get("track") or _last.get("track") or _demo_track()

    def _tune_source(self, u):
        """The undithered cover, for side-by-side comparison."""
        import render as epd
        track = self._tune_track()
        img = epd.cover_art(track) if track else None
        if img is None:
            return self._json({"error": "no artwork"}, 404)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _tune_preview(self, u):
        """Render the current track with the settings in the query string.

        Server-side deliberately: this is the same code path that produces the
        panel's bytes, so the preview cannot drift from the glass.
        """
        q = urllib.parse.parse_qs(u.query)
        def num(k, d):
            try:
                return float(q.get(k, [d])[0])
            except (TypeError, ValueError):
                return d
        track = self._tune_track()
        if not track:
            return self._json({"error": "no artwork available to preview"}, 404)
        method = q.get("method", ["atkinson"])[0]
        if method not in panel.METHODS:
            method = "atkinson"
        import render as epd
        idx, _ = epd.render(track, method,
                            saturate=num("saturate", 1.0),
                            contrast=num("contrast", 1.0),
                            chroma=num("chroma", panel.CHROMA_WEIGHT),
                            gamut=num("gamut", 0.0),
                            palette=q.get("palette", ["ideal"])[0],
                            pal_rgb=_parse_pal(q.get("pal", [None])[0]),
                            art_only=q.get("art", ["0"])[0] == "1",
                            neutral=num("neutral", 0.0),
                            black_point=num("black_point", 0.0),
                            brightness=num("brightness", 1.0),
                            blend=num("blend", 0.0))
        pal_rgb = _parse_pal(q.get("pal", [None])[0])
        shown = (np.asarray(pal_rgb, np.uint8) if pal_rgb is not None
                 else panel.PALETTES.get(q.get("palette", ["ideal"])[0],
                                         panel.PAL_IDEAL).astype(np.uint8))
        buf = io.BytesIO()
        Image.fromarray(shown[idx]).save(buf, "PNG")
        body = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _frame(self, u):
        """Serve the current track as a panel-ready frame.

        Supports long polling: `?wait=N` holds the request open for up to N
        seconds while the frame is unchanged, and returns the moment the track
        actually changes. That is what makes the panel look responsive. Plain
        interval polling meant a song change waited out the poll period before
        the 22 s refresh even started; long polling cuts that to about a second,
        which leaves the refresh itself as the only remaining delay - and that
        one is physics, not software.

        This is what the ESP32 fetches over WiFi, and it is why the panel no
        longer needs a cable. Previously every cover change recompiled the
        firmware with the image baked in as a C array and re-flashed the whole
        binary over USB - about 13 s of build and upload before the 22 s refresh
        could even start.

        Two things make polling cheap enough to do every minute:

        ETag/304   the frame's CRC32 is its ETag, so an unchanged track costs
                   the panel a bare header exchange instead of 120 KB.
        render cache  dithering a cover takes ~0.5 s plus a download, so the
                   packed bytes are kept until the track actually changes.
                   Without this, every poll would re-dither the same picture.
        """
        try:
            wait = max(0, min(120, int(urllib.parse.parse_qs(u.query)
                                       .get("wait", ["0"])[0])))
        except ValueError:
            wait = 0
        inm = self.headers.get("If-None-Match")
        deadline = time.time() + wait
        while True:
            seq = source().seq()
            st = current()
            track = st.get("track")
            body, etag = (render_frame(track, u.path.endswith(".png"))
                          if track else (None, None))
            # An artwork-less card is nearly white, and pushing it costs the
            # panel a full 20 s refresh - then the real cover arrives and costs
            # another. Keep waiting for the picture instead; if the cover truly
            # cannot be fetched, the deadline still lets the card through so a
            # missing cover degrades to text rather than to nothing.
            ready = etag is None or _frame_complete.get(etag, True)
            if not wait or inm is None or (etag != inm and ready) \
                    or time.time() >= deadline:
                break
            # Park until MediaRemote reports a change. When the track comes
            # from Deezer's window, MediaRemote is watching a different app and
            # never signals, so poll on a short timer in that mode instead.
            gap = WINDOW_POLL if _from_window(st) else 20.0
            source().wait_for_change(seq, min(gap, deadline - time.time()))

        if not track:
            self.send_response(204)      # nothing playing: leave the glass alone
            self.send_header("X-State", st.get("state", "idle"))
            self.end_headers()
            return

        if inm == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png"
                         if u.path.endswith(".png") else "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Track", _ascii(track.get("title")))
        self.send_header("X-Artist", _ascii(track.get("artist")))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Writes are loopback-only. The Origin check alone stops a browser
        # being used as a confused deputy, but anything that is not a browser
        # simply omits the header - so with serve_lan on, the whole network
        # could POST new dithering settings, or turn serve_lan itself off. The
        # tuning page and the menu bar app are both local.
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return self._json({"error": "local requests only"}, 403)
        origin = self.headers.get("Origin")
        if origin and not (origin.startswith("http://localhost:")
                           or origin.startswith("http://127.0.0.1:")):
            return self._json({"error": "cross-origin refused"}, 403)
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return self._json({"error": "bad length"}, 400)
        if n > 65536:
            return self._json({"error": "too large"}, 413)
        try:
            req = json.loads(self.rfile.read(n))
        except Exception as e:
            return self._json({"error": "bad json: %s" % e}, 400)
        path = urllib.parse.urlparse(self.path).path

        if path == "/presets":
            # Save the current look under a name, or bind a saved one to the
            # album now playing, or delete one. Everything a preset captures
            # is listed in PRESET_KEYS.
            saved = _cfg.setdefault("saved", {})
            binds = _cfg.setdefault("bindings", {})
            act = req.get("action")
            name = (req.get("name") or "").strip()[:60]

            if act == "save":
                if not name:
                    return self._json({"error": "name required"}, 400)
                src = req.get("settings") or {k: _cfg.get(k) for k in PRESET_KEYS}
                saved[name] = {k: src[k] for k in PRESET_KEYS if k in src}
            elif act == "delete":
                saved.pop(name, None)
                for k in [k for k, v in binds.items() if v == name]:
                    binds.pop(k)          # a binding to nothing is worse than none
            elif act == "bind":
                key = _album_key((current() or {}).get("track"))
                if not key:
                    return self._json({"error": "nothing playing to bind to"}, 400)
                if name:
                    if name not in saved:
                        return self._json({"error": "no such preset"}, 404)
                    binds[key] = name
                else:
                    binds.pop(key, None)  # empty name clears the binding
            else:
                return self._json({"error": "unknown action"}, 400)

            save_cfg()
            with _frame_lock:
                _frame_cache.clear()
            tr = (current() or {}).get("track")
            k = _album_key(tr)
            return self._json({"saved": sorted(saved),
                               "album": k,
                               "bound": binds.get(k) if k else None})

        if path != "/config":
            return self._json({"error": "not found"}, 404)
        changed = {}
        for k, lo, hi in (("saturate", 0.0, 4.0), ("contrast", 0.2, 3.0),
                          ("chroma", 0.0, 12.0), ("gamut", 0.0, 1.0),
                          ("neutral", 0.0, 20.0), ("black_point", 0.0, 0.5),
                          ("brightness", 0.3, 2.5),
                          ("blend", 0.0, 1.0)):
            if k in req:
                try:
                    _cfg[k] = max(lo, min(hi, float(req[k])))
                    changed[k] = _cfg[k]
                except (TypeError, ValueError):
                    pass
        if "method" in req and req["method"] in panel.METHODS:
            _cfg["method"] = req["method"]
            changed["method"] = _cfg["method"]
        if "palette" in req and req["palette"] in panel.PALETTES:
            _cfg["palette"] = req["palette"]
            changed["palette"] = _cfg["palette"]
        if "pal" in req:
            if _parse_pal(req["pal"]) is not None or not req["pal"]:
                _cfg["pal"] = req["pal"] or ""
                changed["pal"] = _cfg["pal"]
        if changed:
            with _frame_lock:
                _frame_cache.clear()      # settings changed: the frame is stale
        if "follow_any" in req:
            _cfg["follow_any"] = bool(req["follow_any"])
            changed["follow_any"] = _cfg["follow_any"]
        if "serve_lan" in req:
            # Takes effect on restart: the listening socket is already bound.
            _cfg["serve_lan"] = bool(req["serve_lan"])
            changed["serve_lan"] = _cfg["serve_lan"]
            changed["restart_required"] = True
        save_cfg()
        self._json(dict(_cfg, **changed))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Now Playing</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--fg:#fff;--dim:rgba(255,255,255,.62);--faint:rgba(255,255,255,.34);
 --acc:#e0714c}
html,body{height:100%;overflow:hidden}
body{background:#0a0a0c;color:var(--fg);
 font:400 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
 -webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
#amb,#amb2{position:fixed;inset:-18%;background-size:cover;background-position:center;
 filter:blur(76px) saturate(2.1) brightness(.62);transform:scale(1.25);
 transition:opacity 1.4s ease;will-change:opacity}
#amb2{opacity:0}
#vig{position:fixed;inset:0;pointer-events:none;
 background:radial-gradient(ellipse at 50% 34%,transparent 6%,rgba(0,0,0,.62) 78%),
 linear-gradient(to bottom,rgba(0,0,0,.34),transparent 26%,rgba(0,0,0,.72))}
main{position:relative;flex:1;display:flex;flex-direction:column;align-items:center;
 justify-content:center;gap:clamp(24px,4.4vh,56px);padding:clamp(24px,4vh,60px) 24px;
 min-height:0}
.artwrap{position:relative;flex:0 1 auto;min-height:0;display:flex;align-items:center}
#art{display:block;height:min(56vh,520px);width:min(56vh,520px);object-fit:cover;
 border-radius:14px;background:rgba(255,255,255,.05);
 box-shadow:0 2px 6px rgba(0,0,0,.42),0 26px 70px rgba(0,0,0,.62),
 0 0 0 .5px rgba(255,255,255,.13) inset;
 transition:opacity .55s ease,transform .55s cubic-bezier(.2,.7,.3,1)}
#art.swap{opacity:0;transform:scale(.965)}
.meta{position:relative;text-align:center;max-width:min(760px,88vw);
 transition:opacity .55s ease,transform .55s cubic-bezier(.2,.7,.3,1)}
.meta.swap{opacity:0;transform:translateY(9px)}
#title{font-size:clamp(25px,3.5vw,42px);font-weight:640;letter-spacing:-.022em;
 line-height:1.14;text-wrap:balance}
#artist{margin-top:11px;font-size:clamp(16px,1.7vw,21px);color:var(--dim);font-weight:450}
.sub{margin-top:18px;font-size:13.5px;color:var(--faint)}
.sub .sep{opacity:.4;margin:0 .55em}
.sub .e{font-size:10.5px;letter-spacing:.08em;opacity:.7;margin-left:.65em}
.card{max-width:520px;padding:32px;border-radius:18px;background:rgba(255,255,255,.055);
 border:.5px solid rgba(255,255,255,.12);backdrop-filter:blur(26px);text-align:left}
.card h2{font-size:20px;font-weight:620;margin-bottom:8px;letter-spacing:-.015em}
.card p{color:var(--dim);font-size:14px;line-height:1.65;margin-bottom:18px}
.card ol{margin:0 0 18px 18px;color:var(--dim);font-size:13.5px;line-height:1.95}
.card a{color:var(--acc);text-decoration:none;border-bottom:.5px solid rgba(224,113,76,.4)}
.card a:hover{border-bottom-color:var(--acc)}
.f{display:flex;flex-direction:column;gap:9px;margin-bottom:14px}
.f input{padding:11px 14px;border-radius:9px;border:.5px solid rgba(255,255,255,.16);
 background:rgba(0,0,0,.28);color:var(--fg);font-size:14px;outline:none;width:100%}
.f input:focus{border-color:rgba(255,255,255,.36);background:rgba(0,0,0,.4)}
.card button{width:100%;padding:11px;border:0;border-radius:9px;background:var(--acc);
 color:#fff;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit}
.card button:disabled{opacity:.4;cursor:not-allowed}
.err{color:#ff9b7a;font-size:13px;margin-top:11px;min-height:18px}
#bar{position:fixed;left:0;right:0;bottom:0;padding:15px 20px;display:flex;gap:11px;
 align-items:center;justify-content:center;
 background:linear-gradient(to top,rgba(0,0,0,.62),transparent);
 opacity:0;transition:opacity .3s;pointer-events:none}
body:hover #bar{opacity:1;pointer-events:auto}
#live{font-size:11.5px;color:var(--faint);display:flex;align-items:center;gap:7px;
 white-space:nowrap}
.pulse{width:6px;height:6px;border-radius:50%;background:#34d17a;flex:none;
 animation:p 2.4s infinite}
.pulse.off{background:var(--faint);animation:none}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(52,209,122,.55)}
 70%{box-shadow:0 0 0 8px rgba(52,209,122,0)}100%{box-shadow:0 0 0 0 rgba(52,209,122,0)}}
</style></head><body>
<div id="amb"></div><div id="amb2"></div><div id="vig"></div>
<main>
  <div class="artwrap" id="artwrap"><img id="art" alt=""></div>
  <div class="meta" id="meta"><div id="title">—</div><div id="artist"></div>
    <div class="sub" id="sub"></div></div>
  <div class="card" id="card" style="display:none"></div>
</main>
<div id="bar">
  <div id="live"><span class="pulse off" id="pulse"></span><span id="livetxt">…</span></div>
</div>
<script>
const $=i=>document.getElementById(i);
let cur=null, ambA=true;
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmt=s=>(s||s===0)?Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0'):null;

function setAmbient(url){
  const next=ambA?$('amb2'):$('amb'), prev=ambA?$('amb'):$('amb2');
  next.style.backgroundImage=url?'url("'+url+'")':'none';
  next.style.opacity=1; prev.style.opacity=0; ambA=!ambA;
}
function show(t){
  if(cur && t && cur.id===t.id) return;
  cur=t;
  $('card').style.display='none';
  $('artwrap').style.display=''; $('meta').style.display='';
  $('art').classList.add('swap'); $('meta').classList.add('swap');
  setTimeout(function(){
    if(t.cover) $('art').src=t.cover; else $('art').removeAttribute('src');
    setAmbient(t.cover);
    $('title').textContent=t.title||'—';
    $('artist').textContent=t.artist||'';
    // Singles and remixes usually name the album after the track, so printing
    // both just repeats the title back at you. Drop the album when it adds
    // nothing - compared loosely, since "X" vs "X - Single" is the same fact.
    const norm=x=>String(x||'').toLowerCase()
      .replace(/\s*[-–—(\[].*$/,'').replace(/[^a-z0-9]/g,'').trim();
    const b=[];
    if(t.album && norm(t.album) && norm(t.album)!==norm(t.title)) b.push(esc(t.album));
    if(t.duration) b.push(fmt(t.duration));
    $('sub').innerHTML=b.join('<span class="sep">/</span>')
      +(t.explicit?'<span class="e">EXPLICIT</span>':'');
    document.title=(t.title||'Now Playing')+(t.artist?' — '+t.artist:'');
    $('art').classList.remove('swap'); $('meta').classList.remove('swap');
  },380);
}
function card(html){
  cur=null; $('artwrap').style.display='none'; $('meta').style.display='none';
  $('card').style.display=''; $('card').innerHTML=html; setAmbient(null);
}
function setup(msg){
  card('<h2>Nothing to set up</h2>'
   +'<p>This reads the track straight from macOS, the same source the Now '
   +'Playing widget in Control Centre uses. No permission, no API key, no '
   +'account to link.</p>'
   +'<p style="margin-bottom:0">Play something and it will appear here.</p>'
   +(msg?'<div class="err">'+esc(msg)+'</div>':''));
}
const MSG={
  idle:['Nothing playing','<p>Play something on Deezer and it will appear here '
        +'within a few seconds.</p>'],
  starting:['Starting\u2026','<p>Asking macOS what is playing.</p>'],
  'not running':['Deezer isn\u2019t running','<p>Open Deezer and play something.</p>']
};
function needsAcc(d){
  // Only reachable when another app holds the macOS now-playing slot AND
  // Deezer is running: the one case where Deezer has to be read from its
  // window instead of from MediaRemote.
  card('<h2>One permission needed</h2>'
   +'<p>'+esc(d.source||'Another app')+' is holding the macOS now-playing slot, '
   +'and macOS only reports one app at a time. To keep following Deezer anyway, '
   +'Now Playing has to read Deezer\u2019s own window.</p>'
   +'<ol><li>System Settings &rsaquo; Privacy &amp; Security &rsaquo; Accessibility</li>'
   +'<li>Turn on <b>Now Playing</b> (use + and pick it from your project folder if '
   +'it is not listed)</li>'
   +'<li>Quit and reopen Now Playing from the menu bar</li></ol>'
   +'<p style="margin-bottom:0">Or just stop the other app\u2019s audio and Deezer '
   +'takes the slot back on its own \u2014 no permission required.</p>');
}
function other(d){
  // Another app holds the system audio session. macOS exposes only one at a
  // time, so Deezer is genuinely invisible right now - say so plainly rather
  // than showing a YouTube title on a panel meant for album art.
  card('<h2>'+esc(d.source||'Another app')+' has the audio session</h2>'
   +'<p>macOS reports one now-playing app at a time, so Deezer is hidden while '
   +'this one is playing.</p>'
   +(d.deezer_running?'<p>Deezer is still running \u2014 press play in it to take '
     +'the session back.</p>':'')
   +'<p style="margin-bottom:0"><label style="cursor:pointer"><input type="checkbox" '
   +'id="fa" style="margin-right:.5em"'+(FOLLOW_ANY?' checked':'')
   +'>Follow whatever is playing, not just Deezer</label></p>');
  const c=document.getElementById('fa');
  if(c) c.onchange=async()=>{
    FOLLOW_ANY=c.checked;
    await fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},
                           body:JSON.stringify({follow_any:c.checked})});
    poll();
  };
}
let FOLLOW_ANY=false;
fetch('/config').then(r=>r.json()).then(d=>{FOLLOW_ANY=!!d.follow_any}).catch(()=>{});
async function poll(){
  try{
    const d=await (await fetch('/now')).json();
    if((d.state==='playing'||d.state==='paused'||d.state==='held') && d.track){
      $('pulse').classList.toggle('off', d.state!=='playing');
      $('livetxt').textContent =
        d.state==='playing' ? 'following '+(d.source||'')
      : d.state==='paused'  ? 'paused'
      :                       'last played';
      show(d.track); return;
    }
    $('pulse').classList.add('off');
    $('livetxt').textContent=d.state;
    if(d.state==='needs_accessibility') return needsAcc(d);
    if(d.state==='other') return other(d);
    const m=MSG[d.state]||[d.state,'<p>'+esc(d.detail||'')+'</p>'];
    card('<h2>'+m[0]+'</h2>'+m[1]);
  }catch(e){$('pulse').classList.add('off'); $('livetxt').textContent='server offline'}
}
poll(); setInterval(poll,4000);
</script></body></html>"""


TUNE_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel tuning</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0c0c0f;color:#eaeaea;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:22px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:18px;font-weight:600;margin-bottom:2px}
.sub{color:#83838d;font-size:13px;margin-bottom:16px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
.cell{background:#151519;border:1px solid #25252d;border-radius:8px;padding:10px}
.cell h2{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
         color:#83838d;margin-bottom:8px}
.cell img{display:block;width:100%;border-radius:4px;background:#000;image-rendering:pixelated}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}
.box{background:#151519;border:1px solid #25252d;border-radius:8px;padding:16px}
.box>h3{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
        color:#83838d;margin-bottom:13px}
.stage{border-top:1px solid #22222a;padding-top:12px;margin-top:12px}
.stage:first-of-type{border-top:0;padding-top:0;margin-top:0}
.head{display:flex;align-items:center;gap:9px;margin-bottom:7px}
.head label{flex:1;font-size:13px;cursor:pointer}
.head .v{color:#9a9aa4;font-variant-numeric:tabular-nums;font-size:12px}
input[type=range]{width:100%;accent-color:#e0714c}
input[type=range]:disabled{opacity:.3}
input[type=checkbox]{accent-color:#e0714c;width:15px;height:15px;cursor:pointer}
select,input[type=text]{width:100%;background:#0f0f13;color:#eaeaea;border:1px solid #32323c;
       border-radius:6px;padding:7px 9px;font-size:13px}
.hint{font-size:11.5px;color:#6f6f78;margin-top:5px;line-height:1.45}
.pig{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.pig input[type=color]{width:38px;height:30px;border:1px solid #32323c;border-radius:5px;
                       background:#0f0f13;padding:2px;cursor:pointer}
.pig .nm{width:56px;font-size:12.5px;color:#b7b7c0}
.pig .hex{flex:1;background:#0f0f13;border:1px solid #32323c;border-radius:5px;
          color:#9a9aa4;font-family:ui-monospace,Menlo,monospace;font-size:12px;padding:6px 8px}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
button{border:1px solid #32323c;background:#1b1b21;color:#eaeaea;border-radius:6px;
       padding:8px 12px;font-size:13px;cursor:pointer}
button.go{background:#e0714c;border-color:#e0714c;color:#1a0d08;font-weight:600;flex:1}
button.warn{border-color:#5a2a2a;color:#d98a8a}
button:disabled{opacity:.45;cursor:default}
.state{font-size:12px;color:#83838d;min-height:17px;margin-top:9px}
.busy{opacity:.5}

/* preset bar */
.presets{background:#151519;border:1px solid #25252d;border-radius:8px;
         padding:14px 16px;margin-bottom:16px}
.prow{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.prow select{flex:1;min-width:180px;width:auto}
.prow input[type=text]{flex:1;min-width:150px;width:auto}
.bound{font-size:12px;color:#83838d;margin-top:10px;line-height:1.5}
.bound b{color:#c9c9d2;font-weight:600}
.tag{display:inline-block;background:#241a14;border:1px solid #4a3225;color:#e0a07c;
     border-radius:4px;padding:1px 7px;font-size:11px;margin-left:6px}
.dirty{color:#e0a07c}
@media(max-width:900px){.grid,.pair{grid-template-columns:1fr}}
</style></head><body><div class="wrap">
<h1>Panel tuning</h1>
<p class="sub">Left is the source. Right is what the panel will print, simulated
with whatever pigment values are set below. Same code path as the real frame.</p>

<div class="presets">
  <div class="prow">
    <select id="preset"><option value="">- saved looks -</option></select>
    <button id="p_apply">Load</button>
    <input type="text" id="p_name" placeholder="name this look" maxlength="60">
    <button id="p_save">Save</button>
    <button id="p_del" class="warn">Delete</button>
  </div>
  <div class="bound" id="bound"></div>
  <div class="prow" style="margin-top:10px">
    <button id="p_bind">Always use this look for this album</button>
    <button id="p_unbind" class="warn">Stop using it here</button>
  </div>
</div>

<div class="pair">
  <div class="cell"><h2>Source</h2><img id="src" alt="Original cover"></div>
  <div class="cell"><h2>On the panel</h2><img id="out" alt="Dithered result"></div>
</div>
<div class="grid">
  <div class="box"><h3>Pigments</h3>
    <div id="pigs"></div>
    <p class="hint">What each of the six inks actually looks like on the glass.
    These drive the matching and the preview, so getting them close to the real
    thing is what makes the preview trustworthy. The built-in values were
    estimated by eye, not instrumented.</p>
    <div class="head" style="margin-top:14px"><label for="blend">Blend</label>
      <span class="v" id="v_blend"></span></div>
    <input type="range" id="blend" min="0" max="1" step="0.05">
    <p class="hint"><b>Keep this at 0.</b> 0 is the pure primaries; 1 is a muted
    real-ink measurement. Muted values sit close to the colours already in a
    photograph, which leaves almost no error for the diffusion to move around,
    so smooth regions collapse into solid blocks of one ink. Measured over six
    covers: at 0 about 0.2% of the picture is flat, at 1.0 it is 12.5% - the
    same sixty-fold rise the eye reads as posterisation. Pure primaries sit far
    from every real colour, so there is always a residual to keep the inks
    mixing. Moving this rewrites the six values above and also feeds the
    reframe engines, which take it directly.</p>
    <div class="btns">
      <button data-preset="ideal">pure</button>
      <button data-preset="reframe">reframe 0.6</button>
      <button data-preset="muted">inky muted</button>
      <button data-preset="measured">my guess</button>
    </div>
  </div>

  <div class="box"><h3>Processing</h3>
    <div class="stage">
      <div class="head"><label for="method">Engine</label></div>
      <select id="method"></select>
      <p class="hint" id="m_hint"></p>
    </div>
    <div class="stage" data-k="brightness">
      <div class="head"><input type="checkbox" id="on_brightness">
        <label for="on_brightness">Brightness</label><span class="v" id="v_brightness"></span></div>
      <input type="range" id="brightness" min="0.5" max="2" step="0.05">
      <p class="hint">A straight multiply, separate from contrast. A reflective
      panel at 30:1 often wants lifting; upstream reframe defaults to 1.1.</p>
    </div>
    <div class="stage" data-k="contrast">
      <div class="head"><input type="checkbox" id="on_contrast">
        <label for="on_contrast">Contrast</label><span class="v" id="v_contrast"></span></div>
      <input type="range" id="contrast" min="0.4" max="2" step="0.05">
      <p class="hint">Pivots on mid grey, so the same setting behaves the same
      on a dark cover and a light one.</p>
    </div>
    <div class="stage" data-k="saturate">
      <div class="head"><input type="checkbox" id="on_saturate">
        <label for="on_saturate">Saturation</label><span class="v" id="v_saturate"></span></div>
      <input type="range" id="saturate" min="0.4" max="4" step="0.05">
      <p class="hint">Pushes colour towards the inks, and the single most
      dangerous control here. The six inks cannot reach magenta or cyan at all,
      and raising this drives those hues further outside what the panel can
      make: on a bright pink cover 77% of the picture is already unreachable,
      and at 2.2 the red channel clips on 97% of it while blue keeps climbing,
      rotating the hue. Measured, that turns an unavoidable 15% blue ink into
      23%, and the cover reads violet rather than pink. Blue-ink share is
      lowest around 1.0 and rises in both directions. Worth raising on a dark,
      low-chroma cover; check anything pink, purple or cyan before committing.</p>
    </div>
    <div class="stage" data-k="neutral">
      <div class="head"><input type="checkbox" id="on_neutral">
        <label for="on_neutral">Keep greys neutral</label><span class="v" id="v_neutral"></span></div>
      <input type="range" id="neutral" min="0.5" max="12" step="0.5">
      <p class="hint">Stops things that should be grey resolving to a colour -
      a silver helmet coming out blue, for instance. Only applies where the
      SOURCE is near-neutral, so saturated colours keep mixing freely.</p>
    </div>
    <div class="stage" data-k="black_point">
      <div class="head"><input type="checkbox" id="on_black_point">
        <label for="on_black_point">Black point</label><span class="v" id="v_black_point"></span></div>
      <input type="range" id="black_point" min="0.01" max="0.3" step="0.01">
      <p class="hint">Anything darker than this becomes solid black before
      dithering. A near-black background otherwise sits just above the darkest
      ink and error diffusion scatters bright dots across it.</p>
    </div>
    <div class="stage" data-k="chroma">
      <div class="head"><input type="checkbox" id="on_chroma">
        <label for="on_chroma">Chroma weight</label><span class="v" id="v_chroma"></span></div>
      <input type="range" id="chroma" min="0.1" max="8" step="0.1">
      <p class="hint">How much a hue error costs against a brightness error -
      on top of a bias the matcher already carries, so 1.0 is not neutral.
      Lowering it below 1 pulls green ink into covers that should have none;
      measured, one blue cover went from 0.1% green to 18%.</p>
    </div>
    <div class="stage" data-k="gamut">
      <div class="head"><input type="checkbox" id="on_gamut">
        <label for="on_gamut">Gamut ease</label><span class="v" id="v_gamut"></span></div>
      <input type="range" id="gamut" min="0.1" max="1" step="0.05">
      <p class="hint">Eases colours the panel cannot reach towards ones it can.
      Measured to do nothing across the mid-tones - it only ever acts on
      near-black and near-white.</p>
    </div>
    <div class="btns">
      <button id="apply" class="go">Apply to panel</button>
      <button id="reset">Reset all</button>
    </div>
    <div class="state" id="state"></div>
  </div>
</div>
</div>
<script>
const SL=['brightness','contrast','saturate','neutral','black_point','chroma','gamut'];
const OFF={brightness:1,contrast:1,saturate:1,neutral:0,black_point:0,chroma:1,gamut:0};
const D={method:'floyd',brightness:1,contrast:1,saturate:1,neutral:0,black_point:0,
         chroma:1,gamut:0,blend:0};
// Everything a saved look carries. Must match PRESET_KEYS on the server, or a
// look would come back missing whatever the two disagree about.
const KEYS=['method','saturate','contrast','chroma','gamut','neutral',
            'black_point','brightness','blend','palette','pal'];
const ENGINE_HINT={
  __native:'Error diffusion written here: serpentine scan, error carried in an '+
    'extended range, and a 64-level table for matching. Bayer uses a fixed '+
    'threshold grid; "nearest" does no dithering at all, as a baseline.',
  reframe:'Upstream reframe, ported as it actually runs there: the whole job '+
    'goes to PIL’s quantize(). Error is carried in 8-bit integers, the scan '+
    'is strictly left to right, and matching happens in PIL’s own space. A '+
    'genuinely separate implementation, not a variation — it shares no code '+
    'with the native one, so the difference you see is the method.',
  reframe_ordered:'Upstream’s other mode: a Bayer threshold grid sized from '+
    'the spacing between inks. No feedback loop, so it cannot drift, and the '+
    'texture is regular rather than scattered.'};
const $=i=>document.getElementById(i);
let cur={...D}, on={brightness:false,contrast:false,saturate:false,neutral:false,
                    black_point:false,chroma:false,gamut:false};
let pig=[], names=[], presets={}, saved={}, album=null, albumLabel=null,
    bound=null, engines=[], timer=null;

function hex(a){return '#'+a}
function palParam(){return pig.join(',')}
function eff(k){ return on[k] ? cur[k] : OFF[k]; }

function paint(){
  SL.forEach(k=>{ $(k).value=cur[k]; $(k).disabled=!on[k];
                  $('on_'+k).checked=on[k];
                  $('v_'+k).textContent=on[k]?(+cur[k]).toFixed(2):'off'; });
  $('method').value=cur.method;
  $('blend').value=cur.blend||0;
  $('v_blend').textContent=(+(cur.blend||0)).toFixed(2);
  $('m_hint').textContent=ENGINE_HINT[cur.method]||ENGINE_HINT.__native;
}
function drawPigs(){
  const box=$('pigs'); box.innerHTML='';
  pig.forEach((h,i)=>{
    const r=document.createElement('div'); r.className='pig';
    r.innerHTML='<span class="nm">'+(names[i]||('ink '+i))+'</span>'+
      '<input type="color" value="'+hex(h)+'">'+
      '<input class="hex" value="'+h+'" spellcheck="false">';
    const c=r.querySelector('input[type=color]'), t=r.querySelector('.hex');
    c.oninput=()=>{ pig[i]=c.value.slice(1); t.value=pig[i]; schedule(); };
    t.onchange=()=>{ const v=t.value.replace(/[^0-9a-f]/gi,'').slice(0,6);
                     if(v.length===6){ pig[i]=v.toLowerCase(); c.value=hex(pig[i]); schedule(); }
                     else t.value=pig[i]; };
    box.appendChild(r);
  });
}
function preview(){
  // Every stage the server reads has to be sent, or it silently uses its own
  // default and the slider appears to do nothing.
  const q=new URLSearchParams({method:cur.method,contrast:eff('contrast'),
    saturate:eff('saturate'),chroma:eff('chroma'),gamut:eff('gamut'),
    brightness:eff('brightness'),neutral:eff('neutral'),
    black_point:eff('black_point'),blend:cur.blend||0,
    pal:palParam(), art:'1', _:Date.now()});
  const out=$('out'); out.classList.add('busy');
  const n=new Image();
  n.onload=()=>{out.src=n.src; out.classList.remove('busy'); $('state').textContent='';};
  n.onerror=()=>{out.classList.remove('busy'); $('state').textContent='nothing to preview';};
  n.src='/tune/preview.png?'+q;
  $('src').src='/tune/source.png?_='+Date.now();
}
function schedule(){ clearTimeout(timer); timer=setTimeout(preview,170); }

function snapshot(){
  const o={pal:palParam(), blend:cur.blend||0, method:cur.method, palette:cur.palette||'ideal'};
  SL.forEach(k=>o[k]=eff(k));
  return o;
}
function drawPresets(){
  const s=$('preset'), keep=s.value;
  s.innerHTML='<option value="">- saved looks -</option>';
  Object.keys(saved).sort().forEach(n=>{
    const o=document.createElement('option'); o.value=n; o.textContent=n;
    if(n===bound) o.textContent=n+'  (used for this album)';
    s.appendChild(o);
  });
  s.value=keep;
  const b=$('bound');
  if(!album){ b.innerHTML='Nothing is playing, so there is no album to bind a look to.'; }
  else if(bound){ b.innerHTML='<b>'+albumLabel+'</b> uses <b>'+bound+'</b>'+
      '<span class="tag">bound</span><br>Everything else uses whatever is set below.'; }
  else { b.innerHTML='<b>'+albumLabel+'</b> uses the settings below, like everything else.'+
      '<br>Bind a saved look to it if this record needs its own treatment.'; }
  $('p_unbind').disabled=!bound;
  $('p_bind').disabled=!album;
}
function post(body,then){
  fetch('/presets',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify(body)})
    .then(r=>r.json()).then(d=>{
      if(d.error){ $('state').textContent=d.error; return; }
      bound=d.bound; album=d.album!==undefined?d.album:album;
      load(then);
    }).catch(e=>{ $('state').textContent=String(e); });
}
function applyPreset(n){
  const p=saved[n]; if(!p) return;
  cur.method=p.method||cur.method;
  cur.blend=p.blend||0; cur.palette=p.palette||cur.palette;
  if(p.pal) pig=p.pal.split(',');
  SL.forEach(k=>{ if(p[k]===undefined) return;
                  const isOff=Math.abs(p[k]-OFF[k])<1e-9;
                  on[k]=!isOff; cur[k]=p[k]; });
  paint(); drawPigs(); preview();
  $('state').textContent='loaded "'+n+'"';
}
SL.forEach(k=>{
  $(k).oninput=()=>{ cur[k]=+$(k).value; $('v_'+k).textContent=(+cur[k]).toFixed(2); schedule(); };
  $('on_'+k).onchange=()=>{ on[k]=$('on_'+k).checked; paint(); schedule(); };
});
$('method').onchange=()=>{ cur.method=$('method').value; paint(); schedule(); };
$('blend').oninput=()=>{
  cur.blend=+$('blend').value;
  $('v_blend').textContent=cur.blend.toFixed(2);
  // The blend means the same thing to both kinds of engine, but reaches them
  // by different routes: it rewrites the six pigments the native matcher uses,
  // and is passed straight through to the reframe engines.
  const pure=presets.ideal, muted=presets.muted;
  if(pure&&muted){
    pig=pure.map((h,i)=>{
      const a=parseInt(h,16), b=parseInt(muted[i],16), t=cur.blend;
      const mix=(sh)=>Math.round((((a>>sh)&255)*(1-t))+(((b>>sh)&255)*t));
      return [mix(16),mix(8),mix(0)].map(v=>v.toString(16).padStart(2,'0')).join('');
    });
    drawPigs();
  }
  schedule();
};
document.querySelectorAll('[data-preset]').forEach(b=>b.onclick=()=>{
  if(presets[b.dataset.preset]){ pig=[...presets[b.dataset.preset]]; drawPigs(); schedule(); }
});
$('p_apply').onclick=()=>{ const n=$('preset').value; if(n) applyPreset(n); };
$('preset').onchange=()=>{ const n=$('preset').value; if(n) $('p_name').value=n; };
$('p_save').onclick=()=>{
  const n=($('p_name').value||$('preset').value||'').trim();
  if(!n){ $('state').textContent='give the look a name first'; return; }
  post({action:'save',name:n,settings:snapshot()},()=>{
    $('preset').value=n; $('state').textContent='saved "'+n+'"'; });
};
$('p_del').onclick=()=>{
  const n=$('preset').value;
  if(!n){ $('state').textContent='pick a saved look to delete'; return; }
  post({action:'delete',name:n},()=>{ $('state').textContent='deleted "'+n+'"'; });
};
$('p_bind').onclick=()=>{
  const n=$('preset').value;
  if(!n){ $('state').textContent='pick a saved look to use for this album'; return; }
  post({action:'bind',name:n},()=>{ $('state').textContent='"'+n+'" will be used here'; });
};
$('p_unbind').onclick=()=>post({action:'bind',name:''},
  ()=>{ $('state').textContent='this album is back to the shared settings'; });

$('apply').onclick=()=>{
  const body=snapshot();
  $('apply').disabled=true; $('state').textContent='applying...';
  fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},
                   body:JSON.stringify(body)})
    .then(r=>r.json()).then(()=>{ $('state').textContent='applied - the panel picks '+
      'it up on its next poll'; $('apply').disabled=false; })
    .catch(e=>{ $('state').textContent=String(e); $('apply').disabled=false; });
};
$('reset').onclick=()=>{ cur={...D}; on={brightness:false,contrast:false,saturate:false,
  neutral:false,black_point:false,chroma:false,gamut:false};
  if(presets.ideal) pig=[...presets.ideal];
  paint(); drawPigs(); preview(); $('state').textContent='back to defaults (not applied)'; };

function load(then){
  fetch('/config').then(r=>r.json()).then(d=>{
    presets=d.presets||{}; names=d.names||[]; engines=d.engines||[];
    saved=d.saved||{}; album=d.album; albumLabel=d.album_label; bound=d.bound;
    const sel=$('method');
    if(!sel.options.length){
      (d.methods||['floyd']).forEach(m=>{
        const o=document.createElement('option'); o.value=m;
        o.textContent=engines.indexOf(m)>=0 ? m.replace(/_/g,' ')+'  (separate engine)' : m;
        sel.appendChild(o);
      });
    }
    if(!pig.length){
      pig=d.pal ? d.pal.split(',')
                : (presets[d.palette||'ideal'] || presets.ideal || []);
    }
    cur.method=d.method||cur.method; cur.palette=d.palette||'ideal';
    cur.blend=d.blend||0;
    SL.forEach(k=>{ if(d[k]===undefined) return;
      const isOff=Math.abs(d[k]-OFF[k])<1e-9;
      on[k]=!isOff; cur[k]=d[k]; });
    paint(); drawPigs(); drawPresets();
    if(then) then(); else preview();
  }).catch(e=>{ $('state').textContent='cannot reach the server: '+e; });
}
load();
// The bound album changes when the track does, so keep the header honest.
setInterval(()=>fetch('/config').then(r=>r.json()).then(d=>{
  if(d.album!==album||d.bound!==bound||
     Object.keys(d.saved||{}).length!==Object.keys(saved).length){
    saved=d.saved||{}; album=d.album; albumLabel=d.album_label; bound=d.bound;
    drawPresets();
  }
}).catch(()=>{}), 4000);
</script></body></html>
"""

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--bind", default=None,
                    help="interface to listen on. The default keeps this to "
                         "this machine; the panel needs 0.0.0.0 to reach it "
                         "over WiFi, which also exposes the current track and "
                         "the rendered frame to your local network. Defaults to "
                         "the serve_lan setting, which is off unless you turn "
                         "it on.")
    ap.add_argument("--verbose", action="store_true",
                    help="log render timings")
    ap.add_argument("--probe", action="store_true",
                    help="show what each source sees, then exit")
    a = ap.parse_args()
    load_cfg()
    if a.verbose:
        _cfg["verbose"] = True

    if a.probe:
        import mediaremote as MR
        snap = MR.poll_once()
        print("adapter    : %s" % MR._LIB)
        print("state      : %s" % snap.get("state"))
        print("active app : %s (%s)" % (snap.get("appName"), snap.get("bundleId")))
        print("track      : %s - %s" % (snap.get("artist"), snap.get("title")))
        print("registered players:")
        for pl in snap.get("players", []):
            mark = "   <- Deezer" if _is_deezer(pl.get("bundleId")) else ""
            print("   %-34s %s%s" % (pl.get("bundleId"), pl.get("name"), mark))
        st = wait_for_track(timeout=10)
        print("resolved   : %s / %s" % (st.get("state"),
                                        (st.get("track") or {}).get("title")))
        return

    bind = a.bind or ("0.0.0.0" if _cfg.get("serve_lan") else "127.0.0.1")
    srv = ThreadingHTTPServer((bind, a.port), Handler)

    # Handle SIGTERM so Cmd-Q shuts down cleanly rather than force-killing.
    def bye(*_):
        # os._exit, not srv.shutdown(): shutdown() blocks until serve_forever()
        # returns, and this handler runs on that very thread, so it deadlocks
        # and the process survives SIGTERM still holding the socket.
        os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    print("Now Playing: http://localhost:%d  (bound to %s)" % (a.port, bind))
    if bind != "127.0.0.1":
        print("panel frame : http://%s:%d/frame.bin" %
              (_lan_ip() or "this-machine", a.port))
    print("source     : macOS MediaRemote - no permission, no key, no setup")
    print("following  : %s" % ("any player" if _cfg.get("follow_any") else "Deezer"))
    source()   # start the watcher now, so the first page load has an answer
    threading.Thread(target=prerender_loop, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
