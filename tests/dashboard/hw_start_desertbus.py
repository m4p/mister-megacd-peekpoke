#!/usr/bin/env python3
"""Load a core via an .mgl on the MiSTer and drive into Desert Bus using injected input.

    python3 hw_start_desertbus.py --mgl "/media/fat/_Dashboard/Desert Bus (dashboard).mgl"

Sequence (Penn & Teller's Smoke and Mirrors): boot, START, START to the game
selection, DOWN, DOWN to "Desert Bus", then START until game state ($FF7002) is 3.
Needs the bridge running and a dashboard RBF (input injection).
"""
import argparse
import http.client
import json
import subprocess
import sys
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
    args = ap.parse_args()

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
    for btn, pause in (("start", 3), ("start", 4), ("down", 1.5), ("down", 1.5)):
        tap(btn)
        time.sleep(pause)
    for i in range(15):
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
