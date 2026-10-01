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
    args = ap.parse_args()
    tmp = tempfile.mkdtemp()

    def start_bridge():
        return subprocess.Popen([args.bridge, "--mock=full", "--listen", f"127.0.0.1:{BRIDGE_PORT}",
                                 "--lock", f"{tmp}/bridge.lock", "--socket", f"{tmp}/sock", "--state-dir", tmp],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    influx = http.server.ThreadingHTTPServer(("127.0.0.1", INFLUX_PORT), FakeInflux)
    threading.Thread(target=influx.serve_forever, daemon=True).start()
    bridge = start_bridge()
    hub_log = open(f"{tmp}/hub.log", "w")
    hubp = subprocess.Popen([sys.executable, os.path.join(HERE, "dashboard_hub.py"),
                             "--mister", f"http://127.0.0.1:{BRIDGE_PORT}", "--listen", f"127.0.0.1:{HUB_PORT}",
                             "--interval", "0.1", "--influx-url", f"http://127.0.0.1:{INFLUX_PORT}",
                             "--influx-org", "home", "--influx-bucket", "desertbus", "--influx-token", "secret",
                             "--influx-interval", "0.2", "--influx-flush", "0.3"],
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
        hub_log.close()
        print("--- hub log (tail):")
        print("".join(open(f"{tmp}/hub.log").readlines()[-12:]))
    print("ALL PASS" if not failures else f"{len(failures)} FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
