#!/usr/bin/env python3
"""End-to-end: HTTP -> megacd-dashboard -> Main's dashboard_ipc.cpp -> Verilated RTL.

The SPI link and SDRAM are simulated; everything between the browser-facing
HTTP API and the RTL registers is the shipping code.
"""
import argparse
import http.client
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

failures = 0
checks = 0


def check(cond, msg):
    global failures, checks
    checks += 1
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures += 1


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wram_word(i, seed=1):
    x = (((i + 1) * 2654435761) & 0xFFFFFFFF) ^ seed
    x ^= x >> 13
    x = (x * 0x5BD1E995) & 0xFFFFFFFF
    x ^= x >> 15
    return x & 0xFFFF


POKED = {}   # byte address -> value written through /bus-poke into the current core


def wram_bytes(addr, length, seed=1):
    out = []
    for a in range(addr, addr + length):
        off = a - 0xFF0000
        w = wram_word(off >> 1, seed)
        out.append(POKED.get(a, w >> 8 if off % 2 == 0 else w & 0xFF))
    return bytes(out).hex()


class Sim:
    def __init__(self, binary, sock, *extra):
        self.proc = subprocess.Popen([binary, "--socket", sock, *extra], stdout=subprocess.PIPE, text=True, bufsize=1)
        self.lines = queue.Queue()
        self.joy = None
        self.main_frozen = 0
        threading.Thread(target=self._reader, daemon=True).start()
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                if self.lines.get(timeout=1) == "READY":
                    return
            except queue.Empty:
                pass
        raise RuntimeError("sim not ready")

    def _reader(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("JOY"):
                self.joy = int(line.split()[1], 16)
            if line.startswith("MAINFROZEN"):
                self.main_frozen = int(line.split()[1])
            self.lines.put((time.time(), line) if line.startswith("JOY") else line)

    def wait_joy(self, value, timeout=3.0):
        t_end = time.time() + timeout
        while time.time() < t_end:
            if self.joy == value:
                return True
            time.sleep(0.002)
        return self.joy == value

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class Bridge:
    def __init__(self, binary, sock, tmp):
        self.port = free_port()
        self.proc = subprocess.Popen([binary, "--listen", f"127.0.0.1:{self.port}", "--socket", sock,
                                      "--state-dir", tmp, "--lock", os.path.join(tmp, "lock")],
                                     stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.1).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("bridge not up")

    def post(self, path, body=None, conn=None):
        c = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        if body is None:
            c.request("POST", path)
        else:
            c.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read())

    def stop(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)
        try:
            return self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def peek(b, addr, length, conn=None):
    return b.post("/bus-peek", {"bus": "main68k", "address": addr, "length": length, "encoding": "hex"}, conn)


