# Native savestate state inventory

**Status:** initial inventory, 2026-09-25. Counts come from a source scan of
`MegaCD_MiSTer` @ `a3a3da81`. No save or restore mechanism has been prototyped yet, so the
**Mechanism** column is empty on purpose. Plan Task 2 fills it with synthesis and fit evidence.
Until every row has a mechanism or a proved reconstruction rule, `/state/save` and
`/state/load` stay explicitly unsupported, and the bridge returns `feature_unavailable`.

“Seq” counts clocked processes (`always @(posedge|negedge …)`, `rising_edge`/`falling_edge`) in
the file. It is a lower bound on the state-holding logic that must be audited, not a register
count. Obtain register totals from the Quartus “Resource Utilization by Entity” report of the
baseline build.

## 1. Memories (must be streamed, never turned into registers)

| Memory | Instance | Size | Current ports and consumers | Notes |
|---|---|---|---|---|
| VRAM | `gen.sv` `vram_l1/u1/l2/u2` (`dpram #(14)`) | 4 × 16 KiB | A: VDP 16-bit port (`vram_req/ack`); B: VDP 32-bit fetch (`vram32_req/ack`) and reset loader | Both ports in use. Access needs arbitration while frozen (protocol §7, `VRAM_READ`). |
| CRAM, VSRAM | `vdp.vhd` (internal arrays, `CRAM_*`, `VSRAM_*` signals) | 64 × 9 bit, 40 × 11 bit | VDP only | Small; may fit a register scan. Check inference first. |
| Z80 RAM | `gen.sv:1034` `ramZ80` (`dpram #(13)`) | 8 KiB | Z80, 68K via bus arbiter | |
| Work RAM (68K) | SDRAM `0x800000–0x80FFFF` | 64 KiB | SDRAM port 1 | Already reachable (port 2, this build). |
| PRG-RAM (sub 68K) | SDRAM `0x1000000…` / `0x0F80000…` (port 0) | 512 KiB | MCD sub-CPU | Port 0 has no free requester slot. Stream through port 2 with a bank-aware address. |
| Word RAM | `MCD.vhd:315/325` `WORDRAM0/1` (`spram`) | 2 × 128 KiB | MCD ASIC, both CPUs | Ownership and 1M/2M mode registers are part of the state. |
| CDC buffer | `MCD.vhd:367` `CDC_RAM` (`dpram_dif`) | 16 KiB | CDC, HPS sector download | |
| PCM RAM | `MCD.vhd:406` `PCM_RAM` (`dpram`) | 64 KiB | PCM chip | |
| Backup RAM | `MegaCD.sv:852` `bram` (`dpram_dif`) | 8 KiB | MCD, HPS save path | Already persisted by the existing save mechanism. Keep it separate from native states. |
| Cart RAM | SDRAM `0xE00000…` | ≤ 1 MiB | Cart, `tmpram` engine | Only if a RAM cart is active. |
| CDDA FIFO | `MCD/CDDA_FIFO.v` | small | CDDA playback | Async reset on `negedge nRESET`. |
| FM operator storage | `jt12_sh*.v` shift registers | per-channel | YM2612 pipeline | Shift-register RAMs. A central mux would destroy their RAM inference (plan §5). |

## 2. Sequential logic by subsystem

| Subsystem | Files (Seq) | State that must be captured | Mechanism |
|---|---|---|---|
| Main 68K | `FX68K/fx68k.sv` (34), `fx68kAlu.sv` (3) | registers, SR, PC, prefetch (IRC/IR/IRD), microcode address, bus FSM, phase enables | — |
| Sub 68K | `MCD/MC68K.vhd` wrapper + FX68K | as above | — |
| Z80 | `GEN/T80/T80.vhd` (5), `T80s.vhd` (1) | both register banks, PC/SP, IFF/IM, `ramZ80` | — |
| VDP | `GEN/vdp.vhd` (23) | registers, DMA, FIFO, read buffer, H/V counters, field, sprite/line pipelines, CRAM/VSRAM, VRAM | — |
| Genesis glue | `GEN/gen.sv` (12, **1 negedge at line 152**), `gen_io.sv` (4), `CART.vhd` (2), `multitap.sv`, `teamplayer.sv`, `fourway.v`, `lightgun.sv` | bus arbitration, Z80 bus/reset, TMSS, pad handshake/TH counters, mapper | — |
| YM2612 | `GEN/jt12/*` (about 25 files, 1–6 each; ADPCM compiled out, `use_adpcm=0`) | registers, operator/envelope/phase shift storage, timers, LFO, DAC/interpolator | — |
| PSG | `GEN/jt89/*` (8) | tone/noise counters, volumes | — |
| Audio filters | `GEN/audio_iir_filter.v` (2), `genesis_lpf.v`, `rtl/audio_fix.sv` | filter state | May be reconstructible (reset to silence), to be proved. |
| MCD ASIC / gate array | `MCD/ASIC.vhd` (17), `MCD.vhd` | memory mode, comm registers, IRQ masks, timers, stopwatch, graphics (rotation/scaling) engine, DMA | — |
| CDC (LC8951) | `MCD/CDC.vhd` (5) | registers, header/status, buffer pointers, DMA | — |
| CDD interface | `MegaCD.sv` `scd_cdd_*`, `hps_ext.v` `cd_out` | pending command/status, toggles | Paired with Linux CDD state (§3). |
| PCM | `MCD/PCM.vhd` (3) | channel registers, address accumulators | — |
| CDDA | `MCD/CDDA.vhd` (3), `CDDA_FIFO.v` (2) | sample position, FIFO contents and pointers | — |
| Top level | `MegaCD.sv` | clock-enable phases (`CEGen.vhd`), region, request toggles, `tmpram`, cheats | Configuration items come from the OSD and are validated, not restored. |

## 3. Linux (Main_MiSTer) state

`support/megacd/megacd.h` `class cdd_t`, @ `aa271e41`, holds this logical state:

| Field | Serialize as |
|---|---|
| `status`, `isData`, `latency`, `loaded` | values |
| `lba`, `index`, `scanOffset`, `audioOffset`, `audioLength`, `sectorSize` | values |
| `stat[10]`, `comm[10]` | bytes |
| `toc` | not serialized: rebuilt from the validated image, with its hash stored |
| `chd_hunknum`, `chd_hunkbuf`, `chd_audio_read_lba` | not serialized: rebuilt from the logical position |
| `SendData`, `CanSendData` | never serialized (function pointers) |
| `megacd.cpp` statics `has_command`, `need_reset`, the `mcd_poll` timer phase | values; timer re-armed relative to resume |

## 4. Next steps (plan Task 2)

1. Take register totals per entity from the baseline fit report and attach them to §2.
2. Prototype the representative spike: FX68K registers with prefetch, one VDP register block
   plus CRAM, and one `jt12_sh` shift register. Record ALM/M10K growth and Fmax for each.
3. Choose between a scan chain and a narrow shared bus from those numbers, then extrapolate
   using the per-entity totals.
4. Record the decision and budget in `dashboard-feasibility.md`. Stop if the extrapolated
   total exceeds the headroom the baseline leaves.
