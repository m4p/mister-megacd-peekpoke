// In-process model of rtl/dashboard_debug.sv for bridge tests.
// It follows docs/dashboard-protocol.md, including the features a future
// RBF may add (writes, VRAM, freeze), so the bridge's use of them can be
// tested before the hardware exists. It is not evidence the hardware works.
#include "transport.h"

#include <stdlib.h>
#include <string.h>

enum { TS_IDLE, TS_STAGING, TS_RUNNING, TS_DONE };

typedef struct {
	uint16_t features;
	int is_megacd;
	uint8_t gen;
	int tstate;
	uint16_t txn;
	uint8_t op, space;
	uint16_t addr;
	int len;
	int up_ptr;
	uint16_t res_code;
	int res_len;
	int run_polls;           // STATUS polls until a RUNNING op completes
	int pause_req;
	uint16_t target, latch, applied;
	uint16_t frame;
	uint8_t staging[DASH_STAGING_BYTES];
	uint8_t wram[65536];
	uint8_t vram[65536];     // canonical VDP byte order
} model;

static uint16_t flags(model *m)
{
	return (uint16_t)((m->gen << 8) | ((m->target | m->applied) ? FL_INPUT : 0) | FL_OWNER |
		(m->pause_req && (m->features & FEAT_FREEZE) ? FL_FROZEN | FL_PAUSE_REQ : 0) |
		(m->tstate == TS_STAGING ? FL_STAGING : 0) | (m->tstate == TS_DONE ? FL_DONE : 0) |
		(m->tstate == TS_RUNNING ? FL_RUNNING : 0));
}

static int frozen(model *m) { return m->pause_req && (m->features & FEAT_FREEZE); }

static void apply_input(model *m)
{
	m->applied = m->target | m->latch;
	m->latch = 0;
}

static void run_op(model *m)
{
	uint8_t *mem = m->space == SP_VRAM ? m->vram : m->wram;
	if (m->op == OP_READ) memcpy(m->staging, mem + m->addr, (size_t)m->len);
	else if (m->op == OP_WRITE) memcpy(mem + m->addr, m->staging, (size_t)m->len);
	m->res_code = RES_OK;
	m->res_len = m->op == OP_WRITE ? 0 : m->len;
	if (m->op == OP_WRITE) m->res_len = m->len;
	m->tstate = TS_DONE;
}

static uint16_t begin_check(model *m, uint16_t op_space, uint16_t ahi, uint16_t alo, uint16_t len)
{
	uint8_t op = op_space >> 8, sp = op_space & 0xFF;
	if (m->tstate == TS_RUNNING || m->tstate == TS_DONE) return RES_BUSY;
	if (len == 0 || len > DASH_STAGING_BYTES) return RES_RANGE;
	if (op == OP_LOOPBACK) return RES_OK;
	if (ahi != 0 || (uint32_t)alo + len > 0x10000) return RES_RANGE;
	if (op == OP_READ && sp == SP_WORKRAM) return RES_OK;
	if (op == OP_WRITE && sp == SP_WORKRAM) return (m->features & FEAT_WORKRAM_WRITE) ? RES_OK : RES_UNSUPPORTED;
	if (op == OP_READ && sp == SP_VRAM) return (m->features & FEAT_VRAM_READ) ? RES_OK : RES_UNSUPPORTED;
	if (op == OP_WRITE && sp == SP_VRAM) return (m->features & FEAT_VRAM_WRITE) ? RES_OK : RES_UNSUPPORTED;
	return RES_UNSUPPORTED;
}

