# Dashboard feasibility and gate status

**Date:** 2026-09-25. This file is the single place that states what the MegaCD dashboard
integration can and cannot do today, and why.

## 1. Gate status

| Gate (plan §7) | Status | Evidence / blocker |
|---|---|---|
| 1. Baseline | **Passed** (2026-09-25) | Baseline and `MegaCD_Dashboard` built in the Ubuntu 16.04 VM (Quartus 17.0.0 Build 595); `check_dashboard_reports.py` passes (fit, budget, timing in all four corners). See [dashboard-baseline.md](dashboard-baseline.md) and [dashboard-validation.md](dashboard-validation.md). |
| 2. Feasibility (freeze, prefetch re-fetch, state restore) | **Not run** | Needs gate 1 numbers and Quartus spikes; inventory started in [dashboard-state-inventory.md](dashboard-state-inventory.md). |
| 3. Control | **Passed on hardware** (2026-09-26) | Steps 1–4 fitted with timing met; pause, CPU patches, VRAM and every non-savestate conformance item pass on the MiSTer. See [dashboard-validation.md](dashboard-validation.md). |
| 4. Full state | **Deferred** | Savestates are nice-to-have (§5). |

**Consequence:** the package is at the **transport milestone, validated on hardware on
2026-09-25** (telemetry, input, conformance read-only items, and 100 req/s load over Wi-Fi with
no glitches reported). It is **not** the control
milestone, and it is **not conformant** with `DASHBOARD-API.md`. The bridge refuses each missing
feature explicitly with HTTP 501 and `feature_unavailable`. Nothing is emulated.

## 2. What exists

| Area | Delivered | Verified by |
|---|---|---|
| HPS protocol v1 (`0x70`) | `docs/dashboard-protocol.md`, `rtl/dashboard_debug.sv`, explicit decode in `rtl/hps_ext.v` | `tests/dashboard/tb_debug_transport.sv` (Icarus, 5 seeds); Verilator lint |
| Work RAM reads (word-coherent) | `rtl/dashboard_sdram_port.sv` on SDRAM port 2, `tmpram` and ROM-download arbitration in `MegaCD.sv` | testbench with a port-2 model (edge accept, delayed busy, shared-dout clobber) |
| Player-1 input injection | frame-boundary apply, same-frame tap latch, owner lease, `SESSION 0` release | testbench + end-to-end |
| Main IPC adapter | `Main_MiSTer/support/megacd/dashboard_ipc.{h,cpp}`, hooks in `user_io.cpp` | compiled into the end-to-end harness; **not cross-compiled for ARM** |
| HTTP bridge | `linux/dashboard-bridge/` (C, no dependencies) | 145 unit + 34 HTTP checks (mock core); 60 end-to-end checks through Verilated RTL; **not cross-compiled for ARM** |
| Remote dashboard | server selection, session pinning, capability gating in `Genesis-Plus-GX/sdl/dashboard.html` | browser test against mock bridges and a GPGX-like legacy server |
| Report tooling | `scripts/check_dashboard_reports.py`, `scripts/report_dashboard_timing.tcl` | checker: 8 tests on **synthetic** reports; Tcl: not run |

## 3. Resource and timing record

| Configuration | ALMs | Registers | M10K | DSP | Worst setup / hold | Result |
|---|---|---|---|---|---|---|
| Unmodified baseline | 25,152 / 41,910 (60 %) | 33,332 | 535 / 553 (97 %) | 49 / 112 | +0.320 ns (HDMI PLL) / +0.157 ns (JTAG) | fit OK, timing met, hardware OK |
| Transport/input (`MegaCD_Dashboard` revision, this code) | 25,737 (+585) | 33,918 (+586) | 537 (+2) | 49 (+0) | +0.293 / +0.071 ns (worst of 4 corners) | fit OK, timing met, budget OK, **hardware OK** |
| Control milestone (steps 1–4) | 25,887 (+735) | 33,900 (+568) | 537 (+2) | 49 (+0) | +0.225 / +0.041 ns (worst of 4 corners) | fit OK, timing met, budget OK, **hardware OK** |
| Full native state | — | — | — | — | — | not implemented |

