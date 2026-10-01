#!/bin/sh
# Remove the MegaCD dashboard from a MiSTer and go back to the backed-up Main.
#
#   ./uninstall.sh root@<mister-ip> [--purge] [--reboot]
#
#   --purge   also delete _Dashboard/, megacd-dashboard/ and MiSTer.dashboard-control
#   --reboot  reboot at the end
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
FAT=${FAT:-/media/fat}
[ $# -ge 1 ] || { sed -n '2,7p' "$0"; exit 2; }
HOST=$1; shift
PURGE=0 REBOOT=0
for a in "$@"; do
	case $a in
	--purge) PURGE=1 ;;
	--reboot) REBOOT=1 ;;
	*) echo "unknown option $a"; exit 2 ;;
	esac
done
remote() { ${MISTER_SSH:-ssh -o BatchMode=yes -o ConnectTimeout=10} "$HOST" "$@"; }

remote "[ -x $FAT/megacd-dashboard/start.sh ] && $FAT/megacd-dashboard/start.sh stop || true
F=$FAT/linux/user-startup.sh
if [ -f \$F ]; then sed '/# >>> megacd-dashboard >>>/,/# <<< megacd-dashboard <<</d' \$F > \$F.tmp && cat \$F.tmp > \$F && rm -f \$F.tmp; fi
B=\$(ls -d $FAT/backup-before-dashboard-* 2>/dev/null | head -1)
if [ -n \"\$B\" ] && [ -f \$B/MiSTer ]; then
	cp \$B/MiSTer $FAT/MiSTer && echo \"Main restored from \$B\"
else
	echo 'WARNING: no backup Main found; run the MiSTer updater to get stock Main back'
fi"

remote "cat $FAT/MiSTer.ini" | python3 "$HERE/tools/ini_bootcore.py" unset > "$HERE/.MiSTer.ini.new"
remote "cat > $FAT/MiSTer.ini" < "$HERE/.MiSTer.ini.new"
rm -f "$HERE/.MiSTer.ini.new"

if [ $PURGE = 1 ]; then
	remote "rm -rf $FAT/_Dashboard $FAT/megacd-dashboard $FAT/MiSTer.dashboard-control"
fi
remote sync
if [ $REBOOT = 1 ]; then remote reboot || true; else echo "Done. Reboot the MiSTer to load the restored Main."; fi
