#include "api.h"
#include "json.h"

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define WORKRAM_BASE 0xFF0000u

static const struct { const char *name; uint16_t bit; } buttons[] = {
	{ "right", JOY_RIGHT }, { "left", JOY_LEFT }, { "down", JOY_DOWN }, { "up", JOY_UP },
	{ "a", JOY_A }, { "b", JOY_B }, { "c", JOY_C }, { "start", JOY_START },
	{ "mode", JOY_MODE }, { "x", JOY_X }, { "y", JOY_Y }, { "z", JOY_Z },
};
#define NBUTTONS (int)(sizeof(buttons) / sizeof(buttons[0]))

static int fail(char *out, size_t cap, dash_err *e)
{
	char msg[256];
	json_escape(msg, sizeof(msg), e->msg);
	snprintf(out, cap, "{\"ok\":false,\"error\":{\"code\":\"%s\",\"message\":%s}}", e->code, msg);
	return e->http;
}

static int bad(char *out, size_t cap, int http, const char *code, const char *msg)
{
	dash_err e;
	dev_set_err(&e, http, code, "%s", msg);
	return fail(out, cap, &e);
}

static int get_int(const json_value *root, const char *key, long min, long max, long *v)
{
	const json_value *j = json_get(root, key);
	if (!j || j->type != J_NUMBER || j->num != floor(j->num) || j->num < min || j->num > max) return -1;
	*v = (long)j->num;
	return 0;
}

static int check_encoding(const json_value *root)
{
	const json_value *enc = json_get(root, "encoding");
	return !enc || (enc->type == J_STRING && !strcmp(enc->str, "hex")) ? 0 : -1;
}

