# Main_MiSTer patch: dashboard IPC adapter

`0001-megacd-dashboard-ipc.patch` adds the Main side of the dashboard protocol
([docs/dashboard-protocol.md](../../docs/dashboard-protocol.md) §9):

- `support/megacd/dashboard_ipc.{h,cpp}`: non-blocking Unix socket
  (`/tmp/megacd-dashboard.sock`) that forwards bridge packets to HPS command `0x70`,
  reports core identity and epoch, and sends `SESSION 0` when the bridge disconnects.
- `user_io.cpp`: calls `dashboard_ipc_poll()` every poll iteration and
  `dashboard_ipc_core_changed()` whenever the core is re-identified.

It was made against Main_MiSTer `aa271e41ebbf616903f9e0216b0900aead5bfce1`. The Makefile's
`support/*/*.cpp` wildcard picks up the new file, so no Makefile change is needed.

```bash
MegaCD_MiSTer/patches/main_mister/apply.sh --clone Main_MiSTer
```

```bash
make -C Main_MiSTer
```

Without `--clone`, the script patches an existing checkout (default `../Main_MiSTer` next to
this repository). Rerunning it is harmless.
