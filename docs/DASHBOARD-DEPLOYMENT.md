# Deploying the Desert Bus dashboard on a MegaCD MiSTer

> **DRAFT: not yet rehearsed on hardware.** Every command below uses the flags and paths the
> code implements. Plan Task 12 requires one full rehearsal on a backed-up or test SD card
> before this becomes the supported procedure. Expected outputs from hardware are marked
> *(to confirm)*.
>
> **Milestone: control** (validated on hardware 2026-09-26). This package gives live telemetry,
> patch badges, autopilot and START input, **pause/resume, all CPU patches, and the air
> freshener**, from a second computer. It does **not** provide Save State or Load Fullauto
> (native savestates are deferred); the dashboard greys those out, and the API answers
> `feature_unavailable`. See [dashboard-feasibility.md](dashboard-feasibility.md).

Command blocks are labelled **build host**, **MiSTer** (an SSH shell as root), or
**dashboard computer**.

## 1. Prerequisites and compatibility

| Item | Tested value |
|---|---|
| Board | DE10-Nano + SDRAM module *(record model/size)* |
| MiSTer Linux | *(record `uname -a`)* |
| Main | `Main_MiSTer` release 20260912 (`47221c1`) + dashboard patch; build 2026-09-25 SHA-256 `359ec1a5579aa93e6ab2e60cd276a9cb5643cb5655f1b8f894f07f0e97e7d93f` |
| RBF | `MegaCD_Dashboard.rbf`, build ID `01yymmdd` shown by `/capabilities`, SHA-256 *(record)* |
| Bridge | `megacd-dashboard 0.1.0 (protocol 1)` |
| Quartus / ARM GCC | 17.0.2 / 10.2-2020.11 |
| Region / video | *(record: NTSC/PAL, HDMI mode, direct video yes/no)* |
| Game / BIOS | Desert Bus (Sega CD), BIOS file name and SHA-256 *(record; never distributed)* |

**Savestate files:** a `.gp0` file written by Genesis Plus GX cannot load on the MiSTer. The
bridge recognises such files and rejects them before touching the machine. Native MiSTer
states do not exist yet.

## 2. Build or obtain the artifacts

Follow [DASHBOARD-BUILD.md](DASHBOARD-BUILD.md). You need `MegaCD_Dashboard.rbf`, the matching
`MiSTer` Main binary, `megacd-dashboard`, `start.sh`, and `dashboard.html`. Check the package
checksums:

**Dashboard computer (or build host):**

```bash
cd megacd-dashboard-<date> && sha256sum -c SHA256SUMS
```

## 3. Back up first

Use a test SD card, or image the card first. At minimum, on the MiSTer:

**MiSTer:**

```bash
B=/media/fat/backup-before-dashboard-$(date +%Y%m%d)
mkdir -p $B
cp -a /media/fat/MiSTer /media/fat/MiSTer.ini $B/ 2>/dev/null
cp -a /media/fat/linux/user-startup.sh $B/ 2>/dev/null
cp -a /media/fat/_Console/MegaCD*.rbf $B/ 2>/dev/null
cp -a /media/fat/saves/MegaCD /media/fat/config $B/ 2>/dev/null
sha256sum $B/MiSTer > $B/SHA256SUMS
ls -l $B
```

If Main stops starting later, put the card in another computer and copy
`backup-before-dashboard-*/MiSTer` back to the card root.

## 4. Install the core next to the stock one

**Dashboard computer:**

```bash
scp _Console/MegaCD_Dashboard.rbf root@mister.local:/media/fat/_Console/
```

It appears in the menu as **MegaCD_Dashboard**. Its internal name stays `MEGACD`, so Main's CD
handling, BIOS lookup (`boot.rom`, or `cd_bios.rom` next to the game), and backup RAM work as
they do for the stock core. Keep the stock `MegaCD_*.rbf`; it is your fallback.

## 5. Install the matching Main

A custom Main replaces the system-wide menu binary. Stock Main has no IPC adapter.

**Dashboard computer:**

```bash
scp MiSTer root@mister.local:/media/fat/MiSTer.dashboard
```

**MiSTer:**

