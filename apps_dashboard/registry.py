"""The machine-local registry: what discovery cannot know about an app.

``~/.config/apps-dashboard/registry.json`` (or ``$APPS_DASHBOARD_REGISTRY``)::

    {"version": 1,
     "apps": {"electrogui": {"name": "ElectroGUI", "port": 8778, "https_port": 8778,
                             "repo": "~/code/electrogui", "launchd": "local.electrogui",
                             "description": "...", "updated": "2026-10-12T13:30:00Z"}},
     "ignore_ports": []}

``port`` is the loopback port the app listens on; it is how a registry entry is
matched to what ``tailscale serve`` publishes and to the listening process.
Everything else is optional.  The server re-reads the file when it changes, so
``register`` takes effect on the next scan without a restart.

``load()`` keeps only the keys above and drops invalid values, so a hand edit that is wrong falls back to
the default instead of breaking the page.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path

DEFAULT = Path("~/.config/apps-dashboard/registry.json").expanduser()
FIELDS = {"name": str, "description": str, "port": int, "https_port": int, "path": str, "repo": str,
          "launchd": str, "on_demand": bool}
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def default_path():
    env = os.environ.get("APPS_DASHBOARD_REGISTRY")
    return Path(env).expanduser() if env else DEFAULT


def empty():
    return {"version": 1, "apps": {}, "ignore_ports": []}


def valid_id(app_id):
    return bool(_ID.match(app_id or ""))


def clean_entry(entry):
    """Only known fields, with the right types; unknown keys are dropped."""
    out = {}
    for key, kind in FIELDS.items():
        value = entry.get(key)
        if value is None or value == "":
            continue
        if kind is int:
            if isinstance(value, bool):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if not 0 < value < 65536:
                continue
        elif kind is bool:
            value = bool(value)
        else:
            value = str(value)
        out[key] = value
    if isinstance(entry.get("updated"), str):
        out["updated"] = entry["updated"]
    return out


def load(path=None):
    path = Path(path) if path else default_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty()
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    apps = data.get("apps") if isinstance(data.get("apps"), dict) else {}
    ports = [p for p in data.get("ignore_ports", []) if isinstance(p, int) and not isinstance(p, bool)]
    return {"version": 1,
            "apps": {k: clean_entry(v) for k, v in apps.items() if valid_id(k) and isinstance(v, dict)},
            "ignore_ports": ports}


def save(data, path=None):
    path = Path(path) if path else default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".registry-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def upsert(data, app_id, fields):
    """Merge ``fields`` (None = leave alone, "" = remove) into entry ``app_id``; returns the entry."""
    if not valid_id(app_id):
        raise ValueError(f"bad app id {app_id!r}: use lowercase letters, digits, '.', '_' or '-'")
    entry = dict(data["apps"].get(app_id, {}))
    for key, value in fields.items():
        if key not in FIELDS or value is None:
            continue
        if value == "":
            entry.pop(key, None)
        else:
            entry[key] = value
    entry = clean_entry(entry)
    entry["updated"] = now_iso()
    data["apps"][app_id] = entry
    return entry


class Registry:
    """The registry file, re-read whenever its mtime changes."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_path()
        self._stamp = None
        self._data = empty()
        self.error = None

    def get(self):
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            self._stamp, self._data, self.error = None, empty(), None
            return self._data
        if stamp != self._stamp:
            try:
                self._data, self.error = load(self.path), None
            except (OSError, ValueError) as exc:
                self.error = f"{self.path}: {exc}"  # keep the last good copy
            self._stamp = stamp
        return self._data
