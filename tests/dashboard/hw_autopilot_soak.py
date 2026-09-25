#!/usr/bin/env python3
"""Endurance run: the dashboard's autopilot algorithm against the real MiSTer.

Same control model as dashboard.html (drift to a random limit, one proportional
steering tap toward the far side, START taps to recover after a stall), talking
to the same HTTP API. Use it when no visible browser can run the dashboard's own
autopilot (a hidden tab deliberately stops steering). Needs a speed source: hold
A, or apply the Full Throttle patch first.

    python3 hw_autopilot_soak.py --minutes 60 --log soak.jsonl

Reports per minute: game-state mix, miles driven, taps, API errors, worst
latency, and at the end whether any button was left held.
"""
import argparse
import http.client
import json
import random
import sys
import time

TICK = 0.22
RATE = 0.0009
TAP_MIN, TAP_MAX = 0.070, 0.300
DEADZONE = 0.12


class Api:
    def __init__(self, host, port):
        self.host, self.port = host, port
        self.errors = 0
        self.worst = 0.0
        self.calls = 0

    def post(self, path, body):
        t0 = time.perf_counter()
        try:
            c = http.client.HTTPConnection(self.host, self.port, timeout=5)
            c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
            r = c.getresponse()
            j = json.loads(r.read() or b"{}")
            ok = r.status == 200
        except Exception:  # noqa: BLE001
            ok, j = False, {}
        dt = time.perf_counter() - t0
        self.calls += 1
        self.worst = max(self.worst, dt)
        if not ok:
            self.errors += 1
        return ok, j

    def u(self, addr, n):
        ok, j = self.post("/bus-peek", {"bus": "main68k", "address": addr, "length": n, "encoding": "hex"})
        return int(j["data"], 16) if ok else None

    def tap(self, btn, secs):
        self.post("/input", {"press": [btn]})
        try:
            time.sleep(secs)
        finally:
            self.post("/input", {"release": [btn]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.128")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--log", default="soak.jsonl")
    args = ap.parse_args()
    api = Api(args.host, args.port)
    log = open(args.log, "a")

    mode, drift_limit, target = "idle", 0.4, None
    recovery_at, last_tap = 0.0, 0.0
    end = time.time() + args.minutes * 60
    minute_end = time.time() + 60
    states = {}
    taps = recoveries = 0
    dist0 = api.u(0xFF6FDC, 4) or 0
    legs_dist = 0
    last_dist = dist0

    try:
        while time.time() < end:
            t0 = time.time()
            gs = api.u(0xFF7002, 2)
            lat = api.u(0xFF6FFA, 2)
            dist = api.u(0xFF6FDC, 4)
            if dist is not None:
                if dist >= last_dist:
                    legs_dist += dist - last_dist
                last_dist = dist
            states[gs] = states.get(gs, 0) + 1
            if gs is not None and lat is not None:
                norm = (lat - 0x6C00) / 0x4800
                if gs != 3:
                    if mode != "recovery":
                        mode, recovery_at, target = "recovery", time.time() + 2.0, None
                        api.post("/input", {"release": ["left", "right", "start"]})
                    elif time.time() >= recovery_at and time.time() - last_tap > 0.8:
                        last_tap = time.time()
                        recoveries += 1
                        api.tap("start", 0.12)
                else:
                    if mode != "driving":
                        mode, drift_limit, target = "driving", 0.30 + random.random() * 0.25, None
                    if target is None and abs(norm) >= drift_limit:
                        target = -(1 if norm > 0 else -1) * (0.10 + random.random() * 0.25)
                    if target is not None:
                        err = target - norm
                        if abs(err) < DEADZONE:
                            target, drift_limit = None, 0.30 + random.random() * 0.25
                        else:
                            api.tap("left" if err < 0 else "right", max(TAP_MIN, min(abs(err) / RATE / 1000, TAP_MAX)))
                            taps += 1
            if time.time() >= minute_end:
                rec = {"t": time.strftime("%H:%M:%S"), "states": {str(k): v for k, v in states.items()},
                       "miles": round(legs_dist / 1800, 3), "taps": taps, "recoveries": recoveries,
                       "api_calls": api.calls, "api_errors": api.errors, "worst_ms": round(api.worst * 1000, 1)}
                print(json.dumps(rec), flush=True)
                log.write(json.dumps(rec) + "\n")
                log.flush()
                states, api.worst = {}, 0.0
                minute_end += 60
            time.sleep(max(0.0, TICK - (time.time() - t0)))
    finally:
        api.post("/input", {"release": ["left", "right", "start", "a"]})
        ok, st = api.post("/status", {})
        summary = {"final": True, "miles": round(legs_dist / 1800, 3), "taps": taps, "recoveries": recoveries,
                   "api_calls": api.calls, "api_errors": api.errors, "held_at_end": st.get("held")}
        print(json.dumps(summary), flush=True)
        log.write(json.dumps(summary) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
