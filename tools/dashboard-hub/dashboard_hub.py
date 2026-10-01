#!/usr/bin/env python3
"""Desert Bus dashboard hub: one central client of the MiSTer bridge.

Dashboards (dashboard.html, scripts) connect to the hub instead of the MiSTer. The hub
speaks the same HTTP API as the bridge, so the dashboard needs no changes: enter the hub's
URL (e.g. http://hub-host:8766) as the server.

    python3 dashboard_hub.py --mister http://mister.lan:8765
    python3 dashboard_hub.py --mister http://mister.lan:8765 \
        --influx-url http://localhost:8086 --influx-org home --influx-bucket desertbus \
        --influx-token-file ~/.influx-token

What it does:
  - Keeps one keep-alive connection to the bridge and sends every request through a single
    worker, in priority order (input/pause first, then client requests, then polling), with
    an upper bound on the request rate. However many dashboards connect, the MiSTer sees one
    client.
  - Polls the telemetry block (speed, lateral position, clock, distance, odometer, day phase,
    game state) and /status on a fixed interval, and answers dashboard reads from that cache.
  - Any other range a dashboard reads (patch sites, VRAM) is added to a demand watch list and
    polled too while dashboards keep asking for it; repeat reads are answered from the cache.
  - Writes (/bus-poke, /poke, /input, /pause, /resume, ...) are forwarded in order and
    invalidate the cache, so the next read sees the new value.
  - Writes decoded telemetry and hub health to InfluxDB (v2 API with token, or v1 with
    database), buffering points while InfluxDB is unreachable.
  - Reconnects to the bridge with backoff after any failure; while it is down, clients get
    HTTP 503 "upstream_unavailable" (the dashboard shows it and keeps polling).
  - Releases all injected buttons when it connects and when it shuts down.

Only the Python standard library is used (Python 3.8+).
"""
import argparse
import collections
import http.client
import http.server
import itertools
import json
import logging
import logging.handlers
import os
import queue
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "1.0.0"
log = logging.getLogger("hub")

BUS_MAX = 64          # bridge limit for /bus-peek
VRAM_MAX = 2048       # bridge limit for /peek domain vram
WORKRAM = (0xFF0000, 0x1000000)
ALL_BUTTONS = ["up", "down", "left", "right", "a", "b", "c", "start", "x", "y", "z", "mode"]

# Telemetry block (see DASHBOARD-API.md §4): (address, length)
TELEMETRY = {
    "distance": (0xFF6FDC, 4),
    "speed": (0xFF6FEA, 2),
    "daynight_parity": (0xFF6FF8, 2),
    "lateral": (0xFF6FFA, 2),
    "game_state": (0xFF7002, 2),
    "palette": (0xFF709C, 4),
    "clock": (0xFF70E4, 4),
    "odometer": (0xFF70EA, 10),
    "daynight_fix": (0xFF7AA8, 6),
}
READ_ONLY = {"/bus-peek", "/peek", "/status", "/capabilities"}
CONTROL = {"/input", "/pause", "/resume"}


def now():
    return time.monotonic()


def error_body(code, message):
    return json.dumps({"ok": False, "error": {"code": code, "message": message}}).encode()


# --------------------------------------------------------------------------- upstream

class Job:
    __slots__ = ("method", "path", "body", "retry", "done", "result", "error")

    def __init__(self, method, path, body, retry):
        self.method, self.path, self.body, self.retry = method, path, body, retry
        self.done = threading.Event()
        self.result = self.error = None


class UpstreamDown(Exception):
    pass


