# Emulator interface required by `sdl/dashboard.html`

This is the minimal contract another Sega CD emulator must implement to run
`sdl/dashboard.html` with every feature working. It lists only what the dashboard
uses. The Genesis Plus GX server (`sdl/api_server.c`) provides much more; see
`CHANGES-desertbus.md`.

The dashboard uses **8 endpoints** on **one CPU bus** and **one memory domain**:

| Endpoint | Used by |
|---|---|
| `POST /bus-peek` | every readout, patch status badges, autopilot |
| `POST /bus-poke` | CPU-bus patches (code and data in 68K work RAM) |
| `POST /peek` | Air Freshener patch status (VRAM) |
| `POST /poke` | Air Freshener patch apply (VRAM) |
| `POST /input` | autopilot steering and START taps |
| `POST /pause`, `POST /resume` | Pause button |
| `POST /state/save`, `POST /state/load` | Save State, Load Fullauto, VRAM cache refresh |

---

## 1. Transport

- **Base URL:** `http://127.0.0.1:8765`, hard-coded as `API_BASE` in the dashboard.
- **HTTP/1.1.** Request bodies are JSON (`Content-Type: application/json`), except
  `/pause` and `/resume`, which are sent with no body and no Content-Type.
- **CORS is mandatory.** The page is opened from `file://` or another origin, so every
  response needs these headers:
  ```
  Access-Control-Allow-Origin: *
  Access-Control-Allow-Methods: GET, POST, OPTIONS
  Access-Control-Allow-Headers: Content-Type
  ```
  Answer the `OPTIONS` preflight with 2xx and the same headers. Browsers send a
  preflight for every JSON POST.
- **Response bodies are JSON.** On success, return HTTP 200 with `"ok": true`. The
  dashboard only checks for `ok === false`, so omitting `ok` also works. On failure,
  return a non-2xx status and, ideally, this body:
  `{"ok": false, "error": {"code": "<slug>", "message": "<text>"}}`.
  The dashboard displays `error.message` in the status indicator.
- **Concurrency and throughput.** One open dashboard sends about 20 requests every 500 ms
  from the two polling loops. With the autopilot on, it sends another 2–4 requests every
  220 ms. The browser keeps up to 6 requests in flight at once. The server must handle
  concurrent connections and must not stall the emulator. Budget roughly 50–100 req/s
  per dashboard at a few ms latency.
  - Latency is critical for `/input`. The autopilot presses a direction, sleeps 70–300 ms,
    then releases it. Every extra ms of latency on the release lengthens the hold and
    over-steers the bus.
- **Survive aborted connections.** Page reloads abort in-flight requests. A write to a
  closed socket must not kill the process: suppress `SIGPIPE`.

## 2. Execution semantics (all endpoints)

- **Frame-boundary execution.** Every read, write, input change, and state operation
  should run *between* emulated frames, on the emulation thread. A multi-byte peek must
  then see a consistent snapshot. More importantly, a multi-byte code patch (up to
  64 bytes) must never be half-written while the 68K is executing it.
- **Works while paused.** When the emulator is paused through `/pause`, it must still
  service peeks, pokes, `/input`, `/state/*`, and `/resume`.
- **Pokes to executable RAM must take effect.** Most patches overwrite *code* that the
  game runs from Main-CPU work RAM (`$FF7xxx`–`$FFCxxx`). If the emulator caches decoded
  or recompiled 68K code (a JIT, block cache, or prefetch queue), it must invalidate the
  cache on these writes.

---

## 3. Endpoints

### 3.1 `POST /bus-peek`: read through the Main 68K bus

```json
// request
{ "bus": "main68k", "address": 16740330, "length": 2, "encoding": "hex" }
// response
{ "ok": true, "data": "3a00" }
```

- `address` is a **JSON number** (the dashboard passes JS numbers such as `0xFF6FEA`
  directly). It is a 24-bit Main-CPU address.
- `length` ranges from 1 to 64 in practice. GPGX allows up to 64 KiB.
- `data` is a lowercase or uppercase hex string of exactly `2 * length` characters. The
  dashboard lowercases it before comparing.
- **Byte order:** return bytes as the 68K sees them, which is **big-endian** (byte at
  `address` first). Host-endian storage must be un-swapped.
