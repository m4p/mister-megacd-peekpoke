// Public dashboard API (Genesis-Plus-GX/DASHBOARD-API.md) on top of dash_dev.
#ifndef DASH_API_H
#define DASH_API_H

#include <stddef.h>
#include "dev.h"

#define API_BODY_MAX       8192
#define API_BUS_MAX        64
#define API_VRAM_MAX       2048
#define API_REFRESH_NAME   "dashboard-cache-refresh.gp0"
#define BRIDGE_VERSION     "0.1.0"

typedef struct api_ctx {
	dash_dev *dev;
	char state_dir[512];
	// reserved VRAM-refresh token (API 3.4 compatibility)
	int refresh_valid;
	uint32_t refresh_epoch;
	uint8_t refresh_gen;
} api_ctx;

// Routes one request. Writes a JSON body to out and returns the HTTP status.
int api_handle(api_ctx *a, const char *method, const char *path,
               const char *body, size_t body_len, char *out, size_t cap);

// Priority class used by the server's scheduler (0 = control, 1 = normal).
int api_priority(const char *path);

// Clears injected input (used on shutdown).
void api_release_all(api_ctx *a);

#endif