static int hexnib(char c)
{
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

static int decode_hex(const json_value *root, uint8_t *buf, int max)
{
	const json_value *d = json_get(root, "data");
	if (!d || d->type != J_STRING) return -1;
	size_t n = strlen(d->str);
	if (n == 0 || n % 2 || n / 2 > (size_t)max) return -1;
	for (size_t i = 0; i < n / 2; i++) {
		int h = hexnib(d->str[2 * i]), l = hexnib(d->str[2 * i + 1]);
		if (h < 0 || l < 0) return -1;
		buf[i] = (uint8_t)(h << 4 | l);
	}
	return (int)(n / 2);
}

static void to_hex(char *dst, const uint8_t *src, int n)
{
	static const char hx[] = "0123456789abcdef";
	for (int i = 0; i < n; i++) {
		dst[2 * i] = hx[src[i] >> 4];
		dst[2 * i + 1] = hx[src[i] & 15];
	}
	dst[2 * n] = 0;
}

static int require_feature(api_ctx *a, uint16_t feat, const char *what, char *out, size_t cap)
{
	dash_err e;
	if (dev_ensure(a->dev, &e)) return fail(out, cap, &e);
	if (!(a->dev->features & feat)) {
		dev_set_err(&e, 501, "feature_unavailable", "%s", what);
		return fail(out, cap, &e);
	}
	return 0;
}

static const char *NEED_FREEZE_WRITE =
	"CPU-bus writes need the coordinated freeze and FX68K prefetch hook, which this RBF does not provide";
static const char *NEED_FREEZE_VRAM =
	"VRAM access needs the coordinated freeze to arbitrate the VDP port, which this RBF does not provide";

// ---------------------------------------------------------------- routes

static int r_bus_peek(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	long addr, len;
	const json_value *bus = json_get(root, "bus");
	if (!bus || bus->type != J_STRING || strcmp(bus->str, "main68k"))
		return bad(out, cap, 400, "unsupported_bus", "only bus \"main68k\" is supported");
	if (check_encoding(root)) return bad(out, cap, 400, "invalid_encoding", "only encoding \"hex\" is supported");
	if (get_int(root, "address", 0, 0xFFFFFF, &addr)) return bad(out, cap, 400, "invalid_range", "address must be an integer 0..0xFFFFFF");
	if (get_int(root, "length", 1, API_BUS_MAX, &len)) return bad(out, cap, 400, "invalid_range", "length must be an integer 1..64");
	if ((unsigned long)addr < WORKRAM_BASE || (unsigned long)(addr + len) > 0x1000000)
		return bad(out, cap, 400, "invalid_range", "only Main-CPU work RAM $FF0000-$FFFFFF is supported");

	int r = require_feature(a, FEAT_WORKRAM_READ, "work RAM reads are not supported by this RBF", out, cap);
	if (r) return r;
	uint8_t buf[API_BUS_MAX];
	dash_err e;
	if (dev_mem(a->dev, OP_READ, SP_WORKRAM, (uint32_t)(addr - WORKRAM_BASE), (int)len, NULL, buf, &e))
		return fail(out, cap, &e);
	char hex[API_BUS_MAX * 2 + 1];
	to_hex(hex, buf, (int)len);
	snprintf(out, cap, "{\"ok\":true,\"data\":\"%s\"}", hex);
	return 200;
}

static int r_bus_poke(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	long addr;
	uint8_t buf[API_BUS_MAX];
	const json_value *bus = json_get(root, "bus");
	if (!bus || bus->type != J_STRING || strcmp(bus->str, "main68k"))
		return bad(out, cap, 400, "unsupported_bus", "only bus \"main68k\" is supported");
	if (check_encoding(root)) return bad(out, cap, 400, "invalid_encoding", "only encoding \"hex\" is supported");
	if (get_int(root, "address", 0, 0xFFFFFF, &addr)) return bad(out, cap, 400, "invalid_range", "address must be an integer 0..0xFFFFFF");
	int len = decode_hex(root, buf, API_BUS_MAX);
	if (len < 0) return bad(out, cap, 400, "invalid_hex", "data must be 1..64 bytes of hex");
	if ((unsigned long)addr < WORKRAM_BASE || (unsigned long)(addr + len) > 0x1000000)
		return bad(out, cap, 400, "invalid_range", "only Main-CPU work RAM $FF0000-$FFFFFF is supported");

	int r = require_feature(a, FEAT_WORKRAM_WRITE | FEAT_FREEZE, NEED_FREEZE_WRITE, out, cap);
	if (r) return r;
	if ((a->dev->features & (FEAT_WORKRAM_WRITE | FEAT_FREEZE)) != (FEAT_WORKRAM_WRITE | FEAT_FREEZE)) {
		dash_err e;
		dev_set_err(&e, 501, "feature_unavailable", "%s", NEED_FREEZE_WRITE);
		return fail(out, cap, &e);
	}
	dash_err e;
	if (dev_mem(a->dev, OP_WRITE, SP_WORKRAM, (uint32_t)(addr - WORKRAM_BASE), len, buf, NULL, &e))
		return fail(out, cap, &e);
	snprintf(out, cap, "{\"ok\":true}");
	return 200;
}

// API VRAM byte A is canonical VDP byte (A ^ 1): GPGX's word-swapped layout.
static int r_peek(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	long addr, len;
	const json_value *dom = json_get(root, "domain");
	if (!dom || dom->type != J_STRING || strcmp(dom->str, "vram"))
		return bad(out, cap, 400, "unsupported_domain", "only domain \"vram\" is supported");
	if (check_encoding(root)) return bad(out, cap, 400, "invalid_encoding", "only encoding \"hex\" is supported");
	if (get_int(root, "address", 0, 0xFFFF, &addr)) return bad(out, cap, 400, "invalid_range", "address must be an integer 0..0xFFFF");
	if (get_int(root, "length", 1, 0x10000, &len)) return bad(out, cap, 400, "invalid_range", "length must be a positive integer");
	if (len > API_VRAM_MAX) return bad(out, cap, 413, "invalid_range", "VRAM transfers are limited to 2048 bytes");
	if (addr + len > 0x10000) return bad(out, cap, 400, "invalid_range", "range exceeds 64 KiB VRAM");

	int r = require_feature(a, FEAT_VRAM_READ, NEED_FREEZE_VRAM, out, cap);
	if (r) return r;
	// canonical span covering every (A ^ 1)
	uint32_t c0 = (uint32_t)addr & ~1u;
	uint32_t c1 = ((uint32_t)(addr + len) + 1) & ~1u;
	if (c1 - c0 > API_VRAM_MAX) return bad(out, cap, 413, "invalid_range", "VRAM transfers are limited to 2048 bytes");
	uint8_t canon[API_VRAM_MAX], api[API_VRAM_MAX];
	dash_err e;
	if (dev_mem(a->dev, OP_READ, SP_VRAM, c0, (int)(c1 - c0), NULL, canon, &e)) return fail(out, cap, &e);
	for (long i = 0; i < len; i++) api[i] = canon[(((uint32_t)addr + i) ^ 1) - c0];
	static char hex[API_VRAM_MAX * 2 + 1];
	to_hex(hex, api, (int)len);
	snprintf(out, cap, "{\"ok\":true,\"data\":\"%s\"}", hex);
	return 200;
}

static int r_poke(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	long addr;
	static uint8_t buf[API_VRAM_MAX], canon[API_VRAM_MAX];
	const json_value *dom = json_get(root, "domain");
	if (!dom || dom->type != J_STRING || strcmp(dom->str, "vram"))
		return bad(out, cap, 400, "unsupported_domain", "only domain \"vram\" is supported");
	if (check_encoding(root)) return bad(out, cap, 400, "invalid_encoding", "only encoding \"hex\" is supported");
	if (get_int(root, "address", 0, 0xFFFF, &addr)) return bad(out, cap, 400, "invalid_range", "address must be an integer 0..0xFFFF");
	const json_value *d = json_get(root, "data");
	if (d && d->type == J_STRING && strlen(d->str) / 2 > API_VRAM_MAX)
		return bad(out, cap, 413, "invalid_range", "VRAM transfers are limited to 2048 bytes");
	int len = decode_hex(root, buf, API_VRAM_MAX);
	if (len < 0) return bad(out, cap, 400, "invalid_hex", "data must be hex, at most 2048 bytes");
	if ((addr | len) & 1) return bad(out, cap, 400, "invalid_range", "VRAM writes must start and end on a word boundary");
	if (addr + len > 0x10000) return bad(out, cap, 400, "invalid_range", "range exceeds 64 KiB VRAM");

	int r = require_feature(a, FEAT_VRAM_WRITE, NEED_FREEZE_VRAM, out, cap);
	if (r) return r;
	for (int i = 0; i < len; i++) canon[i ^ 1] = buf[i];
	dash_err e;
	if (dev_mem(a->dev, OP_WRITE, SP_VRAM, (uint32_t)addr, len, canon, NULL, &e)) return fail(out, cap, &e);
	snprintf(out, cap, "{\"ok\":true}");
	return 200;
}

static int parse_buttons(const json_value *root, const char *key, uint16_t *mask, char *err, size_t errcap)
{
	const json_value *arr = json_get(root, key);
	*mask = 0;
	if (!arr) return 0;
	if (arr->type != J_ARRAY) { snprintf(err, errcap, "%s must be an array of button names", key); return -1; }
	for (const json_value *v = arr->child; v; v = v->next) {
		int found = 0;
		if (v->type == J_STRING)
			for (int i = 0; i < NBUTTONS; i++)
				if (!strcmp(v->str, buttons[i].name)) { *mask |= buttons[i].bit; found = 1; }
		if (!found) {
			snprintf(err, errcap, "unknown button in %s (use right,left,down,up,a,b,c,start,mode,x,y,z)", key);
			return -1;
		}
	}
	return 0;
}

static int held_json(uint16_t mask, char *out, size_t cap)
{
	size_t n = 0;
	n += (size_t)snprintf(out + n, cap - n, "[");
	int first = 1;
	for (int i = 0; i < NBUTTONS; i++)
		if (mask & buttons[i].bit) {
			n += (size_t)snprintf(out + n, cap - n, "%s\"%s\"", first ? "" : ",", buttons[i].name);
			first = 0;
		}
	n += (size_t)snprintf(out + n, cap - n, "]");
	return (int)n;
}

static int r_input(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	uint16_t press, release;
	char msg[160];
	if (parse_buttons(root, "press", &press, msg, sizeof(msg)) ||
	    parse_buttons(root, "release", &release, msg, sizeof(msg)))
		return bad(out, cap, 400, "invalid_button", msg);
	int r = require_feature(a, FEAT_INPUT, "input injection is not supported by this RBF", out, cap);
	if (r) return r;
	dash_err e;
	if (dev_input(a->dev, press, release, &e)) return fail(out, cap, &e);
	char held[160];
	held_json(a->dev->held, held, sizeof(held));
	snprintf(out, cap, "{\"ok\":true,\"held\":%s}", held);
	return 200;
}

static int r_pause(api_ctx *a, int pause, char *out, size_t cap)
{
	dash_err e;
	if (dev_pause(a->dev, pause, &e)) return fail(out, cap, &e);
	snprintf(out, cap, "{\"ok\":true,\"paused\":%s}", pause ? "true" : "false");
	return 200;
}

// Simple relative filenames only: no separators, no leading dot, bounded charset.
static int valid_state_name(const char *s)
{
	size_t n = strlen(s);
	if (n == 0 || n > 128 || s[0] == '.') return 0;
	for (size_t i = 0; i < n; i++) {
		char c = s[i];
		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
		      c == '.' || c == '_' || c == '-' || c == '+'))
			return 0;
	}
	return 1;
}

