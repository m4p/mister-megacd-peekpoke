# Main_MiSTer patch: dashboard IPC adapter

`0001-megacd-dashboard-ipc.patch` adds the Main side of the dashboard protocol
([docs/dashboard-protocol.md](../../docs/dashboard-protocol.md) §9):

- `support/megacd/dashboard_ipc.{h,cpp}`: non-blocking Unix socket
  (`/tmp/megacd-dashboard.sock`) that forwards bridge packets to HPS command `0x70`,
  reports core identity and epoch, and sends `SESSION 0` when the bridge disconnects.
- `user_io.cpp`: calls `dashboard_ipc_poll()` every poll iteration and
  `dashboard_ipc_core_changed()` whenever the core is re-identified.
- `support/megacd/megacd.cpp`: the CD barrier. `mcd_poll()` returns early while the dashboard
  holds the console frozen (`dashboard_ipc_frozen()`), so the CD drive neither advances nor
  sends sectors or CD audio. The drive counts poll ticks, so CD time simply stops.

It is made against Main_MiSTer **release 20260912** (`47221c18987e101f50caafeb3b615f53b62722ca`),
so a patched Main differs from the official release only by the dashboard adapter. The Makefile's
`support/*/*.cpp` wildcard picks up the new file, so no Makefile change is needed.

```bash
MegaCD_MiSTer/patches/main_mister/apply.sh --clone Main_MiSTer
```

```bash
make -C Main_MiSTer
```

Without `--clone`, the script patches an existing checkout (default `../Main_MiSTer` next to
this repository). Rerunning it is harmless.
