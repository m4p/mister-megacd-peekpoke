// megacd-dashboard: HTTP bridge between the Desert Bus dashboard and the
// MegaCD MiSTer core. See docs/dashboard-protocol.md and
// docs/DASHBOARD-DEPLOYMENT.md.
#include "api.h"
#include "transport.h"

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <unistd.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

#define MAX_CONN        64
#define HEADER_MAX      8192
#define IN_CAP          (HEADER_MAX + API_BODY_MAX)
#define OUT_CAP         (API_VRAM_MAX * 2 + 2048)
#define IDLE_TIMEOUT_MS 30000
#define REQ_TIMEOUT_MS  10000

typedef struct conn {
	int fd;
	char in[IN_CAP];
	size_t in_len;
	char out[OUT_CAP + 512];
	size_t out_len, out_off;
	long long last_ms;
	long long req_start_ms;
	int close_after;
	int ready;               // a complete request is buffered
	size_t req_len;          // bytes of the complete request (headers + body)
	char method[8];
	char path[64];
	size_t body_off, body_len;
	int keep_alive;
	int err_status;          // pre-dispatch framing error
} conn;

static conn *conns[MAX_CONN];
static volatile sig_atomic_t stop_flag = 0;
static int verbose = 0;

static void on_signal(int s) { (void)s; stop_flag = 1; }

static void logf_(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
}

static const char *reason(int s)
{
	switch (s) {
	case 200: return "OK"; case 204: return "No Content"; case 400: return "Bad Request";
	case 404: return "Not Found"; case 405: return "Method Not Allowed"; case 409: return "Conflict";
	case 411: return "Length Required"; case 413: return "Payload Too Large";
	case 431: return "Request Header Fields Too Large"; case 500: return "Internal Server Error";
	case 501: return "Not Implemented"; case 503: return "Service Unavailable";
	case 504: return "Gateway Timeout"; default: return "Error";
	}
}