class Upstream:
    """The single connection to the bridge. All traffic goes through one worker thread."""
    P_CONTROL, P_CLIENT, P_POLL = 0, 1, 2

    def __init__(self, base, timeout, max_rate):
        u = urllib.parse.urlsplit(base if "://" in base else "http://" + base)
        self.base = f"{u.scheme}://{u.netloc}"
        self.host, self.port, self.prefix = u.hostname, u.port or 80, u.path.rstrip("/")
        self.timeout = timeout
        self.min_gap = 1.0 / max_rate if max_rate > 0 else 0.0
        self.q = queue.PriorityQueue()
        self.seq = itertools.count()
        self.conn = None
        self.up = False
        self.caps = None
        self.epoch = None
        self.on_connect = []           # callbacks(epoch_changed: bool)
        self.stats = collections.Counter()
        self.latency = collections.deque(maxlen=500)
        self.last_send = 0.0
        self.down_since = now()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._worker, name="upstream", daemon=True)

    def start(self):
        self.thread.start()

    # -- API for other threads
    def request(self, method, path, body=None, prio=P_CLIENT, retry=True, wait=None):
        """Returns (status, body bytes). Raises UpstreamDown when the bridge is unreachable."""
        if not self.up:
            raise UpstreamDown(f"MiSTer bridge at {self.base} is not reachable (reconnecting)")
        job = Job(method, path, body, retry)
        self.q.put((prio, next(self.seq), job))
        if not job.done.wait(wait or self.timeout * 3 + 5):
            raise UpstreamDown("upstream request timed out in the hub queue")
        if job.error:
            raise job.error
        return job.result

    def request_json(self, method, path, body=None, prio=P_POLL):
        status, raw = self.request(method, path, None if body is None else json.dumps(body).encode(), prio)
        try:
            j = json.loads(raw or b"{}")
        except ValueError:
            j = {}
        return status, j

    # -- worker
    def _connect(self):
        self._close()
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        self.conn.connect()
        self.conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _close(self):
        if self.conn:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self.conn = None

    def _send_once(self, method, path, body):
        if self.conn is None:
            self._connect()
        gap = self.min_gap - (now() - self.last_send)
        if gap > 0:
            time.sleep(gap)
        t0 = now()
        self.last_send = t0
        headers = {"Content-Type": "application/json"} if body is not None else {}
        self.conn.request(method, self.prefix + path, body, headers)
        r = self.conn.getresponse()
        data = r.read()
        self.latency.append(now() - t0)
        self.stats["upstream_requests"] += 1
        if r.getheader("Connection", "").lower() == "close":
            self._close()
        return r.status, data

    def _send(self, job):
        try:
            return self._send_once(job.method, job.path, job.body)
        except (OSError, http.client.HTTPException) as e:
            # a keep-alive connection the bridge closed fails on the next use: retry once
            # on a fresh connection when repeating the request is harmless
            self._close()
            if not job.retry:
                raise
            log.debug("retrying %s %s after %r", job.method, job.path, e)
            return self._send_once(job.method, job.path, job.body)

    def _probe(self):
        status, raw = self._send_once("GET", "/capabilities", None)
        caps = json.loads(raw)
        if status != 200 or not caps.get("ok", False):
            raise OSError(f"/capabilities answered HTTP {status}")
        return caps

    def _go_down(self, why):
        if self.up:
            log.warning("lost the MiSTer bridge at %s: %s", self.base, why)
            self.down_since = now()
            self.stats["disconnects"] += 1
        self.up = False
        self._close()
        # fail everything that is queued; callers decide whether to retry
        while True:
            try:
                _, _, job = self.q.get_nowait()
            except queue.Empty:
                break
            job.error = UpstreamDown(f"lost the MiSTer bridge: {why}")
            job.done.set()

    def _reconnect_loop(self):
        delay, last_log = 0.5, 0.0
        while not self.stop.is_set():
            try:
                self._connect()
                caps = self._probe()
            except (OSError, ValueError, http.client.HTTPException) as e:
                self._close()
                if now() - last_log > 60:
                    log.warning("cannot reach the MiSTer bridge at %s (%s); retrying", self.base, e)
                    last_log = now()
                self.stop.wait(delay)
                delay = min(delay * 2, 10.0)
                continue
            epoch = (caps.get("core") or {}).get("epoch")
            changed = epoch != self.epoch
            self.caps, self.epoch = caps, epoch
            self.up = True
            log.info("connected to the MiSTer bridge at %s (core %s, build %s, epoch %s) after %.1f s",
                     self.base, (caps.get("core") or {}).get("name"), (caps.get("core") or {}).get("build_id"),
                     epoch, now() - self.down_since)
            for cb in self.on_connect:
                try:
                    cb(changed)
                except Exception:  # noqa: BLE001
                    log.exception("on_connect callback failed")
            return

    def _worker(self):
        while not self.stop.is_set():
            if not self.up:
                self._reconnect_loop()
                continue
            try:
                _, _, job = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                job.result = self._send(job)
            except (OSError, http.client.HTTPException) as e:
                job.error = UpstreamDown(f"MiSTer bridge request failed: {e}")
                job.done.set()
                self._go_down(repr(e))
                continue
            job.done.set()

    def shutdown(self):
        self.stop.set()
        self._close()


