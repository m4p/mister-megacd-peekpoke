//
// gen_clken.sv
//
// Genesis clock-enable generator (68K 7-MCLK, Z80 15-MCLK periods) with the
// dashboard pause gate. Moved out of gen.sv unchanged except for the gating
// so it can be tested on its own (tests/dashboard/tb_gen_clken.sv).
//
// Pause, the same technique as Genesis_MiSTer's debug pause (hold the clock
// enables low), made safe for a running game:
//  1. gen.sv's main bus FSM raises pause_hold only while it is idle and the
//     68K has no cycle in progress; from then on it accepts no new cycle, so
//     no CPU can be frozen between the FSM answering a read and the CPU
//     latching the (shared, changing) memory data.
//  2. each CPU is gated at its own phase-1 slot, so its phase enables keep
//     alternating (FX68K and T80 both need p/n in order).
//  3. resume ungates each CPU at a phase-1 slot; the FSM releases the bus
//     hold only after both gates are open again.
// FM, PSG, the bus arbiter, the Mega CD main-side interface (VCLK_CE) and
// cartridge timing share these enables and stop with them. The VDP keeps
// scanning out the unchanged VRAM.
//
// This source file is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published
// by the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//

module gen_clken
(
	input      MCLK,
	input      RESET_N,
	input      LOADING,

	input      PAUSE_EN,     // pause requested
	input      pause_hold,   // bus FSM idle and holding (from gen.sv)

	output reg M68K_CLKENp,
	output reg M68K_CLKENn,
	output reg Z80_CLKENp,
	output reg Z80_CLKENn,
	output reg m68k_gate,
	output reg z80_gate
);

initial begin
	m68k_gate = 0;
	z80_gate = 0;
end

always @(negedge MCLK) begin
	reg [3:0] VCLKCNT = 0;
	reg [3:0] ZCLKCNT = 0;

	if(~RESET_N | LOADING) begin
		VCLKCNT <= 0;
		ZCLKCNT = 0;
		Z80_CLKENp <= 0;
		Z80_CLKENn <= 0;
		M68K_CLKENp <= 0;
		M68K_CLKENn <= 1;
		m68k_gate <= 0;
		z80_gate <= 0;
	end
	else begin
		M68K_CLKENp <= 0;
		VCLKCNT <= VCLKCNT + 1'b1;
		if (VCLKCNT == 4'd6) begin
			VCLKCNT <= 0;
			if (pause_hold && PAUSE_EN) m68k_gate <= 1;
			else if (!PAUSE_EN) m68k_gate <= 0;
			M68K_CLKENp <= !(PAUSE_EN && (pause_hold || m68k_gate));
		end

		M68K_CLKENn <= 0;
		if (VCLKCNT == 4'd3) begin
			M68K_CLKENn <= !m68k_gate;
		end

		Z80_CLKENn <= 0;
		ZCLKCNT <= ZCLKCNT + 1'b1;
		if (ZCLKCNT == 14) begin
			ZCLKCNT <= 0;
			Z80_CLKENn <= !z80_gate;
		end

		Z80_CLKENp <= 0;
		if (ZCLKCNT == 7) begin
			if (pause_hold && PAUSE_EN) z80_gate <= 1;
			else if (!PAUSE_EN) z80_gate <= 0;
			Z80_CLKENp <= !(PAUSE_EN && (pause_hold || z80_gate));
		end
	end
end

endmodule
