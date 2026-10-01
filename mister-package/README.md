# MegaCD dashboard — MiSTer install package

Everything we changed on the MiSTer to run the Desert Bus dashboard (control milestone:
telemetry, input, pause/resume, CPU patches, VRAM access; no savestates), plus scripts and
instructions to install it on a MiSTer.

The files in `sdcard/` are byte-identical to the MiSTer that passed the hardware validation
on 2026-09-26 and the autostart/bootcore test on 2026-09-30 (fetched 2026-10-01).

## Contents

| Path in this folder | Goes to | What it is |
|---|---|---|
| `sdcard/_Dashboard/MegaCD_DashCtl_20260926.rbf` | `/media/fat/_Dashboard/` | the MegaCD core with the dashboard endpoint (built from this repo, revision `MegaCD_Dashboard`) |
| `sdcard/_Dashboard/Desert Bus (control).mgl` | `/media/fat/_Dashboard/` | loads that core and mounts `games/MegaCD/Desert Bus/PTSM1.cue` |
| `sdcard/MiSTer.dashboard-control` | `/media/fat/` then copied to `/media/fat/MiSTer` | Main_MiSTer release 20260912 + `source/0001-megacd-dashboard-ipc.patch` |
| `sdcard/megacd-dashboard/megacd-dashboard` | `/media/fat/megacd-dashboard/` | HTTP bridge (ARM, static), listens on port 8765 |
| `sdcard/megacd-dashboard/start.sh` | `/media/fat/megacd-dashboard/` | start/stop/restart/status for the bridge |
| (edit) `/media/fat/linux/user-startup.sh` | | block that starts the bridge at boot |
| (edit, optional) `/media/fat/MiSTer.ini` | | `bootcore=Desert Bus (control).mgl`, `bootcore_timeout` commented out |
| `dashboard/dashboard.html` | the dashboard computer | the dashboard page (from Genesis-Plus-GX/sdl) |
| `source/0001-megacd-dashboard-ipc.patch` | | the Main patch, for reference or rebuilding |
| `install.sh`, `uninstall.sh` | | install/remove over SSH from your computer |
| `fetch-binaries.sh` | | refills `sdcard/` from a MiSTer or the build VM |
| `SHA256SUMS` | | checksums of everything in `sdcard/` |

Sha256 of the validated binaries:

```
3f8107d5ffad1bab82a630ba83c4e5955e40110028c91a0f49b848915ba98274  MiSTer.dashboard-control
d9b45446f81c1ecd5490ea02d96bde8b5623dbc3a29ee3d1b39802c4412d4dba  MegaCD_DashCtl_20260926.rbf
693456cd4aaca6f4d0f1cbd91dcf2848a0ec4a0f5df714b523d7ae65f57db281  megacd-dashboard
```

The binaries are not tracked in git (`.gitignore`). On a fresh checkout run
`./fetch-binaries.sh mister root@<mister-ip>` (or `vm map@<vm-ip>` for the build outputs).

## Requirements

- MiSTer with the MegaCD BIOS installed as for the stock core, and Desert Bus at
  `/media/fat/games/MegaCD/Desert Bus/PTSM1.cue`. If your image lives elsewhere, edit the
  `path=` in `sdcard/_Dashboard/Desert Bus (control).mgl` before installing.
- SSH access as root with a key (`ssh-copy-id root@<mister-ip>`; default password `1`).
- On your computer: `ssh`, `tar`, `python3`, `shasum`.

## Install (scripted)

```bash
./install.sh root@<mister-ip> --bootcore --reboot
```

Leave out `--bootcore` to keep the normal menu at boot (then start the game from
**_Dashboard → Desert Bus (control)**). The script:

1. verifies `SHA256SUMS`;
2. backs up `MiSTer`, `MiSTer.ini` and `linux/user-startup.sh` to
   `/media/fat/backup-before-dashboard-<date>/` (an existing backup folder is kept, never
   overwritten);
3. copies `sdcard/` onto `/media/fat`;
4. makes the dashboard Main active (`MiSTer.dashboard-control` → `MiSTer`);
5. adds the autostart block to `linux/user-startup.sh` and starts the bridge;
6. with `--bootcore`, edits `MiSTer.ini` (keeps its CRLF line endings).

It is safe to run again; nothing is added twice.

## Install (by hand, e.g. with the SD card in a card reader)

1. Back up `MiSTer`, `MiSTer.ini` and `linux/user-startup.sh` from the card.
2. Copy the contents of `sdcard/` to the root of the card (merging folders).
3. Copy `MiSTer.dashboard-control` over `MiSTer` on the card root.
4. Append to `linux/user-startup.sh` (create it from `linux/_user-startup.sh` if missing):

   ```sh
   # >>> megacd-dashboard >>>
   [ -x /media/fat/megacd-dashboard/start.sh ] && /media/fat/megacd-dashboard/start.sh "$1"
   # <<< megacd-dashboard <<<
   ```

5. Optional, in the `[MiSTer]` section of `MiSTer.ini` (use an editor that keeps CRLF):

   ```ini
   bootcore=Desert Bus (control).mgl
   ;bootcore_timeout=10
   ```

6. Put the card back and boot.

## Check it works

```bash
curl -s http://<mister-ip>:8765/capabilities
curl -s http://<mister-ip>:8765/status
```

With the dashboard core loaded, `/capabilities` lists the features (input, work-RAM read/write,
VRAM read/write, freeze). On the MiSTer, `/media/fat/megacd-dashboard/start.sh status` shows
the bridge, and its log is `/tmp/megacd-dashboard.log`.

The game boots to the BIOS/title. To reach the bus: START until the game selection, choose
Desert Bus (DOWN, DOWN), then START until the bus screen, or run
`tests/dashboard/hw_start_desertbus.py --mgl "/media/fat/_Dashboard/Desert Bus (control).mgl"`.

## Use the dashboard

On the dashboard computer:

```bash
cd dashboard && python3 -m http.server 8000
```

Open http://localhost:8000/dashboard.html and enter `http://<mister-ip>:8765` as the server.
Save State / Load Fullauto stay greyed out (not supported on the MiSTer).

## Caveats

- **MiSTer updater:** an update replaces `/media/fat/MiSTer` with stock Main, which disables the
  bridge connection ("dashboard IPC to Main_MiSTer is not connected"). Re-run `./install.sh`
  (or copy `MiSTer.dashboard-control` over `MiSTer`) after updating, as long as the new
  stock release does not need a newer Main.
- The bridge has no authentication; keep port 8765 on a trusted LAN.

## Uninstall

```bash
./uninstall.sh root@<mister-ip> --reboot          # add --purge to delete the files too
```

This stops the bridge, removes the autostart block, restores `MiSTer` from the backup folder
and comments out the `bootcore=` line. `bootcore_timeout` stays commented; re-enable it by hand
if you used it before. Without `--purge` the core, `.mgl` and bridge stay on the card but are
inert. If Main does not start at all, put the card in a computer and copy
`backup-before-dashboard-*/MiSTer` back to the card root.

## Rebuilding from source

See `docs/DASHBOARD-BUILD.md` (core: Quartus 17.0, revision `MegaCD_Dashboard`; Main: apply
`patches/main_mister/` to Main_MiSTer 20260912; bridge: `linux/dashboard-bridge`, ARM
toolchain from `scripts/setup_arm_toolchain.sh`) and `docs/DASHBOARD-DEPLOYMENT.md` for the
full deployment and recovery notes.