Expected footprint of the transport build, **an estimate to replace with the fit report**:

- 2 KiB staging as two 1024×8 true dual-port RAMs, which should infer to about 2 M10K.
- A 26-bit lease counter.
- Roughly 300 flip-flops of protocol state.
- The engine and port-adapter FSMs.

It adds no DSPs or PLLs. The `MEGACD_DASHBOARD` macro removes all of it from the stock
revision: in that configuration the `hps_ext` claim and the injected pad inputs are tied to
0, and `tmpram_busy` equals the raw `busy2`.

## 4. Design decisions taken in this increment

- **Command `0x70`**, audited free in Main, `hps_io.sv`, and the core. `hps_ext.v` now matches
  `0x34`, `0x35` and `0x70` explicitly instead of a min/max range.
- **Main is a thin packet pump.** Protocol logic lives in the bridge, where it is unit-tested.
  Main adds only framing, core identity, the core epoch, and `SESSION 0` on bridge loss.
  At most 8 packets per `user_io_poll` iteration, and it never blocks.
- **Reads without freeze are word-coherent only.** Each 16-bit SDRAM word is atomic. Values
  spanning words (for example the 32-bit distance at `$FF6FDC`) can tear if the 68K updates
  them between the two word reads. `/capabilities` reports `"read_consistency": "word"`. The
  dashboard's readouts tolerate a one-poll glitch, but conformance requires coherent reads,
  which need `FREEZE`.
- **No writes without freeze.** The plan forbids executing half-written code. Writing through
  port 2 while the 68K runs gives no instruction-boundary guarantee and no prefetch
  invalidation, so `WORKRAM_WRITE` stays off until gate 2 proves the FX68K hook.
- **Input** applies at the vblank rising edge, or immediately when frozen. A press and release
  inside one frame still produce a one-frame hold. Holds persist until release, `SESSION 0`,
  lease expiry (2^26 `clk_sys` ≈ 1.25 s without packets), or reset.
- **The owner lease clears `PAUSE_REQ`** as well as input, so a crashed control path cannot
  leave the console frozen. Revisit this when freeze exists: plan §4.1 wants a deliberate user
  pause kept apart from a failed transaction.

## 5. Scope decision (2026-09-25, project owner)

After the transport milestone is validated on hardware, the **next milestone is the control
milestone**: pause/resume, CPU-bus patches, VRAM peek/poke, and the VRAM refresh token, which
is every dashboard feature except Save State and Load Fullauto. **Native savestates are
nice-to-have.** They are deferred behind the control milestone and are no longer a
completion requirement.

This changes plan §1 and §12, which made full native state part of done. Gate 2 now covers
only freeze and prefetch correctness; the state-restore spike moves to the optional
savestate workstream. Until that workstream lands, `/state/save` and `/state/load` keep
answering `feature_unavailable` (the refresh token excepted), and the dashboard keeps those
buttons greyed out.

## 6. Remaining work, in order

1. **Gate 1** (build host): baseline build and reports; build the `MegaCD_Dashboard` revision;
   run `scripts/check_dashboard_reports.py`; confirm the staging RAM inferred as M10K.
2. **Hardware smoke test** of this increment: stock behaviour unchanged (CD audio, saves, second
   title), `/bus-peek` values plausible while driving, input latency measured on the pad
   (DASHBOARD-DEPLOYMENT.md §7).
3. **Gate 2 (control):**
   - an FX68K instruction-boundary stop with prefetch re-fetch, with a testbench that patches an
     already-prefetched extension word;
   - a VDP/DMA quiesce acknowledgement;
   - a measurement of whether a 68K-only stop is enough for work-RAM writes (no other writer
     during driving) or a whole-machine freeze is required.

   Record the area and timing of each.
4. **Control milestone:** `FREEZE` (user pause and transaction freeze, video freezer, audio mute),
   a CD media barrier in `megacd.cpp`, `WORKRAM_WRITE` with prefetch handling, VRAM
   read/write ports, and the refresh token; then conformance against all non-savestate items.
5. **Optional, later:** the native savestate workstream (the state-restore spike, then plan
   Tasks 8–9). If its spike shows it does not fit, drop it without affecting the control
   milestone.
