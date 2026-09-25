# MegaCD dashboard protocol (version 1)

This document is the wire ABI shared by three components:

| Layer | Implementation |
|---|---|
| FPGA | `rtl/dashboard_debug.sv`, reached through `rtl/hps_ext.v` |
| Main_MiSTer | `support/megacd/dashboard_ipc.cpp` (owns all HPS I/O) |
| Linux bridge | `linux/dashboard-bridge/` (HTTP/JSON ↔ this protocol) |

Constants are duplicated in `rtl/dashboard_debug.sv`, `linux/dashboard-bridge/src/protocol.h`
and `Main_MiSTer/support/megacd/dashboard_ipc.h`. Change all three together and bump
`PROTO_VERSION`.

## 1. HPS command allocation

| Value | Owner | Notes |
|---|---|---|
| `0x34` `UIO_CD_GET` | stock MegaCD | unchanged |
| `0x35` `UIO_CD_SET` | stock MegaCD | unchanged |
| **`0x70` `UIO_MCD_DASH`** | this protocol | unused by `Main_MiSTer` @ `aa271e41`, `sys/hps_io.sv`, and the core (audited 2026-09-25) |

`hps_ext.v` recognizes `0x34`, `0x35` and `0x70` explicitly. It never claims the bus for
any other command. On a stock RBF, `0x70` is not claimed, so `hps_io` answers and the
magic below is absent. That is how Main and the bridge detect an unmodified core.

## 2. SPI packet framing

One packet is one `EnableIO … DisableIO` transaction on the user-IO channel. All values are
16-bit words. For every word Main clocks out, the FPGA latches the word at the strobe and
returns a response word computed **at that same strobe**. `r[k]` is the response to `w[k]`.

| Index | Main sends `w[k]` | FPGA returns `r[k]` |
|---|---|---|
| 0 | `0x0070` | `0xDB26` (`MAGIC_ACK`) when the dashboard core is present |
| 1 | header `{subcmd[15:8], gen[7:0]}` | `FLAGS` (§4) |
| 2… | arguments, then zero padding for reads | per subcommand (§5) |

A packet shorter than its subcommand's header has **no side effect**. Mutating actions take
effect only on the strobe of the last header word. Byte data is packed big-endian:
word `k` of a buffer = `{byte[2k], byte[2k+1]}`. Odd lengths pad the final low byte with `0`.

`gen` is the 8-bit session generation that Main assigns through `SESSION`. The FPGA rejects
every subcommand except `PROBE` and `SESSION` when `gen` differs from the current session.
Such a packet gets `ERR_SESSION` and changes nothing. The FPGA resets its session to `0`
on configuration, so no request queued for a previous core can hit a newly loaded one.

## 3. Result codes

| Code | Name | Meaning |
|---|---|---|
| 0 | `OK` | accepted / completed |
| 1 | `ERR_SESSION` | wrong session generation |
| 2 | `ERR_BUSY` | an operation is running or a result is still held |
| 3 | `ERR_TXN` | transaction ID does not match |
| 4 | `ERR_RANGE` | length, offset, or address outside limits |
| 5 | `ERR_UNSUPPORTED` | subcommand/space/op not built into this RBF |
| 6 | `ERR_INCOMPLETE` | COMMIT before the whole payload was staged |
| 7 | `ERR_STATE` | subcommand not valid in the current state |
| 8 | `DUP_OK` | duplicate COMMIT of an already committed transaction; not repeated |
| 9 | `ERR_ABORTED` | ROM download/reset aborted the operation before it mutated memory |
| 10 | `ERR_FAULT` | hardware failure after mutation began; machine stays frozen |

## 4. FLAGS word (`r[1]` of every packet)

| Bit | Name | Meaning |
|---|---|---|
| 0 | `RUNNING` | an operation is executing |
| 1 | `DONE` | a result is held until `ACK` |
| 2 | `STAGING` | `BEGIN` accepted, upload in progress |
| 3 | `PAUSE_REQ` | user pause requested |
| 4 | `FROZEN` | console logic is actually frozen |
| 5 | `FAULT` | sticky fault; see `ERR_FAULT` |
| 6 | `OWNER` | owner lease alive (§6) |
| 7 | `INPUT_ACTIVE` | injected input mask is non-zero |
| 15:8 | `GEN` | current session generation |

## 5. Subcommands

Word indices are packet indices (`w[0]` is the command). “Header” means the argument words
that must all arrive before anything happens.

### `0x01 PROBE`: no session check, no side effect

Main sends `w[2..10] = 0`. Responses:

