# Focused dashboard RTL simulations (Icarus Verilog / Verilator).
# These do not replace integrated simulation or hardware validation.
RTL = ../../rtl

.PHONY: sim sim-vhdl lint clean

SEEDS ?= 1 2 3 4 5

sim: tb_debug_transport.vvp tb_gen_clken.vvp tb_debug_write.vvp tb_gen_vram_dash.vvp
	@rm -f sim.log
	@for s in $(SEEDS); do vvp -n tb_debug_transport.vvp +seed=$$s | tee -a sim.log | grep -E '^(PASS|FAIL)'; done
	@for s in $(SEEDS); do vvp -n tb_gen_clken.vvp +seed=$$s | tee -a sim.log | grep -E '^(PASS|FAIL)'; done
	@for s in $(SEEDS); do vvp -n tb_debug_write.vvp +seed=$$s | tee -a sim.log | grep -E '^(PASS|FAIL)'; done
	@for s in $(SEEDS); do vvp -n tb_gen_vram_dash.vvp +seed=$$s | tee -a sim.log | grep -E '^(PASS|FAIL)'; done
	@! grep -qE '^FAIL' sim.log

tb_debug_transport.vvp: tb_debug_transport.sv $(RTL)/dashboard_debug.sv $(RTL)/dashboard_sdram_port.sv $(RTL)/hps_ext.v
	iverilog -g2012 -Wall -o $@ $^

tb_debug_write.vvp: tb_debug_write.sv $(RTL)/dashboard_debug.sv $(RTL)/dashboard_sdram_port.sv $(RTL)/hps_ext.v
	iverilog -g2012 -Wall -o $@ $^

tb_gen_vram_dash.vvp: tb_gen_vram_dash.sv $(RTL)/GEN/gen_vram_dash.sv
	iverilog -g2012 -Wall -o $@ $^

tb_gen_clken.vvp: tb_gen_clken.sv $(RTL)/GEN/gen_clken.sv
	iverilog -g2012 -Wall -o $@ $^

# ASIC freeze testbench (GHDL). Built in GHDL_DIR (paths with spaces are fine here).
GHDL_DIR ?= /tmp/megacd-dashboard-ghdl
sim-vhdl:
	@mkdir -p $(GHDL_DIR)
	ghdl -a --std=08 --workdir=$(GHDL_DIR) $(RTL)/MCD/ASIC_PKG.vhd
	ghdl -a --std=08 -frelaxed --workdir=$(GHDL_DIR) $(RTL)/MCD/ASIC.vhd
	ghdl -a --std=08 --workdir=$(GHDL_DIR) tb_asic_freeze.vhd
	ghdl -e --std=08 -frelaxed --workdir=$(GHDL_DIR) -o $(GHDL_DIR)/tb_asic_freeze tb_asic_freeze
	@rm -f sim-vhdl.log
	@for s in $(SEEDS); do $(GHDL_DIR)/tb_asic_freeze -gSEED=$$s --ieee-asserts=disable 2>&1 | tee -a sim-vhdl.log | grep -E 'FAIL|DONE'; done
	@! grep -q 'FAIL' sim-vhdl.log

lint:
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-BLKSEQ -Wno-PROCASSINIT --top-module dashboard_debug $(RTL)/dashboard_debug.sv
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-PROCASSINIT -Wno-SYMRSVDWORD --top-module dashboard_sdram_port $(RTL)/dashboard_sdram_port.sv
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-PROCASSINIT -Wno-BLKSEQ -Wno-IMPLICITSTATIC --top-module gen_clken $(RTL)/GEN/gen_clken.sv
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-PROCASSINIT --top-module gen_vram_dash $(RTL)/GEN/gen_vram_dash.sv

clean:
	rm -f *.vvp sim.log sim-vhdl.log
