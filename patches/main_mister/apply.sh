#!/bin/bash
# Apply the MegaCD dashboard IPC adapter to a Main_MiSTer checkout.
#   patches/main_mister/apply.sh [MAIN_DIR]     (default: ../Main_MiSTer next to this repo)
#   patches/main_mister/apply.sh --clone [MAIN_DIR]   clone Main_MiSTer at the pinned commit first
# Idempotent: does nothing if the patch is already applied.
set -euo pipefail

PINNED=aa271e41ebbf616903f9e0216b0900aead5bfce1
HERE=$(cd "$(dirname "$0")" && pwd)
PATCH=$HERE/0001-megacd-dashboard-ipc.patch

clone=0
if [ "${1:-}" = "--clone" ]; then clone=1; shift; fi
MAIN=${1:-$HERE/../../../Main_MiSTer}

if [ "$clone" = 1 ] && [ ! -d "$MAIN/.git" ]; then
	git clone https://github.com/MiSTer-devel/Main_MiSTer.git "$MAIN"
	git -C "$MAIN" checkout -q "$PINNED"
fi
[ -d "$MAIN/.git" ] || { echo "no Main_MiSTer checkout at $MAIN (use --clone)" >&2; exit 1; }

head=$(git -C "$MAIN" rev-parse HEAD)
if [ "$head" != "$PINNED" ]; then
	echo "warning: Main_MiSTer is at $head, the patch was made and tested on $PINNED" >&2
fi

if git -C "$MAIN" apply --reverse --check "$PATCH" 2>/dev/null; then
	echo "patch already applied in $MAIN"
	exit 0
fi
git -C "$MAIN" apply --check "$PATCH"
git -C "$MAIN" apply "$PATCH"
echo "applied: user_io.cpp hooks + support/megacd/dashboard_ipc.{h,cpp} in $MAIN"
