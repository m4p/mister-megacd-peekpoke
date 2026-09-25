#include "dev.h"

#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define HEARTBEAT_MS 250

long long dash_now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

void dev_set_err(dash_err *e, int http, const char *code, const char *fmt, ...)
{
	va_list ap;
	e->http = http;
	e->code = code;
	va_start(ap, fmt);
	vsnprintf(e->msg, sizeof(e->msg), fmt, ap);
	va_end(ap);
}

void dev_init(dash_dev *d, transport_t *t)
{
	memset(d, 0, sizeof(*d));
	d->t = t;
	d->op_timeout_ms = 1000;
	d->ipc_timeout_ms = 500;
}

static void invalidate(dash_dev *d)
{
	d->have_session = 0;
	d->held = 0;
	d->user_paused = 0;
}

static void hdr(xfer_t *x, dash_dev *d, int sub, int n_args, const uint16_t *args, int n_in)
{
	memset(x, 0, sizeof(*x));
	x->out[0] = (uint16_t)((sub << 8) | d->gen);
	for (int i = 0; i < n_args; i++) x->out[1 + i] = args[i];
	x->n_out = 1 + n_args;
	x->n_in = n_in;
}

// Runs a batch and validates link, core identity, and session for every packet.
static int run(dash_dev *d, xfer_t *x, int n, dash_err *e)
{
	uint32_t epoch = d->t->epoch;
	int r = d->t->batch(d->t, x, n, d->ipc_timeout_ms);
	if (r == T_TIMEOUT) {
		invalidate(d);
		dev_set_err(e, 504, "operation_timeout", "Main_MiSTer did not answer within %d ms", d->ipc_timeout_ms);
		return -1;
	}
	if (r != T_OK) {
		invalidate(d);
		dev_set_err(e, 503, "core_unavailable", "dashboard IPC to Main_MiSTer is not connected (is the patched Main running?)");
		return -1;
	}
	d->last_packet_ms = dash_now_ms();
	if (d->have_session && d->t->epoch != epoch) {
		invalidate(d);
		dev_set_err(e, 503, "core_unavailable", "the core was reloaded; the dashboard session was reset");
		return -1;
	}
	for (int i = 0; i < n; i++) {
		if (x[i].status == IPC_NOT_MEGACD) {
			invalidate(d);
			dev_set_err(e, 503, "core_unavailable", "the loaded core is not MegaCD");
			return -1;
		}
		if (x[i].status != IPC_OK) {
			dev_set_err(e, 500, "protocol_mismatch", "Main_MiSTer rejected an IPC frame (status %d)", x[i].status);
			return -1;
		}
		if (x[i].resp[0] != DASH_MAGIC_ACK) {
			invalidate(d);
			dev_set_err(e, 503, "protocol_mismatch", "the loaded MegaCD RBF has no dashboard endpoint (stock core?)");
			return -1;
		}
		int sub = x[i].out[0] >> 8;
		if (sub != SUB_PROBE && sub != SUB_SESSION && x[i].n_out + x[i].n_in >= 2 &&
		    x[i].resp[2] == RES_SESSION && FL_GEN(x[i].resp[1]) != d->gen) {
			invalidate(d);
			dev_set_err(e, 503, "core_unavailable", "the FPGA session was reset (core reset or reload)");
			return -1;
		}
		d->flags = x[i].resp[1];
	}
	return 0;
}

int dev_ensure(dash_dev *d, dash_err *e)
{
	if (d->have_session) return 0;

	int is_mcd = 0;
	int r = d->t->info(d->t, &is_mcd, d->ipc_timeout_ms);
	if (r) {
		dev_set_err(e, 503, "core_unavailable", "dashboard IPC to Main_MiSTer is not connected (is the patched Main running?)");
		return -1;
	}
	if (!is_mcd) {
		dev_set_err(e, 503, "core_unavailable", "the loaded core is not MegaCD");
		return -1;
	}

	xfer_t x;
	hdr(&x, d, SUB_PROBE, 0, NULL, 9);
	if (run(d, &x, 1, e)) return -1;
	if (x.resp[2] != DASH_PROBE_MAGIC0 || x.resp[3] != DASH_PROBE_MAGIC1) {
		dev_set_err(e, 503, "protocol_mismatch", "the loaded MegaCD RBF has no dashboard endpoint (stock core?)");
		return -1;
	}
	if (x.resp[4] != DASH_PROTO_VERSION) {
		dev_set_err(e, 409, "protocol_mismatch", "RBF speaks dashboard protocol %u, bridge speaks %u",
		            x.resp[4], DASH_PROTO_VERSION);
		return -1;
	}
	d->features = x.resp[5];
	d->build_id = (uint32_t)x.resp[8] | ((uint32_t)x.resp[9] << 16);

	uint8_t gen = (uint8_t)(d->gen % 255 + 1);   // never 0 (reserved: no owner)
	uint16_t arg = gen;
	hdr(&x, d, SUB_SESSION, 1, &arg, 0);
	if (run(d, &x, 1, e)) return -1;
	if (x.resp[2] != gen) {
		dev_set_err(e, 503, "protocol_mismatch", "SESSION was not acknowledged");
		return -1;
	}
	d->gen = gen;
	d->epoch = d->t->epoch;
	d->have_session = 1;
	d->held = 0;
	d->user_paused = 0;
	return 0;
}

