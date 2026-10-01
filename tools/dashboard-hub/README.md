# Dashboard hub

`dashboard_hub.py` sits between the dashboards and the MiSTer. It is the only client of the
MiSTer bridge: it polls the telemetry once, answers every dashboard from that, forwards
writes and input in order, logs telemetry to InfluxDB, and reconnects by itself.

```
 dashboard.html ─┐
 dashboard.html ─┼──▶  dashboard_hub.py  ──(one keep-alive connection)──▶  MiSTer bridge :8765
 scripts        ─┘     :8766             └──▶ InfluxDB
```

Python 3.8+, standard library only. Run it on the Mac, not on the MiSTer (the point is to keep load off the MiSTer).

## Run

```bash
python3 dashboard_hub.py --mister http://mister.lan:8765 \
    --dashboard ../../../Genesis-Plus-GX/sdl/dashboard.html
```

Open `http://<hub-host>:8766/` (served by `--dashboard`, or open your own copy of
`dashboard.html`) and connect to `http://<hub-host>:8766`. The hub speaks the bridge's API,
so the dashboard works unchanged, as do the scripts in `tests/dashboard/` (`--host <hub-host>
--port 8766`).

With InfluxDB 2.x:

```bash
export INFLUX_TOKEN=...           # or --influx-token-file ~/.influx-token
python3 dashboard_hub.py --mister http://mister.lan:8765 \
    --influx-url http://localhost:8086 --influx-org home --influx-bucket desertbus
```

InfluxDB 1.x: `--influx-url http://localhost:8086 --influx-db desertbus [--influx-user u --influx-password p]`.

## What it does

| Concern | Behaviour |
|---|---|
| Load on the MiSTer | One connection; one worker sends requests in priority order (writes and input → polling → client reads that missed the cache; concurrent misses for one range share a single read) and never faster than `--max-rate` (60/s). 10 clients making 1000 requests cause about 27 upstream requests. |
| Telemetry | Speed, lateral position, clock, distance, odometer, day phase and game state are read every `--interval` (0.25 s) in 4 reads, plus `/status`. Dashboard reads of these addresses are served from the cache (at most `--max-age`, default 2 × interval, old). |
| Other reads | Any other `/bus-peek` or VRAM `/peek` range a client reads (patch status sites, the air freshener) is forwarded once, then polled every `--demand-interval` (0.5 s; VRAM `--vram-interval` 1 s) while clients keep asking (`--demand-ttl` 10 s) and answered from the cache. |
| Writes | `/bus-poke`, `/poke`, `/input`, `/pause`, `/resume` are forwarded one at a time, invalidate the cache before and after, and are logged. A read after a write always sees the write. |
| Errors | Bridge answers (including `400 invalid_range`, `501 feature_unavailable`) pass through unchanged. |
| Disconnects | Any connection failure marks the bridge down; clients get `503 upstream_unavailable` (the dashboard shows it and keeps polling) and the hub retries with backoff (0.5 s → 10 s). After reconnecting it clears the cache if the core epoch changed. A stale keep-alive connection is retried once transparently. |
| Buttons | All buttons are released when the hub connects (also after every reconnect) and when it shuts down (SIGINT/SIGTERM). `--no-release` turns this off. |
| InfluxDB | Points are buffered (up to `--influx-buffer`, 200 000) while InfluxDB is down and written when it is back; malformed batches are dropped and logged. |

## Endpoints added by the hub

- `GET /hub`: connection state, upstream latency, request counts, cache hits, InfluxDB state.
- `GET /hub/telemetry`: the latest decoded telemetry as JSON (including next bus stop and driver).
- `GET /hub/events?since=ID`: game events newer than `ID` (the last 500 are kept), plus `last_id`.
- `GET /` (with `--dashboard`): the dashboard page.
- `/capabilities` gains a `"hub"` object; everything else is the bridge's API.

## Game events and webhooks

The hub detects these events from the polled memory (each one verified live on the MiSTer
unless noted):