- The read must go through the CPU's address decoding. Every address the dashboard reads
  is in work RAM `$FF0000–$FFFFFF`, so a direct work-RAM read is also enough.
- Reads must not have side effects and must not trigger breakpoints.

### 3.2 `POST /bus-poke`: write through the Main 68K bus

```json
// request
{ "bus": "main68k", "address": 16745516, "data": "0679010000ff6ffa", "encoding": "hex", "unsafe": true }
// response
{ "ok": true }
```

- The dashboard always sends `"unsafe": true`. GPGX rejects bus pokes without it; any
  other emulator can ignore the field.
- `data` is big-endian hex, 2–64 bytes. Write it byte by byte, in order, starting at
  `address`.
- All targets are in work RAM `$FF6FF6–$FFC005`.

### 3.3 `POST /peek`: read a memory domain

```json
// request
{ "domain": "vram", "address": 10560, "length": 32, "encoding": "hex" }
// response
{ "data": "0400242402004242..." }
```

- The dashboard reads only one domain, **`vram`**: the VDP's 64 KiB video RAM, indexed
  by VRAM byte address.
- **Byte-order trap: the dashboard's VRAM data is word-byte-swapped.** GPGX stores VRAM
  as host-endian 16-bit words on little-endian hosts. Its `vram` domain returns raw
  storage, so each pair of bytes comes back swapped compared with the VDP's big-endian
  view. The dashboard's hard-coded `sig` strings and `data` payload for the Air Freshener
  patch are in this swapped layout. You have two options:
  - make `vram` peek and poke swap each byte pair, which matches GPGX exactly, or
  - un-swap the `sig` and `data` strings in the dashboard's `PATCHES` table once and
    expose big-endian VRAM.

  The dashboard reads VRAM only at even addresses with even lengths, so word alignment
  is never an issue.

### 3.4 `POST /poke`: write a memory domain

```json
// request
{ "domain": "vram", "address": 9952, "data": "<2112 hex chars>", "encoding": "hex" }
// response
{ "ok": true }
```

- Writes 1056 bytes to VRAM `$26E0–$2AFF`: tiles `$137`–`$157`, the swinging
  air-freshener sprite sheet. The same byte-order rule as 3.3 applies.
- The body is about 2.2 KB, so the server must accept POST bodies at least that large.
  GPGX allows about 300 KB.
- **Tile-cache refresh.** After the poke, the dashboard runs `/state/save` and then
  `/state/load` on `dashboard-cache-refresh.gp0`. GPGX keeps a decoded tile cache that
  the round trip rebuilds. An emulator whose renderer reads VRAM directly, or which
  marks tiles dirty on domain writes, gets the round trip anyway. The round trip must
  succeed without changing emulated state, so savestates must be complete and exact.
  Otherwise, have `/poke` to `vram` invalidate the tile cache and turn the round trip
  into a harmless no-op.

### 3.5 `POST /input`: hold or release joypad buttons on player 1

```json
// request (either or both arrays)
{ "press": ["left"] }
{ "release": ["left", "right", "start"] }
// response
{ "ok": true, "held": ["left"] }
```

- **Holds persist.** A button stays held on every following frame until a `release`
  names it. It is not a one-frame tap. The autopilot controls timing by sending a
  separate press call and release call.
- The dashboard uses the names `left`, `right`, and `start`. GPGX also accepts `up`,
  `down`, `a`, `b`, `c`, `x`, `y`, `z`, and `mode`.
- Releasing a button that isn't held is a no-op, not an error. The dashboard sends
  `release: ["left","right","start"]` whenever the autopilot stops or hits an error.
- Injected buttons are ORed with real keyboard or gamepad input for **player 1**. A
  standard 3-button pad is enough for the dashboard.
- Latency between the HTTP call and the input reaching the pad should stay under about
  one frame (see §1).

### 3.6 `POST /pause` / `POST /resume`

- No request body. Respond `{"ok": true}`.
- `/pause` stops emulation stepping. `/resume` starts it again. Both are idempotent.
- The dashboard tracks the paused state itself and does not query it.

### 3.7 `POST /state/save`

```json
// request
{ "path": "123.gp0" }
// response
{ "ok": true, "path": "123.gp0" }
```

