#!/bin/sh
# Install the MegaCD dashboard package on a MiSTer over SSH (run on your computer).
#
#   ./install.sh root@<mister-ip> [--bootcore] [--reboot]
#
#   --bootcore  boot straight into "Desert Bus (control).mgl" (no countdown)
#   --reboot    reboot the MiSTer at the end (needed for the new Main to take over)
#
# What it does, in order:
#   1. checks SHA256SUMS of the package
#   2. backs up MiSTer, MiSTer.ini and linux/user-startup.sh to
#      /media/fat/backup-before-dashboard-<date>/ (skipped if that folder exists)
#   3. copies sdcard/ onto /media/fat (core, .mgl, Main, bridge)
#   4. makes the dashboard Main the active one (/media/fat/MiSTer)
#   5. adds the bridge autostart block to linux/user-startup.sh (idempotent)
#   6. with --bootcore, sets bootcore= in MiSTer.ini and comments out bootcore_timeout
# Set FAT to install into another directory (used by the self-test).
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
FAT=${FAT:-/media/fat}
MGL="Desert Bus (control).mgl"
CUE="/media/fat/games/MegaCD/Desert Bus/PTSM1.cue"

[ $# -ge 1 ] || { sed -n '2,8p' "$0"; exit 2; }
HOST=$1; shift
BOOTCORE=0 REBOOT=0
for a in "$@"; do
	case $a in
	--bootcore) BOOTCORE=1 ;;
	--reboot) REBOOT=1 ;;
	*) echo "unknown option $a"; exit 2 ;;
	esac
done
remote() { ${MISTER_SSH:-ssh -o BatchMode=yes -o ConnectTimeout=10} "$HOST" "$@"; }

echo "== 1. checking package"
cd "$HERE"
[ -f SHA256SUMS ] || { echo "SHA256SUMS missing: run ./fetch-binaries.sh first"; exit 1; }
shasum -a 256 -c SHA256SUMS

echo "== 2. backup"
remote "B=$FAT/backup-before-dashboard-\$(date +%Y%m%d)
if ls -d $FAT/backup-before-dashboard-* >/dev/null 2>&1; then
	echo \"existing backup kept: \$(ls -d $FAT/backup-before-dashboard-* | head -1)\"
else
	mkdir -p \$B
	cp -a $FAT/MiSTer $FAT/MiSTer.ini \$B/ 2>/dev/null || true
	cp -a $FAT/linux/user-startup.sh \$B/ 2>/dev/null || true
	(cd \$B && sha256sum MiSTer > SHA256SUMS)
	echo \"backup in \$B\"
fi
[ -f '${CUE}' ] || echo 'WARNING: ${CUE} not found; edit sdcard/_Dashboard/${MGL} to match your disc image'"

echo "== 3. copying files"
remote "[ -x $FAT/megacd-dashboard/start.sh ] && $FAT/megacd-dashboard/start.sh stop || true"
COPYFILE_DISABLE=1 tar -C sdcard --exclude .DS_Store -cf - . | remote "tar -C $FAT -xf -"

echo "== 4. activating the dashboard Main"
remote "cp $FAT/MiSTer.dashboard-control $FAT/MiSTer && chmod +x $FAT/MiSTer $FAT/megacd-dashboard/megacd-dashboard $FAT/megacd-dashboard/start.sh"

echo "== 5. bridge autostart"
remote "F=$FAT/linux/user-startup.sh
mkdir -p $FAT/linux
[ -f \$F ] || { [ -f $FAT/linux/_user-startup.sh ] && cp $FAT/linux/_user-startup.sh \$F || echo '#!/bin/sh' > \$F; }
grep -q '# >>> megacd-dashboard >>>' \$F || cat >> \$F <<'EOF'

# >>> megacd-dashboard >>>
[ -x /media/fat/megacd-dashboard/start.sh ] && /media/fat/megacd-dashboard/start.sh \"\$1\"
# <<< megacd-dashboard <<<
EOF
chmod +x \$F
$FAT/megacd-dashboard/start.sh start || echo 'WARNING: bridge did not start; see /tmp/megacd-dashboard.log'"

if [ $BOOTCORE = 1 ]; then
	echo "== 6. bootcore"
	# edit locally: MiSTer.ini uses CRLF line endings, which must be kept
	remote "cat $FAT/MiSTer.ini" | python3 "$HERE/tools/ini_bootcore.py" set "$MGL" > "$HERE/.MiSTer.ini.new"
	remote "cat > $FAT/MiSTer.ini" < "$HERE/.MiSTer.ini.new"
	rm -f "$HERE/.MiSTer.ini.new"
	remote "grep -n 'bootcore' $FAT/MiSTer.ini | tr -d '\r'"
fi

remote sync
if [ $REBOOT = 1 ]; then
	echo "== rebooting"
	remote reboot || true
else
	echo "Done. Reboot the MiSTer (ssh $HOST reboot) so the new Main takes over."
fi