static int result_err(dash_err *e, uint16_t code, const char *what)
{
	switch (code) {
	case RES_OK: case RES_DUP: return 0;
	case RES_BUSY:
		dev_set_err(e, 409, "busy", "%s: the FPGA endpoint is busy", what); break;
	case RES_RANGE:
		dev_set_err(e, 400, "invalid_range", "%s: range rejected by the FPGA", what); break;
	case RES_UNSUPPORTED:
		dev_set_err(e, 501, "feature_unavailable", "%s: not supported by this RBF", what); break;
	case RES_ABORTED:
		dev_set_err(e, 503, "busy", "%s: aborted by a ROM download; retry", what); break;
	case RES_FAULT:
		dev_set_err(e, 500, "hardware_fault", "%s: hardware fault after memory was modified; machine left frozen", what); break;
	case RES_SESSION:
		dev_set_err(e, 503, "core_unavailable", "%s: session lost", what); break;
	default:
		dev_set_err(e, 500, "protocol_mismatch", "%s: unexpected result %u", what, code); break;
	}
	return -1;
}

// Releases a stale held/staged transaction left by an earlier failure.
static void recover_busy(dash_dev *d)
{
	xfer_t x;
	dash_err e;
	hdr(&x, d, SUB_STATUS, 0, NULL, 5);
	if (run(d, &x, 1, &e)) return;
	uint16_t args[1] = { x.resp[2] };
	hdr(&x, d, SUB_CANCEL, 1, args, 0);
	run(d, &x, 1, &e);
}

