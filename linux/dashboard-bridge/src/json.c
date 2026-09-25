#include "json.h"

#include <stdlib.h>
#include <string.h>
#include <stdio.h>

typedef struct {
	json_doc *doc;
	const char *p, *end;
	int depth;
} parser;

static void skip_ws(parser *ps)
{
	while (ps->p < ps->end && (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r')) ps->p++;
}

static json_value *new_node(parser *ps, enum json_type t)
{
	if (ps->doc->used >= JSON_MAX_NODES) return NULL;
	json_value *v = &ps->doc->nodes[ps->doc->used++];
	memset(v, 0, sizeof(*v));
	v->type = t;
	return v;
}

static int put_char(parser *ps, char c)
{
	if (ps->doc->text_used >= JSON_MAX_TEXT) return -1;
	ps->doc->text[ps->doc->text_used++] = c;
	return 0;
}

static int hexval(char c)
{
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}

static char *parse_string(parser *ps)
{
	if (ps->p >= ps->end || *ps->p != '"') return NULL;
	ps->p++;
	char *start = ps->doc->text + ps->doc->text_used;
	while (ps->p < ps->end && *ps->p != '"') {
		char c = *ps->p++;
		if ((unsigned char)c < 0x20) return NULL;
		if (c == '\\') {
			if (ps->p >= ps->end) return NULL;
			char e = *ps->p++;
			switch (e) {
			case '"': case '\\': case '/': c = e; break;
			case 'b': c = '\b'; break;
			case 'f': c = '\f'; break;
			case 'n': c = '\n'; break;
			case 'r': c = '\r'; break;
			case 't': c = '\t'; break;
			case 'u': {
				if (ps->end - ps->p < 4) return NULL;
				int v = 0;
				for (int i = 0; i < 4; i++) {
					int h = hexval(ps->p[i]);
					if (h < 0) return NULL;
					v = (v << 4) | h;
				}
				ps->p += 4;
				// Only ASCII is meaningful in this API; encode the rest as UTF-8.
				if (v < 0x80) c = (char)v;
				else if (v < 0x800) {
					if (put_char(ps, (char)(0xC0 | (v >> 6)))) return NULL;
					c = (char)(0x80 | (v & 0x3F));
				}
				else {
					if (put_char(ps, (char)(0xE0 | (v >> 12)))) return NULL;
					if (put_char(ps, (char)(0x80 | ((v >> 6) & 0x3F)))) return NULL;
					c = (char)(0x80 | (v & 0x3F));
				}
				if (v == 0) return NULL; // embedded NUL
				break;
			}
			default: return NULL;
			}
		}
		if (put_char(ps, c)) return NULL;
	}
	if (ps->p >= ps->end) return NULL;
	ps->p++;
	if (put_char(ps, 0)) return NULL;
	return start;
}

static json_value *parse_value(parser *ps);

static json_value *parse_container(parser *ps, int is_obj)
{
	json_value *v = new_node(ps, is_obj ? J_OBJECT : J_ARRAY);
	if (!v || ++ps->depth > 16) return NULL;
	ps->p++;
	skip_ws(ps);
	json_value **tail = &v->child;
	if (ps->p < ps->end && *ps->p == (is_obj ? '}' : ']')) {
		ps->p++;
		ps->depth--;
		return v;
	}
	for (;;) {
		char *key = NULL;
		skip_ws(ps);
		if (is_obj) {
			key = parse_string(ps);
			if (!key) return NULL;
			skip_ws(ps);
			if (ps->p >= ps->end || *ps->p != ':') return NULL;
			ps->p++;
		}
		json_value *m = parse_value(ps);
		if (!m) return NULL;
		m->key = key;
		*tail = m;
		tail = &m->next;
		skip_ws(ps);
		if (ps->p >= ps->end) return NULL;
		if (*ps->p == ',') { ps->p++; continue; }
		if (*ps->p == (is_obj ? '}' : ']')) { ps->p++; break; }
		return NULL;
	}
	ps->depth--;
	return v;
}

static json_value *parse_value(parser *ps)
{
	skip_ws(ps);
	if (ps->p >= ps->end) return NULL;
	char c = *ps->p;
	if (c == '{') return parse_container(ps, 1);
	if (c == '[') return parse_container(ps, 0);
	if (c == '"') {
		json_value *v = new_node(ps, J_STRING);
		if (!v) return NULL;
		v->str = parse_string(ps);
		return v->str ? v : NULL;
	}
	if (ps->end - ps->p >= 4 && !memcmp(ps->p, "true", 4)) {
		json_value *v = new_node(ps, J_BOOL);
		if (v) { v->boolean = 1; ps->p += 4; }
		return v;
	}
	if (ps->end - ps->p >= 5 && !memcmp(ps->p, "false", 5)) {
		json_value *v = new_node(ps, J_BOOL);
		if (v) ps->p += 5;
		return v;
	}
	if (ps->end - ps->p >= 4 && !memcmp(ps->p, "null", 4)) {
		json_value *v = new_node(ps, J_NULL);
		if (v) ps->p += 4;
		return v;
	}
	if (c == '-' || (c >= '0' && c <= '9')) {
		char buf[64];
		size_t n = 0;
		while (ps->p + n < ps->end && n < sizeof(buf) - 1 &&
		       strchr("+-0123456789.eE", ps->p[n])) n++;
		memcpy(buf, ps->p, n);
		buf[n] = 0;
		char *endp;
		double d = strtod(buf, &endp);
		if (endp != buf + n) return NULL;
		json_value *v = new_node(ps, J_NUMBER);
		if (!v) return NULL;
		v->num = d;
		ps->p += n;
		return v;
	}
	return NULL;
}

int json_parse(json_doc *doc, const char *src, size_t len)
{
	parser ps = { doc, src, src + len, 0 };
	doc->used = 0;
	doc->text_used = 0;
	doc->root = parse_value(&ps);
	if (!doc->root) return -1;
	skip_ws(&ps);
	return ps.p == ps.end ? 0 : -1;
}

const json_value *json_get(const json_value *obj, const char *key)
{
	if (!obj || obj->type != J_OBJECT) return NULL;
	const json_value *found = NULL;
	for (const json_value *m = obj->child; m; m = m->next)
		if (m->key && !strcmp(m->key, key)) found = m; // last duplicate wins
	return found;
}

int json_escape(char *dst, size_t cap, const char *s)
{
	size_t n = 0;
#define PUT(ch) do { if (n + 1 >= cap) return -1; dst[n++] = (ch); } while (0)
	PUT('"');
	for (; *s; s++) {
		unsigned char c = (unsigned char)*s;
		if (c == '"' || c == '\\') { PUT('\\'); PUT((char)c); }
		else if (c < 0x20) {
			if (n + 7 >= cap) return -1;
			n += (size_t)snprintf(dst + n, cap - n, "\\u%04x", c);
		}
		else PUT((char)c);
	}
	PUT('"');
#undef PUT
	dst[n] = 0;
	return (int)n;
}
