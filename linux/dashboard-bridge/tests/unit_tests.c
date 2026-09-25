// Host unit tests for the bridge API over the mock FPGA endpoint.
#include "api.h"
#include "json.h"
#include "transport.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int failures = 0, checks = 0;

#define CHECK(cond, ...) do { checks++; if (!(cond)) { failures++; \
	printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); } } while (0)

static char out[16384];

static int call(api_ctx *a, const char *path, const char *body)
{
	return api_handle(a, "POST", path, body, body ? strlen(body) : 0, out, sizeof(out));
}

static int has(const char *needle) { return strstr(out, needle) != NULL; }

static void setup(api_ctx *a, dash_dev *d, transport_t *t, const char *dir)
{
	dev_init(d, t);
	memset(a, 0, sizeof(*a));
	a->dev = d;
	snprintf(a->state_dir, sizeof(a->state_dir), "%s", dir);
}

static void test_json(void)
{
	static json_doc doc;
	const char *s = "{\"a\":1,\"b\":\"x\\u0041\\n\",\"c\":[\"left\",\"right\"],\"d\":true,\"e\":null,\"f\":{\"g\":-2.5e1}}";
	CHECK(json_parse(&doc, s, strlen(s)) == 0, "valid json");
	CHECK(json_get(doc.root, "a")->num == 1, "number");
	CHECK(!strcmp(json_get(doc.root, "b")->str, "xA\n"), "string escapes");
	CHECK(json_get(doc.root, "c")->child->next->type == J_STRING, "array");
	CHECK(json_get(json_get(doc.root, "f"), "g")->num == -25, "nested");
	const char *bad[] = { "", "{", "{\"a\":}", "{\"a\":1,}", "[1 2]", "{\"a\":1} x", "{\"a\":\"\\u0000\"}", "{'a':1}" };
	for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++)
		CHECK(json_parse(&doc, bad[i], strlen(bad[i])) != 0, "invalid json %zu rejected", i);
	char esc[64];
	json_escape(esc, sizeof(esc), "a\"b\\c\n");
	CHECK(!strcmp(esc, "\"a\\\"b\\\\c\\u000a\""), "escape: %s", esc);
}