| `r[k]` | Value |
|---|---|
| 2 | `0x4442` (“DB”) |
| 3 | `0x4D43` (“MC”) |
| 4 | `PROTO_VERSION` = `1` |
| 5 | feature bits (§7) |
| 6 | staging capacity in bytes (`2048`) |
| 7 | maximum data words per UPLOAD/FETCH packet (`32`) |
| 8 | build ID low 16 bits |
| 9 | build ID high 16 bits |
| 10 | current generation (low 8 bits) |

### `0x0F SESSION`: `w[2] = new generation`

No session check. It discards staging and any held result (a running operation completes;
its result is then discarded). It clears injected input, clears `PAUSE_REQ`, and starts the
owner lease. `r[2]` echoes the new generation. Main issues this on every bridge attach and
detach. Generation `0` is reserved for “no owner”.

### `0x02 STATUS`

| `r[k]` | Value |
|---|---|
| 2 | current transaction ID |
| 3 | result code of the held result (valid when `DONE`) |
| 4 | result length in bytes |
| 5 | currently applied injected input mask |
| 6 | frame counter (low 16 bits, incremented at each vblank start) |

### `0x03 BEGIN`: header `w[2..6]`

| Word | Field |
|---|---|
| 2 | transaction ID (non-zero) |
| 3 | `{op[15:8], space[7:0]}` |
| 4 | address bits 23:16 (must be 0 for version 1) |
| 5 | address bits 15:0 (byte offset inside `space`) |
| 6 | length in bytes, `1…STAGING_BYTES` |

`r[6]` is the result: `OK`, `ERR_BUSY`, `ERR_RANGE` (`address + length > space size`, or bad
length), or `ERR_UNSUPPORTED` (op/space not built in). On `OK` the state becomes `STAGING`
and the upload pointer is reset to 0.

| op | Name | Upload needed |
|---|---|---|
| `0x01` | `READ` | no |
| `0x02` | `WRITE` | yes, `ceil(length/2)` words |
| `0x7F` | `LOOPBACK` | yes; the result echoes the staged bytes |

| space | Name | Size | Byte addressing |
|---|---|---|---|
| `0x01` | `WORKRAM` | 64 KiB | offset from Main-CPU `$FF0000`; CPU big-endian bytes |
| `0x02` | `VRAM` | 64 KiB | canonical VDP byte addresses (no GPGX swap) |

### `0x04 UPLOAD`: header `w[2..4]`, then data

`w[2]` = transaction ID, `w[3]` = word offset, `w[4]` = count (`1…32`), then `count` data
words. The header is accepted only in `STAGING` for the same transaction, with
`offset == upload pointer` and `offset + count ≤ ceil(length/2)`. `r[4]` is the result. When the
header is rejected, the data words that follow are ignored. Each accepted data word is
stored in staging and advances the upload pointer. Staging is never console memory, so a
truncated or interrupted upload cannot mutate the machine. It only makes the next UPLOAD
offset or the COMMIT fail.

### `0x05 COMMIT`: `w[2]` = transaction ID

| Condition | `r[2]` | Effect |
|---|---|---|
| `STAGING`, same ID, payload complete (or `READ`) | `OK` | state → `RUNNING` |
| `STAGING`, same ID, payload short | `ERR_INCOMPLETE` | none |
| `RUNNING`/`DONE`, same ID | `DUP_OK` | none (never repeats a mutation) |
| otherwise | `ERR_TXN` / `ERR_STATE` | none |

Once an operation is `RUNNING`, it completes autonomously without further packets. On
completion the state becomes `DONE` with a result code. `WRITE` is refused with
`ERR_UNSUPPORTED` at `BEGIN` unless the RBF advertises the matching write feature, which
requires `FREEZE`.

### `0x06 FETCH`: header `w[2..4]`, then read slots

`w[2]` = transaction ID, `w[3]` = word offset, `w[4]` = count (`1…32`), then `count` zero words.
Valid only in `DONE` with the same ID and `offset + count ≤ ceil(result_len/2)`. `r[4]` is the
result. `r[5…]` are the result words, or `0` when rejected.

### `0x07 ACK`: `w[2]` = transaction ID

`DONE` + same ID → `IDLE`, `r[2] = OK`. Otherwise `ERR_TXN`/`ERR_STATE`. Idempotent from the
bridge's view: an `ERR_STATE` reply to a repeated ACK means the result is already released.

### `0x08 CANCEL`: `w[2]` = transaction ID

`STAGING` or `DONE` with the same ID → `IDLE`, `OK`. `RUNNING` → `ERR_BUSY` (an operation that
started is never abandoned half-way).

### `0x09 INPUT`: `w[2]` = press mask, `w[3]` = release mask