static int state_path(const json_value *root, const char **name, char *out, size_t cap)
{
	const json_value *p = json_get(root, "path");
	if (!p || p->type != J_STRING) return bad(out, cap, 400, "invalid_path", "path must be a string");
	if (!valid_state_name(p->str))
		return bad(out, cap, 400, "invalid_path",
		           "path must be a plain filename (letters, digits, . _ - +; no directories or leading dot)");
	*name = p->str;
	return 0;
}

static const char *NO_NATIVE_STATE =
	"native savestates are not implemented in this build (see docs/dashboard-feasibility.md)";

static int r_state_save(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	const char *name;
	int r = state_path(root, &name, out, cap);
	if (r) return r;
	char esc[300];
	json_escape(esc, sizeof(esc), name);

	if (!strcmp(name, API_REFRESH_NAME)) {
		// Compatibility no-op (API 3.4): only valid once VRAM writes reach the VDP directly.
		r = require_feature(a, FEAT_VRAM_WRITE, NEED_FREEZE_VRAM, out, cap);
		if (r) return r;
		a->refresh_valid = 1;
		a->refresh_epoch = a->dev->epoch;
		a->refresh_gen = a->dev->gen;
		snprintf(out, cap, "{\"ok\":true,\"path\":%s}", esc);
		return 200;
	}
	r = require_feature(a, FEAT_STATE, NO_NATIVE_STATE, out, cap);
	if (r) return r;
	return bad(out, cap, 501, "feature_unavailable", NO_NATIVE_STATE);
}

