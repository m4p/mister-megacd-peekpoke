// Bridge side of the Main_MiSTer IPC socket (docs/dashboard-protocol.md §9).
#include "transport.h"

#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

typedef struct {
	char path[108];
	int fd;
	uint16_t next_id;
	long long retry_at_ms;
	int backoff_ms;
} ipc_ctx;

static long long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void wr16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | (p[1] << 8)); }

static void ipc_close(ipc_ctx *c)
{
	if (c->fd >= 0) close(c->fd);
	c->fd = -1;
}

static int ipc_connect(ipc_ctx *c)
{
	if (c->fd >= 0) return 0;
	long long now = now_ms();
	if (now < c->retry_at_ms) return -1;

	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0) return -1;
#ifdef SO_NOSIGPIPE
	int one = 1;
	setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one));
#endif
	struct sockaddr_un addr;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	strncpy(addr.sun_path, c->path, sizeof(addr.sun_path) - 1);
	if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
		close(fd);
		c->backoff_ms = c->backoff_ms ? (c->backoff_ms * 2 > 2000 ? 2000 : c->backoff_ms * 2) : 100;
		c->retry_at_ms = now + c->backoff_ms;
		return -1;
	}
	c->backoff_ms = 0;
	c->fd = fd;
	return 0;
}

static int send_all(int fd, const uint8_t *buf, size_t len, long long deadline)
{
	while (len) {
		ssize_t n = send(fd, buf, len, MSG_NOSIGNAL | MSG_DONTWAIT);
		if (n > 0) { buf += n; len -= (size_t)n; continue; }
		if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
			long long left = deadline - now_ms();
			if (left <= 0) return T_TIMEOUT;
			struct pollfd p = { fd, POLLOUT, 0 };
			poll(&p, 1, (int)left);
			continue;
		}
		return T_DOWN;
	}
	return T_OK;
}

static int recv_all(int fd, uint8_t *buf, size_t len, long long deadline)
{
	while (len) {
		ssize_t n = recv(fd, buf, len, MSG_DONTWAIT);
		if (n > 0) { buf += n; len -= (size_t)n; continue; }
		if (n == 0) return T_DOWN;
		if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) {
			long long left = deadline - now_ms();
			if (left <= 0) return T_TIMEOUT;
			struct pollfd p = { fd, POLLIN, 0 };
			poll(&p, 1, (int)left);
			continue;
		}
		return T_DOWN;
	}
	return T_OK;
}

// Sends frames and reads their replies in order.
static int roundtrip(transport_t *t, int n, const uint16_t *types, xfer_t *x, int *info_words, int timeout_ms)
{
	ipc_ctx *c = t->ctx;
	if (ipc_connect(c)) return T_DOWN;

	long long deadline = now_ms() + timeout_ms;
	uint8_t buf[8 + IPC_MAX_WORDS * 2];
	uint16_t first_id = c->next_id;

	for (int i = 0; i < n; i++) {
		int n_out = x ? x[i].n_out : 0;
		int n_in = x ? x[i].n_in : 0;
		wr16(buf + 0, types[i]);
		wr16(buf + 2, (uint16_t)(first_id + i));
		wr16(buf + 4, (uint16_t)n_out);
		wr16(buf + 6, (uint16_t)n_in);
		for (int k = 0; k < n_out; k++) wr16(buf + 8 + k * 2, x[i].out[k]);
		int r = send_all(c->fd, buf, 8 + (size_t)n_out * 2, deadline);
		if (r) { ipc_close(c); return r; }
	}
	c->next_id = (uint16_t)(first_id + n);

	for (int i = 0; i < n; i++) {
		uint8_t hdr[12];
		int r = recv_all(c->fd, hdr, sizeof(hdr), deadline);
		if (r) { ipc_close(c); return r; }
		uint16_t id = rd16(hdr + 2);
		int status = rd16(hdr + 4);
		int nw = rd16(hdr + 6);
		t->epoch = (uint32_t)rd16(hdr + 8) | ((uint32_t)rd16(hdr + 10) << 16);
		if (id != (uint16_t)(first_id + i) || nw > IPC_MAX_WORDS + 1) { ipc_close(c); return T_DOWN; }
		uint8_t body[(IPC_MAX_WORDS + 1) * 2];
		r = recv_all(c->fd, body, (size_t)nw * 2, deadline);
		if (r) { ipc_close(c); return r; }
		if (x) {
			x[i].status = status;
			for (int k = 0; k < nw; k++) x[i].resp[k] = rd16(body + k * 2);
			for (int k = nw; k <= IPC_MAX_WORDS; k++) x[i].resp[k] = 0;
			if (status == IPC_OK && nw != 1 + x[i].n_out + x[i].n_in) { ipc_close(c); return T_DOWN; }
		}
		else if (info_words) {
			info_words[0] = status == IPC_OK && nw >= 1 ? rd16(body) : 0;
		}
	}
	return T_OK;
}

static int ipc_batch(transport_t *t, xfer_t *x, int n, int timeout_ms)
{
	uint16_t types[16];
	int done = 0;
	while (done < n) {
		int chunk = n - done > 16 ? 16 : n - done;
		for (int i = 0; i < chunk; i++) types[i] = IPC_XFER;
		int r = roundtrip(t, chunk, types, x + done, NULL, timeout_ms);
		if (r) return r;
		done += chunk;
	}
	return T_OK;
}

static int ipc_info(transport_t *t, int *is_megacd, int timeout_ms)
{
	uint16_t type = IPC_INFO;
	int w = 0;
	int r = roundtrip(t, 1, &type, NULL, &w, timeout_ms);
	if (!r) *is_megacd = w;
	return r;
}

static void ipc_destroy(transport_t *t)
{
	ipc_close(t->ctx);
	free(t->ctx);
	free(t);
}

transport_t *transport_ipc_new(const char *socket_path)
{
	transport_t *t = calloc(1, sizeof(*t));
	ipc_ctx *c = calloc(1, sizeof(*c));
	if (!t || !c) { free(t); free(c); return NULL; }
	snprintf(c->path, sizeof(c->path), "%s", socket_path);
	c->fd = -1;
	c->next_id = 1;
	t->ctx = c;
	t->batch = ipc_batch;
	t->info = ipc_info;
	t->destroy = ipc_destroy;
	return t;
}
