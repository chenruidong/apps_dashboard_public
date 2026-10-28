# Agent notes for apps-dashboard

- Python 3.8+ standard library only: no `match`, no `Path.is_relative_to`, no `str.removeprefix`, no
  `functools.cache`, no `zoneinfo`, no `dict | dict`, no runtime `list[int]` annotations;
  `datetime.fromisoformat` does not take a trailing `Z`.
- Discovery is read-only for everything it looks at. Never fetch, pull or touch another repo's index (keep
  `GIT_OPTIONAL_LOCKS=0`); never call `launchctl` verbs other than `list`/`print` from `discover.py`.
- The page is read-only for apps. Do not add endpoints that start, stop or restart apps over the tailnet.
  Besides its caches next to the registry, the server writes one thing: `PUT /api/layout`, which saves only
  the card order and hidden cards (`layout.json` next to the registry). Keep it that narrow.
- The server listens on loopback only; tailnet access goes through `tailscale serve --https=443` and the owner
  check in `server.py`. Do not add a LAN/0.0.0.0 mode. A request is "local" only with a loopback `Host` and none
  of the headers serve adds (`X-Forwarded-For`, `Tailscale-User-Login`, `Tailscale-Funnel-Request`): serve
  passes `Host` on as the caller sent it. Identity headers hold only behind HTTP serve routes, never behind a
  TCP forward.
- Everything from the server is set as text on the page, never as HTML.
- The registry is machine-local (`~/.config/apps-dashboard/registry.json`); test against a scratch
  `--registry`, never the real one. Tests never run real `tailscale`, `launchctl` or `git` against real repos:
  use `tests/test_discover.py`'s `FakeSystem`.
- After changing code that the LaunchAgent runs: `./apps-dashboard launchagent restart`, then
  `./apps-dashboard check apps-dashboard --wait 20`.
- Run `python3 -m unittest discover -s tests -t .` before committing.