static int r_state_load(api_ctx *a, const json_value *root, char *out, size_t cap)
{
	const char *name;
	int r = state_path(root, &name, out, cap);
	if (r) return r;

	if (!strcmp(name, API_REFRESH_NAME)) {
		dash_err e;
		if (dev_ensure(a->dev, &e)) return fail(out, cap, &e);
		if (!a->refresh_valid || a->refresh_epoch != a->dev->epoch || a->refresh_gen != a->dev->gen)
			return bad(out, cap, 409, "state_incompatible",
			           "no matching VRAM refresh token in this core session (save it first)");
		a->refresh_valid = 0;
		snprintf(out, cap, "{\"ok\":true}");
		return 200;
	}

	char path[768];
	snprintf(path, sizeof(path), "%s/%s", a->state_dir, name);
	struct stat st;
	if (lstat(path, &st) < 0) {
		char msg[200];
		snprintf(msg, sizeof(msg), "state file %s not found in the state directory", name);
		return bad(out, cap, 404, "state_not_found", msg);
	}
	if (!S_ISREG(st.st_mode))
		return bad(out, cap, 400, "invalid_path", "state path is not a regular file");

	char sig[16] = { 0 };
	int fd = open(path, O_RDONLY | O_NOFOLLOW);
	if (fd < 0) return bad(out, cap, 500, "state_io_error", "cannot open state file");
	ssize_t n = read(fd, sig, sizeof(sig));
	close(fd);
	if (n >= 10 && !memcmp(sig, "GENPLUS-GX", 10))
		return bad(out, cap, 409, "state_incompatible",
		           "this is a Genesis Plus GX savestate; it cannot be loaded on the MiSTer core");
	r = require_feature(a, FEAT_STATE, NO_NATIVE_STATE, out, cap);
	if (r) return r;
	return bad(out, cap, 501, "feature_unavailable", NO_NATIVE_STATE);
}