- `path` is chosen by the user, and the default is `<miles driven>.gp0`. It is a
  **relative filename**. GPGX resolves it against the emulator's working directory,
  which is normally `sdl/`.
- The response must include `path`, which the dashboard displays.
- The format is up to the emulator. Only that emulator's `/state/load` has to read it
  back.

### 3.8 `POST /state/load`

```json
// request
{ "path": "desertbus-fullauto.gp0" }
// response
{ "ok": true }
```

- Uses the same path resolution as save. A missing or invalid file returns an error
  (GPGX returns HTTP 500).
- **The Load Fullauto button requires `desertbus-fullauto.gp0` in the emulator's working
  directory.** This file is a GPGX-format savestate and won't load in another emulator.
  Create an equivalent state in the new emulator's own format: in-game, driving, with
  the patches you want already applied. Keep the filename or edit the dashboard.

---

## 4. Memory the dashboard touches (Main 68K bus, big-endian)

All addresses are in Main-CPU work RAM. Desert Bus loads its code from the CD into this
RAM, so the code-patch sites below sit next to game variables.

### Reads: telemetry (every 500 ms)

| Address | Size | Meaning |
|---|---|---|
| `$FF6FEA` | 2 | Speed (`0`–`$6000` = 0–45 mph) |
| `$FF6FFA` | 2 | Steering / lateral position (on-road range is `$2400`–`$B400`, center `$6C00`) |
| `$FF70E4` | 4 | Clock digits: hour tens (`$FF` = blank), hour ones, minute tens, minute ones |
| `$FF6FDC` | 4 | Distance accumulator for the current leg (1800 units per mile) |
| `$FF70EA` | 10 | Odometer wheel digits (5 words: tenths through thousands) |
| `$FF709C` | 4 | Live palette-target pointer (day/night ladder, `$20CB52`–`$20CED2`) |
| `$FF6FF8` | 2 | Half-day parity |
| `$FF7AA8` | 6 | Day/night fix signature (`4e714e714e71` means patched) |
| `$FF7002` | 2 | Game state (`3` = driving). Read by the autopilot every 220 ms |

### Reads and writes: patch sites (status polled every 500 ms, written on click)

| Patch | Sites (address:length) | Extra writes |
|---|---|---|
| Steering Drift | `$FF842C:8` | none |
| Full Throttle | `$FF8498:10`, `$FFC004:2` | none |
| Uncap Max Speed | `$FF84CC:2`, `$FF84D6:2` | none |
| Accelerate Time | `$FF7A08:2` | `$FF6FF6 ← 0000` on apply |
| Bus Stops on Left | `$FFBB22:6`, `$FFBB3C:2`, `$FFBB46:2` | none |
| Bus Stop Every Mile | `$FFBA54:18` | none |
| Fix Day/Night Cycle | `$FF7AA8:6`, `$FF7ABC:64`, `$FF7B6C:56` | none |
| Windshield Bug Splat | `$FF7104:2` | `$FF7106`, and `$FF17FA` or `$FF185C` |

The byte values for each state are in the `PATCHES` table in `dashboard.html`.

### VRAM domain

| Address | Size | Use |
|---|---|---|
| `$2940` (10560) | 32 | Signature tile read for the badge |
| `$26E0` (9952) | 1056 | Air-freshener tiles, written on click |

---

## 5. Conformance checklist

- [ ] HTTP server on `127.0.0.1:8765` with CORS headers and `OPTIONS` preflight
- [ ] Concurrent connections, about 100 req/s, and survives aborted sockets
- [ ] `/bus-peek` and `/bus-poke` on `main68k`, big-endian hex, with numeric addresses
- [ ] Operations run between frames and are still serviced while paused
- [ ] Code pokes into work RAM take effect: no stale JIT or instruction cache
- [ ] `/peek` and `/poke` on the `vram` domain, with GPGX's word-swapped byte order *or*
      re-encoded dashboard data
- [ ] VRAM pokes appear on screen after the save/load round trip, or right away
- [ ] `/input` with persistent press and release on player 1, and low latency
- [ ] `/pause` and `/resume`
- [ ] `/state/save` echoes `path`; `/state/load` uses the same relative path resolution
- [ ] A `desertbus-fullauto.<fmt>` state created in the new emulator's own format
