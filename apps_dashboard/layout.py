"""The page layout: the order of the cards and the hidden ones.

The page saves it with ``PUT /api/layout`` (the dashboard's only write; it cannot touch an app) to
``layout.json`` next to the registry::

    {"order": ["electrogui", "notes", ...], "hidden": ["port-5173"],
     "updated": "2026-10-14T18:00:00.123456Z", "by": "mymac"}

Entries are app ids.  Cards the order does not name yet (a new app) come after the ordered ones.
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

from . import registry as reg

MAX_ITEMS = 2000
MAX_SLOT = 200
MAX_BYTES = 256 * 1024  # of a PUT body


def empty():
    return {"order": [], "hidden": [], "updated": None, "by": None}


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _when(layout):
    """``updated`` as a datetime (None if absent or unreadable)."""
    text = (layout or {}).get("updated")
    if not isinstance(text, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=datetime.timezone.utc)


def _slots(value, strict):
    if not isinstance(value, list):
        if strict:
            raise ValueError("order and hidden must be lists of strings")
        return []
    out, seen = [], set()
    for slot in value:
        if not isinstance(slot, str) or not slot or len(slot) > MAX_SLOT:
            if strict:
                raise ValueError(f"bad slot {str(slot)[:40]!r}")
            continue
        if slot not in seen:
            seen.add(slot)
            out.append(slot)
    if len(out) > MAX_ITEMS:
        if strict:
            raise ValueError(f"more than {MAX_ITEMS} slots")
        out = out[:MAX_ITEMS]
    return out


def clean(data, strict=False):
    """A layout with only known fields.  ``strict`` (a PUT) raises ValueError on anything malformed;
    otherwise (a file) junk is dropped."""
    if not isinstance(data, dict):
        if strict:
            raise ValueError("expected a JSON object with order and hidden")
        return empty()
    out = {"order": _slots(data.get("order", []), strict), "hidden": _slots(data.get("hidden", []), strict),
           "updated": None, "by": None}
    if not strict:
        out["updated"] = data["updated"] if _when(data) else None
        out["by"] = data["by"] if isinstance(data.get("by"), str) else None
    return out


class Store:
    """``layout.json``, re-read when it changes; saved atomically."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp = None
        self._data = empty()

    def get(self):
        with self._lock:
            return dict(self._read())

    def _read(self):
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._stamp, self._data = None, empty()
            return self._data
        if stamp != self._stamp:
            try:
                self._data = clean(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass  # keep the last good copy
            self._stamp = stamp
        return self._data

    def _write(self, layout):
        reg.save(layout, self.path)
        self._stamp, self._data = None, layout

    def put(self, data, by=None):
        """Save the page's ``{"order", "hidden"}`` (raises ValueError if malformed); returns the saved layout."""
        layout = clean(data, strict=True)
        layout.update(updated=now_iso(), by=by)
        with self._lock:
            self._write(layout)
        return dict(layout)
