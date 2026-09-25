# Control milestone design (pause, CPU writes, VRAM)

**Status:** design, 2026-09-25. Scope decided in [dashboard-feasibility.md](dashboard-feasibility.md) §5:
pause/resume, `/bus-poke`, VRAM peek/poke, and the VRAM refresh token. Native savestates are out of
scope. This document records the research that shapes the implementation and the order of work.
Each step ends with a simulation test first, then a Quartus build, then a hardware check.

## 1. Findings from the source

**No existing freeze in this core.** `MegaCD.sv` ties `HDMI_FREEZE` low, `gen.sv` wires the 68K's
`HALTn` to 1, and `MCD.ENABLE` only switches the Game Genie handling. Nothing pauses the console today.

**Genesis_MiSTer has a proven debug pause** (`rtl/system.sv`, input `PAUSE_EN`, F9 in debug builds).
It stops the machine by clearing the periodic clock-enables it generates:
`M68K_CLKENp/n`, `Z80_CLKENp/n`, `PSG_CLKEN`, `FM_CLKEN`. The VDP keeps running, so the picture stays
on screen, unchanged because nothing writes VRAM.

**This core's `gen.sv` generates the same enables** (the `negedge MCLK` block at line ~152) and uses
them for more than the CPUs. That makes one gate at the source a whole-Genesis-side freeze:

| Enable | Consumers in this core |
|---|---|
| `M68K_CLKENp` / `M68K_CLKENn` | FX68K (`enPhi1/enPhi2`), the 68K/Z80 bus arbiter (`gen.sv` ~983–1118), YM2612 (`jt12 .cen`), `reset` sampling, and via `VCLK_CE` the Mega CD ASIC (`EXT_VCLK_CE`) and the cartridge interface |
| `Z80_CLKENp` / `Z80_CLKENn` | T80, PSG (`jt89 .clk_en`) |

Gating these between cycles is safe for the logic they drive. It is the same mechanism the core
already uses every cycle: an enable that stays low for longer.

## 2. What the freeze must cover, and how

| Part | Mechanism | Risk / check |
|---|---|---|
| Main 68K, Z80, bus arbiter, FM, PSG | hold the `gen.sv` clock-enables low (as Genesis_MiSTer does) | Must stop only when no bus cycle is half-done; see §3 |
| VDP | keeps running (display stays up) | An in-flight **DMA** or FIFO drain still finishes and writes VRAM; wait for it (`VBUS_BR_N`/DMA busy) before reporting FROZEN. Interrupt flags latch during the pause and fire once on resume, which is harmless |
| Mega CD sub-CPU, ASIC, CDC, PCM, CDDA | a separate enable in `MCD.vhd`; to be traced (step 3) | The CD side runs its own timing from `clk_sys`; `EXT_VCLK_CE` alone does not stop it |
| CD drive emulation in Main | the plan's media barrier: Main stops advancing `cdd` and stops feeding sectors or CDDA while `FROZEN` | Without it, CD audio runs ahead and the drive position jumps |
| Audio output | mute or hold the last sample while frozen | Avoids a DC click or a buzz |
| Video | nothing needed (VDP keeps scanning out the same VRAM) | `HDMI_FREEZE` is not needed |

## 3. CPU writes need an instruction boundary

For `/bus-poke`, freezing between any two clock-enables is not enough. The 68K could be half-way
through an instruction whose later words are the ones we overwrite, or already have them in its
prefetch queue (IRC/IR). Plan:

1. Request the freeze, then let the 68K run until it starts fetching a new instruction. FX68K
   exposes its microcode state; the boundary is the point where it decodes the next opcode. That
   signal gets identified and brought out in step 2.
2. Hold there, and do the work-RAM write through the existing SDRAM port-2 path (already built and
   hardware-tested for reads).
3. Resume with the prefetch refilled from memory, so the patched bytes are what executes: either
   invalidate the queue, or resume through a path that refetches. A focused testbench patches an
   instruction that is already in the prefetch queue and checks which version runs.

**Implemented (step 2):** no FX68K change was needed. `gen.sv` exposes the address of the 68K's
last instruction fetch (`M68K_PROG_A`, from the function codes). `dashboard_debug` freezes the
machine, checks that this address is more than 16 bytes from the write range, and if not, lets the
CPU run about 10 µs and checks again. The write itself happens while frozen. Code the 68K has not
fetched yet is read fresh after resume, so neither a stale prefetch nor a half-old, half-new
instruction can occur. See `docs/dashboard-protocol.md` §8 and `tests/dashboard/tb_debug_write.sv`.

**Shortcut to measure first:** the dashboard's patches only touch 68K work RAM. If the Z80 and DMA
never write there while driving, the 68K alone needs to be at a boundary; the whole-machine freeze
then only matters for the user's pause.

## 4. VRAM access

The staging, protocol, and bridge side exist (`VRAM_READ`/`VRAM_WRITE` feature bits, the GPGX byte
swap, 1056-byte uploads, the refresh token). The missing part is a port into the four VRAM
`dpram`s in `gen.sv`. Both ports are in use, so the access is muxed onto the VDP's 16-bit port while
the machine is frozen and the VDP is not in a VRAM access (`vram_req == vram_ack`). Word ordering:
byte address bit 1 selects `vram_*1` or `vram_*2`, and bits 15:2 give the row.

## 5. Order of work

| Step | Deliverable | Verified by |
|---|---|---|
| 1 | `PAUSE_EN` input in `gen.sv` gating the clock-enable generator (Genesis_MiSTer pattern), driven by `dashboard_debug.pause_req`; `frozen` reported only when the 68K is between bus cycles and no VDP DMA is active | Testbench on `gen.sv` enables; Quartus build; on hardware `/pause` freezes the game, and the picture holds |
| 2 | 68K instruction-boundary stop and prefetch handling; `WORKRAM_WRITE` feature on | FX68K testbench that patches prefetched code; the dashboard patch buttons on hardware |
| 3 | Mega CD side freeze (sub-CPU, ASIC timers, CDC, PCM, CDDA) and the Main media barrier | Pause during CD audio and during loading, then resume without a jump |
| 4 | VRAM mux on the VDP port; `VRAM_READ/WRITE` on; refresh token | Air-freshener patch on hardware |
| 5 | Conformance with `--destructive` (except savestates), the hour-long autopilot run, and the deferred reconnect and autostart checks after reboot | Hardware |

Each step raises only feature bits it has proven. The bridge and dashboard already gate every
button on those bits, so partial progress is always safe to deploy.
