# Dashboard hub

`dashboard_hub.py` is the central point between the Desert Bus dashboards and the MiSTer.
It is the only client of the MiSTer bridge: it polls the game once and serves any number of
dashboards from that, forwards their writes in order, detects game events, calls webhooks,
accepts webhook actions for every dashboard button, runs the autopilot, logs to InfluxDB, and
reconnects by itself.

```
 dashboard.html ─┐                                       ┌──▶ MiSTer bridge :8765 (one keep-alive connection)
 dashboard.html ─┼──▶  dashboard_hub.py :8766  ──────────┤
 automation     ─┘     (on the Mac)                      ├──▶ outgoing webhooks (game events)
 (incoming webhooks)                                     └──▶ InfluxDB (telemetry, events, hub health)
```

Python 3.8+, standard library only, nothing to install. **Run it on the Mac, not on the
MiSTer**: the point is to keep load off the MiSTer.

Contents: [Quick start](#quick-start) · [Features](#features) ·
[Dashboard through the hub](#the-dashboard-through-the-hub) · [Telemetry](#telemetry) ·
[Game events and outgoing webhooks](#game-events-and-outgoing-webhooks) ·
[Actions (incoming webhooks)](#actions-incoming-webhooks) · [Autopilot](#autopilot) ·
[Automation examples](#automation-examples) · [Endpoint reference](#endpoint-reference) ·
[Protecting the MiSTer](#how-the-hub-protects-the-mister) · [InfluxDB](#influxdb) ·
[Options](#command-line-options) · [Running permanently](#running-it-permanently-macos) ·
[Troubleshooting](#troubleshooting) · [Tests](#tests)

## Quick start

```bash
python3 dashboard_hub.py --mister http://mister.lan:8765 \
    --dashboard ../../../Genesis-Plus-GX/sdl/dashboard.html
```

or, from the install package, `mister-package/hub/run-hub.sh http://mister.lan:8765`.

1. Open `http://localhost:8766/`: the hub serves the dashboard page.
2. Connect the page to `http://<mac-ip>:8766` (the page remembers it). Other computers use
   the same address.
3. Point **every** dashboard and script at the hub, not at the MiSTer.

Check it: `curl -s http://localhost:8766/hub` shows `"connected": true`.

## Features

| Feature | What it gives you | Section |
|---|---|---|
| One client for the MiSTer | any number of dashboards; the MiSTer sees ~30-40 requests/s in total | [Protecting the MiSTer](#how-the-hub-protects-the-mister) |
| Decoded telemetry | speed, position, clock, distance, odometer, points, day phase, **next bus stop + ETA**, **driver name** | [Telemetry](#telemetry) |
| Game events | `bus_stop`, `bus_stop_missed`, `crash`, `point`, `bug_splat` | [Events](#game-events-and-outgoing-webhooks) |
| Outgoing webhooks | each event POSTed as JSON to URLs you choose, with retries | [Events](#game-events-and-outgoing-webhooks) |
| Actions (incoming webhooks) | every dashboard button as an HTTP endpoint: pause, autopilot, savestates, all 9 patches | [Actions](#actions-incoming-webhooks) |
| Autopilot | runs in the hub, shared by all dashboards and webhooks | [Autopilot](#autopilot) |
| InfluxDB | telemetry every second, events, hub health; buffered while InfluxDB is down | [InfluxDB](#influxdb) |
| Robustness | reconnects with backoff, releases the pad on connect and shutdown | [Protecting the MiSTer](#how-the-hub-protects-the-mister) |

## The dashboard through the hub

The hub answers the bridge's API (`DASHBOARD-API.md`), so `dashboard.html` works unchanged.
When the page sees the hub (its `/capabilities` contains a `hub` object) it also:

- shows the **Events** card (polls `/hub/events` every second; new events are highlighted);
- switches the **hub's** autopilot with its Autopilot button and shows the hub's autopilot
  status, so a webhook that turns the autopilot on is visible in every open page;
- shows **Next Bus Stop** and **Driver** like on any server (it reads those itself).

Pause, savestates and patch buttons go through the hub's normal write path.

## Telemetry

`GET /hub/telemetry` returns the latest decoded sample (refreshed every `--interval`, 0.25 s):

```json
{"ok": true, "time": 1790850482.14, "telemetry": {
  "in_game": true, "game_state": 3, "speed_raw": 24576, "speed_mph": 45.0,
  "lateral_raw": 30080, "lateral_norm": 0.1319, "offroad": false,
  "clock_hour": 7, "clock_minute": 49, "distance_raw": 18301, "leg_miles": 10.1672,
  "odometer_miles": 121.0, "points": 0, "palette": 2149202, "daynight_parity": 0,
  "daynight_fixed": false, "phase": "day", "return_leg": false, "driver_name": "JOCKO",
  "next_stop_raw": 99200, "next_stop_miles": 44.944, "next_stop_eta_s": 3596, "stop_mode": "table",
  "stop_active": false, "stop_visible": false, "stop_progress": 140,
  "splat_visible": false, "splat_x": 360}}
```

| Field | Meaning (Main 68K work RAM) |
|---|---|
| `in_game` | Desert Bus is running (outside it, the other fields are meaningless) |
| `game_state` | `$FF7002`: 0 interlude/menus, 1 off-road stall, 2 waiting for tow, **3 driving**, 4 towed, 5 tow scene, 6-9 arrival |
| `speed_raw`, `speed_mph` | `$FF6FEA`; mph = raw / 0x6000 × 45 (A = gas, B = brake; the bus coasts to 0 without gas) |
| `lateral_raw`, `lateral_norm`, `offroad` | `$FF6FFA`; −1 = left shoulder … +1 = right shoulder; off road outside 0x2400–0xB400 |
| `clock_hour`, `clock_minute` | in-game 12-hour clock (`$FF70E4`) |
| `distance_raw`, `leg_miles` | `$FF6FDC`, 1800 units per mile; resets every leg (360 mi = 648000) |
| `odometer_miles`, `points` | odometer wheel (`$FF70EA`); completed legs derived from it |
| `phase`, `palette`, `daynight_parity`, `daynight_fixed` | day/night: `day`, `night`, `dawn`, `dusk`, `twilight` |
| `return_leg` | `$FF6FE4` ≠ 0: Las Vegas → Tucson |
| `driver_name` | `$FF7108`, 8 letters (A = 1); the game's default is `JOCKO` |
| `next_stop_raw`, `next_stop_miles`, `next_stop_eta_s`, `stop_mode` | the next bus stop on this leg and the time to it at the current speed (`null` when stopped or when no stop is left). `stop_mode` is `table` (six stops at 0.9, 55.1, 125.3, 166.2, 233.8, 295.1 mi, table `$FFBA2E`) or `every N mi` when the Bus Stop Every Mile patch is on |
| `stop_active`, `stop_visible`, `stop_progress` | the bus stop sign object (`$FF15FA`, progress `$FF164E`, 6 → 140). The bus is **at the stop** at progress ~55–85 |
| `splat_visible`, `splat_x` | windshield bug splat (latch `$FF7104`, object slot 10) |

## Game events and outgoing webhooks

The hub watches the telemetry and raises these events. All were verified live on the MiSTer.

| Event | Fires when | `details` |
|---|---|---|
| `bus_stop` | the bus stops at a bus stop **and then drives off again**. "At the stop" = the sign is next to the bus (progress `--stop-min-progress`..`--stop-max-progress`, 55..85). Fires on the drive-off; never fires if the bus crashes or is towed while standing there. Opening the doors (C) is optional. | `leg_miles`, `stopped_s` |
| `bus_stop_missed` | a bus stop passes without such a stop (also: stopped too early, sign still far away) | `leg_miles` |
| `crash` | driving ends in an off-road stall (state 1/2, `cause`: `off the road`) or in a tow after standing still ~30 s (state 4, `cause`: `stood still too long`) | `leg_miles`, `state`, `cause`, `lateral_raw` |
| `point` | the leg is complete: the distance reaches 360 mi | `points`, `odometer_miles`, `leg` (`outbound`/`return`) |
| `bug_splat` | the windshield bug splat appears while driving | `x`, `leg_miles` |

Events are kept for `GET /hub/events?since=ID` (last 500), shown in the dashboard's Events
card, written to InfluxDB (`desertbus_event`), and POSTed to webhooks:

```bash
python3 dashboard_hub.py --mister http://mister.lan:8765 \
    --webhook "bus_stop=http://homeassistant.lan:8123/api/webhook/desertbus-stop" \
    --webhook "crash=http://homeassistant.lan:8123/api/webhook/desertbus-crash" \
    --webhook "*=http://localhost:9000/all-events" \
    --webhook-header "Authorization: Bearer secret"
```

- `--webhook EVENT=URL` is repeatable; `*` matches every event; one event can go to several URLs.
- Delivery is asynchronous (the game loop never waits), with `--webhook-retries` (3) retries
  after 1, 2, 4 s and `--webhook-timeout` (5 s) per attempt. Failures are logged.
- `--webhook-header` adds headers to every webhook request (e.g. a token).

Payload (`POST`, `Content-Type: application/json`):

```json
{"id": 2, "event": "bus_stop", "unix": 1790848148.933, "time": "2026-10-01T11:49:08+0200",
 "details": {"leg_miles": 0.89, "stopped_s": 12.0},
 "telemetry": {"leg_miles": 0.95, "odometer_miles": 110.0, "points": 0, "speed_mph": 3.1,
               "clock_hour": 7, "clock_minute": 41, "driver_name": "JOCKO", "return_leg": false}}
```

`id` increases by one per event, so receivers can detect duplicates or gaps.

## Actions (incoming webhooks)

Every button of the dashboard is an HTTP endpoint on the hub. Call it from Home Assistant,
a Stream Deck, cron or `curl`. All actions are **`POST`**; a JSON body is optional and the
URL forms need none.

| Dashboard button | Action |
|---|---|
| ⏸ Pause / ▶ Resume | `POST /hub/action/pause`, `POST /hub/action/resume`, `POST /hub/action/toggle-pause` |
| 🚌 Autopilot | `POST /hub/action/autopilot/on` · `/off` · `/toggle` (or body `{"state": "on"}`) |
| 💾 Save State | `POST /hub/action/save-state` with `{"path": "name.gp0"}` (default `<leg miles>.gp0`) |
| 🤖 Load Fullauto | `POST /hub/action/load-fullauto` (stops the autopilot and releases the pad first) |
| Patch buttons | `POST /hub/action/patch/<id>/<state>` (or body `{"id": "<id>", "state": "<state>"}`) |

```bash
curl -X POST http://mac.lan:8766/hub/action/patch/steering/off   # Steering Drift: None
curl -X POST http://mac.lan:8766/hub/action/autopilot/toggle
curl -X POST http://mac.lan:8766/hub/action/toggle-pause
```

Answers: `200 {"ok": true, ...}` (e.g. `{"ok": true, "patch": "steering", "state": "off",
"label": "None"}`), or an error object `{"ok": false, "error": {"code": ..., "message": ...}}`
with `400` (bad body), `404` (unknown action, patch or state), `405` (not a POST),
`501 feature_unavailable` (the server lacks it, e.g. savestates on the MiSTer),
`503 upstream_unavailable` (MiSTer not reachable) or the bridge's own status.

### Patches

| id | Dashboard name | states (`<state>`: label) | What it does |
|---|---|---|---|
| `steering` | Steering Drift | `off`: None, `l1`/`l2`/`l3`: Left 1-3, `r1`/`r2`/`r3`: Right 1-3 | per-frame drift of the bus (`$FF842C`). `r1` is the game's stock drift; `off` drives straight |
| `throttle` | Full Throttle | `off`: Original, `normal`: Normal (0x6000), `max`: Max (0xFFFF) | holds the speed without pressing A (`$FF8498`); `max` also fixes the sway table (`$FFC004`) |
| `maxspeed` | Uncap Max Speed | `off`: Remove, `on`: Apply | raises the speed cap from 0x6000 to 0xFFFF |
| `fasttime` | Accelerate Time | `off`, `on` | one in-game minute per frame |
| `busstopleft` | Bus Stops on Left | `off`, `on` | signs pass on the left shoulder |
| `busstopmile` | Bus Stop Every Mile | `off`, `on` | a bus stop every mile instead of six per leg |
| `daynight` | Fix Day/Night Cycle | `off`, `on` | correct dawn/dusk schedule |
| `bugsplat` | Windshield Bug Splat | `off`: Remove, `on`: Splat! | triggers (or removes) the bug splat |
| `freshener` | Air Freshener Art | `off`: Stock Tree, `on`: FRESHY | swaps the air freshener sprite (VRAM) |

- `GET /hub/actions` lists all actions and every patch with its states.
- `GET /hub/patches` reads the current state of each patch from the game:
  `{"ok": true, "patches": {"steering": "off", "throttle": "max", ..., "freshener": "mixed"}}`
  (`mixed` = memory matches none of the states).
- The hub reads the patch definitions from the dashboard itself (the JSON block
  `<script id="patchData">` in `dashboard.html`, file given by `--dashboard` or `--patches`),
  so the hub and the page always offer the same patches. A patch writes exactly what the
  page's button writes: patches with several memory sites are written while paused (and
  resumed if the hub did the pausing); the air freshener is a VRAM write followed by the VDP
  cache refresh.

**Tip:** apply `steering/off` at the start of a session if you want the bus to stay on the
road without anyone steering.

## Autopilot

`POST /hub/action/autopilot/on` (or the dashboard's Autopilot button when connected through
the hub) starts the hub's autopilot. It uses the dashboard's steering model: the bus drifts
to a random limit (30–55 % of the half-road), then single proportional taps (70–300 ms) ease
it to a random spot (10–35 %) on the other side; one decision every 220 ms.

- Only one autopilot runs, whoever switches it. `/hub/events` reports it as
  `"autopilot": {"on": true, "status": "easing left toward -26% (at +37%)"}`.
- When the game leaves the driving state (stall, tow, arrival) it **releases the pad and
  waits** for driving to resume. It does not tap START like the standalone page does:
  on the MiSTer, START on the timecard after a tow quits Desert Bus, and further presses loop
  the game-selection menu.
- It needs no gas control: hold A or apply the `throttle` patch.
- Off (`/autopilot/off`, Load Fullauto, hub shutdown) releases left/right.

## Automation examples

**Home Assistant**: react to events, and drive the game from scripts or dashboards.

```yaml
# configuration.yaml
rest_command:
  desertbus_action:                       # service: rest_command.desertbus_action
    url: "http://mac.lan:8766/hub/action/{{ action }}"
    method: post

automation:
  - alias: "Desert Bus: bus stop served"
    trigger:
      - platform: webhook
        webhook_id: desertbus-stop        # hub: --webhook bus_stop=http://ha:8123/api/webhook/desertbus-stop
        allowed_methods: [POST]
        local_only: true
    action:
      - service: notify.mobile_app_phone
        data:
          message: "Bus stop served at mile {{ trigger.json.details.leg_miles }} ({{ trigger.json.details.stopped_s }} s)"
  - alias: "Desert Bus: crash lights"
    trigger:
      - platform: webhook
        webhook_id: desertbus-crash
        allowed_methods: [POST]
        local_only: true
    action:
      - service: light.turn_on
        target: { entity_id: light.studio }
        data: { color_name: red, flash: short }
```

Call an action: `service: rest_command.desertbus_action` with `data: {action: "autopilot/toggle"}`
or `{action: "patch/bugsplat/on"}`.

**Shell**: a webhook receiver for testing (prints every event):

```bash
python3 -c 'import http.server as h,json
class H(h.BaseHTTPRequestHandler):
  def do_POST(s):
    print(json.loads(s.rfile.read(int(s.headers["Content-Length"])))); s.send_response(200); s.end_headers()
h.HTTPServer(("",9000),H).serve_forever()'
# hub: --webhook "*=http://localhost:9000/"
```

## Endpoint reference

Hub endpoints (all JSON, CORS enabled):

| Method, path | Purpose |
|---|---|
| `GET /` | the dashboard page (with `--dashboard`) |
| `GET /hub` | hub health: MiSTer connection, upstream latency, request counts, cache hits, InfluxDB |
| `GET /hub/telemetry` | latest decoded telemetry ([Telemetry](#telemetry)) |
| `GET /hub/events?since=ID` | events newer than `ID`, `last_id`, event types, autopilot state |
| `GET /hub/actions` | available actions and patches |
| `GET /hub/patches` | current state of every patch |
| `POST /hub/action/...` | actions ([Actions](#actions-incoming-webhooks)) |

Bridge API, passed through (`DASHBOARD-API.md`): `GET /capabilities` (plus a `hub` object),
`GET|POST /status`, `POST /bus-peek`, `/bus-poke`, `/peek`, `/poke`, `/input`, `/pause`,
`/resume`, `/state/save`, `/state/load`.

`GET /hub` example:

```json
{"ok": true, "server": "dashboard-hub", "version": "1.0.0",
 "upstream": {"url": "http://mister.lan:8765", "connected": true, "epoch": 2, "down_for_s": null,
              "requests": 7758, "disconnects": 0, "latency_ms_p50": 7.46, "latency_ms_max": 18.24},
 "clients": 1, "requests": {"requests": 9996, "cache_hits": 9401, "forwarded": 413, "writes": 4},
 "demand_ranges": 13, "influx": null}
```

There is **no authentication** (same as the bridge): keep the hub on a trusted LAN.

## How the hub protects the MiSTer

| Concern | Behaviour |
|---|---|
| Request load | one connection; one worker sends requests in priority order (writes and input → polling → client reads that missed the cache) and never faster than `--max-rate` (60/s). Measured: 4 dashboards making 50 000 reads in 5 min cost ~38 requests/s on the MiSTer, 99.9 % answered from the cache |
| Telemetry | read every `--interval` (0.25 s, 7 reads + `/status`); bus stop table, patch signature and driver name every `--slow-interval` (2 s). Dashboard reads of these are answered from the cache (at most `--max-age`, 2 × interval, old) |
| Other reads | any other range a client reads (patch badges, the air freshener) is forwarded once, then polled every `--demand-interval` (0.5 s; VRAM `--vram-interval` 1 s) while clients keep asking (`--demand-ttl` 10 s); concurrent misses share one read |
| Writes | forwarded one at a time, cache invalidated before and after: the next read sees the write |
| Errors | bridge answers (e.g. `400 invalid_range`, `501 feature_unavailable`) pass through unchanged |
| Disconnects | any failure marks the MiSTer down; clients get `503 upstream_unavailable` and the hub retries with backoff (0.5 → 10 s). A bridge restart costs one ~50 ms reconnect. After a core reload (new epoch) the cache and event state are cleared |
| Buttons | all buttons are released when the hub connects, after every reconnect, and at shutdown (SIGINT/SIGTERM); `--no-release` turns this off |

## InfluxDB

```bash
export INFLUX_TOKEN=...                   # or --influx-token-file ~/.influx-token
python3 dashboard_hub.py --mister http://mister.lan:8765 \
    --influx-url http://localhost:8086 --influx-org home --influx-bucket desertbus
```

InfluxDB 1.x: `--influx-url http://localhost:8086 --influx-db desertbus [--influx-user u --influx-password p]`.
Points are buffered (up to `--influx-buffer`, 200 000) while InfluxDB is down and written
when it is back; batches InfluxDB rejects as malformed are dropped and logged.

| Measurement | Written | Content |
|---|---|---|
| `desertbus` | every `--influx-interval` (1 s) | all [telemetry](#telemetry) fields plus `paused`, `frame`, `held` (only `in_game`, `game_state`, `paused`, `frame`, `held` while Desert Bus is not running) |
| `desertbus_event` | per event | tag `event`, fields `count` = 1, `details` (JSON) |
| `dashboard_hub` | every `--influx-interval` | `connected`, `core_present`, `upstream_rps`, `upstream_latency_ms_p50`, `upstream_latency_ms_max`, `disconnects`, `clients`, `client_requests`, `cache_hits`, `forwarded`, `demand_ranges`, `influx_buffered` |

All carry the tag `host` (`--influx-tag-host`, default the MiSTer's host name).

```flux
from(bucket: "desertbus")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "desertbus" and r._field == "speed_mph")
```

Grafana annotations from events: query `desertbus_event` and use the `event` tag as text.

## Command-line options

| Option | Default | Meaning |
|---|---|---|
| `--mister URL` | `http://mister.lan:8765` (or `HUB_MISTER`) | the MiSTer bridge |
| `--listen ADDR:PORT` | `0.0.0.0:8766` | where dashboards and automation connect |
| `--dashboard FILE` | none | serve this `dashboard.html` at `/`; its patch table drives the patch actions |
| `--patches FILE` | `--dashboard` | patch definitions (`dashboard.html` or a JSON list) |
| `--interval S` | 0.25 | telemetry poll interval |
| `--max-age S` | 2 × interval | oldest cached telemetry served to clients |
| `--slow-interval S` | 2 | bus stop table, patch signature, driver name |
| `--demand-interval S` | 0.5 | other ranges clients read |
| `--vram-interval S` | 1.0 | VRAM ranges clients read |
| `--demand-ttl S` | 10 | stop watching a range this long after its last read |
| `--max-rate N` | 60 | requests per second to the MiSTer, at most |
| `--timeout S` | 3 | per request to the MiSTer |
| `--no-release` | off | do not release buttons on connect/shutdown |
| `--webhook EVENT=URL` | none | outgoing webhook (repeatable; `*` = all events) |
| `--webhook-header 'Name: value'` | none | extra webhook header (repeatable) |
| `--webhook-timeout S` | 5 | per webhook attempt |
| `--webhook-retries N` | 3 | retries per delivery (backoff 1, 2, 4 s …) |
| `--stop-min-progress N` / `--stop-max-progress N` | 55 / 85 | "at the stop" window of the bus stop sign |
| `--influx-url URL` | none (or `INFLUX_URL`) | enable InfluxDB |
| `--influx-org`, `--influx-bucket`, `--influx-token`, `--influx-token-file` | (env `INFLUX_ORG`, `INFLUX_BUCKET`, `INFLUX_TOKEN`) | InfluxDB 2.x |
| `--influx-db`, `--influx-user`, `--influx-password` | (env `INFLUX_DB`, …) | InfluxDB 1.x |
| `--influx-interval S` / `--influx-flush S` / `--influx-buffer N` | 1 / 2 / 200000 | point rate, write rate, buffer while down |
| `--influx-tag-host NAME` | MiSTer host | `host` tag |
| `--log-file FILE` | none | also log to a file (rotated at 5 MB, 3 kept) |
| `-v`, `--verbose` | off | log every request |

The log records connects and disconnects, every write and action, events and webhook
deliveries.

## Running it permanently (macOS)

`~/Library/LaunchAgents/lan.desertbus.hub.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>lan.desertbus.hub</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/path/to/mister-package/hub/dashboard_hub.py</string>
    <string>--mister</string><string>http://mister.lan:8765</string>
    <string>--dashboard</string><string>/path/to/mister-package/dashboard/dashboard.html</string>
    <string>--log-file</string><string>/tmp/dashboard-hub.log</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/lan.desertbus.hub.plist   # start now and at login
launchctl bootout gui/$(id -u)/lan.desertbus.hub                                   # stop and remove
```

Add `--webhook …` and `--influx-…` options as further `<string>` entries.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `503 upstream_unavailable`, `/hub` shows `"connected": false` | the MiSTer or its bridge is not reachable. Check `curl http://mister.lan:8765/status`; the hub reconnects by itself. The MiSTer's IP can change (DHCP): use `mister.lan` or a reserved address |
| telemetry looks like garbage, `in_game: false` | Desert Bus is not running (BIOS, menu, another game) |
| no `bus_stop` event although the bus stopped | it stopped too early (sign still far away: `bus_stop_missed`) or did not drive off yet; the event fires on the drive-off |
| bus towed while standing | the game tows after ~30 s standing still in the driving state (`crash`, `stood still too long`) |
| patch action `501` | the hub was started without `--dashboard`/`--patches` |
| Save State / Load Fullauto `501` | the MiSTer has no savestates yet |
| webhook not arriving | look for `webhook … failed` in the log; check URL, receiver, headers |

## Tests

```bash
python3 test_events.py   # event detector, bus stop maths, cache freshness (no hardware)
python3 test_hub.py      # end to end, needs the bridge built for this machine (linux/dashboard-bridge/build/)
```

`test_hub.py` runs the bridge with `--mock=full`, a fake InfluxDB and a webhook receiver, and
checks caching, write-through, input, events and webhooks, every action (pause, patches
including multi-site and VRAM, autopilot, savestates, errors), a bridge restart, ten
concurrent clients, InfluxDB output and button release on shutdown.