// One SPI packet. w[0] is the header (the 0x70 command is implicit).
static void packet(model *m, const uint16_t *w, int n, uint16_t *r)
{
	memset(r, 0, sizeof(uint16_t) * (size_t)(n + 1));
	r[0] = DASH_MAGIC_ACK;
	if (n < 1) return;
	uint8_t sc = w[0] >> 8;
	int sess_ok = (w[0] & 0xFF) == m->gen || sc == SUB_PROBE || sc == SUB_SESSION;
	r[1] = flags(m);
	if (!sess_ok) {
		for (int k = 2; k <= n; k++) r[k] = RES_SESSION;
		return;
	}
#define W(k) ((k) - 1 < n ? w[(k) - 1] : 0)
#define HAVE(k) ((k) - 1 < n)
	switch (sc) {
	case SUB_PROBE: {
		uint16_t v[] = { 0x4442, 0x4D43, DASH_PROTO_VERSION, m->features, DASH_STAGING_BYTES,
		                 DASH_MAX_XFER_WORDS, 0x0925, 0x0126, m->gen };
		for (int k = 2; k <= n && k <= 10; k++) r[k] = v[k - 2];
		break;
	}
	case SUB_SESSION:
		if (!HAVE(2)) break;
		m->gen = (uint8_t)W(2);
		m->target = m->latch = m->applied = 0;
		m->pause_req = 0;
		m->tstate = TS_IDLE;
		r[2] = m->gen;
		break;
	case SUB_STATUS:
		if (m->tstate == TS_RUNNING && --m->run_polls <= 0) run_op(m);
		if (n >= 2) r[2] = m->txn;
		if (n >= 3) r[3] = m->res_code;
		if (n >= 4) r[4] = (uint16_t)m->res_len;
		if (n >= 5) r[5] = m->applied;
		if (n >= 6) r[6] = m->frame;
		break;
	case SUB_BEGIN: {
		if (!HAVE(6)) break;
		uint16_t code = W(2) == 0 ? RES_TXN : begin_check(m, W(3), W(4), W(5), W(6));
		r[6] = code;
		if (code == RES_OK) {
			m->tstate = TS_STAGING;
			m->txn = W(2);
			m->op = W(3) >> 8;
			m->space = W(3) & 0xFF;
			m->addr = W(5);
			m->len = W(6);
			m->up_ptr = 0;
			m->res_code = 0;
			m->res_len = 0;
		}
		break;
	}
	case SUB_UPLOAD: {
		if (!HAVE(4)) break;
		int need = (m->len + 1) / 2;
		uint16_t code;
		if (m->tstate != TS_STAGING || m->op == OP_READ) code = RES_STATE;
		else if (W(2) != m->txn) code = RES_TXN;
		else if (W(3) != m->up_ptr || W(4) == 0 || W(4) > DASH_MAX_XFER_WORDS || W(3) + W(4) > need) code = RES_RANGE;
		else code = RES_OK;
		r[4] = code;
		if (code != RES_OK) break;
		for (int k = 0; k < W(4) && 4 + k < n; k++) {
			m->staging[m->up_ptr * 2] = (uint8_t)(w[4 + k] >> 8);
			m->staging[m->up_ptr * 2 + 1] = (uint8_t)w[4 + k];
			m->up_ptr++;
		}
		break;
	}
	case SUB_COMMIT: {
		if (!HAVE(2)) break;
		uint16_t code;
		if (m->tstate == TS_IDLE) code = RES_STATE;
		else if (W(2) != m->txn) code = RES_TXN;
		else if (m->tstate == TS_RUNNING || m->tstate == TS_DONE) code = RES_DUP;
		else if (m->op != OP_READ && m->up_ptr != (m->len + 1) / 2) code = RES_INCOMPLETE;
		else code = RES_OK;
		r[2] = code;
		if (code == RES_OK) {
			m->tstate = TS_RUNNING;
			m->run_polls = m->len > 64 ? 3 : 1;
			if (m->op == OP_LOOPBACK) { m->res_code = RES_OK; m->res_len = m->len; m->tstate = TS_DONE; }
		}
		break;
	}
	case SUB_FETCH: {
		if (!HAVE(4)) break;
		uint16_t code;
		int words = (m->res_len + 1) / 2;
		if (m->tstate != TS_DONE) code = RES_STATE;
		else if (W(2) != m->txn) code = RES_TXN;
		else if (W(4) == 0 || W(4) > DASH_MAX_XFER_WORDS || W(3) + W(4) > words) code = RES_RANGE;
		else code = RES_OK;
		r[4] = code;
		if (code != RES_OK) break;
		for (int k = 0; k < W(4) && 5 + k <= n; k++) {
			int i = (W(3) + k) * 2;
			r[5 + k] = (uint16_t)((m->staging[i] << 8) | m->staging[i + 1]);
		}
		break;
	}
	case SUB_ACK:
	case SUB_CANCEL: {
		if (!HAVE(2)) break;
		uint16_t code;
		if (m->tstate == TS_IDLE) code = RES_STATE;
		else if (W(2) != m->txn) code = RES_TXN;
		else if (m->tstate == TS_RUNNING) code = RES_BUSY;
		else if (sc == SUB_ACK && m->tstate != TS_DONE) code = RES_STATE;
		else code = RES_OK;
		r[2] = code;
		if (code == RES_OK) m->tstate = TS_IDLE;
		break;
	}
	case SUB_INPUT:
		if (!HAVE(3)) break;
		m->target = (uint16_t)((m->target | W(2)) & ~W(3) & 0x0FFF);
		m->latch |= W(2) & 0x0FFF;
		r[3] = m->target;
		if (frozen(m)) apply_input(m);
		break;
	case SUB_HEARTBEAT:
		break;
	case SUB_PAUSE:
	case SUB_RESUME:
		if (!HAVE(2)) break;
		if (!(m->features & FEAT_FREEZE)) { r[2] = RES_UNSUPPORTED; break; }
		m->pause_req = sc == SUB_PAUSE;
		r[2] = RES_OK;
		break;
	default:
		for (int k = 2; k <= n; k++) r[k] = RES_UNSUPPORTED;
	}
#undef W
#undef HAVE
}

