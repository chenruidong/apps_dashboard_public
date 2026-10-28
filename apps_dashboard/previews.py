"""Page previews: a screenshot of each app that is up, for the thumbnail view.

Most apps refuse to be framed (``X-Frame-Options`` / ``frame-ancestors``), so the page cannot show
them in an iframe.  Instead a headless browser on this Mac (Chrome, Chromium, Edge or Brave) loads
the app's URL in a 1280x800 window and saves a screenshot, which ``sips`` shrinks to a JPEG.

* Only on demand: a capture starts when the page asks for previews (``/api/apps`` or
  ``/preview/<id>``), so nothing runs while nobody has the dashboard open.
* Only apps the last scan found up, at a URL that answered below 400; never this dashboard, never an
  idle on-demand app (the browser would only get a 502).
* At most one capture per app every ``ttl`` seconds (``retry`` after a failure), ``workers`` at a time.
* The browser loads the page as the user's browser would (no clicks, no input), with a throwaway
  profile in a temporary folder that is removed afterwards.

The page first gets ``BUDGET_MS`` of virtual time to fetch and draw its data.  A page that keeps a
request open (a live stream) never lets virtual time run out; after ``SETTLE_TIMEOUT`` it is taken
again at its load event instead, and from then on always that way.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

BROWSERS = ("Google Chrome.app/Contents/MacOS/Google Chrome",
            "Chromium.app/Contents/MacOS/Chromium",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Brave Browser.app/Contents/MacOS/Brave Browser")
WINDOW = (1280, 800)
WIDTH = 800            # of the JPEG
TTL = 600.0            # seconds a preview is current
RETRY = 120.0          # seconds before a failed capture is tried again
FRESH = 30.0           # Refresh recaptures previews older than this
BUDGET_MS = 5000       # virtual time the page gets to settle before the screenshot
SETTLE_TIMEOUT = 12.0  # seconds for a capture that lets the page settle
LOAD_MS = 8000         # otherwise: the screenshot at the load event, or after this long
TIMEOUT = 30.0         # seconds for a capture that does not wait to settle


class PreviewError(Exception):
    pass


class PreviewTimeout(PreviewError):
    pass


def find_browser():
    """The headless-capable browser to use, or None.  ``$APPS_DASHBOARD_BROWSER`` wins."""
    chosen = os.environ.get("APPS_DASHBOARD_BROWSER")
    if chosen:
        return chosen if os.access(chosen, os.X_OK) else None
    for root in ("/Applications", os.path.expanduser("~/Applications")):
        for rel in BROWSERS:
            path = os.path.join(root, rel)
            if os.access(path, os.X_OK):
                return path
    return None


def preview_url(app):
    """The URL to take ``app``'s preview of, or None: the first tailnet URL that answered below 400,
    else its loopback URL.  None for this dashboard and for anything not up."""
    if app.get("self") or app.get("status") not in ("up", "degraded"):
        return None

    def good(probe):
        return bool(probe) and probe.get("code") is not None and probe["code"] < 400

    for route in app.get("routes") or ():
        if route.get("url") and not route.get("stale") and good(route.get("probe")):
            return route["url"]
    local = app.get("local")
    if local and local.get("url") and good(local.get("probe")):
        return local["url"]
    return None


def _run(cmd, timeout):
    """Run ``cmd`` in its own process group; on timeout the whole group (browser helpers too) is killed."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        _, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise PreviewTimeout(f"no screenshot in {timeout:g} s")
    return proc.returncode, err.decode("utf-8", "replace")


