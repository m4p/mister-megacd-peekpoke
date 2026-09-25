#!/usr/bin/env python3
"""Dashboard API conformance runner (Genesis-Plus-GX/DASHBOARD-API.md section 5).

Runs from the dashboard computer against any server:
    python3 conformance.py --base http://mister.local:8765
    python3 conformance.py --base http://mister.local:8765 --destructive

Without --destructive, it only reads memory and releases buttons that nobody
should be holding. --destructive writes every patch state, restores the bytes
it found, presses/releases buttons, pauses/resumes, and saves/loads a state.
Use a dedicated game session.

Each contract item is PASS, FAIL, or UNSUPPORTED (the server answered with an
explicit feature_unavailable). UNSUPPORTED is a failure of the full contract
unless --allow-unsupported is given (useful for the transport/control milestones).
"""
import argparse
import http.client
import json
import os
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = json.load(open(os.path.join(HERE, "api-fixtures.json")))
CORS = {"access-control-allow-origin": "*",
        "access-control-allow-methods": "GET, POST, OPTIONS",
        "access-control-allow-headers": "Content-Type"}

results = []


class Unsupported(Exception):
    pass


class Client:
    def __init__(self, base):
        u = urllib.parse.urlparse(base)
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")

    def conn(self):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=10)

    def request(self, method, path, body=None, headers=None):
        c = self.conn()
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body)
            h["Content-Type"] = "application/json"
        c.request(method, self.prefix + path, data, h)
        r = c.getresponse()
        raw = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        try:
            j = json.loads(raw) if raw else None
        except ValueError:
            j = None
        return r.status, hdrs, j

    def post(self, path, body=None):
        st, h, j = self.request("POST", path, body)
        if st == 501 or (j and j.get("error", {}).get("code") == "feature_unavailable"):
            raise Unsupported(j["error"]["message"] if j else "HTTP 501")
        if st != 200 or not isinstance(j, dict) or j.get("ok") is False:
            raise AssertionError(f"{path}: HTTP {st} {j}")
        return j, h


def item(name):
    def deco(fn):
        def run(*a):
            try:
                detail = fn(*a) or ""
                results.append(("PASS", name, detail))
            except Unsupported as e:
                results.append(("UNSUPPORTED", name, str(e)))
            except Exception as e:  # noqa: BLE001
                results.append(("FAIL", name, str(e)))
        return run
    return deco


def is_hex(s, n):
    return isinstance(s, str) and len(s) == 2 * n and all(c in "0123456789abcdefABCDEF" for c in s)


def peek(c, addr, n):
    j, _ = c.post("/bus-peek", {"bus": "main68k", "address": addr, "length": n, "encoding": "hex"})
    assert is_hex(j.get("data"), n), f"bad data for ${addr:06X}:{n}: {j}"
    return j["data"].lower()


@item("CORS headers and OPTIONS preflight")
def t_cors(c):
    for path in FIX["routes"]:
        st, h, _ = c.request("OPTIONS", path, headers={"Origin": "http://dashboard.example",
                                                       "Access-Control-Request-Method": "POST",
                                                       "Access-Control-Request-Headers": "content-type"})
        assert 200 <= st < 300, f"OPTIONS {path} -> {st}"
        for k, v in CORS.items():
            assert h.get(k) == v, f"OPTIONS {path}: {k}={h.get(k)}"
    st, h, j = c.request("POST", "/bus-peek", {"bus": "main68k", "address": 1, "length": 1})
    assert h.get("access-control-allow-origin") == "*", "CORS missing on error responses"
    assert st >= 400 and j and j.get("ok") is False and "message" in j.get("error", {}), f"structured error: {st} {j}"
    return f"{len(FIX['routes'])} routes"


@item("/bus-peek on main68k, big-endian hex, numeric addresses (all dashboard reads)")
def t_reads(c):
    for r in FIX["telemetry_reads"] + FIX["patch_site_reads"]:
        peek(c, r["address"], r["length"])
    for n in (1, 2, 4, 10, 56, 64):
        peek(c, 0xFF7ABD, n)   # odd start
    return f"{len(FIX['telemetry_reads'])} telemetry + {len(FIX['patch_site_reads'])} patch-site reads"


@item("patch status badges resolve (read-only)")
def t_badges(c):
    out = []
    for p in FIX["patches"]:
        if p.get("vram"):
            continue
        live = [peek(c, s["address"], s["length"]) for s in p["sites"]]
        match = next((s["key"] for s in p["states"] if s["hex"] == live), "MIXED")
        out.append(f"{p['id']}={match}")
    return ", ".join(out)


@item("/peek on vram with GPGX word-swapped layout")
def t_vram_peek(c):
    v = FIX["vram"][0]
    j, _ = c.post("/peek", {"domain": "vram", "address": v["sig_address"], "length": v["sig_length"], "encoding": "hex"})
    assert is_hex(j.get("data"), v["sig_length"]), j
    sigs = {s["sig"]: s["key"] for s in FIX["patches"][-1]["states"]}
    return f"signature {'= ' + sigs[j['data'].lower()] if j['data'].lower() in sigs else 'MIXED'}"


@item("/input release is idempotent and reports held")
def t_release(c):
    for _ in range(2):
        j, _ = c.post("/input", {"release": FIX["input_buttons"]})
        assert j.get("held", []) == [] or isinstance(j.get("held"), list), j


