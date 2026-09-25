# Building the MegaCD dashboard integration

This guide covers the three artifacts that must be deployed **together**:

| Artifact | Source | Built on |
|---|---|---|
| `MegaCD_Dashboard.rbf` | this repository, revision `MegaCD_Dashboard` | Linux/Windows x86-64 with Quartus 17.0.2 |
| `MiSTer` (Main) | `Main_MiSTer` release 20260912 (`47221c1`) + `patches/main_mister/` | x86-64 with `arm-none-linux-gnueabihf` GCC 10.2 |
| `megacd-dashboard` (bridge) | `linux/dashboard-bridge/` | same ARM toolchain (static binary) |

All three share protocol version 1 ([dashboard-protocol.md](dashboard-protocol.md)). The bridge
refuses to talk to an RBF that reports another version.

> **Status (2026-09-25):** the simulation and host-test sections below have been run. The
> Quartus and ARM sections have **not** been run yet; they are the commands to use. Record the
> results in [dashboard-feasibility.md](dashboard-feasibility.md) §3.

The workspace layout assumed below:

```text
<root>/MegaCD_MiSTer     this repository
<root>/Main_MiSTer       Main release 20260912 (47221c1) + dashboard patch
<root>/Genesis-Plus-GX   dashboard.html
```

## 1. Host tests (any Unix, no hardware)

Tools: `make`, a C compiler, Python ≥ 3.7, `iverilog` ≥ 12, `verilator` ≥ 5, and `node`
(fixture extraction only). Ubuntu 16.04 ships Python 3.5, which is too old for these test
tools. `scripts/check_dashboard_reports.py` and its self-test are kept compatible with 3.5,
because they run on the Quartus build host.

```bash
cd MegaCD_MiSTer
make -C tests/dashboard sim                  # RTL protocol testbench, 5 random seeds
make -C tests/dashboard lint                 # Verilator lint of the new RTL
make -C linux/dashboard-bridge test          # bridge unit + HTTP tests against the mock core
make -C tests/dashboard/e2e e2e              # bridge -> real dashboard_ipc.cpp -> Verilated RTL
python3 tests/dashboard/extract_fixtures.py --check
python3 scripts/test_check_dashboard_reports.py
```

Expected output ends with, respectively:

```text
PASS: tb_debug_transport (…)            (five lines)
(no output from lint)
PASS: 145 checks, 0 failures / PASS: 34 HTTP checks, 0 failures
PASS: 60 end-to-end checks, 0 failures
fixtures up to date
OK
```

The e2e build runs in `/tmp/megacd-dashboard-e2e` (override with `BUILD_DIR=`), because
Verilator's generated makefiles cannot handle paths that contain spaces. `MAIN_DIR=` points at
the Main checkout (default `../../../../Main_MiSTer`).

## 2. FPGA: baseline, then the dashboard revision