```bash
sha256sum /media/fat/MiSTer.dashboard       # must match SHA256SUMS
cp /media/fat/MiSTer.dashboard /media/fat/MiSTer
sync; reboot
```

After the reboot, check that the OSD, controllers, and a stock core still work. Then load
**MegaCD_Dashboard**, boot a CD game, and confirm CD audio. Main should have logged
`MCD dashboard: listening on /tmp/megacd-dashboard.sock` *(to confirm where Main's stdout goes
on your image)*.

**Updates:** a MiSTer updater run replaces `/media/fat/MiSTer` with stock Main, which silently
removes the adapter. The bridge then reports "dashboard IPC to Main_MiSTer is not connected".
After updating, re-copy `MiSTer.dashboard` if it is still the matching build. *(Record the
updater exclusion you used, if any; do not disable unrelated updates.)*

## 6. Install and configure the bridge

**Dashboard computer:**

```bash
ssh root@mister.local mkdir -p /media/fat/megacd-dashboard
scp megacd-dashboard/megacd-dashboard megacd-dashboard/start.sh root@mister.local:/media/fat/megacd-dashboard/
```

**MiSTer:**

```bash
chmod +x /media/fat/megacd-dashboard/megacd-dashboard /media/fat/megacd-dashboard/start.sh
/media/fat/megacd-dashboard/megacd-dashboard --version
# megacd-dashboard 0.1.0 (protocol 1)
```

Defaults, overridable in `/media/fat/megacd-dashboard/megacd-dashboard.conf`:

| Setting | Default | Meaning |
|---|---|---|
| `LISTEN` | `0.0.0.0:8765` | HTTP address. Use the MiSTer's LAN IP to bind one interface. |
| `SOCKET` | `/tmp/megacd-dashboard.sock` | Main IPC socket (root only, mode 0600) |
| `STATE_DIR` | `/media/fat/config/megacd-dashboard/states` | where state files are looked up |

The log is `/tmp/megacd-dashboard.log`, rotated to `.1` at each start once it exceeds 256 KiB.
The lock file `/tmp/megacd-dashboard.lock` keeps a second instance from starting. A stale
socket is replaced by Main at startup.

Service commands: `start.sh start|stop|restart|status|foreground`. All binary flags:
`--listen ADDR:PORT --socket PATH --state-dir DIR --lock PATH --verbose --version`.
(`--mock` exists only for tests and never touches hardware.)

## 7. Manual smoke test before autostart

**MiSTer:** run it in the foreground (Ctrl+C stops it and releases any injected buttons):

```bash
/media/fat/megacd-dashboard/start.sh foreground
```

Load **MegaCD_Dashboard**, boot Desert Bus, and start driving.

**Dashboard computer:** a read-only probe first, then a pad release, which is always harmless:

```bash
curl -i -X OPTIONS http://mister.local:8765/bus-peek \
  -H 'Origin: null' -H 'Access-Control-Request-Method: POST' -H 'Access-Control-Request-Headers: content-type'
```

```bash
curl -sS http://mister.local:8765/capabilities
```

```bash
curl -sS http://mister.local:8765/bus-peek -H 'Content-Type: application/json' \
  -d '{"bus":"main68k","address":16740330,"length":2,"encoding":"hex"}'
```

```bash
curl -sS http://mister.local:8765/input -H 'Content-Type: application/json' -d '{"release":["left","right","start"]}'
```

```bash
curl -sS -X POST http://mister.local:8765/pause
```

Expected results:

- `OPTIONS` answers `204` with all three `Access-Control-Allow-*` headers.
- `capabilities` shows `"present":true`, `"protocol":1`, and features with `bus_peek` and
  `input` true and the rest false.
- `bus-peek` returns `{"ok":true,"data":"xxxx"}`: exactly four hex digits for the speed word.
  Game values vary, so don't pass or fail on a specific number.
- `input` returns `"held":[]`.
- `pause` returns HTTP 501 with `feature_unavailable`. This is expected in this milestone.

Expected failures that point at the setup:

| Response | Meaning |
|---|---|
| `core_unavailable`: "IPC … not connected" | Main is stock or not running |
| `core_unavailable`: "not MegaCD" | another core is loaded |
| `protocol_mismatch`: "no dashboard endpoint (stock core?)" | the stock MegaCD RBF is loaded |

