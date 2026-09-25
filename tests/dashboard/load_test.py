#!/usr/bin/env python3
"""Load test shaped like the dashboard: telemetry polling plus input traffic.

    python3 load_test.py --base http://mister.local:8765 --rate 100 --concurrency 6 --seconds 300

Default input traffic is releases only (harmless). --with-input BUTTON adds real
press/release pairs of BUTTON (default off; this changes the game).
Reports HTTP latency percentiles per request kind. This is request-to-response
latency measured on the dashboard computer; request-to-pad latency needs the
hardware measurement described in docs/DASHBOARD-DEPLOYMENT.md.
"""
import argparse
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "api-fixtures.json")))


def pct(v, p):
    if not v:
        return float("nan")
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--rate", type=float, default=100)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--with-input", metavar="BUTTON")
    ap.add_argument("--p95-ms", type=float, default=0, help="fail if any kind's p95 exceeds this")
    ap.add_argument("--max-error-rate", type=float, default=0.0)
    args = ap.parse_args()

    u = urllib.parse.urlparse(args.base)
    reads = FIX["telemetry_reads"] + FIX["patch_site_reads"]
    plan = [("read", "/bus-peek", {"bus": "main68k", "address": r["address"], "length": r["length"], "encoding": "hex"})
            for r in reads]
    rel = {"release": FIX["input_buttons"]}
    if args.with_input:
        plan += [("press", "/input", {"press": [args.with_input]}), ("release", "/input", {"release": [args.with_input]})]
    else:
        plan += [("release", "/input", rel)]

    lat = {}
    errors = {}
    lock = threading.Lock()
    interval = args.concurrency / args.rate
    stop_at = time.time() + args.seconds
    inflight = [0]
    max_inflight = [0]

    def worker(k):
        conn = None
        i = k
        next_t = time.time() + k * interval / args.concurrency
        while time.time() < stop_at:
            now = time.time()
            if now < next_t:
                time.sleep(next_t - now)
            next_t += interval
            kind, path, body = plan[i % len(plan)]
            i += 1
            t0 = time.perf_counter()
            with lock:
                inflight[0] += 1
                max_inflight[0] = max(max_inflight[0], inflight[0])
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)
                conn.request("POST", u.path.rstrip("/") + path, json.dumps(body), {"Content-Type": "application/json"})
                r = conn.getresponse()
                data = r.read()
                ok = r.status == 200
                code = None if ok else (json.loads(data).get("error", {}).get("code") if data else f"http_{r.status}")
            except Exception as e:  # noqa: BLE001
                ok, code = False, type(e).__name__
                conn = None
            dt = time.perf_counter() - t0
            with lock:
                inflight[0] -= 1
                lat.setdefault(kind, []).append(dt)
                if not ok:
                    errors[code] = errors.get(code, 0) + 1

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(args.concurrency)]
    t0 = time.time()
    [t.start() for t in ts]
    [t.join() for t in ts]
    dt = time.time() - t0
    if args.with_input:
        try:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)
            conn.request("POST", u.path.rstrip("/") + "/input", json.dumps(rel), {"Content-Type": "application/json"})
            conn.getresponse().read()
        except Exception:  # noqa: BLE001
            pass

    total = sum(len(v) for v in lat.values())
    nerr = sum(errors.values())
    print(f"{total} requests in {dt:.1f}s = {total / dt:.1f} req/s (target {args.rate}), "
          f"concurrency {args.concurrency}, max in flight {max_inflight[0]}")
    fail = False
    for kind, v in sorted(lat.items()):
        p95 = pct(v, 0.95)
        print(f"  {kind:8s} n={len(v):6d}  p50 {pct(v, .5):7.2f} ms  p95 {p95:7.2f} ms  p99 {pct(v, .99):7.2f} ms  max {max(v)*1000:7.2f} ms")
        if args.p95_ms and p95 > args.p95_ms:
            fail = True
    print(f"  errors: {nerr} ({(nerr / total * 100) if total else 0:.2f}%) {errors if errors else ''}")
    if total and nerr / total > args.max_error_rate:
        fail = True
    if total / dt < args.rate * 0.95:
        print("  achieved rate below 95% of target")
        fail = True
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
