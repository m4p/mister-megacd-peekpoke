#!/usr/bin/env python3
"""HTTP-level tests for megacd-dashboard running against its mock core.

Usage: python3 tests/http_tests.py --bin build/megacd-dashboard
"""
import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "Content-Type",
}

failures = 0
checks = 0


def check(cond, msg):
    global failures, checks
    checks += 1
    if not cond:
        failures += 1
        print("FAIL:", msg)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Bridge:
    def __init__(self, binary, mode):
        self.port = free_port()
        self.tmp = tempfile.mkdtemp(prefix="mcddash-http-")
        self.lock = os.path.join(self.tmp, "lock")
        self.proc = subprocess.Popen(
            [binary, "--listen", f"127.0.0.1:{self.port}", f"--mock={mode}",
             "--state-dir", self.tmp, "--lock", self.lock],
            stderr=subprocess.PIPE, text=True)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("bridge did not start")

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def stop(self):
        self.proc.send_signal(signal.SIGTERM)
        try:
            rc = self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            rc = None
        return rc


def post(c, path, body=None, headers=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    c.request("POST", path, body=json.dumps(body) if isinstance(body, dict) else body, headers=h)
    r = c.getresponse()
    data = r.read()
    return r, (json.loads(data) if data else None)


def raw(port, payload, read=True):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(payload)
    out = b""
    if read:
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
                if b"\r\n\r\n" in out:
                    head, _, rest = out.partition(b"\r\n\r\n")
                    cl = [l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")]
                    if cl and len(rest) >= int(cl[0].split(b":")[1]):
                        break
        except socket.timeout:
            pass
    s.close()
    return out


def run(binary):
    b = Bridge(binary, "hw")
    try:
        c = b.conn()
        # preflight
        c.request("OPTIONS", "/bus-peek", headers={
            "Origin": "null", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        r = c.getresponse(); r.read()
        check(r.status == 204, f"OPTIONS status {r.status}")
        for k, v in CORS.items():
            check(r.getheader(k) == v, f"OPTIONS header {k}={r.getheader(k)}")

        # success + CORS, keep-alive on the same connection
        for _ in range(3):
            r, j = post(c, "/bus-peek", {"bus": "main68k", "address": 0xFF6FEA, "length": 2, "encoding": "hex"})
            check(r.status == 200 and j["ok"] is True and len(j["data"]) == 4, f"bus-peek {r.status} {j}")
            check(r.getheader("access-control-allow-origin") == "*", "CORS on success")
            check(r.getheader("content-type") == "application/json", "json content type")

        # structured error + CORS on errors
        r, j = post(c, "/bus-peek", {"bus": "main68k", "address": 1, "length": 2})
        check(r.status == 400 and j["ok"] is False and j["error"]["code"] == "invalid_range", f"error {j}")
        check(r.getheader("access-control-allow-origin") == "*", "CORS on error")

        # /pause and /resume with no body and no Content-Type
        for p in ("/pause", "/resume"):
            c.request("POST", p)
            r = c.getresponse(); j = json.loads(r.read())
            check(r.status == 501 and j["error"]["code"] == "feature_unavailable", f"{p} {r.status} {j}")

        r, j = post(c, "/input", {"press": ["left"]})
        check(r.status == 200 and j["held"] == ["left"], f"input {j}")
        r, j = post(c, "/input", {"release": ["left", "right", "start"]})
        check(j["held"] == [], "release")

        c.request("GET", "/capabilities")
        r = c.getresponse(); j = json.loads(r.read())
        check(r.status == 200 and j["features"]["bus_peek"] and not j["features"]["bus_poke"], f"caps {j}")
        check(j["limits"]["body_max_bytes"] == 8192, "limits")

        # unknown route / method
        r, j = post(c, "/nope", {})
        check(r.status == 404, "404")
        c.request("GET", "/bus-peek"); r = c.getresponse(); r.read()
        check(r.status == 405, "405")

        # oversize body -> 413, chunked -> 411, malformed -> 400
        big = b"x" * 9000
        out = raw(b.port, b"POST /poke HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 9000\r\n\r\n" + big)
        check(out.startswith(b"HTTP/1.1 413"), f"413: {out[:40]}")
        check(b"Access-Control-Allow-Origin: *" in out, "CORS on 413")
        out = raw(b.port, b"POST /poke HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        check(out.startswith(b"HTTP/1.1 411"), f"411: {out[:40]}")
        out = raw(b.port, b"GARBAGE\r\n\r\n")
        check(out.startswith(b"HTTP/1.1 400"), f"400: {out[:40]}")

        # the 2.2 KB air-freshener body is accepted (and refused as a feature, not as a size)
        body = {"domain": "vram", "address": 9952, "data": "00" * 1056, "encoding": "hex"}
        r, j = post(c, "/poke", body)
        check(r.status == 501 and j["error"]["code"] == "feature_unavailable", f"poke {r.status}")

        # pipelined requests on one socket
        pbody = b'{"bus":"main68k","address":16740330,"length":2}'
        req = (b"POST /bus-peek HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(pbody)).encode() + b"\r\n\r\n" + pbody)
        s = socket.create_connection(("127.0.0.1", b.port), timeout=5)
        s.sendall(req * 3)
        data = b""
        deadline = time.time() + 3
        while data.count(b"HTTP/1.1 200") < 3 and time.time() < deadline:
            data += s.recv(65536)
        s.close()
        check(data.count(b"HTTP/1.1 200") == 3, f"pipelining: {data.count(b'HTTP/1.1 200')}")

        # aborted connections must not kill the server
        raw(b.port, b"POST /bus-peek HTTP/1.1\r\nContent-Length: 100\r\n\r\n{\"bus", read=False)
        for _ in range(20):
            raw(b.port, req, read=False)   # close before reading the response
        s = socket.create_connection(("127.0.0.1", b.port)); s.close()
        time.sleep(0.2)
        check(b.proc.poll() is None, "survives aborted sockets")
        c = b.conn()
        r, j = post(c, "/bus-peek", {"bus": "main68k", "address": 0xFF6FEA, "length": 2})
        check(r.status == 200, "still serving after aborts")

        # concurrency: 6 clients like a browser
        errors = []
        lat = []

        def worker():
            cc = b.conn()
            for i in range(150):
                t0 = time.perf_counter()
                try:
                    rr, jj = post(cc, "/bus-peek", {"bus": "main68k", "address": 0xFF6FDC, "length": 4})
                    if rr.status != 200:
                        errors.append(rr.status)
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))
                    cc = b.conn()
                lat.append(time.perf_counter() - t0)
        ts = [threading.Thread(target=worker) for _ in range(6)]
        t0 = time.time()
        [t.start() for t in ts]
        [t.join() for t in ts]
        dt = time.time() - t0
        lat.sort()
        check(not errors, f"concurrency errors: {errors[:5]}")
        print(f"  mock concurrency: {len(lat)} requests in {dt:.2f}s, p50 {lat[len(lat)//2]*1000:.2f} ms, "
              f"p99 {lat[int(len(lat)*0.99)]*1000:.2f} ms")

        # single instance
        p2 = subprocess.run([binary, "--listen", f"127.0.0.1:{free_port()}", "--mock", "--lock", b.lock],
                            capture_output=True, text=True, timeout=5)
        check(p2.returncode != 0 and "another instance" in p2.stderr, "single-instance lock")
    finally:
        rc = b.stop()
        check(rc == 0, f"clean shutdown rc={rc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", required=True)
    args = ap.parse_args()
    run(args.bin)
    print(f"{'PASS' if not failures else 'FAILED'}: {checks} HTTP checks, {failures} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