**Input latency (to measure, not assumed):** have someone hold nothing on the pad while you
run this, and watch the bus steer:

```bash
curl -sS http://mister.local:8765/input -H 'Content-Type: application/json' -d '{"press":["left"]}'; \
sleep 0.2; curl -sS http://mister.local:8765/input -H 'Content-Type: application/json' -d '{"release":["left"]}'
```

For numbers, run `python3 tests/dashboard/load_test.py --base http://mister.local:8765
--with-input mode --rate 100 --concurrency 6 --seconds 120` in a dedicated session and record
the press/release p95. The plan's target is p95 ≤ 20 ms (NTSC) or 24 ms (PAL) from request to
pad on wired Ethernet. The HTTP number is a lower bound; pad timing needs a logic analyser or
the frame counter in `/status`.

## 8. Optional autostart

**MiSTer:** add a delimited block to the existing startup file. This is idempotent, and the
file's other contents are kept:

```bash
F=/media/fat/linux/user-startup.sh
[ -f $F ] || cp /media/fat/linux/_user-startup.sh $F
grep -q '# >>> megacd-dashboard >>>' $F || cat >> $F <<'EOF'

# >>> megacd-dashboard >>>
[ -x /media/fat/megacd-dashboard/start.sh ] && /media/fat/megacd-dashboard/start.sh "$1"
# <<< megacd-dashboard <<<
EOF
chmod +x $F
```

`/etc/init.d/S99user` runs this file with `start` at boot and `stop` at shutdown, and the block
passes that on, so the bridge also shuts down cleanly (releasing any injected buttons). Test
both without rebooting: `/etc/init.d/S99user stop` then `/etc/init.d/S99user start`.

The bridge may start before Main or the core. It retries the IPC socket with backoff (100 ms
up to 1 s) on each request. Main restarts itself on every core load; the bridge notices the
dead connection before sending, reconnects, re-opens its session, and retries once, so the
first request after loading the core already succeeds. Reboot and check with `start.sh status`.

Removal:

```bash
sed -i '/# >>> megacd-dashboard >>>/,/# <<< megacd-dashboard <<</d' /media/fat/linux/user-startup.sh
```

## 9. Connect the dashboard computer

1. Find the MiSTer address: the MiSTer menu's network info, `ip -4 addr` on the MiSTer, or
   your router. `mister.local` works only where mDNS resolves it.
2. Serve the page over plain HTTP from the dashboard computer. This is the tested scheme:

   **Dashboard computer:**

   ```bash
   cd dashboard && python3 -m http.server 8000 --bind 127.0.0.1
   ```

   Then open `http://127.0.0.1:8000/dashboard.html`. Opening the file directly (`file://`)
   also works in most browsers. **Do not** host the page over HTTPS: browsers block an HTTPS
   page from calling the MiSTer's plain-HTTP API (mixed content).
3. Enter `http://<MiSTer-IP>:8765` in **Server** and press **Connect**. The line under the field
   shows the connected server and build, and lists the features this server lacks.
4. The choice is remembered in the browser. `dashboard.html?api=http://192.168.1.50:8765`
   connects to another server for one visit without changing the saved choice.
5. **Switching servers:** **Disconnect**, or connecting elsewhere, stops the autopilot and
   polling, cancels in-flight requests, and sends `release left/right/start` to the **old**
   server. A tap already in flight still releases on the server it pressed on. Nothing
   continues on the new server until you start it.

Browser permissions: the bridge answers Private Network Access preflights
(`Access-Control-Allow-Private-Network: true`). Chrome may still ask for local-network
permission when the page's origin is less private than the MiSTer. Serving from `127.0.0.1`
avoids this *(to confirm per browser)*. If requests fail with "cannot reach", check reachability:

```bash
ping -c 2 <MiSTer-IP>
```

```bash
curl -sS -m 3 http://<MiSTer-IP>:8765/status
```

The API has wildcard CORS and no authentication. Keep port 8765 on your trusted LAN; never
forward it from the internet.

## 10. Exercise the dashboard

- **Live indicator** turns green ("live"). Speed, time, distance, ETA, points, day phase, and
  lateral position update twice a second.
