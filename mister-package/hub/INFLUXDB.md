# Desert Bus data in InfluxDB

What the dashboard hub (`dashboard_hub.py`) writes to InfluxDB, when, and how to read it.
The hub reads the game's memory on the MiSTer; this document describes the resulting
series. Setup and the other hub features are in [README.md](README.md).

Contents: [Connection](#connection) · [Overview](#overview) ·
[`desertbus`: game telemetry](#desertbus-game-telemetry) ·
[`desertbus_event`: game events](#desertbus_event-game-events) ·
[`dashboard_hub`: hub health](#dashboard_hub-hub-health) · [Timing and gaps](#timing-and-gaps) ·
[Interpreting the values](#interpreting-the-values) · [Queries](#queries) · [Grafana](#grafana)

## Connection

```bash
INFLUX_TOKEN=... python3 dashboard_hub.py --mister http://192.168.1.128:8765 --influx-bucket desertbus
```

- Server: `https://serv-influxdb.desertbus.org/` by default (`--influx-url` or `INFLUX_URL`
  for another one). Writing starts once a bucket (`--influx-bucket`, InfluxDB 2.x/3 write API
  with token, optional `--influx-org`) or a database (`--influx-db`, 1.x API) is given.
- Points are written in batches every `--influx-flush` (2 s) with nanosecond timestamps.
- While InfluxDB is unreachable, up to `--influx-buffer` (200 000) points are kept and
  written when it is back (that is ~27 hours of the default point rate). Batches the
  server rejects as malformed are dropped and logged.

## Overview

| Measurement | One point | Tags | Content |
|---|---|---|---|
| [`desertbus`](#desertbus-game-telemetry) | every `--influx-interval` (1 s), while the MiSTer is reachable | `host` | the game state: speed, position on the road, distance, clock, bus stops, … |
| [`desertbus_event`](#desertbus_event-game-events) | per game event, at the moment it happened | `host`, `event` | bus stop served/missed, crash, point, bug splat, honk |
| [`dashboard_hub`](#dashboard_hub-hub-health) | every `--influx-interval` (1 s), always | `host` | connection to the MiSTer, request rates, latency, clients |

Tag `host` = `--influx-tag-host`, default the MiSTer's host name or IP as given in
`--mister` (e.g. `192.168.1.128`). Use it to tell several MiSTers or setups apart.

Field types never change between points (integers are written as InfluxDB integers,
`42i`). A field without a value at that moment is left out of the point rather than
written as zero (e.g. `next_stop_eta_s` while standing).

## `desertbus`: game telemetry

Sampled from the hub's latest poll (the hub polls the game every `--interval`, 0.25 s).

**While Desert Bus is not running** (`in_game` = false: BIOS, intro, game-selection menu,
another game) a point carries only `in_game`, `game_state`, `paused`, `frame`, `held`.

### Game and session state

| Field | Type | Unit / range | Meaning (work RAM address) |
|---|---|---|---|
| `in_game` | bool | | Desert Bus is running. All other game fields are only meaningful when true |
| `game_state` | int | 0–13 | `$FF7002`: **3 = driving**, 0 = interlude / timecard / menus, 1 = off-road stall, 2 = waiting for the tow, 4 = towed, 5 = tow scene, 6–9 = arrival and leg change, 12–13 = game ending |
| `paused` | bool | | the console is paused by the dashboard (from the bridge) |
| `frame` | int | | the bridge's frame counter (keeps counting while paused) |
| `held` | string | e.g. `"a"`, `"a,left"` | buttons held by injected input (dashboard, autopilot, actions), comma-separated, empty if none |
| `driver_name` | string | e.g. `JOCKO` | `$FF7108`, shown on the timecard; `JOCKO` unless a name was entered |

### Speed and position

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `speed_raw` | int | 0–0x6000 (24576), higher with the speed patches | `$FF6FEA`, the value the game adds to the distance every frame |
| `speed_mph` | float | 0–45 mph | `speed_raw / 0x6000 × 45`; can exceed 45 with the Uncap Max Speed / Full Throttle Max patches |
| `lateral_raw` | int | on the road 0x2400–0xB400 (9216–46080), centre 0x6C00 (27648) | `$FF6FFA`, position across the road |
| `lateral_norm` | float | −1 … +1 on the road | `(lateral_raw − 0x6C00) / 0x4800`: −1 = left shoulder, 0 = centre, +1 = right shoulder; values past ±1 mean off the road (the game keeps the position within about −1.0 … +1.06) |
| `offroad` | bool | | off the road (`lateral_raw` ≤ 0x2400 or ≥ 0xB400); 3 s off the road ends in a stall |
| `return_leg` | bool | | false = Tucson → Las Vegas, true = Las Vegas → Tucson (`$FF6FE4`) |

### Distance, odometer and points

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `distance_raw` | int | 0–648000 | `$FF6FDC`, distance on the current leg in game units (1800 = 1 mile, 648000 = 360 mi = end of the leg); resets at the start of every leg |
| `leg_miles` | float | 0–360 mi | `distance_raw / 1800` |
| `odometer_miles` | float | 109.3 + 360 per leg | the odometer wheel on the dashboard (`$FF70EA`) |
| `points` | int | completed legs | derived: `(odometer − 109.3 − leg_miles) / 360`, rounded |

### Clock and day/night

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `clock_hour`, `clock_minute` | int | 1–12, 0–59 | the in-game 12-hour clock (`$FF70E4`); the trip starts at 7:30 |
| `phase` | string | `day`, `night`, `dawn`, `dusk`, `twilight`, `unknown` | from the palette pointer; `dawn`/`dusk` only with the Fix Day/Night Cycle patch, otherwise transitions read `twilight` |
| `palette` | int | 0x20CB52 (day) … 0x20CED2 (night) | `$FF709C`, the palette the game is fading to (0x20D252 = the 6:55 am special) |
| `daynight_parity` | int | | `$FF6FF8`, half-day counter (even = 1 am–1 pm with the fix) |
| `daynight_fixed` | bool | | the Fix Day/Night Cycle patch is applied |

### Bus stops

| Field | Type | Unit / range | Meaning |
|---|---|---|---|
| `next_stop_miles` | float | mi | distance to the next bus stop on this leg; absent when no stop is left (after 295.1 mi) |
| `next_stop_eta_s` | int | seconds | time to the next stop at the current speed; absent when standing or no stop is left |
| `next_stop_raw` | int | game units | position of the next stop on the leg |
| `stop_mode` | string | `table`, `every 1 mi` | `table` = the six stops per leg (0.9, 55.1, 125.3, 166.2, 233.8, 295.1 mi); `every N mi` with the Bus Stop Every Mile patch |
| `stop_active` | bool | | a bus stop sign is passing (spawned at the stop's distance) |
| `stop_visible` | bool | | the sign is on screen |
| `stop_progress` | int | 6–140 | how far the sign has come; **55–85 = next to the bus** ("at the stop") |

### Bug splat

| Field | Type | Meaning |
|---|---|---|
| `splat_visible` | bool | the windshield bug splat is showing |
| `splat_x` | int | its x position (360 = parked off-screen) |

## `desertbus_event`: game events

One point per event, timestamped when the hub detected it (at most one poll, 0.25 s,
after it happened in the game). Tags `host` and `event`.

| Field | Type | Meaning |
|---|---|---|
| `count` | int | always 1, so `sum(count)` counts events |
| `details` | string | the event's details as a JSON object (below) |

| `event` tag | Fires when | `details` keys |
|---|---|---|
| `bus_stop` | the bus stopped next to a bus stop sign and then drove off again (fires on the drive-off; not if it crashed or was towed there) | `leg_miles` (where the stop is), `stopped_s` (how long it stood) |
| `bus_stop_missed` | a bus stop passed without such a stop (also: stopped too early, sign still far away) | `leg_miles` |
| `crash` | driving ended in an off-road stall or in a tow after standing still ~30 s | `leg_miles`, `state` (1/2 = off-road stall, 4 = tow), `cause` (`off the road` or `stood still too long`), `lateral_raw` |
| `point` | the leg was completed (360 mi) | `points` (after this one), `odometer_miles`, `leg` (`outbound`, `return`) |
| `bug_splat` | the windshield bug splat appeared while driving | `x`, `leg_miles` |
| `honk` | the driver honked (START while driving), one point per honk | `leg_miles`, `speed_mph` |

Example (line protocol):

```text
desertbus_event,event=bus_stop,host=192.168.1.128 count=1i,details="{\"leg_miles\": 0.89, \"stopped_s\": 16.4}" 1790848148933000000
```

Actions (dashboard buttons, webhooks), the autopilot and webhook deliveries are **not**
written to InfluxDB; they are in the hub's log.

## `dashboard_hub`: hub health

Written every `--influx-interval`, also while the MiSTer is unreachable (that is how gaps in
`desertbus` can be explained).

| Field | Type | Meaning |
|---|---|---|
| `connected` | bool | the MiSTer bridge is reachable |
| `core_present` | bool | the dashboard core is loaded |
| `upstream_rps` | float | requests per second the hub sends to the MiSTer |
| `upstream_latency_ms_p50`, `upstream_latency_ms_max` | float | latency of the last 500 requests to the MiSTer |
| `disconnects` | int | connection losses since the hub started (cumulative) |
| `clients` | int | dashboards and other clients seen in the last 30 s |
| `client_requests`, `cache_hits`, `forwarded` | int | requests from clients, how many were answered from the cache, how many went to the MiSTer (cumulative since the hub started; use `non_negative_difference` / `derivative` for rates) |
| `demand_ranges` | int | extra memory ranges watched for clients |
| `influx_buffered` | int | points waiting to be written (rises while InfluxDB is unreachable) |

## Timing and gaps

- **Resolution:** one `desertbus` point per second by default. Steering corrections are
  faster than that; `--influx-interval 0.25` records every poll (no extra load on the
  MiSTer, 4× the points).
- **No `desertbus` points** while the MiSTer or its bridge is unreachable
  (`dashboard_hub.connected` = false) or the hub's data is older than 5 s.
- **Timestamps** come from the computer running the hub (keep its clock synced).
- **Counters** in `dashboard_hub` restart at 0 when the hub restarts.
- **Hub restarts** do not lose game data that has not happened yet, but events during the
  restart are not detected afterwards.

## Interpreting the values

- **Driving time** is when `game_state` = 3. Filter on it for speed and position statistics;
  in other states the bus is stalled, towed, or between legs.
- **During a tow** (`game_state` 4/5) the game counts the distance **down** (the bus is towed
  back) and it can briefly wrap below zero, showing as a huge `distance_raw` (≥ 2 147 483 648)
  before the game resets it to 0. Ignore `distance_raw`/`leg_miles` outside `game_state` = 3.
- **Speed and distance** are tied: distance units per second = `speed_raw × 60 / 65536`, so
  45 mph = 22.5 units/s = 0.0125 mi/s.
- **`points`** is derived from the odometer and correct in normal play. If the distance was
  changed by a patch or test, it can disagree with the timecard; the `point` events count
  completed legs directly.
- **Patches** change the meaning of some values: Uncap Max Speed / Full Throttle Max allow
  `speed_mph` above 45; Accelerate Time makes the clock race; Bus Stop Every Mile changes
  `stop_mode` and the next-stop fields.

## Queries

Python: [`query_latest.py`](query_latest.py) prints the newest record and, with
`--events N`, the newest events (`INFLUX_TOKEN=... python3 query_latest.py --events 5`;
`--json` for raw rows, `--host` to pick one hub). Standard library only.

SQL (InfluxDB 3):

```sql
-- speed and position while driving, last hour
SELECT time, speed_mph, lateral_norm, leg_miles
FROM desertbus
WHERE time > now() - INTERVAL '1 hour' AND game_state = 3
ORDER BY time;

-- events per type today
SELECT event, SUM(count) AS n
FROM desertbus_event
WHERE time > date_trunc('day', now())
GROUP BY event;
```

Flux (InfluxDB 2.x):

```flux
from(bucket: "desertbus")
  |> range(start: -6h)
  |> filter(fn: (r) => r._measurement == "desertbus" and (r._field == "speed_mph" or r._field == "lateral_norm"))
  |> aggregateWindow(every: 10s, fn: mean)
```

```flux
// honks per minute
from(bucket: "desertbus")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "desertbus_event" and r.event == "honk" and r._field == "count")
  |> aggregateWindow(every: 1m, fn: sum, createEmpty: true)
```

InfluxQL (1.x):

```sql
SELECT mean("speed_mph") FROM "desertbus" WHERE "game_state" = 3 AND time > now() - 6h GROUP BY time(1m)
```

## Grafana

- **Speed** (`speed_mph`) and **position on the road** (`lateral_norm` with thresholds at
  ±1) as time series; `offroad` as a state timeline.
- **Progress**: `leg_miles` (0–360) as a gauge, `odometer_miles` and `points` as stats,
  `next_stop_miles` / `next_stop_eta_s` as stats.
- **Clock and phase**: `clock_hour`, `clock_minute`, `phase` as stat or state timeline.
- **Events as annotations**: query `desertbus_event` and use the `event` tag (and
  `details`) as annotation text; crashes and points stand out on any time series.
- **Health**: `dashboard_hub.connected` and `upstream_latency_ms_p50` to see why data is
  missing.