static void feature_json(api_ctx *a, char *out, size_t cap, int known)
{
	uint16_t f = known ? a->dev->features : 0;
	int freeze = !!(f & FEAT_FREEZE);
	snprintf(out, cap,
		"{\"bus_peek\":%s,\"bus_poke\":%s,\"vram_peek\":%s,\"vram_poke\":%s,\"input\":%s,"
		"\"pause\":%s,\"state\":%s,\"vram_refresh_token\":%s}",
		f & FEAT_WORKRAM_READ ? "true" : "false",
		(f & FEAT_WORKRAM_WRITE) && freeze ? "true" : "false",
		f & FEAT_VRAM_READ ? "true" : "false",
		f & FEAT_VRAM_WRITE ? "true" : "false",
		f & FEAT_INPUT ? "true" : "false",
		freeze ? "true" : "false",
		f & FEAT_STATE ? "true" : "false",
		f & FEAT_VRAM_WRITE ? "true" : "false");
}

static int r_capabilities(api_ctx *a, char *out, size_t cap)
{
	dash_err e;
	int ok = dev_ensure(a->dev, &e) == 0;
	char feats[400], core[400];
	feature_json(a, feats, sizeof(feats), ok);
	if (ok)
		snprintf(core, sizeof(core),
			"{\"present\":true,\"name\":\"MegaCD\",\"protocol\":%d,\"build_id\":\"%08x\",\"feature_bits\":%u,\"epoch\":%u}",
			DASH_PROTO_VERSION, a->dev->build_id, a->dev->features, a->dev->epoch);
	else {
		char msg[256];
		json_escape(msg, sizeof(msg), e.msg);
		snprintf(core, sizeof(core), "{\"present\":false,\"error\":{\"code\":\"%s\",\"message\":%s}}", e.code, msg);
	}
	snprintf(out, cap,
		"{\"ok\":true,\"server\":\"megacd-dashboard\",\"version\":\"" BRIDGE_VERSION "\",\"api\":\"desertbus-dashboard/1\","
		"\"target\":\"mister-megacd\",\"core\":%s,\"features\":%s,"
		"\"limits\":{\"bus_max_bytes\":%d,\"vram_max_bytes\":%d,\"body_max_bytes\":%d},"
		"\"read_consistency\":\"%s\",\"reserved_state_names\":[\"" API_REFRESH_NAME "\"],"
		"\"buses\":[\"main68k\"],\"bus_ranges\":[[16711680,16777215]],\"domains\":[\"vram\"]}",
		core, feats, API_BUS_MAX, API_VRAM_MAX, API_BODY_MAX,
		ok && (a->dev->features & FEAT_READ_COHERENT) ? "coherent" : "word");
	return 200;
}

