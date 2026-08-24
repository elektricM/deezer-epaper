"""What macOS reports as now playing, via the private MediaRemote framework.

Gives the player's own metadata - exact title, artist, album, duration, elapsed
time and a real playback rate - with no permission prompt, whether the window is
visible or not.

Since macOS 15.4 MRMediaRemoteGetNowPlayingInfo inspects the calling binary and
returns nil to anything that is not an Apple platform binary. The code therefore
ships as a dylib that /usr/bin/perl loads, so perl is the process being
inspected. No entitlement, signing or SIP change required.

Scope: this reports what is playing on THIS Mac. Deezer exposes no now-playing
endpoint in its public API, new API registration has been closed to individuals
since October 2025, and its Last.fm bridge is batched by hours. There is no
cloud route to live playback.
"""
import json, os, subprocess, sys, threading, time

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mradapter")
_SRC = os.path.join(_DIR, "mradapter.m")
_LIB = os.path.join(_DIR, "mradapter.dylib")
_PERL = "/usr/bin/perl"

# Stale after this long with no word from the adapter at all.
STALE_AFTER = 90.0


def build(force=False):
    """Compile the adapter if it is missing or older than its source."""
    if not force and os.path.exists(_LIB) and \
            os.path.getmtime(_LIB) >= os.path.getmtime(_SRC):
        return _LIB
    subprocess.run(["clang", "-fobjc-arc", "-O2", "-dynamiclib",
                    "-framework", "Foundation", "-o", _LIB, _SRC],
                   check=True, capture_output=True, text=True)
    return _LIB


def _perl_argv(mode):
    return [_PERL, "-e",
            'use DynaLoader; DynaLoader::dl_load_file($ARGV[0],0) or die "load failed";'
            'select(undef,undef,undef,$ARGV[1]);', _LIB,
            "3600" if mode == "stream" else "8"]


def poll_once(timeout=10):
    """One-shot read. Returns a dict, possibly {'state': 'none'}."""
    build()
    env = dict(os.environ)
    env.pop("MRA_MODE", None)
    try:
        p = subprocess.run(_perl_argv("once"), capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"state": "timeout"}
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                pass
    return {"state": "none", "detail": (p.stderr or "").strip()[:200]}


class Watcher:
    """Long-running adapter, restarted if it dies. Thread-safe snapshot().

    Event-driven: the adapter registers for MediaRemote change notifications and
    emits only when the track actually changes, deduping on a stable identity.
    Not on the whole payload (elapsed time is republished every second) and not
    on ContentItemIdentifier, which is regenerated on every query.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # Signalled whenever the adapter reports anything new, so a waiting
        # HTTP request can wake the instant the track changes instead of
        # discovering it on the next poll.
        self._change = threading.Condition()
        self._seq = 0
        self._state = {"state": "starting"}
        self._at = 0.0
        self._proc = None
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            p.kill()

    def _set(self, st):
        with self._lock:
            self._state, self._at = st, time.time()
        with self._change:
            self._seq += 1
            self._change.notify_all()

    def seq(self):
        """Monotonic counter, bumped on every adapter emission."""
        with self._change:
            return self._seq

    def wait_for_change(self, since, timeout):
        """Block until the adapter reports something new, or timeout.

        Callers pass the sequence number they last saw so a change landing
        between their read and this call is not missed.
        """
        deadline = time.time() + timeout
        with self._change:
            while self._seq == since:
                left = deadline - time.time()
                if left <= 0:
                    break
                self._change.wait(left)
            return self._seq

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            try:
                build()
            except subprocess.CalledProcessError as e:
                self._set({"state": "adapter build failed",
                           "detail": (e.stderr or "")[-200:]})
                self._stop.wait(30)
                continue
            env = dict(os.environ)
            env["MRA_MODE"] = "stream"
            try:
                self._proc = subprocess.Popen(
                    _perl_argv("stream"), stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1, env=env)
            except Exception as e:
                self._set({"state": "adapter failed to start", "detail": str(e)})
                self._stop.wait(min(60, backoff)); backoff *= 2
                continue
            backoff = 1.0
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    self._set(json.loads(line))
                except ValueError:
                    pass
            if self._stop.is_set():
                break
            # The adapter exited. Restart it, but do not spin.
            self._set({"state": "adapter exited"})
            self._stop.wait(min(30, backoff))
            backoff = min(30, backoff * 2)

    def snapshot(self):
        with self._lock:
            st, at = dict(self._state), self._at
        if at and time.time() - at > STALE_AFTER and st.get("state") == "playing":
            # Adapter heartbeats every 15s; silence far beyond that means the
            # track may have ended long ago.
            st["stale_for"] = round(time.time() - at)
        return st


if __name__ == "__main__":
    if "--stream" in sys.argv:
        w = Watcher().start()
        try:
            last = None
            while True:
                s = w.snapshot()
                if s != last:
                    print(json.dumps(s)); last = s
                time.sleep(1)
        except KeyboardInterrupt:
            w.stop()
    else:
        print(json.dumps(poll_once(), indent=2))