static int mock_batch(transport_t *t, xfer_t *x, int n, int timeout_ms)
{
	(void)timeout_ms;
	model *m = t->ctx;
	for (int i = 0; i < n; i++) {
		if (!m->is_megacd) { x[i].status = IPC_NOT_MEGACD; continue; }
		uint16_t w[IPC_MAX_WORDS];
		int nw = x[i].n_out + x[i].n_in;
		memcpy(w, x[i].out, sizeof(uint16_t) * (size_t)x[i].n_out);
		memset(w + x[i].n_out, 0, sizeof(uint16_t) * (size_t)x[i].n_in);
		packet(m, w, nw, x[i].resp);
		x[i].status = IPC_OK;
	}
	return T_OK;
}

static int mock_info(transport_t *t, int *is_megacd, int timeout_ms)
{
	(void)timeout_ms;
	*is_megacd = ((model *)t->ctx)->is_megacd;
	return T_OK;
}

static void mock_destroy(transport_t *t)
{
	free(t->ctx);
	free(t);
}

transport_t *transport_mock_new(uint16_t features)
{
	transport_t *t = calloc(1, sizeof(*t));
	model *m = calloc(1, sizeof(*m));
	if (!t || !m) { free(t); free(m); return NULL; }
	m->features = features;
	m->is_megacd = 1;
	t->ctx = m;
	t->epoch = 1;
	t->batch = mock_batch;
	t->info = mock_info;
	t->destroy = mock_destroy;
	return t;
}

void mock_set_core(transport_t *t, int is_megacd, int bump_epoch)
{
	model *m = t->ctx;
	m->is_megacd = is_megacd;
	if (bump_epoch) {
		uint16_t f = m->features;
		memset(m, 0, sizeof(*m));   // a freshly configured core
		m->features = f;
		m->is_megacd = is_megacd;
		t->epoch++;
	}
}

void mock_set_ram(transport_t *t, int space, uint32_t addr, const uint8_t *data, int len)
{
	model *m = t->ctx;
	memcpy((space == SP_VRAM ? m->vram : m->wram) + addr, data, (size_t)len);
}

void mock_get_ram(transport_t *t, int space, uint32_t addr, uint8_t *data, int len)
{
	model *m = t->ctx;
	memcpy(data, (space == SP_VRAM ? m->vram : m->wram) + addr, (size_t)len);
}

void mock_advance_frame(transport_t *t)
{
	model *m = t->ctx;
	m->frame++;
	apply_input(m);
}

uint16_t mock_joy(transport_t *t)
{
	return ((model *)t->ctx)->applied;
}
