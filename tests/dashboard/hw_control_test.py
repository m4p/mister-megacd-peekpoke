#!/usr/bin/env python3
"""Hardware test of the control milestone on a real MiSTer (pause, CPU patches, VRAM).

Needs Desert Bus in the driving state (game state 3) on the MegaCD_Dashboard core,
the bridge on the MiSTer, and SSH access to it for screenshots:

    python3 hw_control_test.py --base http://192.168.1.128:8765 --ssh mister --out /tmp/hw

It holds A (accelerate) while it runs, restores every patch it applies, and releases
all buttons at the end. Features the core does not advertise are skipped.
Screenshots are saved to --out for review (the air freshener has to be looked at).
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "api-fixtures.json")))

results = []


def rec(ok, name, detail=""):
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(("  ok   " if ok else "  FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    return ok


class Api:
    def __init__(self, base):
        u = urllib.parse.urlparse(base)
        self.host, self.port = u.hostname, u.port or 80

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection(self.host, self.port, timeout=15)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Content-Type": "application/json"} if body is not None else {})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def post(self, path, body=None):
        return self.call("POST", path, body)

    def peek(self, addr, n):
        st, j = self.post("/bus-peek", {"bus": "main68k", "address": addr, "length": n, "encoding": "hex"})
        if st != 200:
            raise RuntimeError(f"bus-peek ${addr:06X}: {st} {j}")
        return j["data"]

    def poke(self, addr, hexdata):
        return self.post("/bus-poke", {"bus": "main68k", "address": addr, "data": hexdata, "encoding": "hex", "unsafe": True})

    def u16(self, addr):
        return int(self.peek(addr, 2), 16)

    def u32(self, addr):
        return int(self.peek(addr, 4), 16)


class Screen:
    def __init__(self, ssh, out):
        self.ssh, self.out = ssh, out
        os.makedirs(out, exist_ok=True)

    def shot(self, name):
        cmd = ('b=$(ls -t /media/fat/screenshots/MegaCD 2>/dev/null | head -1); echo screenshot > /dev/MiSTer_cmd; '
               'for i in $(seq 1 20); do sleep 0.25; n=$(ls -t /media/fat/screenshots/MegaCD 2>/dev/null | head -1); '
               '[ "$n" != "$b" ] && break; done; echo $n')
        f = subprocess.run(["ssh", "-o", "BatchMode=yes", self.ssh, cmd], capture_output=True, text=True, timeout=30).stdout.strip()
        dst = os.path.join(self.out, name + ".png")
        subprocess.run(["scp", "-q", f"{self.ssh}:/media/fat/screenshots/MegaCD/{f}", dst], check=True, timeout=30)
        return dst


def same_image(a, b):
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    return ia.size == ib.size and ia.tobytes() == ib.tobytes()


DIST, SPEED, STEER, STATE = 0xFF6FDC, 0xFF6FEA, 0xFF6FFA, 0xFF7002


def test_pause(api, scr):
    print("pause / resume")
    st, j = api.post("/pause")
    if not rec(st == 200 and j.get("paused") is True, "pause", str(j)):
        return
    time.sleep(0.3)
    d0 = api.u32(DIST)
    a = scr.shot("pause-1")
    time.sleep(2.0)
    b = scr.shot("pause-2")
    d1 = api.u32(DIST)
    rec(d1 == d0, "distance counter frozen for 2 s", f"{d0} -> {d1}")
    rec(same_image(a, b), "picture frozen: two screenshots 2 s apart are identical")
    st, j = api.call("GET", "/status")
    rec(j.get("paused") is True, "/status paused")
    rec(api.post("/pause")[0] == 200, "pause is idempotent")
    st, j = api.post("/resume")
    rec(st == 200 and j.get("paused") is False, "resume", str(j))
    time.sleep(2.0)
    d2 = api.u32(DIST)
    c = scr.shot("resume-1")
    rec(d2 > d1, "distance counter runs again after resume", f"{d1} -> {d2}")
    rec(not same_image(b, c), "picture moves again after resume")
    rec(api.post("/resume")[0] == 200, "resume is idempotent")


def patch(name):
    return next(p for p in FIX["patches"] if p["id"] == name)


def apply_state(api, p, key):
    s = next(x for x in p["states"] if x["key"] == key)
    ok = True
    for site, hx in zip(p["sites"], s["hex"]):
        st, j = api.poke(site["address"], hx)
        ok = ok and st == 200
    return ok, s


def test_patches(api):
    print("CPU patches")
    # Full Throttle: speed goes to $FFFF while applied
    thr = patch("throttle")
    orig = [api.peek(s["address"], s["length"]) for s in thr["sites"]]
    ok, s = apply_state(api, thr, "max")
    rec(ok, "Full Throttle Max applied (10 + 2 bytes of code/data)")
    rec([api.peek(x["address"], x["length"]) for x in thr["sites"]] == s["hex"], "throttle patch reads back")
    time.sleep(1.5)
    spd = api.u16(SPEED)
    rec(spd == 0xFFFF, "the running game executes the patch: speed = $FFFF", f"${spd:04X}")
    for site, hx in zip(thr["sites"], orig):
        api.poke(site["address"], hx)
    rec([api.peek(x["address"], x["length"]) for x in thr["sites"]] == orig, "throttle restored")
    time.sleep(1.5)
    spd = api.u16(SPEED)
    rec(spd <= 0x6000, "speed back within the stock cap", f"${spd:04X}")

    # Steering drift None: the per-frame drift stops (no steering input is held)
    drift = patch("steering")
    orig = [api.peek(s["address"], s["length"]) for s in drift["sites"]]
    ok, s = apply_state(api, drift, "off")
    rec(ok, "Steering Drift None applied (8 bytes of code)")
    time.sleep(0.5)
    a = api.u16(STEER)
    time.sleep(2.0)
    b = api.u16(STEER)
    rec(a == b, "steering stops drifting while patched", f"${a:04X} -> ${b:04X}")
    for site, hx in zip(drift["sites"], orig):
        api.poke(site["address"], hx)
    rec([api.peek(x["address"], x["length"]) for x in drift["sites"]] == orig, "drift restored")
    time.sleep(2.0)
    c = api.u16(STEER)
    rec(c != b, "steering drifts again after restore", f"${b:04X} -> ${c:04X}")


def test_vram(api, scr):
    print("VRAM (air freshener)")
    v = FIX["vram"][0]
    fresh = patch("freshener")
    st, j = api.post("/peek", {"domain": "vram", "address": v["address"], "length": 1056, "encoding": "hex"})
    if not rec(st == 200, "read the current air-freshener tiles"):
        return
    orig = j["data"]
    on = next(s for s in fresh["states"] if s["key"] == "on")
    st, j = api.post("/poke", {"domain": "vram", "address": v["address"], "data": on["data"], "encoding": "hex"})
    rec(st == 200, "upload FRESHY tiles (1056 bytes)", str(j))
    for ep in ("/state/save", "/state/load"):
        rec(api.post(ep, {"path": "dashboard-cache-refresh.gp0"})[0] == 200, f"refresh token {ep}")
    st, j = api.post("/peek", {"domain": "vram", "address": v["sig_address"], "length": v["sig_length"], "encoding": "hex"})
    rec(j.get("data") == on["sig"], "badge signature reads FRESHY")
    time.sleep(0.5)
    shot = scr.shot("air-freshener-freshy")
    rec(True, "screenshot saved for visual review", shot)
    st, j = api.post("/poke", {"domain": "vram", "address": v["address"], "data": orig, "encoding": "hex"})
    rec(st == 200, "original tiles restored")
    st, j = api.post("/peek", {"domain": "vram", "address": v["address"], "length": 1056, "encoding": "hex"})
    rec(j.get("data") == orig, "restore reads back")
    time.sleep(0.5)
    scr.shot("air-freshener-restored")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://192.168.1.128:8765")
    ap.add_argument("--ssh", default="mister")
    ap.add_argument("--out", default="/tmp/megacd-hw-test")
    ap.add_argument("--json")
    args = ap.parse_args()
    api, scr = Api(args.base), Screen(args.ssh, args.out)

    st, caps = api.call("GET", "/capabilities")
    print("core:", caps.get("core"))
    f = caps.get("features", {})
    if api.u16(STATE) != 3:
        print("Desert Bus is not in the driving state (game state 3); start driving first.")
        sys.exit(2)
    api.post("/input", {"press": ["a"]})   # accelerate so the game is moving
    time.sleep(2.0)
    try:
        if f.get("pause"):
            test_pause(api, scr)
        else:
            print("pause: not advertised, skipped")
        if f.get("bus_poke"):
            test_patches(api)
        else:
            print("CPU patches: not advertised, skipped")
        if f.get("vram_poke"):
            test_vram(api, scr)
        else:
            print("VRAM: not advertised, skipped")
    finally:
        api.post("/resume") if f.get("pause") else None
        api.post("/input", {"release": ["a", "left", "right", "start"]})

    bad = [r for r in results if r[0] == "FAIL"]
    if args.json:
        json.dump([dict(zip(("status", "item", "detail"), r)) for r in results], open(args.json, "w"), indent=1)
    print(f"{'PASS' if not bad else 'FAILED'}: {len(results)} hardware checks, {len(bad)} failures; screenshots in {args.out}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
