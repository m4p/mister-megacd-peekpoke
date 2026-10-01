#!/bin/sh
# megacd-dashboard service control for MiSTer (busybox sh, no systemd).
#   start.sh start|stop|restart|status|foreground
#
# Optional settings in $DIR/megacd-dashboard.conf (shell syntax):
#   LISTEN=0.0.0.0:8765
#   SOCKET=/tmp/megacd-dashboard.sock
#   STATE_DIR=/media/fat/config/megacd-dashboard/states
# The bridge may start before Main or the core is ready; it reconnects to the
# Main IPC socket with backoff on every request.

DIR=${MEGACD_DASH_DIR:-/media/fat/megacd-dashboard}
BIN=$DIR/megacd-dashboard
LISTEN=0.0.0.0:8765
SOCKET=/tmp/megacd-dashboard.sock
STATE_DIR=/media/fat/config/megacd-dashboard/states
LOG=/tmp/megacd-dashboard.log
PIDFILE=/tmp/megacd-dashboard.pid
LOCK=/tmp/megacd-dashboard.lock
LOG_MAX=262144

[ -f "$DIR/megacd-dashboard.conf" ] && . "$DIR/megacd-dashboard.conf"

running() {
	[ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

args() {
	echo "--listen $LISTEN --socket $SOCKET --state-dir $STATE_DIR --lock $LOCK"
}

start() {
	if running; then echo "megacd-dashboard already running (pid $(cat "$PIDFILE"))"; return 0; fi
	[ -x "$BIN" ] || { echo "missing $BIN"; return 1; }
	mkdir -p "$STATE_DIR"
	# keep the log bounded: one rotation per start
	if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt "$LOG_MAX" ]; then mv -f "$LOG" "$LOG.1"; fi
	# shellcheck disable=SC2046
	nohup "$BIN" $(args) >> "$LOG" 2>&1 &
	echo $! > "$PIDFILE"
	sleep 1
	if running; then
		echo "megacd-dashboard started (pid $(cat "$PIDFILE")), listening on $LISTEN, log $LOG"
	else
		echo "megacd-dashboard failed to start; see $LOG"
		tail -n 5 "$LOG"
		return 1
	fi
}

stop() {
	if ! running; then echo "megacd-dashboard not running"; rm -f "$PIDFILE"; return 0; fi
	pid=$(cat "$PIDFILE")
	kill "$pid"          # SIGTERM: the bridge releases injected buttons before exiting
	i=0
	while kill -0 "$pid" 2>/dev/null && [ $i -lt 30 ]; do sleep 0.1; i=$((i + 1)); done
	kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
	rm -f "$PIDFILE"
	echo "megacd-dashboard stopped"
}

status() {
	if running; then
		echo "megacd-dashboard running (pid $(cat "$PIDFILE")), listening on $LISTEN"
		port=${LISTEN##*:}
		if command -v wget >/dev/null 2>&1; then
			wget -q -O - "http://127.0.0.1:$port/status" 2>/dev/null; echo
		fi
	else
		echo "megacd-dashboard not running"
		return 3
	fi
}

case "$1" in
	start) start ;;
	stop) stop ;;
	restart) stop; start ;;
	status) status ;;
	foreground)
		mkdir -p "$STATE_DIR"
		# shellcheck disable=SC2046
		exec "$BIN" $(args) --verbose ;;
	*) echo "usage: $0 start|stop|restart|status|foreground"; exit 2 ;;
esac