static int r_status(api_ctx *a, char *out, size_t cap)
{
	dash_err e;
	char held[160];
	if (dev_status(a->dev, &e)) {
		char msg[256];
		json_escape(msg, sizeof(msg), e.msg);
		snprintf(out, cap,
			"{\"ok\":true,\"connected\":false,\"error\":{\"code\":\"%s\",\"message\":%s}}", e.code, msg);
		return 200;
	}
	held_json(a->dev->held, held, sizeof(held));
	uint16_t f = a->dev->flags;
	snprintf(out, cap,
		"{\"ok\":true,\"connected\":true,\"paused\":%s,\"pause_requested\":%s,\"busy\":%s,\"fault\":%s,"
		"\"held\":%s,\"frame\":%u,\"session\":%u,\"epoch\":%u,\"ops\":%llu,\"errors\":%llu}",
		f & FL_FROZEN ? "true" : "false", f & FL_PAUSE_REQ ? "true" : "false",
		f & FL_RUNNING ? "true" : "false", f & FL_FAULT ? "true" : "false",
		held, a->dev->frame, a->dev->gen, a->dev->epoch, a->dev->ops, a->dev->errors);
	return 200;
}

int api_priority(const char *path)
{
	return (!strcmp(path, "/input") || !strcmp(path, "/pause") || !strcmp(path, "/resume")) ? 0 : 1;
}

void api_release_all(api_ctx *a)
{
	dash_err e;
	if (a->dev->have_session) dev_input(a->dev, 0, 0x0FFF, &e);
}

int api_handle(api_ctx *a, const char *method, const char *path,
               const char *body, size_t body_len, char *out, size_t cap)
{
	int is_get = !strcmp(method, "GET");
	int is_post = !strcmp(method, "POST");

	if (!strcmp(path, "/capabilities") && (is_get || is_post)) return r_capabilities(a, out, cap);
	if (!strcmp(path, "/status") && (is_get || is_post)) return r_status(a, out, cap);

	static const char *posts[] = { "/bus-peek", "/bus-poke", "/peek", "/poke", "/input",
	                               "/pause", "/resume", "/state/save", "/state/load" };
	int known = 0;
	for (size_t i = 0; i < sizeof(posts) / sizeof(posts[0]); i++)
		if (!strcmp(path, posts[i])) known = 1;
	if (!known) return bad(out, cap, 404, "not_found", "unknown endpoint");
	if (!is_post) return bad(out, cap, 405, "method_not_allowed", "use POST");

	if (!strcmp(path, "/pause")) return r_pause(a, 1, out, cap);
	if (!strcmp(path, "/resume")) return r_pause(a, 0, out, cap);

	static json_doc doc;
	if (body_len == 0 || json_parse(&doc, body, body_len) || doc.root->type != J_OBJECT)
		return bad(out, cap, 400, "invalid_json", "request body must be a JSON object");
	const json_value *root = doc.root;

	if (!strcmp(path, "/bus-peek")) return r_bus_peek(a, root, out, cap);
	if (!strcmp(path, "/bus-poke")) return r_bus_poke(a, root, out, cap);
	if (!strcmp(path, "/peek")) return r_peek(a, root, out, cap);
	if (!strcmp(path, "/poke")) return r_poke(a, root, out, cap);
	if (!strcmp(path, "/input")) return r_input(a, root, out, cap);
	if (!strcmp(path, "/state/save")) return r_state_save(a, root, out, cap);
	return r_state_load(a, root, out, cap);
}
