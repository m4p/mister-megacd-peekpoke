#!/usr/bin/env python3
"""Extract API fixtures from the dashboard itself so tests cover exactly what it sends.

Reads Genesis-Plus-GX/sdl/dashboard.html, evaluates its PATCHES table with node,
collects every route and telemetry read, validates the payloads, and writes
api-fixtures.json next to this script.

usage: extract_fixtures.py [--dashboard PATH] [--out PATH] [--check]
  --check  fail if the committed fixture file differs from a fresh extraction
"""
import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DASH = os.path.normpath(os.path.join(HERE, "../../../Genesis-Plus-GX/sdl/dashboard.html"))
ROUTES = ["/bus-peek", "/bus-poke", "/peek", "/poke", "/input", "/pause", "/resume", "/state/save", "/state/load"]


def eval_patches(html):
    start = html.index("const PATCHES = [")
    end = html.index("];", html.index("\n];", start)) + 2
    js = html[start:end] + "\nprocess.stdout.write(JSON.stringify(PATCHES));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dashboard", default=DEFAULT_DASH)
    ap.add_argument("--out", default=os.path.join(HERE, "api-fixtures.json"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    html = open(args.dashboard, encoding="utf-8").read()
    patches = eval_patches(html)

    routes = sorted(r for r in ROUTES if f'"{r}"' in html or f"'{r}'" in html or f"${{API_BASE}}{r}`" in html)
    missing = [r for r in ROUTES if r not in routes]

    reads = []
    for m in re.finditer(r"busPeek\((0x[0-9A-Fa-f]+),\s*(\d+)", html):
        item = {"address": int(m.group(1), 16), "length": int(m.group(2))}
        if item not in reads:
            reads.append(item)

    errors = []
    bus_sites = []
    for p in patches:
        if p.get("vram"):
            for s in p["states"]:
                if len(s["data"]) != 2112:
                    errors.append(f"{p['id']}/{s['key']}: VRAM data is {len(s['data'])} hex chars, expected 2112")
                if len(s["sig"]) != p["vram"]["sig"]["length"] * 2:
                    errors.append(f"{p['id']}/{s['key']}: signature length mismatch")
            continue
        for site in p["sites"]:
            bus_sites.append(site)
        for s in p["states"]:
            for site, hx in zip(p["sites"], s["hex"]):
                if len(hx) != site["length"] * 2:
                    errors.append(f"{p['id']}/{s['key']}: {len(hx)} hex chars for {site['length']}-byte site")
            for ex in s.get("extra", []):
                if not 0xFF0000 <= ex["address"] <= 0xFFFFFF:
                    errors.append(f"{p['id']}/{s['key']}: extra write outside work RAM")

    fixtures = {
        "source": os.path.relpath(args.dashboard, HERE),
        "routes": routes,
        "route_count": len(routes),
        "telemetry_reads": reads,
        "patch_site_reads": bus_sites,
        "max_bus_length": max(s["length"] for s in bus_sites + reads),
        "vram": [{"id": p["id"], "address": p["vram"]["address"], "length": 1056,
                  "sig_address": p["vram"]["address"] + p["vram"]["sig"]["offset"],
                  "sig_length": p["vram"]["sig"]["length"]} for p in patches if p.get("vram")],
        "reserved_state_names": ["dashboard-cache-refresh.gp0"],
        "fullauto_state": "desertbus-fullauto.gp0",
        "input_buttons": ["left", "right", "start"],
        "patches": patches,
    }

    if missing:
        errors.append(f"routes not found in dashboard: {missing}")
    if fixtures["route_count"] != 9:
        errors.append(f"expected 9 distinct POST routes, found {fixtures['route_count']}")
    if errors:
        print("\n".join("ERROR: " + e for e in errors))
        sys.exit(1)

    text = json.dumps(fixtures, indent=1) + "\n"
    if args.check:
        old = open(args.out).read() if os.path.exists(args.out) else ""
        if old != text:
            print(f"{args.out} is out of date; rerun extract_fixtures.py")
            sys.exit(1)
        print("fixtures up to date")
        return
    open(args.out, "w").write(text)
    print(f"wrote {args.out}: {len(routes)} routes, {len(reads)} telemetry reads, "
          f"{len(bus_sites)} patch sites, {len(patches)} patches, max bus length {fixtures['max_bus_length']}")


if __name__ == "__main__":
    main()
