#!/usr/bin/env python3
"""End-to-end test of dashboard_hub.py against the bridge in --mock mode and a fake InfluxDB.

    python3 test_hub.py [--bridge ../../linux/dashboard-bridge/build/megacd-dashboard]

Checks caching, write-through, input, reconnect after the bridge restarts, the request
rate bound with many clients, InfluxDB output, and button release on shutdown.
"""
import argparse
import http.client
import http.server
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_PORT, HUB_PORT, INFLUX_PORT = 18765, 18766, 18086
influx_lines = []
hooks = []
HOOK_PORT = 18090
failures = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


class FakeInflux(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        influx_lines.extend(body.splitlines())
        FakeInflux.last_auth = self.headers.get("Authorization")
        FakeInflux.last_path = self.path
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


class FakeHook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        hooks.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def call(port, method, path, body=None, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request(method, path, None if body is None else json.dumps(body), {"Content-Type": "application/json"})
    r = c.getresponse()
    data = r.read()
    try:
        return r.status, json.loads(data)
    except ValueError:
        return r.status, data


def hub(method, path, body=None):
    return call(HUB_PORT, method, path, body)


def wait_for(pred, timeout=15, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except OSError:
            pass
        time.sleep(step)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bridge", default=os.path.join(HERE, "../../linux/dashboard-bridge/build/megacd-dashboard"))
    ap.add_argument("--dashboard", default=os.path.join(HERE, "../../mister-package/dashboard/dashboard.html"))
    args = ap.parse_args()
    tmp = tempfile.mkdtemp()

    def start_bridge():
        return subprocess.Popen([args.bridge, "--mock=full", "--listen", f"127.0.0.1:{BRIDGE_PORT}",
                                 "--lock", f"{tmp}/bridge.lock", "--socket", f"{tmp}/sock", "--state-dir", tmp],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    influx = http.server.ThreadingHTTPServer(("127.0.0.1", INFLUX_PORT), FakeInflux)
    threading.Thread(target=influx.serve_forever, daemon=True).start()
    hookd = http.server.ThreadingHTTPServer(("127.0.0.1", HOOK_PORT), FakeHook)
    threading.Thread(target=hookd.serve_forever, daemon=True).start()
    bridge = start_bridge()
    hub_log = open(f"{tmp}/hub.log", "w")
    hubp = subprocess.Popen([sys.executable, os.path.join(HERE, "dashboard_hub.py"),
                             "--mister", f"http://127.0.0.1:{BRIDGE_PORT}", "--listen", f"127.0.0.1:{HUB_PORT}",
                             "--interval", "0.1", "--influx-url", f"http://127.0.0.1:{INFLUX_PORT}",
                             "--influx-org", "home", "--influx-bucket", "desertbus", "--influx-token", "secret",
                             "--influx-interval", "0.2", "--influx-flush", "0.3",
                             "--webhook", f"crash=http://127.0.0.1:{HOOK_PORT}/crash",
                             "--webhook", f"*=http://127.0.0.1:{HOOK_PORT}/all",
                             "--dashboard", args.dashboard, "--state-file", f"{tmp}/state.json"],
                            stdout=hub_log, stderr=subprocess.STDOUT)
    try:
        check(wait_for(lambda: hub("GET", "/hub")[1]["upstream"]["connected"]), "hub connects to the bridge")
        st, caps = hub("GET", "/capabilities")
        check(st == 200 and caps.get("features", {}).get("bus_peek") and caps.get("hub"), "capabilities passed through with hub info")

        # telemetry read is served from the cache
        time.sleep(0.5)
        before = hub("GET", "/hub")[1]["requests"]
        for _ in range(20):
            st, j = hub("POST", "/bus-peek", {"bus": "main68k", "address": 0xFF6FEA, "length": 2, "encoding": "hex"})
        after = hub("GET", "/hub")[1]["requests"]
        check(st == 200 and len(j["data"]) == 4, "telemetry bus-peek answered")
        check(after.get("cache_hits", 0) - before.get("cache_hits", 0) >= 20, "20 telemetry reads all served from cache")
        direct = call(BRIDGE_PORT, "POST", "/bus-peek", {"bus": "main68k", "address": 0xFF6FEA, "length": 2})[1]
        check(direct["data"] == j["data"], "cached value equals the bridge's value")

        # other ranges: first forwarded, then watched and cached
        req = {"bus": "main68k", "address": 0xFF842C, "length": 8, "encoding": "hex"}
        hub("POST", "/bus-peek", req)
        time.sleep(1.2)
        b = hub("GET", "/hub")[1]["requests"].get("cache_hits", 0)
        hub("POST", "/bus-peek", req)
        check(hub("GET", "/hub")[1]["requests"].get("cache_hits", 0) == b + 1, "demand range is watched and then cached")

        # write-through: poke via hub, read back immediately via hub
        st, _ = hub("POST", "/bus-poke", {"bus": "main68k", "address": 0xFF842C, "data": "0123456789abcdef", "encoding": "hex", "unsafe": True})
        st2, j = hub("POST", "/bus-peek", req)
        check(st == 200 and j.get("data") == "0123456789abcdef", "write is visible to the next read")

        # VRAM read through the hub
        st, j = hub("POST", "/peek", {"domain": "vram", "address": 0x26E0, "length": 64, "encoding": "hex"})
        check(st == 200 and len(j.get("data", "")) == 128, "VRAM peek answered")

        # bridge errors are passed through unchanged
        st, j = hub("POST", "/bus-peek", {"bus": "main68k", "address": 0xFF0000, "length": 500})
        check(st == 400 and j["error"]["code"] == "invalid_range", "bridge validation errors pass through")

        # input and status
        hub("POST", "/input", {"press": ["left"]})
        st, s = hub("GET", "/status")
        check("left" in s.get("held", []), "press is visible in /status")
        hub("POST", "/input", {"release": ["left"]})
        check("left" not in hub("GET", "/status")[1].get("held", []), "release is visible in /status")

        # events: driving, then an off-road stall -> crash event, webhooks, /hub/events
        def poke(addr, hexdata):
            return hub("POST", "/bus-poke", {"bus": "main68k", "address": addr, "data": hexdata, "encoding": "hex", "unsafe": True})
        poke(0xFF7108, "0a0f030b0f000000")
        poke(0xFF7002, "0003")
        time.sleep(2.5)                       # driving samples, slow reads (name) done
        poke(0xFF7002, "0001")
        check(wait_for(lambda: len(hooks) >= 2, 5), "crash webhook delivered to both routes")
        paths = sorted(p for p, _ in hooks)
        check(paths == ["/all", "/crash"] and hooks[0][1]["event"] == "crash", f"webhook routes and payload ({paths})")
        st, ev = hub("GET", "/hub/events?since=0")
        check(st == 200 and [e["event"] for e in ev["events"]] == ["crash"], "crash listed in /hub/events")
        check(ev["events"][0]["telemetry"]["driver_name"] == "JOCKO", "event carries telemetry (driver name)")
        st, ev2 = hub("GET", f"/hub/events?since={ev['last_id']}")
        check(ev2["events"] == [], "since= returns only newer events")
        st, t = hub("GET", "/hub/telemetry")
        check({"next_stop_miles", "next_stop_eta_s", "driver_name", "stop_active"} <= set(t["telemetry"]), "telemetry has next stop and driver fields")
        poke(0xFF7002, "0003")

        # actions: every dashboard button as POST /hub/action/...
        def peek(addr, n):
            return call(BRIDGE_PORT, "POST", "/bus-peek", {"bus": "main68k", "address": addr, "length": n})[1]["data"]
        st, acts = hub("GET", "/hub/actions")
        check(st == 200 and len(acts["patches"]) == 10 and "autopilot" in acts["actions"], f"/hub/actions lists actions and {len(acts.get('patches', []))} patches")
        st, j = hub("POST", "/hub/action/patch/steering/off", {})
        check(st == 200 and peek(0xFF842C, 8) == "4e714e714e714e71", "patch via URL form writes the site")
        st, j = hub("POST", "/hub/action/patch", {"id": "throttle", "state": "max"})
        check(st == 200 and peek(0xFF8498, 10) == "33fcffff00ff6fea4e75" and peek(0xFFC004, 2) == "1400", "multi-site patch writes every site")
        check(hub("GET", "/status")[1]["paused"] is False, "multi-site patch resumes after its own pause")
        st, ps = hub("GET", "/hub/patches")
        check(st == 200 and ps["patches"]["steering"] == "off" and ps["patches"]["throttle"] == "max", f"/hub/patches reports states ({ps.get('patches')})")
        st, j = hub("POST", "/hub/action/patch/freshener/on", {})
        check(st == 200, f"VRAM patch (air freshener) applied ({st} {j})")
        check(hub("GET", "/hub/patches")[1]["patches"]["freshener"] == "on", "VRAM patch status matches its signature")
        check(hub("POST", "/hub/action/pause", {})[0] == 200 and hub("GET", "/status")[1]["paused"] is True, "pause action")
        check(hub("POST", "/hub/action/toggle-pause", {})[0] == 200, "toggle-pause action")
        time.sleep(0.6)
        check(hub("GET", "/status")[1]["paused"] is False, "toggle-pause resumed")
        st, j = hub("POST", "/hub/action/save-state", {"path": "hubtest.gp0"})
        print(f"     save-state on the mock: HTTP {st} {j.get('error', {}).get('code', 'ok')}")
        check(st in (200, 501), "save-state forwarded (501 when the server has no savestates)")
        poke(0xFF7002, "0003")
        poke(0xFF6FFA, "2800")                 # far left: the autopilot must steer right
        st, j = hub("POST", "/hub/action/autopilot/on", {})
        check(st == 200 and j["autopilot"] == "on", "autopilot on")
        check(wait_for(lambda: "easing right" in hub("GET", "/hub/events?since=999999")[1]["autopilot"]["status"], 5),
              "hub autopilot steers toward the road centre")
        st, j = hub("POST", "/hub/action/autopilot", {"state": "off"})
        time.sleep(0.5)
        check(j["autopilot"] == "off" and hub("GET", "/status")[1]["held"] == [], "autopilot off releases the wheel")
        check(hub("POST", "/hub/action/patch/steering/nope", {})[0] == 404, "unknown patch state -> 404")
        check(hub("POST", "/hub/action/jump", {})[0] == 404, "unknown action -> 404")
        check(hub("GET", "/hub/action/pause")[0] == 405, "actions need POST")
        hub("POST", "/hub/action/patch/throttle/off", {})

        # honk: horn messages (cmd 0x11, sound 4) appended to the Sub-CPU command ring
        def ring_push(wptr, hexmsgs):
            poke(wptr, hexmsgs)
            new = wptr + len(hexmsgs) // 2
            poke(0xFF0938, f"{new:08x}")
            return new
        poke(0xFF7002, "0003")
        poke(0xFF0938, "00ff0ac8")
        time.sleep(0.6)                                    # hub learns the pointer
        st, ev = hub("GET", "/hub/events?since=0"); last = ev["last_id"]
        w = ring_push(0xFF0AC8, "11020004" "11020001" "11020004")
        check(wait_for(lambda: [e["event"] for e in hub("GET", f"/hub/events?since={last}")[1]["events"]] == ["honk", "honk"], 3),
              "two horn messages -> two honk events (other sound ignored)")
        check(wait_for(lambda: sum(1 for _, b in hooks if b.get("event") == "honk") >= 2, 3), "honk webhooks delivered")
        st, ev = hub("GET", "/hub/events?since=0"); last = ev["last_id"]
        w = ring_push(0xFF0B38, "11020004")                # wraps: 2 bytes at the end, 2 at the start
        poke(0xFF093C, "0004")
        poke(0xFF0938, "00ff093e")
        check(wait_for(lambda: [e["event"] for e in hub("GET", f"/hub/events?since={last}")[1]["events"]] == ["honk"], 3),
              "honk across the ring wrap")
        poke(0xFF7002, "0000")
        time.sleep(0.4)
        st, ev = hub("GET", "/hub/events?since=0"); last = ev["last_id"]
        ring_push(0xFF093E, "11020004")
        time.sleep(1.0)
        check(hub("GET", f"/hub/events?since={last}")[1]["events"] == [], "horn sound outside driving is not a honk")
        poke(0xFF7002, "0003")

        # mirror chroma key: VRAM patch over the bridge's 2048-byte limit, kept applied by the hub
        import re as _re
        page = open(args.dashboard).read()
        mirror = next(p for p in json.loads(_re.search(r'id="patchData">\n(.*?)\n</script>', page, _re.S).group(1)) if p["id"] == "mirror")
        sig_addr = int(mirror["vram"]["address"], 16) + mirror["vram"]["sig"]["offset"]
        def vram_sig():
            return call(BRIDGE_PORT, "POST", "/peek", {"domain": "vram", "address": sig_addr, "length": 32})[1]["data"]
        poke(0xFF7002, "0000")                              # not driving: only recorded
        time.sleep(0.6)
        before = vram_sig()
        st, j = hub("POST", "/hub/action/patch/mirror/key", {})
        check(st == 200 and j.get("pending") is True and vram_sig() == before, "outside driving the key is only recorded")
        poke(0xFF7002, "0003")
        check(wait_for(lambda: vram_sig() == mirror["states"][1]["sig"], 6), "written once the bus is driving")
        st, j = hub("POST", "/hub/action/patch/mirror/key", {})
        check(st == 200 and j.get("reapply") is True, f"mirror key applied and kept ({st} {j})")
        check(vram_sig() == mirror["states"][1]["sig"], "mirror tiles written (2400 bytes in pieces)")
        check(json.load(open(f"{tmp}/state.json")) == {"kept_applied": {"mirror": "key"}}, "enabled key saved to the state file")
        stock = mirror["states"][0]["data"]
        for off in range(0, len(stock), 4096):             # the game reloads the stock mirror
            call(BRIDGE_PORT, "POST", "/poke", {"domain": "vram", "address": int(mirror["vram"]["address"], 16) + off // 2, "data": stock[off:off + 4096]})
        check(vram_sig() == mirror["states"][0]["sig"], "stock mirror reloaded behind the hub's back")
        check(wait_for(lambda: vram_sig() == mirror["states"][1]["sig"], 6), "hub re-applies the chroma key")
        st, j = hub("POST", "/hub/action/patch/mirror/off", {})
        check(st == 200 and j.get("reapply") is False and hub("GET", "/hub/actions")[1]["kept_applied"] == {}, "Stock ends re-applying")
        check(json.load(open(f"{tmp}/state.json")) == {"kept_applied": {}}, "Stock removed from the state file")

        # many clients: upstream rate stays bounded
        t0 = time.time()
        up0 = hub("GET", "/hub")[1]["upstream"]["requests"]
        n = [0]

        def hammer():
            for _ in range(50):
                hub("POST", "/bus-peek", {"bus": "main68k", "address": 0xFF6FDC, "length": 4})
                hub("GET", "/status")
                n[0] += 2
        ts = [threading.Thread(target=hammer) for _ in range(10)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        dt = time.time() - t0
        ups = hub("GET", "/hub")[1]["upstream"]["requests"] - up0
        print(f"     {n[0]} client requests in {dt:.1f} s -> {ups} upstream requests ({ups / dt:.0f}/s)")
        check(ups < n[0] / 4, "10 concurrent clients do not multiply upstream traffic")

        # reconnect
        bridge.terminate()
        bridge.wait()
        check(wait_for(lambda: not hub("GET", "/hub")[1]["upstream"]["connected"], 10), "hub notices the bridge is gone")
        st, j = hub("POST", "/input", {"press": ["start"]})
        check(st == 503 and j["error"]["code"] == "upstream_unavailable", "clients get 503 upstream_unavailable while down")
        st, j = hub("GET", "/capabilities")
        check(st == 503, "capabilities answer 503 while down")
        time.sleep(3)
        bridge = start_bridge()
        check(wait_for(lambda: hub("GET", "/hub")[1]["upstream"]["connected"], 20), "hub reconnects after the bridge restarts")
        st, j = hub("POST", "/bus-peek", {"bus": "main68k", "address": 0xFF7002, "length": 2})
        check(st == 200, "reads work again after reconnect")

        # influx
        check(wait_for(lambda: any(l.startswith("desertbus,") for l in influx_lines), 5), "telemetry points written to InfluxDB")
        check(any(l.startswith("dashboard_hub,") and "connected=false" in l for l in influx_lines),
              "hub health records the disconnect")
        check(any(l.startswith("desertbus_event,") and "event=crash" in l for l in influx_lines), "crash event written to InfluxDB")
        check(FakeInflux.last_auth == "Token secret" and "bucket=desertbus" in FakeInflux.last_path, "InfluxDB v2 auth and bucket")
        sample = next(l for l in influx_lines if l.startswith("desertbus,"))
        print("     sample:", sample[:160], "...")

        # shutdown releases buttons
        hub("POST", "/input", {"press": ["right"]})
        hubp.send_signal(signal.SIGTERM)
        hubp.wait(timeout=20)
        held = call(BRIDGE_PORT, "GET", "/status")[1].get("held", [])
        check(held == [], f"buttons released on hub shutdown (held={held})")
    finally:
        for p in (hubp, bridge):
            if p.poll() is None:
                p.kill()
        influx.shutdown()
        hookd.shutdown()
        hub_log.close()
        print("--- hub log (tail):")
        print("".join(open(f"{tmp}/hub.log").readlines()[-12:]))
    print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
