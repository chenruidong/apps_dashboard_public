"""HTTP server: the dashboard page and ``/api/apps`` (a fresh or recent scan).

It listens on loopback only.  Nothing about an app can be started, stopped or changed from the
page: everything is GET and HEAD, except ``PUT /api/layout``, which saves the card order and the hidden
cards (``layout.py``) and nothing else.  Two kinds of request are served:

* **local**: a browser on this Mac; ``Host`` is a loopback name and none of the
  headers ``tailscale serve`` adds is present (``X-Forwarded-For``, which it sets on
  every request, ``Tailscale-User-Login``, ``Tailscale-Funnel-Request``);
* **tailnet**: a device reaching this Mac through ``tailscale serve``
  (``https://<node>.ts.net/`` -> ``http://127.0.0.1:8790``).  Tailscale adds
  ``Tailscale-User-Login``, which must be this node's owner.

Anything else (a foreign ``Host`` from DNS rebinding, another tailnet user) gets 403.  Serve passes the
caller's ``Host`` on as it came, so a serve request that names a loopback host is still a tailnet one: a
tagged device or a Funnel visitor (no login) cannot pass as local by sending ``Host: localhost``.
A layout PUT must also come from this page: JSON, and an ``Origin`` (when the browser sends one, and only
one) that is this page's exactly, scheme, host and port (``null`` is refused, so the page's fetch keeps the
default ``cors`` mode).

What the server writes: the layout (on a PUT, above) and its caches next to the registry (the last scan,
the screenshots).
"""
from __future__ import annotations

import gzip
import hmac
import json
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, discover, layout, previews
from .registry import Registry

STATIC = Path(__file__).with_name("static")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# Headers only tailscale serve adds: a request with none of them came straight to loopback.
SERVE_HEADERS = ("Tailscale-User-Login", "X-Forwarded-For", "Tailscale-Funnel-Request")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".json": "application/json", ".webmanifest": "application/manifest+json"}
ROOT_FILES = {"/apple-touch-icon.png": "apple-touch-icon.png", "/apple-touch-icon-precomposed.png": "apple-touch-icon.png",
              "/favicon.ico": "favicon-32.png", "/manifest.webmanifest": "manifest.webmanifest"}
_STATIC_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
DEFAULT_PORT = 8790
API = 1  # shape of /api/apps


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _path_only(path):
    try:
        return urlsplit(path).path
    except ValueError:
        return "?"


def _origin(url):
    """``(scheme, host, port)`` of an ``Origin`` value, or of ``scheme://`` + a ``Host`` value; None for one that
    is opaque (``null``) or malformed, or that has user info, a path, a query or a fragment."""
    if not url or len(url) > 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path or parts.query or parts.fragment):
            return None
        port = parts.port if parts.port is not None else 443 if parts.scheme == "https" else 80
    except ValueError:  # a port that is not a number or out of range
        return None
    return parts.scheme, parts.hostname.lower(), port


def check_scan(data):
    """``data`` if it looks like a scan (the fields the page relies on), else raise ValueError."""
    if not isinstance(data, dict) or not isinstance(data.get("apps"), list):
        raise ValueError("not an apps-dashboard scan")
    apps = []
    for app in data["apps"]:
        if (isinstance(app, dict) and isinstance(app.get("id"), str) and isinstance(app.get("status"), str)
                and isinstance(app.get("routes", []), list)):
            apps.append(dict(app, name=str(app.get("name") or app["id"]), routes=app.get("routes") or [],
                             problems=list(app.get("problems") or []), warnings=list(app.get("warnings") or [])))
    host = data.get("host") if isinstance(data.get("host"), dict) else {}
    errors = [str(e) for e in data.get("errors") or [] if e]
    return dict(data, apps=apps, host=host, errors=errors)


class Tailnet:
    """This node's MagicDNS name and owner login, from ``tailscale status --json``.

    Read lazily and retried every 30 s until found, so a server started at boot
    before Tailscale is up still learns them.  ``owner``/``names`` given on the
    command line win.
    """

    def __init__(self, owner=None, names=()):
        self._fixed_owner = owner
        self._extra = {n.lower().rstrip(".") for n in names if n}
        self._owner = owner
        self._dns = None
        self._checked = None  # time.monotonic() may start near 0 at process start, so no 0.0 sentinel
        self._lock = threading.Lock()

    def _read(self):
        exe = discover.tailscale_exe()
        if not exe:
            print("tailnet: no tailscale CLI found; tailnet requests are refused", file=sys.stderr, flush=True)
            return None
        rc, out, err = discover.run([exe, "status", "--json"], timeout=5)
        dns, login = discover.parse_self(out) if rc == 0 else (None, None)
        if not login:
            print(f"tailnet: `{exe} status --json` failed (exit {rc}): {(err or out).strip()[:200]}",
                  file=sys.stderr, flush=True)
            return None
        return dns, login

    def refresh(self, force=False):
        with self._lock:
            if self._dns and self._owner and not force:
                return
            if not force and self._checked is not None and time.monotonic() - self._checked < 30:
                return
            self._checked = time.monotonic()
            found = self._read()
            if found:
                self._dns = found[0]
                self._owner = self._fixed_owner or found[1]

    @property
    def owner(self):
        self.refresh()
        return self._owner

    @property
    def dns(self):
        self.refresh()
        return self._dns

    @property
    def names(self):
        self.refresh()
        return self._extra | ({self._dns} if self._dns else set())


