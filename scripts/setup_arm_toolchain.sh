#!/bin/bash
# Install the ARM cross compiler used for Main_MiSTer and megacd-dashboard:
# ARM GNU Toolchain 10.2-2020.11, prefix arm-none-linux-gnueabihf- (the version
# Main_MiSTer's Makefile and setup_default_toolchain.sh expect).
#
# Tested target host: Ubuntu 16.04+ x86_64. Idempotent; safe to rerun.
#
#   scripts/setup_arm_toolchain.sh              install to /opt (uses sudo if needed)
#   scripts/setup_arm_toolchain.sh --profile    also add it to PATH in ~/.bashrc
#   PREFIX=$HOME/toolchains scripts/setup_arm_toolchain.sh
#
# Do not use Ubuntu's gcc-arm-linux-gnueabihf package instead: it is GCC 5 on
# 16.04 and uses the arm-linux-gnueabihf- prefix the Makefile does not look for.

set -euo pipefail

VER=10.2-2020.11
PKG=gcc-arm-$VER-x86_64-arm-none-linux-gnueabihf
URL=https://developer.arm.com/-/media/Files/downloads/gnu-a/$VER/binrel/$PKG.tar.xz
PREFIX=${PREFIX:-/opt}
DIR=$PREFIX/$PKG
BIN=$DIR/bin

add_profile=0
for arg in "$@"; do
	case "$arg" in
		--profile) add_profile=1 ;;
		-h|--help) sed -n '2,15p' "$0"; exit 0 ;;
		*) echo "unknown option: $arg" >&2; exit 2 ;;
	esac
done

if [ "$(uname -m)" != "x86_64" ]; then
	echo "This toolchain build is for x86_64 hosts (this is $(uname -m))." >&2
	exit 1
fi

SUDO=""
mkdir -p "$PREFIX" 2>/dev/null || true   # e.g. PREFIX=$HOME/toolchains needs no sudo
if [ ! -w "$PREFIX" ] && [ "$(id -u)" -ne 0 ]; then SUDO=sudo; fi

if command -v apt-get >/dev/null 2>&1; then
	missing=""
	for p in build-essential wget xz-utils file; do
		dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"
	done
	if [ -n "$missing" ]; then
		echo "Installing host packages:$missing"
		sudo apt-get update
		# shellcheck disable=SC2086
		sudo apt-get install -y $missing
	fi
fi

if [ -x "$BIN/arm-none-linux-gnueabihf-gcc" ]; then
	echo "Toolchain already installed in $DIR"
else
	tmp=$(mktemp -d)
	trap 'rm -rf "$tmp"' EXIT
	echo "Downloading $PKG ..."
	# Ubuntu 16.04's CA bundle can fail the TLS handshake with developer.arm.com;
	# MiSTer's own setup script downloads the same file the same way.
	wget --no-check-certificate -c -O "$tmp/$PKG.tar.xz" "$URL"
	$SUDO mkdir -p "$PREFIX"
	echo "Unpacking to $PREFIX ..."
	$SUDO tar -xf "$tmp/$PKG.tar.xz" -C "$PREFIX"
fi

echo
if ! "$BIN/arm-none-linux-gnueabihf-gcc" --version | head -1; then
	echo "The compiler does not run on this host (see the error above, e.g. GLIBC version)." >&2
	exit 1
fi

# Smoke test: compile and link a trivial program for ARM.
tmpc=$(mktemp -d)
printf 'int main(void){return 0;}\n' > "$tmpc/t.c"
"$BIN/arm-none-linux-gnueabihf-gcc" -static -o "$tmpc/t" "$tmpc/t.c"
file "$tmpc/t" | sed 's/^[^:]*: /test binary: /'
rm -rf "$tmpc"

line="export PATH=$BIN:\$PATH"
if [ "$add_profile" = 1 ]; then
	if grep -qF "$BIN" "$HOME/.bashrc" 2>/dev/null; then
		echo "~/.bashrc already adds $BIN to PATH"
	else
		printf '\n# ARM cross compiler for MiSTer (scripts/setup_arm_toolchain.sh)\n%s\n' "$line" >> "$HOME/.bashrc"
		echo "Added to ~/.bashrc; open a new shell or run: source ~/.bashrc"
	fi
else
	echo
	echo "Add the compiler to PATH for this shell with:"
	echo "  $line"
	echo "(or rerun with --profile to add it to ~/.bashrc)"
fi

cat <<EOF

Next, from the directory that contains the repositories:
  make -C Main_MiSTer
  make -C MegaCD_MiSTer/linux/dashboard-bridge CROSS_COMPILE=arm-none-linux-gnueabihf-
EOF
