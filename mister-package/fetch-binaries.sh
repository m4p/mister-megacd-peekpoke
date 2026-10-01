#!/bin/sh
# Collect the three built binaries into sdcard/ and write SHA256SUMS.
#
#   ./fetch-binaries.sh mister root@<mister-ip>   # copy what the validated MiSTer runs (preferred)
#   ./fetch-binaries.sh vm map@192.168.64.2         # copy the build outputs from the build VM
#
# The MiSTer copies are the ones validated on hardware on 2026-09-26/30. The VM copies are
# the latest builds in /home/map/claude and are only right if nothing was rebuilt since.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SD=$HERE/sdcard
RBF=MegaCD_DashCtl_20260926.rbf
MAIN_SHA_PREFIX=3f8107d5ffad1bab   # first 16 hex digits of the validated control Main

[ $# -eq 2 ] || { sed -n '2,8p' "$0"; exit 2; }
SRC=$1 HOST=$2
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"

case $SRC in
mister)
	$SSH "$HOST" "cat /media/fat/_Dashboard/$RBF" > "$SD/_Dashboard/$RBF"
	$SSH "$HOST" "cat /media/fat/MiSTer.dashboard-control" > "$SD/MiSTer.dashboard-control"
	$SSH "$HOST" "cat /media/fat/megacd-dashboard/megacd-dashboard" > "$SD/megacd-dashboard/megacd-dashboard"
	;;
vm)
	W=/home/map/claude
	$SSH "$HOST" "cat $W/MegaCD_MiSTer/output_files/MegaCD_Dashboard.rbf" > "$SD/_Dashboard/$RBF"
	$SSH "$HOST" "cat $W/Main_MiSTer/bin/MiSTer" > "$SD/MiSTer.dashboard-control"
	$SSH "$HOST" "cat $W/MegaCD_MiSTer/linux/dashboard-bridge/build-arm/megacd-dashboard" > "$SD/megacd-dashboard/megacd-dashboard"
	;;
*)
	echo "source must be 'mister' or 'vm'"; exit 2 ;;
esac
chmod +x "$SD/MiSTer.dashboard-control" "$SD/megacd-dashboard/megacd-dashboard"

sum=$(shasum -a 256 "$SD/MiSTer.dashboard-control" | cut -c1-16)
if [ "$sum" != "$MAIN_SHA_PREFIX" ]; then
	echo "WARNING: Main is $sum..., not the validated control build $MAIN_SHA_PREFIX..."
fi
file "$SD/megacd-dashboard/megacd-dashboard" | grep -q "ARM" ||
	echo "WARNING: megacd-dashboard is not an ARM binary"

cd "$HERE"
find sdcard -type f ! -name .DS_Store | LC_ALL=C sort | while IFS= read -r f; do shasum -a 256 "$f"; done > SHA256SUMS
cat SHA256SUMS