class Scanner:
    """One scan at a time; page requests can reuse the last copy while a new scan runs."""

    def __init__(self, registry, self_port=None, system=None, ttl=10.0, min_interval=2.0, cache_path=None):
        self.registry = registry
        self.self_port = self_port
        self.system = system or discover.System()
        self.ttl = ttl
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._result = None
        self._at = None
        self._running = None
        self._error = None
        self.cache_path = Path(cache_path) if cache_path else None
        if self.cache_path:
            try:
                self._result = check_scan(json.loads(self.cache_path.read_text()))
            except (OSError, ValueError):
                pass

    @property
    def pending(self):
        with self._lock:
            return self._running is not None

    def get(self, fresh=False, background=False):
        with self._lock:
            age = None if self._at is None else time.monotonic() - self._at
            if self._running is None and (age is None or age > self.ttl or (fresh and age > self.min_interval)):
                self._running = threading.Event()
                threading.Thread(target=self._work, args=(self._running,), daemon=True).start()
            done = self._running
            cached = self._result
        if done is not None and (not background or cached is None):
            done.wait()
        with self._lock:
            if self._result is None:
                raise RuntimeError(self._error or "No scan available.")
            if self._error:
                return dict(self._result, errors=self._result["errors"] + [self._error])
            return self._result

    def _work(self, done):
        try:
            try:
                data = self.registry.get()
                result = discover.scan(self.system, data, self.self_port)
                if self.registry.error:
                    result["errors"].append(self.registry.error)
                result["registry"] = discover.tilde(str(self.registry.path))
                result["version"] = __version__
                with self._lock:
                    self._result, self._error = result, None
                if self.cache_path:
                    try:
                        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = self.cache_path.with_suffix(".tmp")
                        tmp.write_text(json.dumps(result, ensure_ascii=False))
                        tmp.replace(self.cache_path)
                    except OSError:
                        pass  # the in-memory copy still works if the cache cannot be saved
            except Exception as exc:
                with self._lock:
                    self._error = f"Could not refresh this Mac's apps: {type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._at, self._running = time.monotonic(), None
            done.set()


