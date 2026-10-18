#!/usr/bin/env python3
"""Example: read the latest Desert Bus record from InfluxDB (InfluxDB 3 SQL API).

    INFLUX_TOKEN=... python3 query_latest.py
    python3 query_latest.py --token-file ~/influxtoken.txt
    python3 query_latest.py --token-file ~/influxtoken.txt --events 5 --json

Prints the newest point of the `desertbus` measurement (what the dashboard hub writes once
per second) and, with --events N, the N newest game events. The fields are described in
INFLUXDB.md. Standard library only.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://serv-influxdb.desertbus.org/"


def query(url, token, db, sql):
    """Run one SQL query; returns a list of row dicts."""
    q = urllib.parse.urlencode({"db": db, "q": sql, "format": "json"})
    req = urllib.request.Request(url.rstrip("/") + "/api/v3/query_sql?" + q,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read() or b"[]")
    except urllib.error.HTTPError as e:
        sys.exit(f"InfluxDB answered HTTP {e.code}: {e.read()[:300].decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {url}: {e.reason}")


def show_record(row):
    """Human-readable summary of one desertbus row."""
    if not row.get("in_game"):
        return f"{row['time']}  Desert Bus not running (game_state {row.get('game_state')})"
    clock = f"{row.get('clock_hour')}:{row.get('clock_minute', 0):02d}"
    if row.get("game_state") != 3:
        side = "— (not driving: the game parks the position outside the driving state)"
    else:
        side = "off the road" if row.get("offroad") else f"{row.get('lateral_norm', 0):+.2f} (−1 left … +1 right)"
    stop = (f"{row['next_stop_miles']:.2f} mi" + (f", ETA {row['next_stop_eta_s']} s" if row.get("next_stop_eta_s") is not None else "")
            if row.get("next_stop_miles") is not None else "none left on this leg")
    lines = [
        f"time          {row['time']}  (host {row.get('host')})",
        f"state         {row.get('game_state')} {'(driving)' if row.get('game_state') == 3 else ''}"
        f"{'  PAUSED' if row.get('paused') else ''}",
        f"driver        {row.get('driver_name')}",
        f"speed         {row.get('speed_mph', 0):.1f} mph",
        f"position      {side}",
        f"leg           {row.get('leg_miles', 0):.2f} of 360 mi ({'Las Vegas → Tucson' if row.get('return_leg') else 'Tucson → Las Vegas'})",
        f"odometer      {row.get('odometer_miles')} mi, {row.get('points')} point(s)",
        f"clock         {clock} ({row.get('phase')})",
        f"next stop     {stop}",
    ]
    if row.get("splat_visible"):
        lines.append("bug splat     on the windshield")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.environ.get("INFLUX_URL", DEFAULT_URL))
    ap.add_argument("--db", default=os.environ.get("INFLUX_DB", "telemetry"), help="database (default telemetry)")
    ap.add_argument("--token-file", help="file with the API token (default: INFLUX_TOKEN)")
    ap.add_argument("--host", help="only records of this hub host tag (e.g. mister.lan)")
    ap.add_argument("--events", type=int, default=0, metavar="N", help="also show the N newest game events")
    ap.add_argument("--json", action="store_true", help="print raw JSON rows")
    args = ap.parse_args()

    token = os.environ.get("INFLUX_TOKEN")
    if args.token_file:
        with open(os.path.expanduser(args.token_file)) as f:
            token = f.read().strip()
    if not token:
        sys.exit("no token: set INFLUX_TOKEN or pass --token-file")

    where = f"WHERE host = '{args.host}'" if args.host else ""
    rows = query(args.url, token, args.db, f"SELECT * FROM desertbus {where} ORDER BY time DESC LIMIT 1")
    events = query(args.url, token, args.db,
                   f"SELECT time, event, details FROM desertbus_event {where} ORDER BY time DESC LIMIT {args.events}") \
        if args.events > 0 else []

    if args.json:
        print(json.dumps({"latest": rows[0] if rows else None, "events": events}, indent=2))
        return 0
    print(show_record(rows[0]) if rows else "no desertbus records yet")
    if events:
        print(f"\nlatest {len(events)} event(s):")
        for e in events:
            print(f"  {e['time']}  {e['event']:16s} {e.get('details', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