@item("large VRAM body accepted by the server (2.2 KB)")
def t_body_size(c):
    body = {"domain": "vram", "address": 0x10000 - 2, "data": "00" * 1056, "encoding": "hex"}
    st, _, j = c.request("POST", "/poke", body)
    assert st not in (413, 431) and st < 500 or st == 501, f"large body rejected: {st} {j}"
    assert st != 200, "out-of-range VRAM poke must not succeed"


@item("/state/load rejects a missing file clearly")
def t_missing_state(c):
    st, _, j = c.request("POST", "/state/load", {"path": "conformance-does-not-exist.gp0"})
    assert st >= 400 and j and j.get("ok") is False, f"{st} {j}"


# ---------------------------------------------------------------- destructive

@item("/bus-poke code/data patches take effect and restore (all PATCHES states)")
def t_patches(c):
    n = 0
    for p in FIX["patches"]:
        if p.get("vram"):
            continue
        writes = [(s["address"], s["length"]) for s in p["sites"]]
        for st_ in p["states"]:
            writes += [(e["address"], len(e["hex"]) // 2) for e in st_.get("extra", [])]
        before = {a: peek(c, a, ln) for a, ln in set(writes)}
        try:
            for st_ in p["states"]:
                for site, hx in zip(p["sites"], st_["hex"]):
                    c.post("/bus-poke", {"bus": "main68k", "address": site["address"], "data": hx,
                                         "encoding": "hex", "unsafe": True})
                    assert peek(c, site["address"], site["length"]) == hx, f"{p['id']}/{st_['key']} readback"
                    n += 1
        finally:
            for a, hx in before.items():
                try:
                    c.post("/bus-poke", {"bus": "main68k", "address": a, "data": hx, "encoding": "hex", "unsafe": True})
                except Unsupported:
                    pass
    return f"{n} site writes verified and restored"


@item("/poke vram 1056 bytes + refresh round trip, restored")
def t_vram_poke(c):
    v = FIX["vram"][0]
    fresh = FIX["patches"][-1]["states"]
    orig, _ = c.post("/peek", {"domain": "vram", "address": v["address"], "length": 1056, "encoding": "hex"})
    try:
        for s in fresh:
            c.post("/poke", {"domain": "vram", "address": v["address"], "data": s["data"], "encoding": "hex"})
            j, _ = c.post("/state/save", {"path": FIX["reserved_state_names"][0]})
            assert j.get("path") == FIX["reserved_state_names"][0], j
            c.post("/state/load", {"path": FIX["reserved_state_names"][0]})
            back, _ = c.post("/peek", {"domain": "vram", "address": v["address"], "length": 1056, "encoding": "hex"})
            assert back["data"].lower() == s["data"], f"{s['key']} readback"
    finally:
        c.post("/poke", {"domain": "vram", "address": v["address"], "data": orig["data"], "encoding": "hex"})


@item("/input persistent press and release on player 1")
def t_input(c):
    for b in FIX["input_buttons"]:
        j, _ = c.post("/input", {"press": [b]})
        assert b in j.get("held", [b]), j
        time.sleep(0.05)
        j, _ = c.post("/input", {"release": [b]})
        assert b not in j.get("held", []), j


@item("/pause and /resume, idempotent, reads serviced while paused")
def t_pause(c):
    for p in ("/pause", "/pause"):
        c.post(p)
    peek(c, 0xFF6FEA, 2)
    c.post("/input", {"release": FIX["input_buttons"]})
    for p in ("/resume", "/resume"):
        c.post(p)


@item("/state/save echoes path; /state/load of the same relative path")
def t_state(c):
    name = f"conformance-{int(time.time())}.gp0"
    j, _ = c.post("/state/save", {"path": name})
    assert j.get("path") == name, j
    c.post("/state/load", {"path": name})


@item("desertbus-fullauto.gp0 loads in this server's own format")
def t_fullauto(c):
    c.post("/state/load", {"path": FIX["fullauto_state"]})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--destructive", action="store_true")
    ap.add_argument("--allow-unsupported", action="store_true")
    ap.add_argument("--json", help="write results as JSON to this file")
    args = ap.parse_args()
    c = Client(args.base)

    st, _, caps = c.request("GET", "/capabilities")
    print(f"server {args.base}: " + (json.dumps(caps.get("core")) if st == 200 and caps else f"no /capabilities (HTTP {st})"))

    for t in (t_cors, t_reads, t_badges, t_vram_peek, t_release, t_body_size, t_missing_state):
        t(c)
    if args.destructive:
        for t in (t_patches, t_vram_poke, t_input, t_pause, t_state, t_fullauto):
            t(c)
    else:
        results.append(("SKIPPED", "destructive items (writes, input, pause, savestates)", "run with --destructive"))

    width = max(len(r[0]) for r in results)
    for status, name, detail in results:
        print(f"{status:<{width}}  {name}" + (f"  [{detail}]" if detail else ""))
    bad = [r for r in results if r[0] == "FAIL" or (r[0] == "UNSUPPORTED" and not args.allow_unsupported)]
    if args.json:
        json.dump([dict(zip(("status", "item", "detail"), r)) for r in results], open(args.json, "w"), indent=1)
    print(f"{'CONFORMANT' if not bad else 'NOT CONFORMANT'}: "
          f"{sum(r[0] == 'PASS' for r in results)} pass, {sum(r[0] == 'FAIL' for r in results)} fail, "
          f"{sum(r[0] == 'UNSUPPORTED' for r in results)} unsupported")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