def capture(browser, url, settle=True):
    """``(bytes, content type)`` of a screenshot of ``url``: a JPEG, or the PNG if ``sips`` fails.
    ``settle``: give the page virtual time to draw its data first (raises PreviewTimeout if it never settles)."""
    timeout = SETTLE_TIMEOUT if settle else TIMEOUT
    tmp = tempfile.mkdtemp(prefix="apps-dashboard-preview-")
    try:
        png = os.path.join(tmp, "page.png")
        cmd = [browser, "--headless", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
               "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
               "--disable-background-networking", "--use-mock-keychain", "--password-store=basic",
               f"--user-data-dir={os.path.join(tmp, 'profile')}", f"--window-size={WINDOW[0]},{WINDOW[1]}",
               f"--virtual-time-budget={BUDGET_MS}" if settle else f"--timeout={LOAD_MS}", f"--screenshot={png}", url]
        try:
            rc, err = _run(cmd, timeout)
        except OSError as exc:
            raise PreviewError(f"cannot run {os.path.basename(browser)}: {exc}")
        if not os.path.isfile(png) or not os.path.getsize(png):
            lines = [line for line in err.splitlines() if line.strip()]
            raise PreviewError(f"no screenshot (exit {rc}){': ' + lines[-1][-200:] if lines else ''}")
        jpg = os.path.join(tmp, "page.jpg")
        sips = shutil.which("sips") or "/usr/bin/sips"
        try:
            subprocess.run([sips, "-s", "format", "jpeg", "-s", "formatOptions", "75", "--resampleWidth", str(WIDTH),
                            png, "--out", jpg], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError):
            pass
        if os.path.isfile(jpg) and os.path.getsize(jpg):
            with open(jpg, "rb") as handle:
                return handle.read(), "image/jpeg"
        with open(png, "rb") as handle:
            return handle.read(), "image/png"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Previews:
    """The latest preview of each app, captured in the background.  ``browser=None`` turns previews off."""

    def __init__(self, browser, capture=capture, ttl=TTL, retry=RETRY, workers=3, clock=time.time, cache_dir=None):
        self.browser = browser
        self._capture = capture
        self.ttl = ttl
        self.retry = retry
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = {}  # app id -> {"url", "at", "data", "ctype", "error", "tried", "settle"}
        self._running = {}  # app id -> threading.Event, set when its capture ends
        self._fresh = set()  # Refresh requests deferred until the scan confirms the app is still up
        self._slots = threading.BoundedSemaphore(workers)
        self.cache_dir = Path(cache_dir) if cache_dir else None

    @property
    def pending(self):
        with self._lock:
            return bool(self._running)

    def _path(self, app_id, url):
        key = hashlib.sha256((app_id + "\n" + url).encode()).hexdigest()
        return self.cache_dir / (key + ".json")

    def _entry(self, app_id, url):
        """Called under the lock; restore a preview after a restart, keyed by app and exact URL."""
        if app_id not in self._entries and self.cache_dir:
            try:
                entry = json.loads(self._path(app_id, url).read_text())
                if (entry["url"] == url and isinstance(entry["at"], (int, float))
                        and entry["ctype"] in ("image/jpeg", "image/png")):
                    entry["data"] = base64.b64decode(entry["data"], validate=True)
                    entry.update(error=None, tried=entry["at"], settle=bool(entry.get("settle", True)))
                    self._entries[app_id] = entry
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return self._entries.get(app_id)

    def _save(self, app_id, entry):
        if not self.cache_dir or entry["error"]:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._path(app_id, entry["url"])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(dict(entry, data=base64.b64encode(entry["data"]).decode("ascii"))))
            tmp.replace(path)
        except OSError:
            pass

    def _due(self, entry, url, now, fresh):
        if entry is None or entry["url"] != url:
            return True
        if entry["error"]:  # the last capture failed (an older screenshot may still be shown)
            return now - entry["tried"] > self.retry
        return now - entry["at"] > (FRESH if fresh else self.ttl)

    def _start(self, app_id, url):
        with self._lock:
            done = self._running.get(app_id)
            if done is not None:
                return done
            done = self._running[app_id] = threading.Event()
        threading.Thread(target=self._work, args=(app_id, url, done), daemon=True).start()
        return done

    def _work(self, app_id, url, done):
        entry = None
        try:
            with self._slots:
                tried = self._clock()
                with self._lock:
                    old = self._entry(app_id, url)
                if old is not None and old["url"] != url:
                    old = None
                settle = old["settle"] if old is not None else True
                try:
                    try:
                        data, ctype = self._capture(self.browser, url, settle)
                    except PreviewTimeout:
                        if not settle:
                            raise
                        settle = False  # a live page: take it at its load event, now and from now on
                        data, ctype = self._capture(self.browser, url, settle)
                    entry = {"url": url, "at": self._clock(), "data": data, "ctype": ctype, "error": None,
                             "tried": tried, "settle": settle}
                except Exception as exc:  # noqa: BLE001 - a failed capture is reported on the card
                    message = str(exc) if isinstance(exc, PreviewError) else f"{type(exc).__name__}: {exc}"
                    entry = dict(old) if old is not None else {"url": url, "at": None, "data": None, "ctype": None}
                    entry.update(error=message, tried=tried, settle=settle)
                self._save(app_id, entry)
        finally:
            with self._lock:
                if entry is not None:
                    self._entries[app_id] = entry
                self._running.pop(app_id, None)
            done.set()

    def annotate(self, apps, fresh=False, refresh=True):
        """Copies of ``apps``, each with ``preview``: None, or ``{"src", "error"}``.  Starts the captures that are due."""
        out = []
        now = self._clock()
        for app in apps:
            url = preview_url(app) if self.browser else None
            preview = None
            if url:
                with self._lock:
                    entry = self._entry(app["id"], url)
                    if fresh:
                        self._fresh.add(app["id"])
                    requested = app["id"] in self._fresh
                    if refresh:
                        self._fresh.discard(app["id"])
                if refresh and self._due(entry, url, now, requested):
                    self._start(app["id"], url)
                src = f"/preview/{quote(app['id'], safe='')}"
                current = entry is not None and entry["url"] == url
                if current and entry["data"] is not None:
                    src += f"?v={int(entry['at'] * 1000)}"
                preview = {"src": src, "error": entry["error"] if current and entry["data"] is None else None}
            out.append(dict(app, preview=preview))
        return out

    def get(self, app_id, url, refresh=True):
        """``(bytes, content type)`` of ``url``'s latest preview, waiting for a capture if there is none yet."""
        with self._lock:
            entry = self._entry(app_id, url)
        if refresh and self._due(entry, url, self._clock(), False):
            done = self._start(app_id, url)
            if entry is None or entry["url"] != url or entry["data"] is None:
                done.wait(SETTLE_TIMEOUT + TIMEOUT + 60)  # a slot, then up to two tries
                with self._lock:
                    entry = self._entries.get(app_id)
        if entry is None or entry["url"] != url or entry["data"] is None:
            raise PreviewError(entry["error"] if entry is not None and entry["url"] == url and entry["error"]
                               else "no preview yet")
        return entry["data"], entry["ctype"]