At `w[3]`: `target = (target | press) & ~release`, and `press` bits are also latched until the next
frame boundary. At each vblank start, or immediately while `FROZEN`,
`applied = target | latched_press`, and the latch clears. A press and release that fall in the same
frame therefore still produce a one-frame hold, and the pair is never reordered. `r[3]` returns
the new `target`. Masks use the core's active-high `JOY` layout:

| Bit | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Button | right | left | down | up | A | B | C | start | mode | X | Y | Z |

The applied mask is ORed into **logical player 1**, after the core's player-swap selection.

### `0x0A HEARTBEAT`

It has no arguments and exists only to refresh the owner lease (§6).

### `0x0B PAUSE` / `0x0C RESUME`

`r[2]` = `OK`, or `ERR_UNSUPPORTED` when the RBF lacks `FREEZE`. Both are idempotent. `FLAGS.FROZEN`
reports the actual state and may lag `PAUSE_REQ` until the freeze controller reaches a safe
boundary.

## 6. Owner lease

Every packet with the correct generation refreshes a lease of 2^26 `clk_sys` cycles
(≈1.25 s at 53.69 MHz). When it expires, the FPGA clears injected input, discards
staging (not a running operation), and clears `FLAGS.OWNER`. The lease survives HTTP connection
churn because the bridge, not the browser, keeps it alive. If Main dies, the lease expires.
If the bridge dies, Main issues `SESSION 0`, which releases input immediately.

## 7. Feature bits (`PROBE r[5]`)

| Bit | Feature | This build |
|---|---|---|
| 0 | `INPUT` | yes |
| 1 | `WORKRAM_READ` | yes (word-coherent only; §8) |
| 2 | `WORKRAM_WRITE` | **no**: requires `FREEZE` and the FX68K prefetch hook |
| 3 | `VRAM_READ` | **no**: requires `FREEZE` to arbitrate the VDP port |
| 4 | `VRAM_WRITE` | **no** |
| 5 | `FREEZE` (pause/resume) | **no**: blocked on the feasibility gate |
| 6 | `STATE` (native savestates) | **no** |
| 7 | `LOOPBACK` | yes |
| 8 | `READ_COHERENT` | **no**: set only when reads execute under freeze |

The bridge derives its public `/capabilities` from these bits. It never emulates a missing
feature.

## 8. Work RAM access path

For `WORKRAM` offset `A`, the SDRAM word address is `{9'b010000000, A[15:1]}` (byte
`0x800000 + A`), matching `sdram.addr1` for `GEN_RAM_CE_N`. The even CPU byte is `dout[15:8]`
and the odd byte is `dout[7:0]`. Writes use `wrh` for the even byte and `wrl` for the odd byte.
Access goes through SDRAM port 2 via `rtl/dashboard_sdram_port.sv`:

- A request is issued only while no ROM download is active and the backup-RAM (`tmpram`)
  engine is idle. The `tmpram` engine waits for the dashboard port to finish its in-flight word.
- The request is held until `busy2` rises and is then lowered. Data is sampled on the
  `clk_sys` cycle where the delayed busy is high and the current busy is low. This is the same
  proven pattern as the existing `tmpram` engine.
- Port 1 (the Genesis CPU) keeps priority inside `sdram.sv`, so the game is not starved.

Without `FREEZE`, a read of several words can observe a CPU update between words. Each
16-bit word is atomic, but multi-word values are not. `/capabilities` reports this as
`"read_consistency": "word"`.

## 9. Bridge ↔ Main IPC (Unix stream socket)

Default path `/tmp/megacd-dashboard.sock`, mode `0600`, owned by root. One client at a time.
All integers are little-endian.

Request: `u16 type, u16 id, u16 n_out, u16 n_in, then u16 words[n_out]`

Response: `u16 type, u16 id, u16 status, u16 n_words, u32 core_epoch, then u16 words[n_words]`

| type | Name | Request words | Response words |
|---|---|---|---|
| 1 | `XFER` | packet words starting at the header `w[1]` (Main prepends `0x0070`) | `r[0…n_out+n_in]` (`1 + n_out + n_in` words) |
| 2 | `INFO` | none | `[is_megacd, core_epoch_lo, core_epoch_hi]` |
| 3 | `PING` | none | none |

| status | Meaning |
|---|---|
| 0 | OK |
| 1 | `NOT_MEGACD`: the loaded core is not MegaCD; nothing was sent to the FPGA |
| 2 | `BAD_REQUEST`: malformed frame; nothing was sent |

`core_epoch` increments whenever Main (re)initializes a core. The bridge treats any change
as a lost session: it re-probes, issues a new `SESSION`, and fails outstanding requests
with `core_unavailable`.

Main services at most `DASH_MAX_XFER_PER_POLL` (8) packets per `user_io_poll` iteration and
never blocks on the socket. On client disconnect, Main issues `SESSION 0` if a MegaCD core is
loaded.
