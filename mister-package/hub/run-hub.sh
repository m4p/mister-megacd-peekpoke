#!/bin/sh
# Run the dashboard hub on the Mac (Python 3.8+). It is the only client of the MiSTer
# bridge; dashboards connect to the Mac. The hub is not installed on the MiSTer.
# Extra options are passed on, e.g. InfluxDB settings:
#
#   ./run-hub.sh http://<mister-ip>:8765
#   ./run-hub.sh http://<mister-ip>:8765 --influx-url http://localhost:8086 \
#       --influx-org home --influx-bucket desertbus      # token in INFLUX_TOKEN
#
# Then open http://localhost:8766/ and connect the dashboard to http://<mac-ip>:8766.
HERE=$(cd "$(dirname "$0")" && pwd)
[ $# -ge 1 ] || { sed -n '2,10p' "$0"; exit 2; }
MISTER=$1; shift
src="$HERE/../../tools/dashboard-hub/dashboard_hub.py"
if [ -f "$src" ] && ! cmp -s "$src" "$HERE/dashboard_hub.py"; then
	echo "note: $HERE/dashboard_hub.py differs from tools/dashboard-hub/; copy the current one over" >&2
fi
exec python3 "$HERE/dashboard_hub.py" --mister "$MISTER" --dashboard "$HERE/../dashboard/dashboard.html" "$@"