- **Patch badges** show each patch's current state (read-only in this milestone). The patch
  buttons are greyed out, with the tooltip "not supported by the connected server".
- **Autopilot:** press **Autopilot: ON** while driving. It steers with short left/right taps,
  and after a stall it waits 2 s and taps START. Physical pad input keeps working at the same
  time, because injected buttons are ORed into player 1. **Autopilot: OFF** releases
  everything.
- **Stuck input** cannot outlive its controller:
  - `start.sh stop` or a bridge crash makes Main send `SESSION 0`;
  - if Main hangs, the FPGA lease expires after about 1.25 s without packets.

## 11. Fullauto

Not available in this milestone: native savestates do not exist yet. The **Save State** and
**Load Fullauto** buttons are greyed out. The reserved name `dashboard-cache-refresh.gp0` is
the VRAM-refresh token and is never a real file. A copied GPGX `desertbus-fullauto.gp0` is
rejected with `state_incompatible`. When native states land, this section will cover creating
the file on the modified core in game state 3 (`$FF7002 == 3`) with the chosen patches.

## 12. Failure and recovery

| Symptom | Where to look | Bounded recovery |
|---|---|---|
| "cannot reach http://…" | `ping`, `curl …/status` from the dashboard computer | fix the IP or hostname, check that the bridge is running (`start.sh status`) |
| Connection refused | `start.sh status`, `/tmp/megacd-dashboard.log` | `start.sh restart` |
| Hostname not found | `ping mister.local` | use the IP |
| Preflight/CORS or local-network error in the browser console | browser devtools → Network | serve the page as in §9; allow local-network access |
| `core_unavailable` "IPC … not connected" | `ls -l /tmp/megacd-dashboard.sock` | reinstall the matching Main (§5) and reboot |
| `core_unavailable` "not MegaCD" | OSD | load MegaCD_Dashboard |
| `protocol_mismatch` | `/capabilities` | load MegaCD_Dashboard, not the stock RBF; rebuild the pair if the versions differ |
| Readouts look scrambled (byte order) | compare `bus-peek $FF6FEA:2` with the speedometer | report it with the RBF build ID; do not ship |
| Build fails fit or timing | `check_dashboard_reports.py` output | see DASHBOARD-BUILD.md §2; do not relax constraints |
| Slow input release | `load_test.py` press/release p95 | wired Ethernet; stop other polling dashboards |
| Button stays held | `/status` → `held` | `curl …/input -d '{"release":[...]}'`, or `start.sh stop` (sends `SESSION 0`) |
| `state_not_found` / `state_incompatible` | response message | expected in this milestone |
| Bridge died mid-request | log, `start.sh status` | `start.sh restart`; reads have no side effects and writes are not enabled in this milestone |

## 13. Rollback

**MiSTer:**

```bash
/media/fat/megacd-dashboard/start.sh stop
sed -i '/# >>> megacd-dashboard >>>/,/# <<< megacd-dashboard <<</d' /media/fat/linux/user-startup.sh
B=$(ls -d /media/fat/backup-before-dashboard-* | tail -1)
cp $B/MiSTer /media/fat/MiSTer && sha256sum -c $B/SHA256SUMS
sync; reboot
```

Then select the stock MegaCD core and check boot, CD audio, controller, and a backup-RAM save.
`MegaCD_Dashboard.rbf` and `/media/fat/megacd-dashboard/` can stay or be deleted; they are
inert without the custom Main. If Main no longer starts, restore `MiSTer` from the backup
folder by mounting the SD card on another computer.

## 14. Acceptance record (fill in)

| Item | Value |
|---|---|
| Date, tester | |
| RBF / Main / bridge SHA-256 | |
| Fit report: ALMs, M10K, worst setup/hold | |
| `conformance.py` result (non-destructive; expected: NOT CONFORMANT, VRAM unsupported) | |
| `load_test.py` 300 s @ 100 req/s: achieved rate, error %, p95 per kind | |
| Measured press/release latency to pad | |
| Regression: stock boot, CD audio, pad, backup save, second CD title | |
| Rehearsal notes: steps that needed correction | |
| Known limitations | transport milestone (see top) |