| Event | Fires when | Details |
|---|---|---|
| `bus_stop` | the bus stops (speed 0) while the bus stop sign is beside the road (sign progress `--stop-min-progress`..`--stop-max-progress`, 55..85: large on the shoulder next to the bus; at 32 it is still small near the horizon), then **drives off again**. Never fires if the bus crashes or is towed while standing there. | `leg_miles`, `stopped_s` |
| `bus_stop_missed` | a bus stop sign passes without such a stop | `leg_miles` |
| `crash` | driving (state 3) ends in an off-road stall (state 1/2; unit-tested) or in a tow after standing still ~30 s (state 4; the game's stall timer `$FF6FFC`) | `leg_miles`, `state`, `cause` |
| `point` | the distance reaches the end of the leg (648000 units = 360 mi) | `points`, `odometer_miles`, `leg` |
| `bug_splat` | the windshield splat appears while driving | `x`, `leg_miles` |

Webhooks POST each event as JSON:

```bash
python3 dashboard_hub.py --mister http://mister.lan:8765 \
    --webhook "bus_stop=http://homeassistant.lan:8123/api/webhook/desertbus-stop" \
    --webhook "*=http://localhost:9000/all-events" \
    --webhook-header "Authorization: Bearer secret"
```

`--webhook EVENT=URL` is repeatable; `*` matches every event. Delivery runs in the background
with `--webhook-retries` (3, backoff 1-2-4 s) and `--webhook-timeout` (5 s). Payload:

```json
{"id": 2, "event": "bus_stop", "unix": 1790848148.933, "time": "2026-10-01T11:49:08+0200",
 "details": {"leg_miles": 0.89, "stopped_s": 12.0},
 "telemetry": {"leg_miles": 0.95, "odometer_miles": 110.0, "points": 0, "speed_mph": 3.1,
               "clock_hour": 7, "clock_minute": 41, "driver_name": "JOCKO", "return_leg": false}}
```

Events also go to InfluxDB (measurement `desertbus_event`, tag `event`) and to the
dashboard's Events card when it is connected through the hub.

Bus stop geometry on the return leg is not verified: there any stop while the sign is
visible counts.

## InfluxDB schema

Measurement **`desertbus`** (every `--influx-interval`, 1 s), tag `host`:

| Field | Meaning |
|---|---|
| `in_game` | Desert Bus is running. When false only `game_state`, `paused`, `frame`, `held` are written. |
| `game_state` | `$FF7002` (3 = driving) |
| `speed_raw`, `speed_mph` | `$FF6FEA`, mph = raw / 0x6000 × 45 |
| `lateral_raw`, `lateral_norm`, `offroad` | `$FF6FFA`; −1 left shoulder … +1 right; off road outside 0x2400–0xB400 |
| `clock_hour`, `clock_minute` | in-game 12-hour clock |
| `distance_raw`, `leg_miles` | `$FF6FDC`; miles on the current leg (raw / 1800) |
| `odometer_miles`, `points` | odometer wheel; completed legs |
| `palette`, `daynight_parity`, `daynight_fixed`, `phase` | day/night state (`day`, `night`, `dawn`, `dusk`, `twilight`) |
| `paused`, `frame`, `held` | from the bridge's `/status` |
| `return_leg` | `$FF6FE4` != 0 |
| `driver_name` | `$FF7108`, 8 letters, A = 1 (default JOCKO) |
| `next_stop_raw`, `next_stop_miles`, `next_stop_eta_s`, `stop_mode` | next bus stop from the six-entry table at `$FFBA2E`, or every N units when the "Bus Stop Every Mile" patch (`80FC nnnn` at `$FFBA54`) is applied; ETA at the current speed |
| `stop_active`, `stop_visible`, `stop_progress` | the bus stop sign object (`$FF15FA`, progress `$FF164E`) |
| `splat_visible`, `splat_x` | bug splat latch `$FF7104` and object slot 10 |

Measurement **`dashboard_hub`**: `connected`, `core_present`, `upstream_rps`,
`upstream_latency_ms_p50`, `upstream_latency_ms_max`, `disconnects`, `clients`,
`client_requests`, `cache_hits`, `forwarded`, `demand_ranges`, `influx_buffered`.

Example Flux query (miles per hour while driving):

```flux
from(bucket: "desertbus")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "desertbus" and r._field == "speed_mph")
```

## Run as a service (Linux, systemd)

`/etc/systemd/system/dashboard-hub.service`:

```ini
[Unit]
Description=Desert Bus dashboard hub
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/dashboard-hub/dashboard_hub.py --mister http://mister.lan:8765 \
    --dashboard /opt/dashboard-hub/dashboard.html \
    --influx-url http://localhost:8086 --influx-org home --influx-bucket desertbus
Environment=INFLUX_TOKEN=change-me
Restart=always
RestartSec=2
User=nobody

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now dashboard-hub
journalctl -u dashboard-hub -f
```

On macOS, use `--log-file hub.log` and a launchd agent, or run it in `tmux`.

## Notes

- Point dashboards at the hub only. A dashboard or script talking to the bridge directly
  still works but bypasses the hub's ordering and caching.
- Telemetry served from the cache can be up to `--max-age` old. For tighter autopilot timing,
  lower `--interval` (each step costs ~5 requests per interval on the MiSTer).
- The hub has no authentication, like the bridge; keep it on a trusted LAN.

## Test

```bash
python3 test_hub.py      # needs the bridge built for this machine: linux/dashboard-bridge/build/
```

Runs the bridge with `--mock=full`, a fake InfluxDB and the hub, and checks caching,
write-through, input, a bridge restart, ten concurrent clients, InfluxDB output and button
release on shutdown.