static void test_hw_features(const char *dir)
{
	transport_t *t = transport_mock_new(FEAT_INPUT | FEAT_WORKRAM_READ | FEAT_LOOPBACK);
	dash_dev d;
	api_ctx a;
	setup(&a, &d, t, dir);

	uint8_t speed[2] = { 0x3A, 0x00 };
	mock_set_ram(t, SP_WORKRAM, 0x6FEA, speed, 2);
	uint8_t pattern[256];
	for (int i = 0; i < 256; i++) pattern[i] = (uint8_t)(i * 7 + 3);
	mock_set_ram(t, SP_WORKRAM, 0x7A00, pattern, 256);

	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":2,\"encoding\":\"hex\"}") == 200, "%s", out);
	CHECK(has("\"data\":\"3a00\""), "speed: %s", out);

	// every dashboard length, odd and even offsets
	int lens[] = { 1, 2, 4, 6, 8, 10, 18, 56, 64 };
	for (size_t li = 0; li < sizeof(lens) / sizeof(lens[0]); li++)
		for (int off = 0; off < 3; off++) {
			char body[160];
			snprintf(body, sizeof(body), "{\"bus\":\"main68k\",\"address\":%d,\"length\":%d}", 0xFF7A00 + off, lens[li]);
			CHECK(call(&a, "/bus-peek", body) == 200, "peek %d+%d: %s", off, lens[li], out);
			char exp[140] = "";
			for (int i = 0; i < lens[li]; i++) sprintf(exp + 2 * i, "%02x", pattern[off + i]);
			CHECK(has(exp), "peek data %d+%d", off, lens[li]);
		}

	// validation
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"sub68k\",\"address\":16740330,\"length\":2}") == 400 && has("unsupported_bus"), "%s", out);
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":\"0xFF6FEA\",\"length\":2}") == 400 && has("invalid_range"), "string address");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330.5,\"length\":2}") == 400, "fractional address");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":65}") == 400, "length 65");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":0}") == 400, "length 0");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16777215,\"length\":2}") == 400, "past $FFFFFF");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":1024,\"length\":2}") == 400 && has("work RAM"), "ROM range");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":2,\"encoding\":\"base64\"}") == 400, "encoding");
	CHECK(call(&a, "/bus-peek", "not json") == 400 && has("invalid_json"), "bad json");
	CHECK(call(&a, "/bus-peek", "[1]") == 400, "array body");
	CHECK(call(&a, "/bus-peek", "") == 400, "empty body");

	// features this RBF lacks: explicit, never a silent success
	CHECK(call(&a, "/bus-poke", "{\"bus\":\"main68k\",\"address\":16745516,\"data\":\"0679010000ff6ffa\",\"encoding\":\"hex\",\"unsafe\":true}") == 501
	      && has("feature_unavailable"), "bus-poke unsupported: %s", out);
	CHECK(call(&a, "/bus-poke", "{\"bus\":\"main68k\",\"address\":16745516,\"data\":\"0g\"}") == 400 && has("invalid_hex"), "bad hex");
	CHECK(call(&a, "/bus-poke", "{\"bus\":\"main68k\",\"address\":16745516,\"data\":\"abc\"}") == 400, "odd hex");
	CHECK(call(&a, "/peek", "{\"domain\":\"vram\",\"address\":10560,\"length\":32,\"encoding\":\"hex\"}") == 501, "vram peek unsupported");
	CHECK(call(&a, "/peek", "{\"domain\":\"cram\",\"address\":0,\"length\":2}") == 400 && has("unsupported_domain"), "cram");
	CHECK(call(&a, "/pause", NULL) == 501 && has("feature_unavailable"), "pause: %s", out);
	CHECK(call(&a, "/resume", NULL) == 501, "resume");
	CHECK(call(&a, "/state/save", "{\"path\":\"123.gp0\"}") == 501 && has("feature_unavailable"), "save: %s", out);
	CHECK(call(&a, "/state/save", "{\"path\":\"dashboard-cache-refresh.gp0\"}") == 501, "refresh token needs VRAM writes");

	// input
	CHECK(call(&a, "/input", "{\"press\":[\"left\"]}") == 200 && has("\"held\":[\"left\"]"), "press: %s", out);
	CHECK(mock_joy(t) == 0, "not applied before frame");
	mock_advance_frame(t);
	CHECK(mock_joy(t) == JOY_LEFT, "applied at frame");
	CHECK(call(&a, "/input", "{\"press\":[\"start\",\"right\"]}") == 200 && has("[\"right\",\"left\",\"start\"]"), "%s", out);
	CHECK(call(&a, "/input", "{\"release\":[\"left\",\"right\",\"start\"]}") == 200 && has("\"held\":[]"), "%s", out);
	CHECK(call(&a, "/input", "{\"release\":[\"left\",\"right\",\"start\"]}") == 200 && has("\"held\":[]"), "idempotent release");
	CHECK(call(&a, "/input", "{\"press\":[\"turbo\"]}") == 400 && has("invalid_button"), "unknown button");
	CHECK(call(&a, "/input", "{\"press\":\"left\"}") == 400, "non-array");
	CHECK(call(&a, "/input", "{\"press\":[\"a\",\"b\",\"c\",\"x\",\"y\",\"z\",\"mode\",\"up\",\"down\"]}") == 200, "all names");
	call(&a, "/input", "{\"release\":[\"a\",\"b\",\"c\",\"x\",\"y\",\"z\",\"mode\",\"up\",\"down\"]}");
	CHECK(has("\"held\":[]"), "released all names");

	// state paths
	const char *badp[] = { "{\"path\":\"../x.gp0\"}", "{\"path\":\"/etc/passwd\"}", "{\"path\":\".hidden\"}",
	                       "{\"path\":\"a/b.gp0\"}", "{\"path\":\"\"}", "{\"path\":5}", "{}" };
	for (size_t i = 0; i < sizeof(badp) / sizeof(badp[0]); i++)
		CHECK(call(&a, "/state/load", badp[i]) == 400 && has("invalid_path"), "bad path %zu: %s", i, out);
	CHECK(call(&a, "/state/load", "{\"path\":\"missing.gp0\"}") == 404 && has("state_not_found"), "%s", out);
	CHECK(call(&a, "/state/load", "{\"path\":\"desertbus-fullauto.gp0\"}") == 409 && has("Genesis Plus GX"), "gpgx: %s", out);
	CHECK(call(&a, "/state/load", "{\"path\":\"native.gp0\"}") == 501, "native load unsupported: %s", out);
	CHECK(call(&a, "/state/load", "{\"path\":\"link.gp0\"}") == 400, "symlink rejected: %s", out);
	CHECK(call(&a, "/state/load", "{\"path\":\"dashboard-cache-refresh.gp0\"}") == 409, "refresh load without token");

	// routing
	CHECK(call(&a, "/nope", "{}") == 404, "unknown route");
	CHECK(api_handle(&a, "GET", "/bus-peek", "", 0, out, sizeof(out)) == 405, "GET on POST route");
	CHECK(api_handle(&a, "GET", "/capabilities", "", 0, out, sizeof(out)) == 200 && has("\"bus_poke\":false")
	      && has("\"input\":true") && has("\"read_consistency\":\"word\""), "capabilities: %s", out);
	CHECK(api_handle(&a, "GET", "/status", "", 0, out, sizeof(out)) == 200 && has("\"connected\":true"), "status: %s", out);

	// core reload: the old session is rejected (nothing executes), the bridge
	// re-probes and retries once, so the first request after a reload succeeds
	call(&a, "/input", "{\"press\":[\"left\"]}");
	mock_set_core(t, 1, 1);
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":2}") == 200, "first request after reload: %s", out);
	CHECK(d.held == 0, "held cleared by reload");
	CHECK(mock_joy(t) == 0, "new core has no injected input");

	// different core loaded
	mock_set_core(t, 0, 1);
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":2}") == 503 && has("not MegaCD"), "%s", out);
	CHECK(api_handle(&a, "GET", "/capabilities", "", 0, out, sizeof(out)) == 200 && has("\"present\":false"), "caps no core");
	mock_set_core(t, 1, 1);
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":2}") == 200, "back");

	t->destroy(t);
}

