// Transport to the FPGA endpoint: the real IPC client (Main_MiSTer) or an
// in-process model of the RTL used for tests.
#ifndef DASH_TRANSPORT_H
#define DASH_TRANSPORT_H

#include <stdint.h>
#include "protocol.h"

typedef struct xfer {
	uint16_t out[IPC_MAX_WORDS];  // header word w[1] onwards
	int n_out;
	int n_in;                     // trailing zero words to clock
	uint16_t resp[IPC_MAX_WORDS + 1]; // r[0..n_out+n_in]
	int status;                   // IPC_*
} xfer_t;

enum { T_OK = 0, T_DOWN = -1, T_TIMEOUT = -2 };

typedef struct transport {
	// Executes n packets in order. Returns T_OK when every reply arrived
	// (check each xfer.status), otherwise T_DOWN/T_TIMEOUT: the link was
	// reset and the outcome of unanswered packets is unknown.
	int (*batch)(struct transport *t, xfer_t *x, int n, int timeout_ms);
	int (*info)(struct transport *t, int *is_megacd, int timeout_ms);
	void (*destroy)(struct transport *t);
	uint32_t epoch;               // core epoch seen in the last reply
	void *ctx;
} transport_t;

transport_t *transport_ipc_new(const char *socket_path);

// Mock endpoint. features: PROBE feature bits the model advertises.
transport_t *transport_mock_new(uint16_t features);
// Test hooks for the mock.
void mock_set_core(transport_t *t, int is_megacd, int bump_epoch);
void mock_set_ram(transport_t *t, int space, uint32_t addr, const uint8_t *data, int len);
void mock_get_ram(transport_t *t, int space, uint32_t addr, uint8_t *data, int len);
void mock_advance_frame(transport_t *t);
uint16_t mock_joy(transport_t *t);

#endif
