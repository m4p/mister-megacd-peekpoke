#!/usr/bin/env python3
"""Load a core via an .mgl on the MiSTer and drive into Desert Bus using injected input.

    python3 hw_start_desertbus.py --mgl "/media/fat/_Dashboard/Desert Bus (dashboard).mgl"

Sequence (Penn & Teller's Smoke and Mirrors): boot, START, START to the game
selection, DOWN, DOWN to "Desert Bus", then START until game state ($FF7002) is 3.
Needs the bridge running and a dashboard RBF (input injection).

With --ref-dir (screenshots menu_top.png and menu_db.png of the game-selection
menu, taken once on your own MiSTer; they are not shipped because they are game
screenshots) the script waits for the menu by comparing screenshots instead of
relying on fixed delays, which vary between a cold boot and a core reload.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time


def api(base_host, base_port, path, body):
    c = http.client.HTTPConnection(base_host, base_port, timeout=10)
    c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.128")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ssh", default="mister")
    ap.add_argument("--mgl", required=True)
    ap.add_argument("--boot-wait", type=float, default=55)
    ap.add_argument("--ref-dir", help="directory with menu_top.png and menu_db.png")
    args = ap.parse_args()

    def screenshot():
        cmd = ('b=$(ls -t /media/fat/screenshots/MegaCD 2>/dev/null | head -1); echo screenshot > /dev/MiSTer_cmd; '
               'for i in $(seq 1 20); do sleep 0.25; n=$(ls -t /media/fat/screenshots/MegaCD 2>/dev/null | head -1); '
               '[ "$n" != "$b" ] && break; done; echo $n')
        f = subprocess.run(["ssh", "-o", "BatchMode=yes", args.ssh, cmd], capture_output=True, text=True).stdout.strip()
        dst = os.path.join(tempfile.gettempdir(), "hw_start_shot.png")
        subprocess.run(["scp", "-q", f"{args.ssh}:/media/fat/screenshots/MegaCD/{f}", dst], check=True)
        return dst

    def matches(ref_name, threshold=0.97):
        from PIL import Image
        a = Image.open(screenshot()).convert("RGB")
        b = Image.open(os.path.join(args.ref_dir, ref_name)).convert("RGB")
        if a.size != b.size:
            return False
        pa, pb = a.tobytes(), b.tobytes()
        same = sum(1 for i in range(0, len(pa), 3) if pa[i:i + 3] == pb[i:i + 3])
        return same / (len(pa) // 3) >= threshold

    def tap(btn, hold=0.15):
        api(args.host, args.port, "/input", {"press": [btn]})
        time.sleep(hold)
        api(args.host, args.port, "/input", {"release": [btn]})

    def state():
        for _ in range(20):
            try:
                st, j = api(args.host, args.port, "/bus-peek", {"bus": "main68k", "address": 0xFF7002, "length": 2})
                if st == 200:
                    return int(j["data"], 16)
            except OSError:
                pass
            time.sleep(0.5)
        return None

    subprocess.run(["ssh", "-o", "BatchMode=yes", args.ssh, f'echo "load_core {args.mgl}" > /dev/MiSTer_cmd'], check=True)
    print(f"loading {args.mgl}; waiting {args.boot_wait:.0f} s for BIOS and intro", flush=True)
    time.sleep(args.boot_wait)
    if args.ref_dir:
        for i in range(30):   # START through intro and title until the menu shows
            if matches("menu_top.png"):
                break
            tap("start")
            time.sleep(3)
        else:
            print("game-selection menu not recognised")
            return 1
        for i in range(4):
            tap("down")
            time.sleep(2)
            if matches("menu_db.png"):
                break
        else:
            print("could not select Desert Bus")
            return 1
    else:
        # the game-selection menu needs several seconds after the second START; DOWN
        # pressed earlier is lost and the top entry (another game, on disc 2) stays selected
        for btn, pause in (("start", 5), ("start", 8), ("down", 2), ("down", 2)):
            tap(btn)
            time.sleep(pause)
    for i in range(20):
        tap("start")
        time.sleep(4)
        s = state()
        print(f"START {i + 1}: game state {s}", flush=True)
        if s == 3:
            print("driving")
            return 0
    print("did not reach the driving state")
    return 1


if __name__ == "__main__":
    sys.exit(main())
