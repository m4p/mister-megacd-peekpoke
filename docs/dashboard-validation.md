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

## Open (needs build host / hardware)

- [ ] Baseline and `MegaCD_Dashboard` Quartus builds, reports, `check_dashboard_reports.py` on real reports
- [ ] ARM builds of Main and the bridge; `--version` on target
- [ ] Hardware smoke test (DASHBOARD-DEPLOYMENT.md §7), stock-behaviour regression, input latency on the pad
- [ ] `load_test.py` 300 s at 100 req/s against hardware; one-hour autopilot session
- [ ] Everything behind gates 2–4