static void respond(conn *c, int status, const char *body)
{
	size_t blen = body ? strlen(body) : 0;
	int n = snprintf(c->out, sizeof(c->out),
		"HTTP/1.1 %d %s\r\n"
		"Access-Control-Allow-Origin: *\r\n"
		"Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
		"Access-Control-Allow-Headers: Content-Type\r\n"
		"Access-Control-Allow-Private-Network: true\r\n"
		"Access-Control-Max-Age: 600\r\n"
		"Cache-Control: no-store\r\n"
		"%s"
		"Content-Length: %zu\r\n"
		"Connection: %s\r\n\r\n",
		status, reason(status),
		blen ? "Content-Type: application/json\r\n" : "",
		blen, c->keep_alive && !c->close_after ? "keep-alive" : "close");
	if (n < 0 || (size_t)n + blen > sizeof(c->out)) {
		c->close_after = 1;
		n = snprintf(c->out, sizeof(c->out), "HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
		blen = 0;
	}
	if (blen) memcpy(c->out + n, body, blen);
	c->out_len = (size_t)n + blen;
	c->out_off = 0;
}

static void conn_close(int i)
{
	if (!conns[i]) return;
	close(conns[i]->fd);
	free(conns[i]);
	conns[i] = NULL;
}

static int hdr_value(const char *hdrs, const char *name, char *val, size_t cap)
{
	size_t nl = strlen(name);
	const char *p = hdrs;
	while ((p = strstr(p, "\r\n"))) {
		p += 2;
		if (!strncasecmp(p, name, nl) && p[nl] == ':') {
			p += nl + 1;
			while (*p == ' ' || *p == '\t') p++;
			const char *e = strstr(p, "\r\n");
			size_t n = e ? (size_t)(e - p) : strlen(p);
			if (n >= cap) n = cap - 1;
			memcpy(val, p, n);
			val[n] = 0;
			return 1;
		}
	}
	return 0;
}

// Parses a buffered request. Returns 1 when complete, 0 when more bytes are needed.
static int parse_request(conn *c)
{
	char *end = NULL;
	for (size_t i = 0; i + 3 < c->in_len; i++)
		if (!memcmp(c->in + i, "\r\n\r\n", 4)) { end = c->in + i; break; }
	if (!end) {
		if (c->in_len >= HEADER_MAX) { c->err_status = 431; c->close_after = 1; return 1; }
		return 0;
	}
	size_t hlen = (size_t)(end - c->in) + 4;
	char save = *end;
	*end = 0;

	char proto[16] = "";
	char target[256] = "";
	c->method[0] = 0;
	c->err_status = 0;
	if (sscanf(c->in, "%7s %255s %15s", c->method, target, proto) != 3 || strncmp(proto, "HTTP/1.", 7)) {
		c->err_status = 400;
		c->close_after = 1;
	}
	char *q = strchr(target, '?');
	if (q) *q = 0;
	snprintf(c->path, sizeof(c->path), "%s", target);

	char val[64];
	c->keep_alive = !strcmp(proto, "HTTP/1.1");
	if (hdr_value(c->in, "Connection", val, sizeof(val))) {
		if (!strcasecmp(val, "close")) c->keep_alive = 0;
		else if (!strcasecmp(val, "keep-alive")) c->keep_alive = 1;
	}
	size_t clen = 0;
	if (hdr_value(c->in, "Transfer-Encoding", val, sizeof(val))) {
		c->err_status = 411;
		c->close_after = 1;
	}
	if (hdr_value(c->in, "Content-Length", val, sizeof(val))) {
		char *ep;
		unsigned long v = strtoul(val, &ep, 10);
		if (*ep || ep == val) { c->err_status = 400; c->close_after = 1; }
		else if (v > API_BODY_MAX) { c->err_status = 413; c->close_after = 1; }
		else clen = v;
	}
	*end = save;

	if (c->err_status) { c->req_len = c->in_len; return 1; }
	if (c->in_len < hlen + clen) return 0;
	c->body_off = hlen;
	c->body_len = clen;
	c->req_len = hlen + clen;
	return 1;
}

static void dispatch(api_ctx *api, conn *c)
{
	static char body[OUT_CAP];
	long long t0 = dash_now_ms();
	int status;

	if (c->err_status) {
		const char *code = c->err_status == 413 ? "body_too_large" :
		                   c->err_status == 411 ? "length_required" : "bad_request";
		snprintf(body, sizeof(body), "{\"ok\":false,\"error\":{\"code\":\"%s\",\"message\":\"malformed or oversized HTTP request\"}}", code);
		status = c->err_status;
		respond(c, status, body);
	}
	else if (!strcmp(c->method, "OPTIONS")) {
		status = 204;
		respond(c, 204, NULL);
	}
	else {
		status = api_handle(api, c->method, c->path, c->in + c->body_off, c->body_len, body, sizeof(body));
		respond(c, status, body);
	}
	if (verbose || status >= 500)
		logf_("%s %s -> %d (%lld ms)", c->method, c->path, status, dash_now_ms() - t0);

	// consume the request (pipelined bytes stay buffered)
	size_t used = c->req_len > c->in_len ? c->in_len : c->req_len;
	memmove(c->in, c->in + used, c->in_len - used);
	c->in_len -= used;
	c->ready = 0;
	c->req_start_ms = c->in_len ? dash_now_ms() : 0;
	if (!c->keep_alive) c->close_after = 1;

	// Write immediately; the poll loop finishes anything the socket did not take.
	ssize_t n = send(c->fd, c->out, c->out_len, MSG_NOSIGNAL);
	if (n > 0) c->out_off = (size_t)n;
	c->last_ms = dash_now_ms();
}

static int listen_on(const char *spec)
{
	char host[64] = "0.0.0.0";
	int port = 8765;
	const char *colon = strrchr(spec, ':');
	if (colon) {
		size_t n = (size_t)(colon - spec);
		if (n >= sizeof(host)) return -1;
		memcpy(host, spec, n);
		host[n] = 0;
		port = atoi(colon + 1);
	}
	else port = atoi(spec);

	struct sockaddr_in addr;
	memset(&addr, 0, sizeof(addr));
	addr.sin_family = AF_INET;
	addr.sin_port = htons((uint16_t)port);
	if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) return -1;

	int fd = socket(AF_INET, SOCK_STREAM, 0);
	if (fd < 0) return -1;
	int one = 1;
	setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(fd, 32) < 0) {
		close(fd);
		return -1;
	}
	fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
	return fd;
}