static void test_full_features(const char *dir)
{
	transport_t *t = transport_mock_new(FEAT_INPUT | FEAT_WORKRAM_READ | FEAT_WORKRAM_WRITE | FEAT_VRAM_READ |
	                                    FEAT_VRAM_WRITE | FEAT_FREEZE | FEAT_LOOPBACK | FEAT_READ_COHERENT);
	dash_dev d;
	api_ctx a;
	setup(&a, &d, t, dir);

	CHECK(call(&a, "/bus-poke", "{\"bus\":\"main68k\",\"address\":16745516,\"data\":\"0679010000ff6ffa\",\"encoding\":\"hex\",\"unsafe\":true}") == 200, "%s", out);
	uint8_t b[64];
	mock_get_ram(t, SP_WORKRAM, 0x842C, b, 8);
	CHECK(!memcmp(b, "\x06\x79\x01\x00\x00\xff\x6f\xfa", 8), "poke landed");
	// 64-byte write at an odd address
	char body[400], hex[140] = "";
	for (int i = 0; i < 64; i++) sprintf(hex + 2 * i, "%02x", (i * 13) & 0xFF);
	snprintf(body, sizeof(body), "{\"bus\":\"main68k\",\"address\":%d,\"data\":\"%s\"}", 0xFF7ABD, hex);
	CHECK(call(&a, "/bus-poke", body) == 200, "64B poke");
	snprintf(body, sizeof(body), "{\"bus\":\"main68k\",\"address\":%d,\"length\":64}", 0xFF7ABD);
	CHECK(call(&a, "/bus-peek", body) == 200 && has(hex), "readback");

	// VRAM: API bytes are the word-swapped (GPGX) view of canonical VRAM
	uint8_t canon[8] = { 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88 };
	mock_set_ram(t, SP_VRAM, 0x2940, canon, 8);
	CHECK(call(&a, "/peek", "{\"domain\":\"vram\",\"address\":10560,\"length\":8}") == 200 && has("\"2211443366558877\""), "swap: %s", out);
	CHECK(call(&a, "/peek", "{\"domain\":\"vram\",\"address\":10561,\"length\":3}") == 200 && has("\"114433\""), "odd peek: %s", out);
	CHECK(call(&a, "/poke", "{\"domain\":\"vram\",\"address\":10560,\"data\":\"aabbccdd\"}") == 200, "vram poke");
	mock_get_ram(t, SP_VRAM, 0x2940, b, 4);
	CHECK(b[0] == 0xBB && b[1] == 0xAA && b[2] == 0xDD && b[3] == 0xCC, "vram canonical");
	CHECK(call(&a, "/poke", "{\"domain\":\"vram\",\"address\":10561,\"data\":\"aabb\"}") == 400, "odd poke rejected");

	// 1056-byte air freshener upload
	static char big[2200 + 200];
	char *p = big + sprintf(big, "{\"domain\":\"vram\",\"address\":9952,\"data\":\"");
	for (int i = 0; i < 1056; i++) p += sprintf(p, "%02x", (i * 31 + 7) & 0xFF);
	strcpy(p, "\"}");
	CHECK(call(&a, "/poke", big) == 200, "1056B poke: %s", out);
	static uint8_t vb[1056];
	mock_get_ram(t, SP_VRAM, 9952, vb, 1056);
	int okv = 1;
	for (int i = 0; i < 1056; i++) if (vb[i ^ 1] != ((i * 31 + 7) & 0xFF)) okv = 0;
	CHECK(okv, "1056B content");

	// refresh token round trip, then refused after a reload
	CHECK(call(&a, "/state/save", "{\"path\":\"dashboard-cache-refresh.gp0\"}") == 200 && has("\"path\":\"dashboard-cache-refresh.gp0\""), "%s", out);
	CHECK(call(&a, "/state/load", "{\"path\":\"dashboard-cache-refresh.gp0\"}") == 200, "token load");
	CHECK(call(&a, "/state/load", "{\"path\":\"dashboard-cache-refresh.gp0\"}") == 409, "token single use");
	call(&a, "/state/save", "{\"path\":\"dashboard-cache-refresh.gp0\"}");
	mock_set_core(t, 1, 1);
	call(&a, "/status", NULL);
	call(&a, "/status", NULL);
	CHECK(call(&a, "/state/load", "{\"path\":\"dashboard-cache-refresh.gp0\"}") == 409, "token invalid after reload: %s", out);

	// pause/resume with freeze
	CHECK(call(&a, "/pause", NULL) == 200 && has("\"paused\":true"), "pause: %s", out);
	CHECK(call(&a, "/pause", NULL) == 200, "pause idempotent");
	CHECK(call(&a, "/input", "{\"press\":[\"left\"]}") == 200, "input while paused");
	CHECK(mock_joy(t) == JOY_LEFT, "applied immediately while frozen");
	CHECK(call(&a, "/bus-peek", "{\"bus\":\"main68k\",\"address\":16740330,\"length\":2}") == 200, "peek while paused");
	CHECK(call(&a, "/resume", NULL) == 200 && has("\"paused\":false"), "resume");
	CHECK(call(&a, "/resume", NULL) == 200, "resume idempotent");
	CHECK(api_handle(&a, "GET", "/capabilities", "", 0, out, sizeof(out)) == 200 && has("\"bus_poke\":true")
	      && has("\"read_consistency\":\"coherent\""), "full caps: %s", out);
	t->destroy(t);
}

int main(void)
{
	char dir[] = "/tmp/megacd-dash-test-XXXXXX";
	if (!mkdtemp(dir)) return 1;
	char path[256];
	snprintf(path, sizeof(path), "%s/desertbus-fullauto.gp0", dir);
	FILE *f = fopen(path, "wb"); fputs("GENPLUS-GX 1.7.6....", f); fclose(f);
	snprintf(path, sizeof(path), "%s/native.gp0", dir);
	f = fopen(path, "wb"); fputs("MCDDASH1", f); fclose(f);
	char link[256];
	snprintf(link, sizeof(link), "%s/link.gp0", dir);
	symlink("/etc/hosts", link);

	test_json();
	test_hw_features(dir);
	test_full_features(dir);

	char cmd[300];
	snprintf(cmd, sizeof(cmd), "rm -rf '%s'", dir);
	system(cmd);
	printf("%s: %d checks, %d failures\n", failures ? "FAILED" : "PASS", checks, failures);
	return failures ? 1 : 0;
}