Install Quartus Prime Lite/Standard **17.0.2** with Cyclone V device support. Newer versions
need an IP migration of `sys/pll_q17.qip` and are not equivalent for resource comparisons.
See the [MiSTer compile guide](https://mister-devel.github.io/MkDocs_MiSTer/developer/mistercompile/).

**Build host: unchanged baseline (do this first, once):**

```bash
set -euo pipefail
export PATH="/opt/intelFPGA_lite/17.0/quartus/bin:$PATH"   # adjust
quartus_sh --version
mkdir -p build/megacd-baseline reports/megacd-baseline
git -C MegaCD_MiSTer archive a3a3da81d04b22533def34f26eb9d748be9d2d0c | tar -x -C build/megacd-baseline
( cd build/megacd-baseline && quartus_sh --flow compile MegaCD -c MegaCD 2>&1 | tee baseline-build.log )
cp build/megacd-baseline/output_files/*.rpt build/megacd-baseline/output_files/*.summary \
   build/megacd-baseline/baseline-build.log reports/megacd-baseline/
sha256sum build/megacd-baseline/output_files/MegaCD.rbf > reports/megacd-baseline/rbf.sha256
```

**Build host: dashboard revision:**

```bash
set -euo pipefail
cd MegaCD_MiSTer
quartus_sh --flow compile MegaCD -c MegaCD_Dashboard 2>&1 | tee dashboard-build.log
test -s output_files/MegaCD_Dashboard.rbf
quartus_sta -t scripts/report_dashboard_timing.tcl MegaCD MegaCD_Dashboard
python3 scripts/check_dashboard_reports.py --baseline ../reports/megacd-baseline \
    --candidate output_files --revision MegaCD_Dashboard --budget docs/dashboard-resource-budget.json
sha256sum output_files/MegaCD_Dashboard.rbf
```

`MegaCD_Dashboard.qsf` sources `MegaCD.qsf` and adds only
`VERILOG_MACRO "MEGACD_DASHBOARD=1"`. If the Quartus GUI rewrites the QSF, restore that
two-line form. New HDL files belong in `files.qip`.

Also build the stock revision from the modified tree
(`quartus_sh --flow compile MegaCD -c MegaCD`) and compare it with the baseline. With the macro
off, the dashboard logic must synthesize away. Resource differences above noise mean
something leaked.

What to check (plan §9), in addition to the script's exit status:

- The fitter status is Successful, on `5CSEBA6U23I7`.
- In the RAM Summary, `dashboard_debug|stg_e` and `stg_o` are block RAM (M10K), not registers.
  In Resource Utilization by Entity, `dashboard_debug` and `dashboard_sdram_port` are small.
- `check_timing` shows no new unconstrained paths. `hps_ext`, `dashboard_debug` and the
  port-2 adapter all run on `clk_sys`, the same domain as the existing `tmpram` engine. They
  add no clock-domain crossing beyond the one `sdram.sv` already has on port 2.
- Setup and hold are met in every operating condition the Tcl script reports.

The checker was validated against synthetic reports only. On the first real run, compare its
numbers against the GUI once.

## 3. Main_MiSTer (ARM)

Install the ARM GNU Toolchain 10.2-2020.11 (`arm-none-linux-gnueabihf-`) once. The script
downloads it to `/opt`, smoke-tests it, and with `--profile` adds it to `PATH` in `~/.bashrc`.
Do not use Ubuntu's `gcc-arm-linux-gnueabihf` package: it has the wrong prefix and, on 16.04,
GCC 5.

**Build host (x86_64 Linux, e.g. Ubuntu 16.04):**

```bash
MegaCD_MiSTer/scripts/setup_arm_toolchain.sh --profile && source ~/.bashrc
```

The Main changes ship in this repository as
[`patches/main_mister/`](../patches/main_mister/README.md). Apply them to a checkout of
the pinned commit (`--clone` creates one):

```bash
MegaCD_MiSTer/patches/main_mister/apply.sh --clone Main_MiSTer
```

```bash
set -euo pipefail
git -C Main_MiSTer rev-parse HEAD            # 47221c18987e101f50caafeb3b615f53b62722ca (release 20260912)
git -C Main_MiSTer status --short            # user_io.cpp modified, support/megacd/dashboard_ipc.* added
make -C Main_MiSTer
file Main_MiSTer/bin/MiSTer                  # ELF 32-bit LSB executable, ARM, EABI5 ... hard-float
arm-none-linux-gnueabihf-readelf -d Main_MiSTer/bin/MiSTer | grep NEEDED
sha256sum Main_MiSTer/bin/MiSTer
```

The Makefile's `support/*/*.cpp` wildcard picks up `dashboard_ipc.cpp`; no Makefile change
is needed. (`MiSTer.vcxproj` is not updated; it is only used for Visual Studio browsing.)

## 4. Bridge (ARM)

**Build host:**

```bash
make -C MegaCD_MiSTer/linux/dashboard-bridge clean
make -C MegaCD_MiSTer/linux/dashboard-bridge CROSS_COMPILE=arm-none-linux-gnueabihf-
file MegaCD_MiSTer/linux/dashboard-bridge/build/megacd-dashboard   # ... ARM, statically linked
sha256sum MegaCD_MiSTer/linux/dashboard-bridge/build/megacd-dashboard
```

The cross build links statically, so it does not depend on the MiSTer image's libc version.
Run `megacd-dashboard --version` on the MiSTer to confirm it executes there.

## 5. Package

```text
megacd-dashboard-<date>/
  _Console/MegaCD_Dashboard.rbf
  MiSTer                              (matching Main)
  megacd-dashboard/megacd-dashboard   (bridge)
  megacd-dashboard/start.sh
  dashboard/dashboard.html
  docs/DASHBOARD-DEPLOYMENT.md, dashboard-protocol.md, dashboard-feasibility.md
  reports/  (fit/sta summaries, check_dashboard_reports.py output)
  SOURCES.txt   (the three git SHAs + `git diff` of each tree)
  LICENSES/     (GPL-3.0 texts of MegaCD_MiSTer and Main_MiSTer; GPGX license for dashboard.html)
  SHA256SUMS
```

Never include BIOS, disc images, or savestates made from them.
