# apps-dashboard

One page listing every web app a Mac serves over its [Tailscale](https://tailscale.com) tailnet. For each app
it shows whether it answers (locally and over the tailnet), its links, the process, the LaunchAgent that keeps
it alive and the git checkout it runs from. The page is itself served on the tailnet, at the node's bare URL
(`https://<node>.<tailnet>.ts.net/`), and only the node's owner can open it.

Python 3.8+ standard library only, macOS. The page cannot start, stop or change any app.

This is a skeleton: the core of a dashboard I run on my own Macs, with the personal parts taken out. Fork it
and make it yours. [ElectroGUI](https://github.com/chenruidong/electrogui) is the worked example
([examples/electrogui](examples/electrogui/README.md)).

## How it works

Every app, the dashboard included, listens only on `127.0.0.1`. `tailscale serve` puts each one on the tailnet
over HTTPS at its own port. The dashboard never talks to an app's code; it reads the machine around it. Each
scan reads:

| Source | Gives |
|---|---|
| `tailscale serve status --json` | tailnet URLs (https port + path) and the backend each proxies to; Funnel |
| `lsof` (listening TCP) | the process on each backend port, its working directory |
| `ps` | command line, start time, parent chain |
| `launchctl list` / `print`, `~/Library/LaunchAgents/*.plist` | the LaunchAgent running the process: state, KeepAlive, runs, last exit, log files |
| `git` (`GIT_OPTIONAL_LOCKS=0`, never fetches) | checkout, branch@HEAD, uncommitted files, ahead/behind the upstream as of the last fetch, remote link |
| one `GET` per local and tailnet URL | status code and latency |

An **app** is one backend port. It is listed if `tailscale serve` proxies to it, if the registry names it, or
if an interpreter (python, node, …) outside an `.app` bundle listens on loopback and answers HTTP. The last
group appears under "Only on this Mac" and is marked as not registered.

Status: **up** (everything answers below 500), **degraded** (the backend answers but a tailnet URL does not, or
it is not published where the registry says), **down** (the backend does not answer), **idle** (an on-demand
app that is not running). Warnings do not change the status: uncommitted or unpushed code, a process older
than its checkout's HEAD (restart to pick it up), no LaunchAgent for a tailnet app, a listener on all
interfaces, Funnel turned on.

Command lines and log tails are shown with `--token`/`--password`/`?token=`-style values replaced by •••.

## Quick start

```bash
git clone https://github.com/chenruidong/apps_dashboard_public ~/code/apps-dashboard
cd ~/code/apps-dashboard
python3 -m unittest discover -s tests -t .
./apps-dashboard serve --open             # http://127.0.0.1:8790/, loopback only; Ctrl-C to stop
```

To keep it running and put it on the tailnet:

```bash
./apps-dashboard launchagent install      # RunAtLoad + KeepAlive, log in ~/Library/Logs/apps-dashboard.log
tailscale serve --bg --https=443 http://127.0.0.1:8790
./apps-dashboard register apps-dashboard --port 8790 --name "Apps dashboard"
./apps-dashboard check apps-dashboard --wait 20
```

The LaunchAgent's label is `local.apps-dashboard`; set `APPS_DASHBOARD_LABEL` (for example
`com.<you>.apps-dashboard`) before `launchagent install` to use another. It runs this checkout with the Python
that installed it, so reinstall after moving the checkout or changing Python. `tailscale serve --bg` settings
survive reboots; `tailscale serve --https=443 off` stops publishing.

The dashboard is owner-only: through `tailscale serve` it answers only the login that owns the node (read from
`tailscale status --json`, or `serve --owner`), and only for the node's MagicDNS name. A browser on the Mac
itself, at `127.0.0.1`, is always served.

## The registry

Discovery cannot know names, descriptions, or apps that are not running. Those live in a machine-local file,
`~/.config/apps-dashboard/registry.json` (or `$APPS_DASHBOARD_REGISTRY`), which the server re-reads when it
changes. Edit it through the CLI:

```bash
./apps-dashboard register electrogui --port 8765 --name "ElectroGUI" --description "Session explorer"
./apps-dashboard register notebook --port 8888 --on-demand    # normally stopped: idle, not down
./apps-dashboard unregister notebook
./apps-dashboard ignore 51234                                 # hide an unregistered listener
./apps-dashboard list
```

`register` matches an app by its loopback `--port`. When something is running there, it fills in `repo`,
`launchd` and `https_port` (unless `--no-detect`). `https_port` is where the app should be published; if it is
missing from `tailscale serve`, the app shows as degraded. `--repo` names the checkout when the process runs a
deployed copy elsewhere. [examples/registry.example.json](examples/registry.example.json) shows the format.

## Checking from the terminal

```bash
./apps-dashboard scan                          # what the page shows, as text (--json for the API payload)
./apps-dashboard check electrogui              # one app; exit 1 unless up (or idle, if on demand)
./apps-dashboard check --port 8765 --wait 20   # after a restart: retry until healthy, up to 20 s
```

`check` exits 0 when the app is up (or idle, for an on-demand app), 1 when anything is wrong (`✗` lines: the
backend does not answer, a tailnet URL answers 502, the app is not published where registered, the LaunchAgent
is not loaded or last exited non-zero). `!` lines are warnings; `--strict` makes them fail too.

## Adding an app

1. Pick a port: `./apps-dashboard scan` lists the ports apps already use, and
   `lsof -nP -iTCP:<port> -sTCP:LISTEN` confirms a candidate is free. Port 443 belongs to the dashboard; a
   simple convention is https port = loopback port.
2. Serve on `127.0.0.1` only, give it a LaunchAgent (RunAtLoad + KeepAlive), and publish it:
   `tailscale serve --bg --https=<port> http://127.0.0.1:<port>`. Keep Host-header and same-origin checks in
   the app; an app that needs to know the caller can read the `Tailscale-User-Login` header that serve sets.
3. `./apps-dashboard register <id> --port <port> --name "<Name>" --description "<what it is for>"`.
4. `./apps-dashboard check <id> --wait 20`.

To remove one: `tailscale serve --https=<port> off`, unload its LaunchAgent, `./apps-dashboard unregister <id>`.

The full walk-through for one real app is in [examples/electrogui](examples/electrogui/README.md).

## Keeping it accurate with an agent

The registry is only accurate if whatever creates, restarts or removes an app also updates it. If you work with
a coding agent (Claude Code, Codex), [skills/apps-dashboard/SKILL.md](skills/apps-dashboard/SKILL.md) is a
skill that tells it to register, then check, every time it touches a tailnet app. Copy it into your agent's
skills folder (for example `~/.claude/skills/apps-dashboard/`).

## The page

Cards show name, status and an **Open** button; **Diagnostics** expands the probes, process, LaunchAgent, code
and warnings. Drag a card to reorder it, **×** to hide it, **Show all apps** to bring hidden ones back; the
order and hidden cards are saved on the server (`PUT /api/layout`, the page's only write, to `layout.json`
next to the registry). **··· → Show preview** shows a screenshot: the server takes it with headless Chrome
(or Chromium, Edge, Brave; `$APPS_DASHBOARD_BROWSER` to choose), only while the page is open, at most every
10 minutes per app, and never of an app that is not answering. `serve --no-previews` turns them off. On an
iPhone, Share → Add to Home Screen opens it full screen.

## Layout of the repo

| Path | What |
|---|---|
| `apps_dashboard/discover.py` | the scan: every source above, read-only |
| `apps_dashboard/registry.py` | the registry file |
| `apps_dashboard/server.py` | the HTTP server, owner check, `/api/apps`, `/api/layout`, `/preview/<id>` |
| `apps_dashboard/previews.py`, `layout.py` | screenshots, saved card order |
| `apps_dashboard/cli.py`, `launchagent.py` | the command line and the dashboard's own LaunchAgent |
| `apps_dashboard/static/` | the page (plain JavaScript, no build step) |
| `examples/` | ElectroGUI walk-through, example registry |
| `skills/apps-dashboard/` | the agent skill |
| `tests/` | `python3 -m unittest discover -s tests -t .`; the page test also needs Node and Chrome |

## License

MIT; see [LICENSE](LICENSE).