int dev_mem(dash_dev *d, int op, int space, uint32_t addr, int len,
            const uint8_t *wdata, uint8_t *rdata, dash_err *e)
{
	static xfer_t x[40];
	if (len < 1 || len > DASH_STAGING_BYTES) {
		dev_set_err(e, 413, "invalid_range", "length %d outside 1..%d", len, DASH_STAGING_BYTES);
		return -1;
	}
	if (dev_ensure(d, e)) return -1;
	d->ops++;

	for (int attempt = 0; attempt < 2; attempt++) {
		d->txn = (uint16_t)(d->txn + 1 ? d->txn + 1 : 1);
		uint16_t txn = d->txn;
		int n = 0;
		uint16_t b[5] = { txn, (uint16_t)((op << 8) | space), (uint16_t)(addr >> 16), (uint16_t)addr, (uint16_t)len };
		hdr(&x[n++], d, SUB_BEGIN, 5, b, 0);
		int words = (len + 1) / 2;
		if (op != OP_READ) {
			for (int off = 0; off < words; off += DASH_MAX_XFER_WORDS) {
				int cnt = words - off > DASH_MAX_XFER_WORDS ? DASH_MAX_XFER_WORDS : words - off;
				xfer_t *u = &x[n++];
				uint16_t a[3] = { txn, (uint16_t)off, (uint16_t)cnt };
				hdr(u, d, SUB_UPLOAD, 3, a, 0);
				for (int k = 0; k < cnt; k++) {
					int i = (off + k) * 2;
					uint8_t hi = wdata[i];
					uint8_t lo = i + 1 < len ? wdata[i + 1] : 0;
					u->out[u->n_out++] = (uint16_t)((hi << 8) | lo);
				}
			}
		}
		int commit_idx = n;
		hdr(&x[n++], d, SUB_COMMIT, 1, &txn, 0);
		int status_idx = n;
		hdr(&x[n++], d, SUB_STATUS, 0, NULL, 5);

		if (run(d, x, n, e)) return -1;

		uint16_t code = x[0].resp[6];
		if (code == RES_BUSY && attempt == 0) { recover_busy(d); continue; }
		if (result_err(e, code, "BEGIN")) return -1;
		for (int i = 1; i < commit_idx; i++)
			if (result_err(e, x[i].resp[4], "UPLOAD")) return -1;
		if (result_err(e, x[commit_idx].resp[2], "COMMIT")) return -1;

		// wait for completion
		xfer_t *s = &x[status_idx];
		long long deadline = dash_now_ms() + d->op_timeout_ms;
		while (!(s->resp[1] & FL_DONE) || s->resp[2] != txn) {
			if (dash_now_ms() > deadline) {
				dev_set_err(e, 504, "operation_timeout", "operation did not complete within %d ms", d->op_timeout_ms);
				d->errors++;
				return -1;
			}
			usleep(300);
			hdr(s, d, SUB_STATUS, 0, NULL, 5);
			if (run(d, s, 1, e)) return -1;
		}
		uint16_t res = s->resp[3];
		int res_len = s->resp[4];

		n = 0;
		if (op != OP_WRITE && res == RES_OK) {
			for (int off = 0; off < (res_len + 1) / 2; off += DASH_MAX_XFER_WORDS) {
				int cnt = (res_len + 1) / 2 - off;
				if (cnt > DASH_MAX_XFER_WORDS) cnt = DASH_MAX_XFER_WORDS;
				uint16_t a[3] = { txn, (uint16_t)off, (uint16_t)cnt };
				hdr(&x[n++], d, SUB_FETCH, 3, a, cnt);
			}
		}
		int ack_idx = n;
		hdr(&x[n++], d, SUB_ACK, 1, &txn, 0);
		if (run(d, x, n, e)) return -1;

		if (result_err(e, res, op == OP_WRITE ? "write" : "read")) { d->errors++; return -1; }
		if (op != OP_WRITE) {
			if (res_len != len) {
				dev_set_err(e, 500, "protocol_mismatch", "result length %d, expected %d", res_len, len);
				return -1;
			}
			for (int i = 0; i < ack_idx; i++) {
				if (result_err(e, x[i].resp[4], "FETCH")) return -1;
				int off = x[i].out[2], cnt = x[i].out[3];
				for (int k = 0; k < cnt; k++) {
					int bi = (off + k) * 2;
					uint16_t w = x[i].resp[5 + k];
					if (bi < len) rdata[bi] = (uint8_t)(w >> 8);
					if (bi + 1 < len) rdata[bi + 1] = (uint8_t)w;
				}
			}
		}
		return 0;
	}
	dev_set_err(e, 409, "busy", "the FPGA endpoint stayed busy");
	return -1;
}

int dev_input(dash_dev *d, uint16_t press, uint16_t release, dash_err *e)
{
	if (dev_ensure(d, e)) return -1;
	xfer_t x;
	uint16_t a[2] = { press, release };
	hdr(&x, d, SUB_INPUT, 2, a, 0);
	if (run(d, &x, 1, e)) return -1;
	d->held = x.resp[3];
	return 0;
}

int dev_pause(dash_dev *d, int pause, dash_err *e)
{
	if (dev_ensure(d, e)) return -1;
	if (!(d->features & FEAT_FREEZE)) {
		dev_set_err(e, 501, "feature_unavailable",
		            "pause/resume needs the coordinated console freeze, which this RBF does not provide");
		return -1;
	}
	xfer_t x;
	hdr(&x, d, pause ? SUB_PAUSE : SUB_RESUME, 0, NULL, 1);
	if (run(d, &x, 1, e)) return -1;
	if (result_err(e, x.resp[2], pause ? "pause" : "resume")) return -1;
	d->user_paused = pause;

	// Report success only once the machine reached the requested state.
	long long deadline = dash_now_ms() + d->op_timeout_ms;
	for (;;) {
		if (dev_status(d, e)) return -1;
		if (!!(d->flags & FL_FROZEN) == !!pause) return 0;
		if (dash_now_ms() > deadline) {
			dev_set_err(e, 504, "operation_timeout", "the console did not %s within %d ms",
			            pause ? "freeze" : "resume", d->op_timeout_ms);
			return -1;
		}
		usleep(500);
	}
}

int dev_status(dash_dev *d, dash_err *e)
{
	if (dev_ensure(d, e)) return -1;
	xfer_t x;
	hdr(&x, d, SUB_STATUS, 0, NULL, 5);
	if (run(d, &x, 1, e)) return -1;
	d->frame = x.resp[6];
	return 0;
}

void dev_heartbeat(dash_dev *d)
{
	if (!d->have_session) return;
	if (dash_now_ms() - d->last_packet_ms < HEARTBEAT_MS) return;
	xfer_t x;
	dash_err e;
	hdr(&x, d, SUB_HEARTBEAT, 0, NULL, 0);
	run(d, &x, 1, &e);
}
