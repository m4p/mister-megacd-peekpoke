// Session and transaction layer over the FPGA dashboard endpoint.
#ifndef DASH_DEV_H
#define DASH_DEV_H

#include <stdint.h>
#include "transport.h"

typedef struct dash_err {
	int http;
	const char *code;
	char msg[200];
} dash_err;

typedef struct dash_dev {
	transport_t *t;
	int have_session;
	uint8_t gen;
	uint32_t epoch;
	uint16_t features;
	uint32_t build_id;
	uint16_t txn;
	uint16_t flags;          // FLAGS from the most recent packet
	uint16_t held;           // injected input target mask
	uint16_t frame;
	long long last_packet_ms;
	int op_timeout_ms;
	int ipc_timeout_ms;
	int user_paused;         // last pause state this bridge requested
	// statistics
	unsigned long long ops, errors;
} dash_dev;

void dev_init(dash_dev *d, transport_t *t);

// Probes the core and opens a session when needed.
int dev_ensure(dash_dev *d, dash_err *e);

// Memory transaction: op = OP_READ / OP_WRITE / OP_LOOPBACK, canonical byte order.
int dev_mem(dash_dev *d, int op, int space, uint32_t addr, int len,
            const uint8_t *wdata, uint8_t *rdata, dash_err *e);

int dev_input(dash_dev *d, uint16_t press, uint16_t release, dash_err *e);
int dev_pause(dash_dev *d, int pause, dash_err *e);
int dev_status(dash_dev *d, dash_err *e);

// Keeps the FPGA owner lease alive while idle. Never blocks long.
void dev_heartbeat(dash_dev *d);

void dev_set_err(dash_err *e, int http, const char *code, const char *fmt, ...);

long long dash_now_ms(void);

#endif
