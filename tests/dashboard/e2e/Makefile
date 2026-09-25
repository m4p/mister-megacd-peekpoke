# End-to-end: bridge -> real Main dashboard_ipc.cpp -> Verilated RTL.
#   make e2e [MAIN_DIR=../../../../Main_MiSTer] [BUILD_DIR=/tmp/megacd-dashboard-e2e]
#
# Verilator's generated makefiles cannot build in paths containing spaces, so
# the build runs in BUILD_DIR through a symlink ("src") back to this directory.
MAIN_DIR  ?= ../../../../Main_MiSTer
BUILD_DIR ?= /tmp/megacd-dashboard-e2e
BRIDGE     = ../../../linux/dashboard-bridge

VFLAGS = --cc --exe --build -j 0 -O2 --top-module sim_top \
	-Wno-fatal -Wno-WIDTH -Wno-PINMISSING -Wno-UNOPTFLAT -Wno-MULTIDRIVEN -Wno-IMPLICITSTATIC \
	-CFLAGS "-O2 -DDASH_IPC_HOST_TEST -I../src -I../src/main_src"
SRCS = src/sim_top.sv src/rtl/dashboard_debug.sv src/rtl/dashboard_sdram_port.sv src/rtl/hps_ext.v \
	src/main_sim.cpp src/main_src/dashboard_ipc.cpp
DEPS = sim_top.sv main_sim.cpp dashboard_ipc_host.h ../../../rtl/dashboard_debug.sv \
	../../../rtl/dashboard_sdram_port.sv ../../../rtl/hps_ext.v $(MAIN_DIR)/support/megacd/dashboard_ipc.cpp

.PHONY: e2e build clean links

build: $(BUILD_DIR)/obj_dir/Vsim_top $(BUILD_DIR)/obj_dir_stock/Vsim_top $(BRIDGE)/build/megacd-dashboard

links:
	ln -sfn "$(MAIN_DIR)/support/megacd" main_src
	ln -sfn ../../../rtl rtl
	mkdir -p "$(BUILD_DIR)"
	ln -sfn "$(CURDIR)" "$(BUILD_DIR)/src"

$(BUILD_DIR)/obj_dir/Vsim_top: $(DEPS) | links
	cd "$(BUILD_DIR)" && verilator $(VFLAGS) --Mdir obj_dir $(SRCS) >/dev/null

$(BUILD_DIR)/obj_dir_stock/Vsim_top: $(DEPS) | links
	cd "$(BUILD_DIR)" && verilator $(VFLAGS) -GSTOCK=1 --Mdir obj_dir_stock $(SRCS) >/dev/null

$(BRIDGE)/build/megacd-dashboard: FORCE
	$(MAKE) -C $(BRIDGE)
FORCE:

e2e: build
	python3 e2e_test.py --sim "$(BUILD_DIR)/obj_dir/Vsim_top" --stock-sim "$(BUILD_DIR)/obj_dir_stock/Vsim_top" \
		--bridge $(BRIDGE)/build/megacd-dashboard

clean:
	rm -rf "$(BUILD_DIR)" main_src rtl