# --------------------------------------------------------------------------- cache

class Cache:
    """Memory blocks per address space ('bus' or 'vram') with their read time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.blocks = {"bus": {}, "vram": {}}    # space -> {start: (bytes, t)}
        self.status = None                        # (raw bytes, t)
        self.generation = 0

    def put(self, space, start, data, t=None, generation=None):
        with self.lock:
            if generation is not None and generation != self.generation:
                return    # read raced with a write: drop it
            self.blocks[space][start] = (bytes(data), t or now())

    def get(self, space, addr, length, max_age):
        t_now = now()
        with self.lock:
            for start, (data, t) in self.blocks[space].items():
                if start <= addr and addr + length <= start + len(data) and t_now - t <= max_age:
                    return data[addr - start:addr - start + length]
        return None

    def read(self, space, addr, length):
        """Latest value regardless of age (for telemetry decoding)."""
        return self.get(space, addr, length, float("inf"))

    def put_status(self, raw, generation=None):
        with self.lock:
            if generation is None or generation == self.generation:
                self.status = (raw, now())

    def get_status(self, max_age):
        with self.lock:
            if self.status and now() - self.status[1] <= max_age:
                return self.status[0]
        return None

    def invalidate(self):
        with self.lock:
            self.generation += 1
            for space in self.blocks:
                self.blocks[space] = {s: (d, 0.0) for s, (d, _) in self.blocks[space].items()}
            self.status = None

    def clear(self):
        with self.lock:
            self.generation += 1
            self.blocks = {"bus": {}, "vram": {}}
            self.status = None


def plan_reads(ranges, max_len, gap=16):
    """Merge (start, length) ranges into reads of at most max_len bytes. Each input range
    lies wholly inside one read, so a cached read answers it."""
    out = []
    for start, length in sorted(set(ranges)):
        end = start + length
        if out and start - out[-1][1] <= gap and max(end, out[-1][1]) - out[-1][0] <= max_len:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(s, e - s) for s, e in out]


# --------------------------------------------------------------------------- telemetry

def u(b):
    return int.from_bytes(b, "big")


def decode_telemetry(c):
    """Decode the telemetry block like dashboard.html does. Returns a dict or None."""
    raw = {k: c.read("bus", a, n) for k, (a, n) in TELEMETRY.items()}
    if any(v is None for v in raw.values()):
        return None
    speed, lat, dist = u(raw["speed"]), u(raw["lateral"]), u(raw["distance"])
    clk, od = raw["clock"], raw["odometer"]
    hour = (0 if clk[0] == 0xFF else 10) + clk[1]
    minute = clk[2] * 10 + clk[3]
    digit = [u(od[i * 2:i * 2 + 2]) for i in range(5)]
    odo = digit[4] * 1000 + digit[3] * 100 + digit[2] * 10 + digit[1] + digit[0] / 10
    leg_miles = dist / 1800
    pal, parity = u(raw["palette"]), u(raw["daynight_parity"])
    fixed = raw["daynight_fix"].hex() == "4e714e714e71"
    day, night, sunrise = 0x20CB52, 0x20CED2, 0x20D252
    on_ramp = day < pal <= night or pal == sunrise
    if pal in (day, 0):
        phase = "day"
    elif pal == night:
        phase = "night"
    elif on_ramp:
        phase = ("dawn" if parity % 2 == 0 else "dusk") if fixed else "twilight"
    else:
        phase = "unknown"
    gs = u(raw["game_state"])
    return {
        # outside Desert Bus (BIOS, game menu, other title) work RAM holds other data
        "in_game": gs < 0x20 and pal in (0, sunrise) or (gs < 0x20 and day <= pal <= night),
        "game_state": gs,
        "speed_raw": speed, "speed_mph": round(speed / 0x6000 * 45, 3),
        "lateral_raw": lat, "lateral_norm": round((lat - 0x6C00) / 0x4800, 4),
        "offroad": lat <= 0x2400 or lat >= 0xB400,
        "clock_hour": hour, "clock_minute": minute,
        "distance_raw": dist, "leg_miles": round(leg_miles, 4),
        "odometer_miles": round(odo, 1),
        "points": max(0, round((odo - 109.3 - leg_miles) / 360)),
        "palette": pal, "daynight_parity": parity, "daynight_fixed": fixed, "phase": phase,
    }


class Poller:
    def __init__(self, up, cache, args):
        self.up, self.cache, self.args = up, cache, args
        self.demand = {}               # (space, addr, len) -> expiry (monotonic)
        self.demand_lock = threading.Lock()
        self.telemetry = None          # (dict, wall time)
        self.status = None             # parsed /status
        self.stop = threading.Event()
        self.last_demand = {"bus": 0.0, "vram": 0.0}
        self.thread = threading.Thread(target=self._run, name="poller", daemon=True)
        self.hot = plan_reads(list(TELEMETRY.values()), BUS_MAX)

    def start(self):
        self.thread.start()

    def is_hot(self, space, addr, length):
        return space == "bus" and any(s <= addr and addr + length <= s + n for s, n in self.hot)

    def want(self, space, addr, length):
        if self.is_hot(space, addr, length):
            return
        with self.demand_lock:
            self.demand[(space, addr, length)] = now() + self.args.demand_ttl

    def demand_count(self):
        with self.demand_lock:
            return len(self.demand)

    def _demand_ranges(self, space):
        t = now()
        with self.demand_lock:
            for k in [k for k, exp in self.demand.items() if exp < t]:
                del self.demand[k]
            return [(a, n) for (s, a, n) in self.demand if s == space]

    def _read(self, space, addr, length):
        gen = self.cache.generation
        if space == "bus":
            st, j = self.up.request_json("POST", "/bus-peek", {"bus": "main68k", "address": addr, "length": length, "encoding": "hex"})
        else:
            st, j = self.up.request_json("POST", "/peek", {"domain": "vram", "address": addr, "length": length, "encoding": "hex"})
        if st == 200 and j.get("ok"):
            self.cache.put(space, addr, bytes.fromhex(j["data"]), generation=gen)
            return True
        return False

    def _cycle(self):
        gen = self.cache.generation
        status, raw = self.up.request("GET", "/status", None, Upstream.P_POLL)
        if status == 200:
            self.cache.put_status(raw, gen)
            try:
                self.status = json.loads(raw)
            except ValueError:
                self.status = None
        for addr, length in self.hot:
            self._read("bus", addr, length)
        t = now()
        for space, interval, max_len in (("bus", self.args.demand_interval, BUS_MAX),
                                         ("vram", self.args.vram_interval, VRAM_MAX)):
            if t - self.last_demand[space] >= interval:
                self.last_demand[space] = t
                for addr, length in plan_reads(self._demand_ranges(space), max_len):
                    self._read(space, addr, length)
        tel = decode_telemetry(self.cache)
        if tel:
            self.telemetry = (tel, time.time())

    def _run(self):
        while not self.stop.is_set():
            t0 = now()
            if self.up.up:
                try:
                    self._cycle()
                except UpstreamDown:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("poll cycle failed")
            self.stop.wait(max(0.02, self.args.interval - (now() - t0)))


# --------------------------------------------------------------------------- influx

def lp_escape(s, chars=",= "):
    for ch in chars:
        s = s.replace(ch, "\\" + ch)
    return s


def lp_field(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v}i"
    if isinstance(v, float):
        return repr(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def line(measurement, tags, fields, ts_ns):
    t = "".join(f",{lp_escape(k)}={lp_escape(str(v))}" for k, v in sorted(tags.items()) if v not in (None, ""))
    f = ",".join(f"{lp_escape(k)}={lp_field(v)}" for k, v in fields.items() if v is not None)
    return f"{lp_escape(measurement, ', ')}{t} {f} {ts_ns}"


class Influx:
    """Batches line protocol and writes it in the background; buffers while InfluxDB is down."""

    def __init__(self, args):
        self.args = args
        u = args.influx_url.rstrip("/")
        if args.influx_bucket:
            q = {"bucket": args.influx_bucket, "precision": "ns"}
            if args.influx_org:
                q["org"] = args.influx_org
            self.url = f"{u}/api/v2/write?{urllib.parse.urlencode(q)}"
        else:
            q = {"db": args.influx_db, "precision": "ns"}
            if args.influx_user:
                q.update(u=args.influx_user, p=args.influx_password or "")
            self.url = f"{u}/write?{urllib.parse.urlencode(q)}"
        self.headers = {"Content-Type": "text/plain; charset=utf-8"}
        if args.influx_token:
            self.headers["Authorization"] = f"Token {args.influx_token}"
        self.buf = collections.deque(maxlen=args.influx_buffer)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ok = None
        self.stats = collections.Counter()
        self.thread = threading.Thread(target=self._run, name="influx", daemon=True)

    def start(self):
        self.thread.start()

    def add(self, ln):
        with self.lock:
            if len(self.buf) == self.buf.maxlen:
                self.stats["dropped"] += 1
            self.buf.append(ln)

    def _post(self, lines):
        req = urllib.request.Request(self.url, data="\n".join(lines).encode(), headers=self.headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()

    def _run(self):
        delay = 1.0
        while not self.stop.is_set():
            self.stop.wait(self.args.influx_flush)
            self.flush_once()
            if self.ok is False:
                self.stop.wait(delay)
                delay = min(delay * 2, 60.0)
            else:
                delay = 1.0
        self.flush_once()

    def flush_once(self):
        while True:
            with self.lock:
                batch = list(itertools.islice(self.buf, 0, 5000))
            if not batch:
                return
            try:
                self._post(batch)
            except urllib.error.HTTPError as e:
                body = e.read()[:300].decode(errors="replace")
                if 400 <= e.code < 500 and e.code not in (401, 403, 404, 429):
                    log.error("InfluxDB rejected %d points (HTTP %d: %s); dropping them", len(batch), e.code, body)
                    self._drop(len(batch))
                    self.stats["rejected"] += len(batch)
                    continue
                self._fail(f"HTTP {e.code}: {body}")
                return
            except (OSError, ValueError) as e:
                self._fail(str(e))
                return
            self._drop(len(batch))
            self.stats["written"] += len(batch)
            if self.ok is not True:
                log.info("InfluxDB writes OK (%s)", self.args.influx_url)
            self.ok = True

    def _drop(self, n):
        with self.lock:
            for _ in range(min(n, len(self.buf))):
                self.buf.popleft()

    def _fail(self, why):
        if self.ok is not False:
            log.warning("InfluxDB write failed (%s); buffering up to %d points", why, self.buf.maxlen)
        self.ok = False


def recorder(poller, up, influx, server, args, stop):
    """Samples telemetry and hub health into InfluxDB."""
    tags = {"host": args.influx_tag_host or up.host}
    last_reqs = 0
    while not stop.wait(args.influx_interval):
        ts = time.time_ns()
        tel = poller.telemetry
        st = poller.status or {}
        if up.up and tel and time.time() - tel[1] < max(5.0, 4 * args.interval):
            fields = dict(tel[0]) if tel[0]["in_game"] else {"in_game": False, "game_state": tel[0]["game_state"]}
            fields.update(paused=bool(st.get("paused")), frame=st.get("frame"),
                          held=",".join(st.get("held") or []))
            influx.add(line("desertbus", tags, fields, ts))
        lat = sorted(up.latency)
        reqs = up.stats["upstream_requests"]
        influx.add(line("dashboard_hub", tags, {
            "connected": up.up,
            "core_present": bool(((up.caps or {}).get("core") or {}).get("present")) if up.up else False,
            "upstream_rps": round((reqs - last_reqs) / args.influx_interval, 2),
            "upstream_latency_ms_p50": round(lat[len(lat) // 2] * 1000, 2) if lat else None,
            "upstream_latency_ms_max": round(lat[-1] * 1000, 2) if lat else None,
            "disconnects": up.stats["disconnects"],
            "clients": server.active_clients(),
            "client_requests": server.stats["requests"],
            "cache_hits": server.stats["cache_hits"],
            "forwarded": server.stats["forwarded"],
            "demand_ranges": poller.demand_count(),
            "influx_buffered": len(influx.buf),
        }, ts))
        last_reqs = reqs


# --------------------------------------------------------------------------- HTTP server

class Hub(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, up, cache, poller, args):
        super().__init__(addr, Handler)
        self.up, self.cache, self.poller, self.args = up, cache, poller, args
        self.stats = collections.Counter()
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.influx = None
        self.dashboard = None
        if args.dashboard and os.path.isfile(args.dashboard):
            self.dashboard = args.dashboard

    def seen(self, ip):
        with self.clients_lock:
            self.clients[ip] = now()

    def active_clients(self):
        t = now()
        with self.clients_lock:
            return sum(1 for v in self.clients.values() if t - v < 30)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"dashboard-hub/{VERSION}"

    def log_message(self, fmt, *a):
        log.debug("%s %s", self.client_address[0], fmt % a)

    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj).encode())

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"")

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")

    def _handle(self, method):
        s = self.server
        s.stats["requests"] += 1
        s.seen(self.client_address[0])
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            return self._send(413, error_body("body_too_large", "request body too large"))
        body = self.rfile.read(n) if n else None

        if path in ("/", "/dashboard.html") and method == "GET" and s.dashboard:
            with open(s.dashboard, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/hub":
            return self._json(200, self._hub_status())
        if path == "/hub/telemetry":
            tel = s.poller.telemetry
            return self._json(200, {"ok": tel is not None, "time": tel and tel[1], "telemetry": tel and tel[0]})

        try:
            if path == "/capabilities":
                return self._capabilities()
            if path == "/status":
                raw = s.cache.get_status(s.args.max_age)
                if raw is not None:
                    s.stats["cache_hits"] += 1
                    return self._send(200, raw)
                return self._forward(method, path, body, Upstream.P_CLIENT)
            if path in ("/bus-peek", "/peek") and method == "POST" and self._cached_read(path, body):
                return None
            prio = Upstream.P_CONTROL if path in CONTROL else Upstream.P_CLIENT
            return self._forward(method, path, body, prio)
        except UpstreamDown as e:
            return self._send(503, error_body("upstream_unavailable", str(e)))

    def _capabilities(self):
        up = self.server.up
        if not up.up or not up.caps:
            raise UpstreamDown(f"MiSTer bridge at {up.base} is not reachable (reconnecting)")
        caps = dict(up.caps)
        caps["hub"] = {"server": "dashboard-hub", "version": VERSION, "upstream": up.base}
        return self._json(200, caps)

    def _cached_read(self, path, body):
        """Answer /bus-peek or /peek from the cache. Returns False to forward instead."""
        s = self.server
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return False
        if req.get("encoding", "hex") != "hex":
            return False
        addr, length = req.get("address"), req.get("length")
        if not isinstance(addr, int) or not isinstance(length, int) or isinstance(addr, bool) or length < 1:
            return False
        if path == "/bus-peek":
            if req.get("bus") != "main68k" or length > BUS_MAX or addr < WORKRAM[0] or addr + length > WORKRAM[1]:
                return False
            space, max_age = "bus", s.args.max_age
        else:
            if req.get("domain") != "vram" or length > VRAM_MAX or addr < 0 or addr + length > 0x10000:
                return False
            space, max_age = "vram", s.args.vram_interval * 2
        s.poller.want(space, addr, length)
        if space == "bus" and not s.poller.is_hot(space, addr, length):
            max_age = max(max_age, s.args.demand_interval * 2)
        data = s.cache.get(space, addr, length, max_age)
        if data is None:
            gen = s.cache.generation
            status, raw = s.up.request("POST", path, body, Upstream.P_CLIENT)
            s.stats["forwarded"] += 1
            if status == 200:
                try:
                    s.cache.put(space, addr, bytes.fromhex(json.loads(raw)["data"]), generation=gen)
                except (ValueError, KeyError):
                    pass
            self._send(status, raw)
            return True
        s.stats["cache_hits"] += 1
        self._send(200, json.dumps({"ok": True, "data": data.hex()}).encode())
        return True

    def _forward(self, method, path, body, prio):
        s = self.server
        write = path not in READ_ONLY
        # writes from different dashboards go upstream one at a time, and the cache is
        # invalidated before and after so no reader sees a value from before the write
        lock = s.write_lock if write else None
        if lock:
            lock.acquire()
        try:
            if write:
                s.cache.invalidate()
            # /input with both press and release in one body would tap twice if repeated
            retry = not (path.startswith("/state") or (path == "/input" and body and b"press" in body and b"release" in body))
            status, raw = s.up.request(method, path, body, prio, retry=retry)
            s.stats["forwarded"] += 1
            if write:
                s.stats["writes"] += 1
                s.cache.invalidate()
                log.info("%s %s %s -> %d", self.client_address[0], path, (body or b"")[:200].decode(errors="replace"), status)
            return self._send(status, raw)
        finally:
            if lock:
                lock.release()

    def _hub_status(self):
        s = self.server
        up = s.up
        lat = sorted(up.latency)
        return {
            "ok": True, "server": "dashboard-hub", "version": VERSION,
            "upstream": {"url": up.base, "connected": up.up, "epoch": up.epoch,
                         "down_for_s": None if up.up else round(now() - up.down_since, 1),
                         "requests": up.stats["upstream_requests"], "disconnects": up.stats["disconnects"],
                         "latency_ms_p50": round(lat[len(lat) // 2] * 1000, 2) if lat else None,
                         "latency_ms_max": round(lat[-1] * 1000, 2) if lat else None},
            "clients": s.active_clients(),
            "requests": dict(s.stats),
            "demand_ranges": s.poller.demand_count(),
            "influx": None if not s.influx else {"ok": s.influx.ok, "buffered": len(s.influx.buf), **s.influx.stats},
        }


# --------------------------------------------------------------------------- main

def release_all(up, why):
    try:
        st, _ = up.request("POST", "/input", json.dumps({"release": ALL_BUTTONS}).encode(), Upstream.P_CONTROL)
        log.info("released all buttons (%s): HTTP %d", why, st)
    except UpstreamDown as e:
        log.warning("could not release buttons (%s): %s", why, e)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mister", default=os.environ.get("HUB_MISTER", "http://mister.lan:8765"), help="bridge URL")
    ap.add_argument("--listen", default="0.0.0.0:8766", help="hub address for dashboards (default 0.0.0.0:8766)")
    ap.add_argument("--interval", type=float, default=0.25, help="telemetry poll interval in s (default 0.25)")
    ap.add_argument("--max-age", type=float, default=None, help="oldest cached telemetry served, s (default 2x interval)")
    ap.add_argument("--demand-interval", type=float, default=0.5, help="poll interval for other watched ranges (default 0.5)")
    ap.add_argument("--vram-interval", type=float, default=1.0, help="poll interval for watched VRAM ranges (default 1.0)")
    ap.add_argument("--demand-ttl", type=float, default=10.0, help="stop watching a range this long after the last read (default 10)")
    ap.add_argument("--max-rate", type=float, default=60.0, help="upper bound on requests/s to the MiSTer (default 60)")
    ap.add_argument("--timeout", type=float, default=3.0, help="upstream request timeout in s (default 3)")
    ap.add_argument("--dashboard", default=None, help="serve this dashboard.html at http://<hub>/")
    ap.add_argument("--no-release", action="store_true", help="do not release buttons on connect/shutdown")
    g = ap.add_argument_group("InfluxDB (optional)")
    g.add_argument("--influx-url", default=os.environ.get("INFLUX_URL"), help="e.g. http://localhost:8086")
    g.add_argument("--influx-org", default=os.environ.get("INFLUX_ORG"), help="v2: organisation")
    g.add_argument("--influx-bucket", default=os.environ.get("INFLUX_BUCKET"), help="v2: bucket")
    g.add_argument("--influx-token", default=os.environ.get("INFLUX_TOKEN"), help="v2: API token (or INFLUX_TOKEN)")
    g.add_argument("--influx-token-file", help="v2: read the API token from this file")
    g.add_argument("--influx-db", default=os.environ.get("INFLUX_DB"), help="v1: database")
    g.add_argument("--influx-user", default=os.environ.get("INFLUX_USER"), help="v1: user")
    g.add_argument("--influx-password", default=os.environ.get("INFLUX_PASSWORD"), help="v1: password")
    g.add_argument("--influx-interval", type=float, default=1.0, help="seconds between telemetry points (default 1)")
    g.add_argument("--influx-flush", type=float, default=2.0, help="seconds between writes (default 2)")
    g.add_argument("--influx-buffer", type=int, default=200000, help="points kept while InfluxDB is down (default 200000)")
    g.add_argument("--influx-tag-host", help="value of the host tag (default: MiSTer host name)")
    ap.add_argument("--log-file", help="also log to this file (rotated at 5 MB)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    if args.max_age is None:
        args.max_age = 2 * args.interval
    if args.influx_token_file:
        with open(os.path.expanduser(args.influx_token_file)) as f:
            args.influx_token = f.read().strip()
    if args.influx_url and not (args.influx_bucket or args.influx_db):
        ap.error("--influx-url needs --influx-bucket (v2) or --influx-db (v1)")

    handlers = [logging.StreamHandler()]
    if args.log_file:
        handlers.append(logging.handlers.RotatingFileHandler(args.log_file, maxBytes=5 << 20, backupCount=3))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    up = Upstream(args.mister, args.timeout, args.max_rate)
    cache = Cache()
    poller = Poller(up, cache, args)

    def connected(epoch_changed):
        if epoch_changed:
            cache.clear()
        else:
            cache.invalidate()
        if not args.no_release:
            # runs on the upstream thread: send directly, the worker is not serving the queue yet
            try:
                up._send_once("POST", "/input", json.dumps({"release": ALL_BUTTONS}).encode())
                log.info("released all buttons on connect")
            except (OSError, http.client.HTTPException) as e:
                log.warning("could not release buttons on connect: %s", e)
    up.on_connect.append(connected)

    host, _, port = args.listen.rpartition(":")
    server = Hub((host or "0.0.0.0", int(port)), up, cache, poller, args)
    server.influx = Influx(args) if args.influx_url else None

    stop = threading.Event()
    up.start()
    poller.start()
    if server.influx:
        server.influx.start()
        threading.Thread(target=recorder, args=(poller, up, server.influx, server, args, stop),
                         name="recorder", daemon=True).start()
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    log.info("dashboard hub %s listening on http://%s:%s, MiSTer bridge %s%s", VERSION, host or "0.0.0.0", port,
             up.base, f", InfluxDB {args.influx_url}" if server.influx else "")

    def on_signal(signum, _frame):
        log.info("signal %d: shutting down", signum)
        stop.set()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    stop.wait()

    server.shutdown()
    poller.stop.set()
    if not args.no_release and up.up:
        release_all(up, "shutdown")
    if server.influx:
        server.influx.stop.set()
        server.influx.thread.join(timeout=12)
    up.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