class App:
    """What the handler serves."""

    def __init__(self, scanner, tailnet, previewer=None):
        self.scanner = scanner
        self.tailnet = tailnet
        self.previews = previewer or previews.Previews(None)  # None: no browser, no previews
        self.layout = layout.Store(scanner.registry.path.with_name("layout.json"))
        self.started = time.time()

    def csp(self):
        return CSP

    def apps(self, where, fresh=False):
        """The ``/api/apps`` payload: this Mac's scan, with screenshots and the saved layout."""
        result = self.scanner.get(fresh=fresh, background=True)
        pending = self.scanner.pending
        return dict(result, api=API, viewer=where, previews=bool(self.previews.browser),
                    apps=self.previews.annotate(result["apps"], fresh=fresh, refresh=not pending),
                    layout=self.layout.get(), pending=pending, previews_pending=self.previews.pending)

    def save_layout(self, data):
        return self.layout.put(data, by=(self.tailnet.dns or socket.gethostname()).split(".")[0].lower())


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"apps-dashboard/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet: one line per error only
            pass

        def _send(self, status, body, ctype, headers=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            if (len(body) > 1024 and ctype.startswith(("application/json", "text/"))
                    and "gzip" in self.headers.get("Accept-Encoding", "")):
                body = gzip.compress(body, compresslevel=5)
                headers = dict(headers or {}, **{"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", app.csp())
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(status, json.dumps(obj, ensure_ascii=False, separators=(",", ":")),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _where(self):
            """``"local"`` or ``"tailnet"``; raises HTTPError(403) for anything else."""
            host = (urlsplit("//" + self.headers.get("Host", "")).hostname or "").lower()
            login = self.headers.get("Tailscale-User-Login")
            via_serve = any(self.headers.get(name) is not None for name in SERVE_HEADERS)
            if host in LOOPBACK and not via_serve:  # through serve, Host is whatever the caller sent
                return "local"
            if host not in LOOPBACK and host not in app.tailnet.names:
                raise HTTPError(403, "Unknown Host header.")
            owner = app.tailnet.owner
            if not owner or login is None or not hmac.compare_digest(login.encode(), owner.encode()):
                raise HTTPError(403, "Only this tailnet node's owner may use the dashboard.")
            return "tailnet"

        def _static(self, name):
            if not _STATIC_NAME.match(name):
                raise HTTPError(404, "Not found.")
            path = STATIC / name
            if not path.is_file():
                raise HTTPError(404, "Not found.")
            ctype = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
            cache = "no-cache" if path.suffix in (".html", ".js", ".css") else "max-age=86400"
            self._send(200, path.read_bytes(), ctype, {"Cache-Control": cache})

        def _preview(self, app_id, versioned):
            """The screenshot of an app the last scan found up; the URL comes from the scan, never the request."""
            found = [a for a in app.scanner.get(background=True)["apps"] if a["id"] == app_id]
            url = previews.preview_url(found[0]) if found and app.previews.browser else None
            if not url:
                raise HTTPError(404, "No preview for this app.")
            try:
                data, ctype = app.previews.get(app_id, url, refresh=not app.scanner.pending)
            except previews.PreviewError as exc:
                raise HTTPError(503, f"No preview: {exc}")
            cache = "private, max-age=31536000, immutable" if versioned else "no-cache"
            self._send(200, data, ctype, {"Cache-Control": cache})

        def _guard(self, handler):
            try:
                handler()
            except HTTPError as exc:
                self._json({"error": exc.message}, exc.status)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:  # noqa: BLE001 - report, keep serving
                # the path without its query: a query is content, and the LaunchAgent's log keeps it
                print(f"error: {self.command} {_path_only(self.path)}: {exc!r}", file=sys.stderr, flush=True)
                try:
                    self._json({"error": f"internal error: {type(exc).__name__}"}, 500)
                except OSError:
                    pass

        def _put(self):
            """``PUT /api/layout``: the card order and hidden cards, from this page only."""
            self.close_connection = True  # an unread body must not become the next request
            where = self._where()
            if urlsplit(self.path).path != "/api/layout":
                raise HTTPError(405, "The dashboard is read-only; only the layout can be saved.")
            # No Origin at all is a client other than a browser (curl, a test); a browser's must be this page's.
            origins = self.headers.get_all("Origin") or []
            page = _origin(("https://" if where == "tailnet" else "http://") + self.headers.get("Host", ""))
            if origins and (len(origins) > 1 or page is None or _origin(origins[0]) != page):
                raise HTTPError(403, "The layout can only be saved from this page.")
            if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
                raise HTTPError(403, "The layout can only be saved from this page.")
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise HTTPError(415, "Send the layout as application/json.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise HTTPError(411, "Content-Length required.")
            if not 0 <= length <= layout.MAX_BYTES:
                raise HTTPError(413, "Layout too large.")
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                saved = app.save_layout(data)
            except (UnicodeDecodeError, ValueError) as exc:
                raise HTTPError(400, f"Bad layout: {exc}")
            self._json(saved)

        def _get(self):
            where = self._where()
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/":
                return self._static("index.html")
            if url.path in ROOT_FILES:
                return self._static(ROOT_FILES[url.path])
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            if url.path == "/healthz":
                return self._send(200, "ok\n", "text/plain; charset=utf-8", {"Cache-Control": "no-store"})
            if url.path == "/api/apps":
                return self._json(app.apps(where, fresh=query.get("fresh") == "1"))
            if url.path.startswith("/preview/"):
                return self._preview(unquote(url.path[len("/preview/"):]), "v" in query)
            raise HTTPError(404, "Not found.")

        def do_GET(self):
            self._guard(self._get)

        def do_HEAD(self):
            self._guard(self._get)

        def do_PUT(self):
            self._guard(self._put)

        def _refuse(self):
            self.close_connection = True
            self._json({"error": "The dashboard is read-only."}, 405)

        do_POST = do_DELETE = do_PATCH = _refuse

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(app, host="127.0.0.1", port=DEFAULT_PORT):
    if host not in LOOPBACK:
        raise ValueError("apps-dashboard listens on loopback only; reach it from other devices with tailscale serve.")
    return Server((host, port), make_handler(app))


def serve(port=DEFAULT_PORT, registry_path=None, owner=None, allow_host=(), open_browser=False, with_previews=True):
    registry = Registry(registry_path)
    browser = previews.find_browser() if with_previews else None
    app = App(Scanner(registry, self_port=port, cache_path=registry.path.with_name("scan-cache.json")),
              Tailnet(owner, allow_host), previews.Previews(browser, cache_dir=registry.path.parent / "previews"))
    server = make_server(app, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"apps-dashboard {__version__} on {url} (registry {registry.path})", flush=True)
    if with_previews:
        print(f"previews: {browser or 'off, no Chrome, Chromium, Edge or Brave in /Applications'}", flush=True)
    threading.Thread(target=app.tailnet.refresh, daemon=True).start()  # first tailnet lookup at once
    if open_browser:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
