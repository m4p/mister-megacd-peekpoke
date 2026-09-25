# Dashboard validation log

Evidence for the current increment (transport milestone). Hardware and Quartus rows stay
open until a build host and a MiSTer produce them. [dashboard-feasibility.md](dashboard-feasibility.md)
has the gate status.

## 2026-09-25: development workspace (macOS arm64, no Quartus, no MiSTer)

Tools: Icarus Verilog 13.0, Verilator 5.046, Apple clang, Python 3, Node, Chromium (in-app browser).

| Check | Command | Result |
|---|---|---|
| RTL protocol testbench | `make -C tests/dashboard sim` | PASS on seeds 1–5 (≈1372 SDRAM reads each) |
| New RTL lint | `make -C tests/dashboard lint` | clean |
| `MegaCD.sv` parse, macro on and off | Icarus parse of the edited files | only error is at upstream line 447 (unpacked-array initializer Icarus can't parse); nothing in the new blocks. **Not a synthesis check.** |
| Bridge unit tests (mock core, both feature sets) | `make -C linux/dashboard-bridge test` | 145 checks PASS |
| Bridge HTTP tests | same | 34 checks PASS (CORS, preflight, keep-alive, pipelining, 413/411/400, aborted sockets, 6-client concurrency, single instance, SIGTERM) |
| End-to-end: HTTP → bridge → Main `dashboard_ipc.cpp` → Verilated RTL + SDRAM port model | `make -C tests/dashboard/e2e e2e` | 60 checks PASS, including bridge SIGKILL / SIGSTOP input release, Main restart, non-MegaCD core, stock RBF |
| Conformance runner through the RTL | inside e2e | read-only items PASS; VRAM UNSUPPORTED |
| Conformance, mock `full` feature set, destructive | `conformance.py --destructive` | 11 PASS; savestate UNSUPPORTED; Fullauto FAIL (no native file), as expected |
| Load, mock `hw` | `load_test.py --rate 100 --concurrency 6 --seconds 10` | 100.0 req/s, 0 errors, read p95 1.35 ms (mock; not a hardware number) |
| Fixtures from the original vs updated dashboard | `extract_fixtures.py` | identical: 9 routes, 9 telemetry reads, 14 sites, 9 patches |
| Report checker | `python3 scripts/test_check_dashboard_reports.py` | 8 tests OK (**synthetic** reports) |
| Dashboard in a browser | mock `hw`, mock `full`, a GPGX-like legacy server | connect via `?api=` (not saved) and via form (saved); feature gating; two-site patch wrapped in pause/resume; release sent to the old server on switch, none to the new; autopilot START tap release pinned; unreachable server and bad URL errors; reload reconnects |
| `start.sh` | start/start/status/stop/status on macOS `sh` | behaves; **busybox not tested** |

## 2026-09-25: build host (Ubuntu 16.04 VM, Quartus 17.0.0) and MiSTer hardware

| Check | Result |
|---|---|
| ARM toolchain | `scripts/setup_arm_toolchain.sh` into `~/toolchains`, no sudo; GCC 10.2.1 runs on 16.04 |
| Main (release 20260912 + patch) ARM build | 0 warnings; SHA-256 `359ec1a5…d93f` |
| Bridge ARM build | static, 0 warnings; deployed build SHA-256 `693456cd…b281` |
| Baseline Quartus build | fit OK, timing met; 25,152 ALMs, 535/553 M10K (see dashboard-baseline.md) |
| First `MegaCD_Dashboard` build | **failed review**: staging RAM not inferred ("unsupported read-during-write"), built as 16,834 registers; stopped. Fixed in `545b9d6` |
| `MegaCD_Dashboard` build | fit OK; +585 ALMs, +2 M10K, +16,384 block-memory bits, 0 DSP/PLL; `check_dashboard_reports.py` PASS; `report_dashboard_timing.tcl` worst setup +0.293 ns, hold +0.071 ns over 4 operating conditions; RBF SHA-256 `4bc302d8…537b` |
| Baseline RBF on hardware | boots Desert Bus; audio, controller, backup-RAM persistence OK (owner) |
| Patched Main on hardware | runs; creates `/tmp/megacd-dashboard.sock` (0600); OSD and core loading normal |
| `/capabilities` over LAN | core present, protocol 1, build `01260925`, features 0x83 |
| Live telemetry while driving | speed `6000` (45 mph), state 3, steering drift, clock, odometer 109.0→113.0 mi, distance counter consistent with 45 mph |
| Input injection | 0.3 s `left` tap: steering −10752; `right`: +7936; press/release round trip 15–21 ms |
| `conformance.py` (read-only items) | 6 PASS, 0 FAIL, VRAM UNSUPPORTED; all patch badges resolve to stock bytes (`steering=r1`, others `off`), confirming addresses and byte order |
| `load_test.py` 60 s, 100 req/s, 6 clients, **Wi-Fi** | 100.0 req/s, 0 errors; read p50 7.1 / p95 10.0 / p99 13.7 ms; release p95 9.7 ms |
| Gameplay during load | no video, audio, or input glitches (owner) |
| `start.sh` on busybox | start / status / stop work; PID file correct |
| Reconnect after core load | **bug found on hardware**: first request after Main's restart failed ("IPC not connected"). Fixed (stale-connection check + one retry on session loss); new e2e scenario fails on the old bridge and passes on the new one |

## Open (needs build host / hardware)

- [ ] Reconnect fix verified on hardware (reload the core while the bridge runs)
- [ ] Input latency measured on the pad (HTTP round trip is measured; frame placement is not)
- [ ] `load_test.py` 300 s at 100 req/s; one-hour autopilot session with the browser dashboard
- [ ] Wired-Ethernet latency comparison (optional)
- [ ] Everything behind gates 2–4
