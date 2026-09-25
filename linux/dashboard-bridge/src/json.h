// Minimal JSON reader for the dashboard's small request bodies.
#ifndef DASH_JSON_H
#define DASH_JSON_H

#include <stddef.h>

enum json_type { J_NULL, J_BOOL, J_NUMBER, J_STRING, J_ARRAY, J_OBJECT };

typedef struct json_value {
	enum json_type type;
	double num;
	int boolean;
	char *str;                 // J_STRING (NUL-terminated, unescaped)
	char *key;                 // member name when inside an object
	struct json_value *child;  // first element / member
	struct json_value *next;   // next sibling
} json_value;

#define JSON_MAX_NODES 256
#define JSON_MAX_TEXT  8192

typedef struct json_doc {
	json_value nodes[JSON_MAX_NODES];
	int used;
	char text[JSON_MAX_TEXT + 1];
	size_t text_used;
	json_value *root;
} json_doc;

// Returns 0 on success, -1 on syntax error or when limits are exceeded.
int json_parse(json_doc *doc, const char *src, size_t len);

const json_value *json_get(const json_value *obj, const char *key);

// Writes s as a JSON string literal (with quotes). Returns bytes written or -1.
int json_escape(char *dst, size_t cap, const char *s);

#endif