# Every read the dashboard performs (telemetry + patch sites), API section 4.
READS = [(0xFF6FEA, 2), (0xFF6FFA, 2), (0xFF70E4, 4), (0xFF6FDC, 4), (0xFF70EA, 10), (0xFF709C, 4),
         (0xFF6FF8, 2), (0xFF7AA8, 6), (0xFF7002, 2), (0xFF842C, 8), (0xFF8498, 10), (0xFFC004, 2),
         (0xFF84CC, 2), (0xFF84D6, 2), (0xFF7A08, 2), (0xFFBB22, 6), (0xFFBB3C, 2), (0xFFBB46, 2),
         (0xFFBA54, 18), (0xFF7ABC, 64), (0xFF7B6C, 56), (0xFF7104, 2),
         (0xFF0000, 1), (0xFFFFFF, 1), (0xFFFFC0, 64), (0xFF1235, 7)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", required=True)
    ap.add_argument("--stock-sim", required=True)
    ap.add_argument("--bridge", required=True)
    args = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="mcddash-e2e-")
    sock = os.path.join(tmp, "dash.sock")
    with open(os.path.join(tmp, "desertbus-fullauto.gp0"), "wb") as f:
        f.write(b"GENPLUS-GX 1.7.6" + b"\0" * 64)

    sim = Sim(args.sim, sock)
    b = Bridge(args.bridge, sock, tmp)
    try:
        print("capabilities")
        st, j = b.get("/capabilities")
        check(st == 200 and j["core"]["present"], f"core present: {j.get('core')}")
        check(j["core"].get("build_id") == "01260925" and j["core"].get("feature_bits") == 0x1BF, "build id / features from RTL PROBE (freeze, writes, coherent reads, VRAM)")
        f = j["features"]
        check(f["bus_peek"] and f["input"] and f["pause"] and f["bus_poke"] and not f["state"]
              and f["vram_peek"] and f["vram_poke"] and f["vram_refresh_token"], f"features {f}")
        check(j["read_consistency"] == "coherent", "read consistency is coherent")

        print("reads through RTL + SDRAM model")
        for addr, n in READS:
            st, j = peek(b, addr, n)
            check(st == 200 and j["data"] == wram_bytes(addr, n), f"bus-peek ${addr:06X}:{n}")

        print("refused features are explicit")
        print("CPU-bus patches through the RTL")
        written = {}
        for addr, data in ((0xFF842C, "4e714e714e714e71"), (0xFF7A08, "0001"), (0xFF6FF6, "0000"),
                           (0xFF7ABD, "".join("%02x" % ((k * 37) & 0xFF) for k in range(64))), (0xFFBB23, "a5")):
            st, j = b.post("/bus-poke", {"bus": "main68k", "address": addr, "data": data, "encoding": "hex", "unsafe": True})
            check(st == 200, "bus-poke $%06X (%d bytes): %s" % (addr, len(data) // 2, j))
            written[addr] = data
            if st == 200:
                for k in range(len(data) // 2):
                    POKED[addr + k] = int(data[2 * k:2 * k + 2], 16)
        for addr, data in written.items():
            st, j = peek(b, addr, len(data) // 2)
            check(st == 200 and j["data"] == data, "readback $%06X" % addr)
        st, j = peek(b, 0xFFBB22, 1)
        check(j["data"] == wram_bytes(0xFFBB22, 1), "neighbour of an odd single-byte write unchanged")
        print("VRAM through the RTL (GPGX word-swapped API view)")
        def canon(bb):
            return ((bb * 7 + 3) ^ (bb >> 8)) & 0xFF
        def api_bytes(a, n):
            return bytes(canon(x ^ 1) for x in range(a, a + n)).hex()
        st, j = b.post("/peek", {"domain": "vram", "address": 10560, "length": 32, "encoding": "hex"})
        check(st == 200 and j["data"] == api_bytes(10560, 32), "signature tile read, API byte A = VDP byte A^1")
        st, j = b.post("/peek", {"domain": "vram", "address": 10561, "length": 3, "encoding": "hex"})
        check(st == 200 and j["data"] == api_bytes(10561, 3), "odd-address VRAM read")
        fix = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api-fixtures.json")))
        fresh = [p for p in fix["patches"] if p.get("vram")][0]
        for state in fresh["states"]:
            st, j = b.post("/poke", {"domain": "vram", "address": 9952, "data": state["data"], "encoding": "hex"})
            check(st == 200, "air freshener '%s' upload (1056 bytes): %s" % (state["key"], j))
            st, j = b.post("/peek", {"domain": "vram", "address": 9952, "length": 1056, "encoding": "hex"})
            check(st == 200 and j["data"] == state["data"], "air freshener '%s' reads back" % state["key"])
            st, j = b.post("/peek", {"domain": "vram", "address": 9952 + 608, "length": 32, "encoding": "hex"})
            check(j.get("data") == state["sig"], "badge signature matches '%s'" % state["key"])
            for ep in ("/state/save", "/state/load"):
                st, j = b.post(ep, {"path": "dashboard-cache-refresh.gp0"})
                check(st == 200, "refresh token %s" % ep)
        st, j = b.post("/state/load", {"path": "dashboard-cache-refresh.gp0"})
        check(st == 409, "refresh token is single use")
        st, j = b.post("/peek", {"domain": "vram", "address": 9950, "length": 2, "encoding": "hex"})
        check(j.get("data") == api_bytes(9950, 2), "byte before the upload unchanged")
        st, j = b.post("/peek", {"domain": "vram", "address": 9952 + 1056, "length": 2, "encoding": "hex"})
        check(j.get("data") == api_bytes(9952 + 1056, 2), "byte after the upload unchanged")
        print("pause / resume through the RTL")
        st, j = b.post("/pause")
        check(st == 200 and j.get("paused") is True, "pause reports paused once frozen: %s" % j)
        st, j = b.get("/status")
        check(j.get("paused") is True, "/status shows paused")
        t_end = time.time() + 2
        while sim.main_frozen != 1 and time.time() < t_end:
            time.sleep(0.01)
        check(sim.main_frozen == 1, "Main sees FROZEN (CD barrier: mcd_poll stops the drive)")
        st, j = b.post("/pause")
        check(st == 200, "pause is idempotent")
        st, j = peek(b, 0xFF6FEA, 2)
        check(st == 200 and j["data"] == wram_bytes(0xFF6FEA, 2), "reads work while paused")
        st, j = b.post("/input", {"press": ["up"]})
        check(st == 200 and sim.wait_joy(0x008, timeout=1), "input applies immediately while frozen")
        b.post("/input", {"release": ["up"]})
        sim.wait_joy(0)
        st, j = b.post("/resume")
        check(st == 200 and j.get("paused") is False, "resume: %s" % j)
        st, j = b.post("/resume")
        check(st == 200, "resume is idempotent")
        st, j = b.get("/status")
        check(j.get("paused") is False, "/status shows running")
        t_end = time.time() + 2
        while sim.main_frozen != 0 and time.time() < t_end:
            time.sleep(0.01)
        check(sim.main_frozen == 0, "Main sees running again (CD drive resumes)")
        st, j = b.post("/state/save", {"path": "123.gp0"})
        check(st == 501, "state save 501")
        st, j = b.post("/state/load", {"path": "desertbus-fullauto.gp0"})
        check(st == 409 and j["error"]["code"] == "state_incompatible", "GPGX fullauto rejected before mutation")

        print("input reaches logical player 1 at a frame boundary")
        lat = []
        for btn, bit in (("left", 0x002), ("right", 0x001), ("start", 0x080)):
            t0 = time.time()
            st, j = b.post("/input", {"press": [btn]})
            ok = st == 200 and j["held"] == [btn] and sim.wait_joy(bit)
            lat.append(time.time() - t0)
            check(ok, f"press {btn} -> JOY {bit:03x}")
            t0 = time.time()
            st, j = b.post("/input", {"release": ["left", "right", "start"]})
            ok = st == 200 and j["held"] == [] and sim.wait_joy(0)
            lat.append(time.time() - t0)
            check(ok, f"release {btn}")
        print(f"  (sim wall-clock press/release latency max {max(lat)*1000:.1f} ms; not a hardware measurement)")
        st, j = b.post("/input", {"press": ["left"]})
        st, j = b.post("/input", {"press": ["right"]})
        check(j["held"] == ["right", "left"] and sim.wait_joy(0x003), "holds accumulate")
        time.sleep(1.0)
        check(sim.joy == 0x003, "hold persists across frames and idle time (heartbeat keeps lease)")
        b.post("/input", {"release": ["left", "right"]})
        check(sim.wait_joy(0), "released")

        print("concurrency: 6 clients")
        errs = []

        def worker(k):
            c = http.client.HTTPConnection("127.0.0.1", b.port, timeout=10)
            for i in range(40):
                addr, n = READS[(k * 7 + i) % len(READS)]
                st, j = peek(b, addr, n, c)
                if st != 200 or j["data"] != wram_bytes(addr, n):
                    errs.append((addr, n, st))
        ts = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
        t0 = time.time()
        [t.start() for t in ts]
        [t.join() for t in ts]
        check(not errs, f"240 concurrent reads correct in {time.time()-t0:.2f}s (errors {errs[:3]})")

        print("conformance runner (read-only items) through the RTL")
        conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "conformance.py")
        r = subprocess.run([sys.executable, conf, "--base", f"http://127.0.0.1:{b.port}", "--allow-unsupported"],
                           capture_output=True, text=True, timeout=120)
        check(r.returncode == 0 and "FAIL " not in r.stdout and "UNSUPPORTED" not in r.stdout,
              "conformance.py read-only items all PASS, VRAM included")
        if r.returncode:
            print(r.stdout)

        print("bridge crash releases held input (Main sends SESSION 0)")
        b.post("/input", {"press": ["left"]})
        check(sim.wait_joy(0x002), "left held")
        b.stop(signal.SIGKILL)
        check(sim.wait_joy(0), "input released after bridge SIGKILL")
        b = Bridge(args.bridge, sock, tmp)
        st, j = peek(b, 0xFF6FEA, 2)
        check(st == 200 and j["data"] == wram_bytes(0xFF6FEA, 2), "new bridge re-probes and works")

        print("owner lease expiry releases input when the bridge hangs")
        b.post("/input", {"press": ["down"]})
        check(sim.wait_joy(0x004), "down held")
        b.proc.send_signal(signal.SIGSTOP)
        released = sim.wait_joy(0, timeout=20)
        check(released, "lease expiry released input")
        b.proc.send_signal(signal.SIGCONT)
        st, j = peek(b, 0xFF6FEA, 2)
        check(st == 200, "bridge resumes after stall")
        st, j = b.post("/input", {"release": ["down"]})
        check(st == 200 and j["held"] == [], "release after lease expiry is a no-op")

        print("Main restart / new core instance")
        b.post("/input", {"press": ["left"]})
        sim.wait_joy(0x002)
        sim.stop()
        st, j = peek(b, 0xFF6FEA, 2)
        check(st in (503, 504) and j["error"]["code"] in ("core_unavailable", "operation_timeout"), f"Main gone: {st}")
        POKED.clear()   # the new core starts from its own RAM
        sim = Sim(args.sim, sock, "--wram-seed", "7")
        time.sleep(0.2)   # let the new Main create its socket (it does so on its first poll)
        st, j = peek(b, 0xFF6FEA, 2)
        check(st == 200 and j.get("data") == wram_bytes(0xFF6FEA, 2, seed=7),
              "first request after a Main restart succeeds and sees the new core's RAM: %s %s" % (st, j))
        check(sim.joy in (None, 0), "no stale input carried into the new core")

        print("Main restarts between two requests (what a core load does on the MiSTer)")
        st, j = peek(b, 0xFF6FEA, 2)
        check(st == 200, "connected before the restart")
        sim.stop()
        sim = Sim(args.sim, sock, "--wram-seed", "9")   # no request while Main is gone
        time.sleep(0.2)
        st, j = peek(b, 0xFF6FEA, 2)
        check(st == 200 and j.get("data") == wram_bytes(0xFF6FEA, 2, seed=9),
              "first request after the restart succeeds: %s %s" % (st, j))

        print("wrong core / stock RBF")
        sim.stop()
        sim = Sim(args.sim, sock, "--not-megacd")
        time.sleep(0.3)
        for _ in range(40):
            st, j = peek(b, 0xFF6FEA, 2)
            if "not MegaCD" in j.get("error", {}).get("message", ""):
                break
            time.sleep(0.1)
        check(st == 503 and "not MegaCD" in j["error"]["message"], f"non-MegaCD core: {j}")
        sim.stop()
        sim = Sim(args.stock_sim, sock)
        for _ in range(40):
            st, j = peek(b, 0xFF6FEA, 2)
            if j.get("error", {}).get("code") == "protocol_mismatch":
                break
            time.sleep(0.1)
        check(st == 503 and j["error"]["code"] == "protocol_mismatch", f"stock RBF detected: {j}")
        st, j = b.post("/input", {"press": ["left"]})
        check(st == 503, "no input sent to a stock RBF")
    finally:
        b.stop()
        sim.stop()

    print(f"{'PASS' if not failures else 'FAILED'}: {checks} end-to-end checks, {failures} failures")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
