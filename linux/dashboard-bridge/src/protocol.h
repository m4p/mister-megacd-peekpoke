// Dashboard wire protocol constants: docs/dashboard-protocol.md.
// Keep in sync with rtl/dashboard_debug.sv and
// Main_MiSTer/support/megacd/dashboard_ipc.h.
#ifndef DASH_PROTOCOL_H
#define DASH_PROTOCOL_H

#include <stdint.h>

#define DASH_PROTO_VERSION   1
#define DASH_MAGIC_ACK       0xDB26
#define DASH_PROBE_MAGIC0    0x4442
#define DASH_PROBE_MAGIC1    0x4D43

enum dash_sub {
	SUB_PROBE     = 0x01,
	SUB_STATUS    = 0x02,
	SUB_BEGIN     = 0x03,
	SUB_UPLOAD    = 0x04,
	SUB_COMMIT    = 0x05,
	SUB_FETCH     = 0x06,
	SUB_ACK       = 0x07,
	SUB_CANCEL    = 0x08,
	SUB_INPUT     = 0x09,
	SUB_HEARTBEAT = 0x0A,
	SUB_PAUSE     = 0x0B,
	SUB_RESUME    = 0x0C,
	SUB_SESSION   = 0x0F,
};

enum dash_result {
	RES_OK = 0,
	RES_SESSION = 1,
	RES_BUSY = 2,
	RES_TXN = 3,
	RES_RANGE = 4,
	RES_UNSUPPORTED = 5,
	RES_INCOMPLETE = 6,
	RES_STATE = 7,
	RES_DUP = 8,
	RES_ABORTED = 9,
	RES_FAULT = 10,
};

enum dash_op { OP_READ = 0x01, OP_WRITE = 0x02, OP_LOOPBACK = 0x7F };
enum dash_space { SP_WORKRAM = 0x01, SP_VRAM = 0x02 };

// FLAGS word
#define FL_RUNNING   (1u << 0)
#define FL_DONE      (1u << 1)
#define FL_STAGING   (1u << 2)
#define FL_PAUSE_REQ (1u << 3)
#define FL_FROZEN    (1u << 4)
#define FL_FAULT     (1u << 5)
#define FL_OWNER     (1u << 6)
#define FL_INPUT     (1u << 7)
#define FL_GEN(f)    (((f) >> 8) & 0xFF)

// PROBE feature bits
#define FEAT_INPUT          (1u << 0)
#define FEAT_WORKRAM_READ   (1u << 1)
#define FEAT_WORKRAM_WRITE  (1u << 2)
#define FEAT_VRAM_READ      (1u << 3)
#define FEAT_VRAM_WRITE     (1u << 4)
#define FEAT_FREEZE         (1u << 5)
#define FEAT_STATE          (1u << 6)
#define FEAT_LOOPBACK       (1u << 7)
#define FEAT_READ_COHERENT  (1u << 8)

#define DASH_STAGING_BYTES   2048
#define DASH_MAX_XFER_WORDS  32

// Joypad bits (active high, core JOY layout)
#define JOY_RIGHT (1u << 0)
#define JOY_LEFT  (1u << 1)
#define JOY_DOWN  (1u << 2)
#define JOY_UP    (1u << 3)
#define JOY_A     (1u << 4)
#define JOY_B     (1u << 5)
#define JOY_C     (1u << 6)
#define JOY_START (1u << 7)
#define JOY_MODE  (1u << 8)
#define JOY_X     (1u << 9)
#define JOY_Y     (1u << 10)
#define JOY_Z     (1u << 11)

// Bridge <-> Main IPC (section 9)
#define IPC_XFER 1
#define IPC_INFO 2
#define IPC_PING 3

#define IPC_OK          0
#define IPC_NOT_MEGACD  1
#define IPC_BAD_REQUEST 2

#define IPC_MAX_WORDS   64

#endif