static void usage(void)
{
	fprintf(stderr,
		"megacd-dashboard " BRIDGE_VERSION "\n"
		"usage: megacd-dashboard [options]\n"
		"  --listen ADDR:PORT   HTTP listen address (default 0.0.0.0:8765)\n"
		"  --socket PATH        Main_MiSTer IPC socket (default /tmp/megacd-dashboard.sock)\n"
		"  --state-dir DIR      savestate directory (default /media/fat/config/megacd-dashboard/states)\n"
		"  --lock PATH          single-instance lock file (default /tmp/megacd-dashboard.lock)\n"
		"  --mock[=hw|full]     serve a simulated core instead of Main (tests only)\n"
		"  --verbose            log every request\n"
		"  --version            print version and exit\n");
}

int main(int argc, char **argv)
{
	const char *listen_spec = "0.0.0.0:8765";
	const char *sock = "/tmp/megacd-dashboard.sock";
	const char *state_dir = "/media/fat/config/megacd-dashboard/states";
	const char *lock_path = "/tmp/megacd-dashboard.lock";
	const char *mock = NULL;

	for (int i = 1; i < argc; i++) {
		const char *a = argv[i];
		if (!strcmp(a, "--listen") && i + 1 < argc) listen_spec = argv[++i];
		else if (!strcmp(a, "--socket") && i + 1 < argc) sock = argv[++i];
		else if (!strcmp(a, "--state-dir") && i + 1 < argc) state_dir = argv[++i];
		else if (!strcmp(a, "--lock") && i + 1 < argc) lock_path = argv[++i];
		else if (!strcmp(a, "--mock")) mock = "hw";
		else if (!strncmp(a, "--mock=", 7)) mock = a + 7;
		else if (!strcmp(a, "--verbose")) verbose = 1;
		else if (!strcmp(a, "--version")) {
			printf("megacd-dashboard %s (protocol %d)\n", BRIDGE_VERSION, DASH_PROTO_VERSION);
			return 0;
		}
		else { usage(); return 2; }
	}

	signal(SIGPIPE, SIG_IGN);
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);

	int lock_fd = open(lock_path, O_RDWR | O_CREAT | O_CLOEXEC, 0600);
	if (lock_fd < 0 || flock(lock_fd, LOCK_EX | LOCK_NB) < 0) {
		logf_("megacd-dashboard: another instance holds %s", lock_path);
		return 1;
	}

	transport_t *t;
	if (mock) {
		uint16_t feats = FEAT_INPUT | FEAT_WORKRAM_READ | FEAT_LOOPBACK;  // this RBF
		if (!strcmp(mock, "full"))
			feats |= FEAT_WORKRAM_WRITE | FEAT_VRAM_READ | FEAT_VRAM_WRITE | FEAT_FREEZE | FEAT_READ_COHERENT;
		else if (strcmp(mock, "hw")) { usage(); return 2; }
		t = transport_mock_new(feats);
		logf_("megacd-dashboard: MOCK core (%s features) - not talking to hardware", mock);
	}
	else t = transport_ipc_new(sock);
	if (!t) return 1;

	dash_dev dev;
	dev_init(&dev, t);
	api_ctx api;
	memset(&api, 0, sizeof(api));
	api.dev = &dev;
	snprintf(api.state_dir, sizeof(api.state_dir), "%s", state_dir);

	int lfd = listen_on(listen_spec);
	if (lfd < 0) {
		logf_("megacd-dashboard: cannot listen on %s: %s", listen_spec, strerror(errno));
		return 1;
	}
	logf_("megacd-dashboard %s listening on %s (IPC %s, states %s)", BRIDGE_VERSION, listen_spec,
	      mock ? "mock" : sock, state_dir);

	struct pollfd pfd[MAX_CONN + 1];
	while (!stop_flag) {
		int np = 0;
		pfd[np++] = (struct pollfd){ lfd, POLLIN, 0 };
		int map[MAX_CONN + 1];
		for (int i = 0; i < MAX_CONN; i++) {
			conn *c = conns[i];
			if (!c) continue;
			short ev = 0;
			if (c->out_len) ev |= POLLOUT;
			else if (!c->ready) ev |= POLLIN;
			map[np] = i;
			pfd[np++] = (struct pollfd){ c->fd, ev, 0 };
		}
		int pr = poll(pfd, (nfds_t)np, 100);
		if (pr < 0 && errno != EINTR) break;
		long long now = dash_now_ms();

		if (pfd[0].revents & POLLIN) {
			for (;;) {
				int fd = accept(lfd, NULL, NULL);
				if (fd < 0) break;
				fcntl(fd, F_SETFL, fcntl(fd, F_GETFL, 0) | O_NONBLOCK);
				fcntl(fd, F_SETFD, FD_CLOEXEC);
				int one = 1;
				setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
#ifdef SO_NOSIGPIPE
				setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
				int slot = -1;
				for (int i = 0; i < MAX_CONN; i++) if (!conns[i]) { slot = i; break; }
				conn *c = slot >= 0 ? calloc(1, sizeof(conn)) : NULL;
				if (!c) {
					static const char busy[] = "HTTP/1.1 503 Service Unavailable\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
					send(fd, busy, sizeof(busy) - 1, MSG_NOSIGNAL);
					close(fd);
					continue;
				}
				c->fd = fd;
				c->last_ms = now;
				conns[slot] = c;
			}
		}

		for (int k = 1; k < np; k++) {
			int i = map[k];
			conn *c = conns[i];
			if (!c) continue;
			if (pfd[k].revents & (POLLERR | POLLNVAL)) { conn_close(i); continue; }
			if ((pfd[k].revents & POLLOUT) && c->out_off < c->out_len) {
				ssize_t n = send(c->fd, c->out + c->out_off, c->out_len - c->out_off, MSG_NOSIGNAL);
				if (n > 0) {
					c->out_off += (size_t)n;
					c->last_ms = now;
				}
				else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) { conn_close(i); continue; }
			}
			if (c->out_len && c->out_off == c->out_len) {
				c->out_len = c->out_off = 0;
				if (c->close_after) { conn_close(i); continue; }
			}
			if (pfd[k].revents & (POLLIN | POLLHUP)) {
				ssize_t n = recv(c->fd, c->in + c->in_len, sizeof(c->in) - c->in_len, 0);
				if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
					// Client went away (e.g. page reload). A request already
					// executed stays executed; nothing to undo here.
					conn_close(i);
					continue;
				}
				if (n > 0) {
					if (!c->in_len) c->req_start_ms = now;
					c->in_len += (size_t)n;
					c->last_ms = now;
				}
			}
			if (!c->ready && !c->out_len && c->in_len && parse_request(c)) c->ready = 1;
		}

		// Scheduler: control requests (input/pause/resume) before telemetry,
		// otherwise in slot order. Each connection carries one request at a time,
		// so a press/release pair from one client is never reordered.
		for (int pass = 0; pass < 2; pass++)
			for (int i = 0; i < MAX_CONN; i++) {
				conn *c = conns[i];
				if (!c || !c->ready || c->out_len) continue;
				if (pass == 0 && (c->err_status || api_priority(c->path) != 0)) continue;
				dispatch(&api, c);
			}

		// timeouts
		now = dash_now_ms();
		for (int i = 0; i < MAX_CONN; i++) {
			conn *c = conns[i];
			if (!c) continue;
			if (c->in_len && !c->ready && c->req_start_ms && now - c->req_start_ms > REQ_TIMEOUT_MS) conn_close(i);
			else if (!c->in_len && !c->out_len && now - c->last_ms > IDLE_TIMEOUT_MS) conn_close(i);
		}

		dev_heartbeat(&dev);
	}

	logf_("megacd-dashboard: shutting down, releasing injected input");
	api_release_all(&api);
	for (int i = 0; i < MAX_CONN; i++) conn_close(i);
	close(lfd);
	t->destroy(t);
	return 0;
}
