# Focused dashboard RTL simulations (Icarus Verilog / Verilator).
# These do not replace integrated simulation or hardware validation.
RTL = ../../rtl

.PHONY: sim lint clean

SEEDS ?= 1 2 3 4 5

sim: tb_debug_transport.vvp tb_gen_clken.vvp
	@rm -f sim.log
	@for s in $(SEEDS); do vvp -n tb_debug_transport.vvp +seed=$$s | tee -a sim.log | grep -E '^(PASS|FAIL)'; done
	@for s in $(SEEDS); do vvp -n tb_gen_clken.vvp +seed=$$s | tee -a sim.log | grep -E '^(PASS|FAIL)'; done
	@! grep -qE '^FAIL' sim.log

tb_debug_transport.vvp: tb_debug_transport.sv $(RTL)/dashboard_debug.sv $(RTL)/dashboard_sdram_port.sv $(RTL)/hps_ext.v
	iverilog -g2012 -Wall -o $@ $^

tb_gen_clken.vvp: tb_gen_clken.sv $(RTL)/GEN/gen_clken.sv
	iverilog -g2012 -Wall -o $@ $^

lint:
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-UNUSEDSIGNAL -Wno-BLKSEQ -Wno-PROCASSINIT --top-module dashboard_debug $(RTL)/dashboard_debug.sv
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-PROCASSINIT -Wno-SYMRSVDWORD --top-module dashboard_sdram_port $(RTL)/dashboard_sdram_port.sv
	verilator --lint-only -Wall -Wno-DECLFILENAME -Wno-PROCASSINIT -Wno-BLKSEQ -Wno-IMPLICITSTATIC --top-module gen_clken $(RTL)/GEN/gen_clken.sv

clean:
	rm -f *.vvp sim.log
