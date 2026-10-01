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
- `GET /hub/telemetry`: the latest decoded telemetry as JSON.
- `GET /` (with `--dashboard`): the dashboard page.
- `/capabilities` gains a `"hub"` object; everything else is the bridge's API.

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
