#!/usr/bin/env python3
"""Compare a candidate Quartus build against the baseline and the resource budget.

    python3 scripts/check_dashboard_reports.py --baseline ../reports/megacd-baseline \
        --candidate output_files --revision MegaCD_Dashboard --budget docs/dashboard-resource-budget.json

Reads <rev>.flow.rpt, <rev>.fit.summary and <rev>.sta.summary (Quartus 17
Standard). Exits 1 on a missing/inconsistent report, a failed flow, a device
mismatch, a budget violation, or any negative slack in the candidate.
The baseline revision name is detected from its fit summary.

Parser status: validated against synthetic reports in scripts/testdata that
follow the documented Quartus 17 summary layout. Re-validate against the first
real baseline before trusting a pass.
"""
import argparse
import glob
import json
import os
import re
import sys


def kv(path):
    out = {}
    with open(path, errors="replace") as f:
        for line in f:
            m = re.match(r"^\s*([^:;]+?)\s*:\s*(.*?)\s*$", line)
            if m:
                out.setdefault(m.group(1), m.group(2))
    return out


def used_avail(s):
    m = re.match(r"([\d,]+)\s*(?:/\s*([\d,]+))?", s or "")
    if not m:
        return None, None
    used = int(m.group(1).replace(",", ""))
    avail = int(m.group(2).replace(",", "")) if m.group(2) else None
    return used, avail


def parse_fit(path):
    d = kv(path)
    r = {"status": d.get("Fitter Status", ""), "device": d.get("Device", ""), "revision": d.get("Revision Name", "")}
    for key, name in (("Logic utilization (in ALMs)", "alm"), ("Total registers", "registers"),
                      ("Total block memory bits", "mem_bits"), ("Total RAM Blocks", "ram_blocks"),
                      ("Total DSP Blocks", "dsp"), ("Total PLLs", "pll")):
        r[name], r[name + "_avail"] = used_avail(d.get(key))
    return r


def parse_sta(path):
    entries = []
    cur = None
    with open(path, errors="replace") as f:
        for line in f:
            m = re.match(r"^\s*Type\s*:\s*(.+?)\s*$", line)
            if m:
                cur = {"type": m.group(1)}
                entries.append(cur)
                continue
            m = re.match(r"^\s*(Slack|TNS)\s*:\s*(-?[\d.]+)", line)
            if m and cur is not None:
                cur[m.group(1).lower()] = float(m.group(2))
    return entries


def find(dirpath, suffix, revision=None):
    if revision:
        p = os.path.join(dirpath, revision + suffix)
        return p if os.path.exists(p) else None
    hits = sorted(glob.glob(os.path.join(dirpath, "*" + suffix)))
    return hits[0] if len(hits) == 1 else None


def load(dirpath, revision, problems, label):
    fit_p = find(dirpath, ".fit.summary", revision)
    if not fit_p:
        problems.append(f"{label}: no unique .fit.summary in {dirpath}")
        return None
    rev = revision or os.path.basename(fit_p)[:-len(".fit.summary")]
    sta_p = find(dirpath, ".sta.summary", rev)
    flow_p = find(dirpath, ".flow.rpt", rev)
    fit = parse_fit(fit_p)
    if not sta_p:
        problems.append(f"{label}: missing {rev}.sta.summary")
    if not flow_p:
        problems.append(f"{label}: missing {rev}.flow.rpt")
    sta = parse_sta(sta_p) if sta_p else []
    flow = kv(flow_p) if flow_p else {}
    if not fit["status"].startswith("Successful"):
        problems.append(f"{label}: fitter status '{fit['status']}'")
    if flow and not flow.get("Flow Status", "").startswith("Successful"):
        problems.append(f"{label}: flow status '{flow.get('Flow Status')}'")
    if fit["revision"] and fit["revision"] != rev:
        problems.append(f"{label}: fit summary is for revision {fit['revision']}, expected {rev}")
    for k in ("alm", "registers", "ram_blocks", "dsp", "pll"):
        if fit[k] is None:
            problems.append(f"{label}: could not parse '{k}' from {fit_p}")
    if sta_p and not sta:
        problems.append(f"{label}: no timing entries in {sta_p}")
    return {"rev": rev, "fit": fit, "sta": sta}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--candidate", required=True)
    ap.add_argument("--revision", required=True)
    ap.add_argument("--budget", required=True)
    args = ap.parse_args()

    budget = json.load(open(args.budget))
    problems = []
    base = load(args.baseline, None, problems, "baseline")
    cand = load(args.candidate, args.revision, problems, "candidate")
    if not base or not cand:
        print("\n".join("FAIL: " + p for p in problems))
        sys.exit(1)

    bf, cf = base["fit"], cand["fit"]
    for label, f in (("baseline", bf), ("candidate", cf)):
        if f["device"] != budget["device"]:
            problems.append(f"{label}: device {f['device']} != {budget['device']}")

    def delta(k):
        return None if cf[k] is None or bf[k] is None else cf[k] - bf[k]

    rows = [("ALMs", "alm"), ("Registers", "registers"), ("Block memory bits", "mem_bits"),
            ("RAM blocks (M10K)", "ram_blocks"), ("DSP blocks", "dsp"), ("PLLs", "pll")]
    print(f"{'resource':20s} {'baseline':>12s} {'candidate':>12s} {'delta':>8s} {'available':>10s}")
    for name, k in rows:
        print(f"{name:20s} {str(bf[k]):>12s} {str(cf[k]):>12s} {str(delta(k)):>8s} {str(cf[k + '_avail']):>10s}")

    checks = (("alm", "max_alm_delta"), ("ram_blocks", "max_ram_block_delta"), ("dsp", "max_dsp_delta"), ("pll", "max_pll_delta"))
    for k, bk in checks:
        d = delta(k)
        if d is not None and d > budget[bk]:
            problems.append(f"{k} grew by {d}, budget {budget[bk]}")
    if cf["alm_avail"]:
        free = 100.0 * (cf["alm_avail"] - cf["alm"]) / cf["alm_avail"]
        print(f"ALM free: {free:.1f}% (minimum {budget['min_alm_free_pct']}%)")
        if free < budget["min_alm_free_pct"]:
            problems.append(f"only {free:.1f}% ALMs free")
    if cf["ram_blocks_avail"] and cf["ram_blocks_avail"] - cf["ram_blocks"] < budget["min_ram_block_free"]:
        problems.append("RAM block headroom below minimum")

    print("\ntiming (candidate):")
    for e in cand["sta"]:
        flag = "" if e.get("slack", 0) >= 0 else "   <-- NEGATIVE"
        print(f"  {e.get('slack', float('nan')):9.3f}  {e['type']}{flag}")
        if "slack" not in e:
            problems.append(f"no slack parsed for {e['type']}")
        elif e["slack"] < 0:
            problems.append(f"negative slack {e['slack']} for {e['type']}")
    kinds = {k for e in cand["sta"] for k in ("Setup", "Hold") if f" {k} " in f" {e['type']} "}
    for k in ("Setup", "Hold"):
        if k not in kinds:
            problems.append(f"no {k} timing entries in candidate sta.summary")

    if problems:
        print("\n" + "\n".join("FAIL: " + p for p in problems))
        sys.exit(1)
    print("\nPASS: fit, budget and timing gates")


if __name__ == "__main__":
    main()
