// Testbench for rtl/GEN/gen_clken.sv (dashboard pause, control milestone step 1).
// The bus FSM's pause_hold is modelled exactly as in gen.sv's MBUS_IDLE state.
`timescale 1ns/1ps

// The generator exactly as it was in gen.sv before the pause was added.
module gen_clken_ref
(
	input      MCLK,
	input      RESET_N,
	input      LOADING,
	output reg M68K_CLKENp,
	output reg M68K_CLKENn,
	output reg Z80_CLKENp,
	output reg Z80_CLKENn
);
always @(negedge MCLK) begin
	reg [3:0] VCLKCNT = 0;
	reg [3:0] ZCLKCNT = 0;
	if(~RESET_N | LOADING) begin
		VCLKCNT <= 0; ZCLKCNT = 0;
		Z80_CLKENp <= 0; Z80_CLKENn <= 0; M68K_CLKENp <= 0; M68K_CLKENn <= 1;
	end
	else begin
		M68K_CLKENp <= 0;
		VCLKCNT <= VCLKCNT + 1'b1;
		if (VCLKCNT == 4'd6) begin VCLKCNT <= 0; M68K_CLKENp <= 1; end
		M68K_CLKENn <= 0;
		if (VCLKCNT == 4'd3) M68K_CLKENn <= 1;
		Z80_CLKENn <= 0;
		ZCLKCNT <= ZCLKCNT + 1'b1;
		if (ZCLKCNT == 14) begin ZCLKCNT <= 0; Z80_CLKENn <= 1; end
		Z80_CLKENp <= 0;
		if (ZCLKCNT == 7) Z80_CLKENp <= 1;
	end
end
endmodule

module tb_gen_clken;

reg MCLK = 0;
always #9.3 MCLK = ~MCLK;

reg RESET_N = 0, LOADING = 0, PAUSE_EN = 0;
reg as_n = 1, bus_idle = 1;       // 68K address strobe, main bus FSM in MBUS_IDLE
reg pause_hold = 0;

wire mp, mn, zp, zn, m68k_gate, z80_gate;
wire rmp, rmn, rzp, rzn;
wire PAUSED = pause_hold & m68k_gate & z80_gate;

gen_clken dut(.MCLK(MCLK), .RESET_N(RESET_N), .LOADING(LOADING), .PAUSE_EN(PAUSE_EN),
              .pause_hold(pause_hold), .M68K_CLKENp(mp), .M68K_CLKENn(mn), .Z80_CLKENp(zp),
              .Z80_CLKENn(zn), .m68k_gate(m68k_gate), .z80_gate(z80_gate));
gen_clken_ref ref_(.MCLK(MCLK), .RESET_N(RESET_N), .LOADING(LOADING),
                   .M68K_CLKENp(rmp), .M68K_CLKENn(rmn), .Z80_CLKENp(rzp), .Z80_CLKENn(rzn));

// gen.sv MBUS_IDLE: if (pause_hold || (PAUSE_EN && M68K_AS_N)) pause_hold <= PAUSE_EN || gates
always @(posedge MCLK) begin
	if (!RESET_N) pause_hold <= 0;
	else if (bus_idle && (pause_hold || (PAUSE_EN && as_n)))
		pause_hold <= PAUSE_EN || m68k_gate || z80_gate;
end

integer errors = 0, seed, i, cycles = 0;
integer m_last = 0, z_last = 0;           // 1 = last pulse was p, 2 = n
integer m_p = 0, m_n = 0, z_p = 0, z_n = 0, pauses = 0, frozen_cycles = 0;
reg     m_resumed = 0, z_resumed = 0;
reg     compare = 1;                      // DUT must equal the reference (no pause so far)

task fail(input [8*64-1:0] what);
	begin
		errors = errors + 1;
		if (errors < 20) $display("FAIL @%0d: %0s", cycles, what);
	end
endtask

// All checks on the posedge, after the negedge-generated enables settled.
reg m_gate_d = 0, z_gate_d = 0;
always @(posedge MCLK) if (!RESET_N) begin
	// in reset the original generator holds M68K_CLKENn high: start tracking afresh
	m_last = 0; z_last = 0; m_resumed = 0; z_resumed = 0; m_gate_d = 0; z_gate_d = 0;
end
else begin
	cycles = cycles + 1;
	// a gate that just opened means the next pulse of that CPU must be p
	if (m_gate_d && !m68k_gate) m_resumed = 1;
	if (z_gate_d && !z80_gate) z_resumed = 1;
	m_gate_d = m68k_gate;
	z_gate_d = z80_gate;
	if (compare && {mp, mn, zp, zn} !== {rmp, rmn, rzp, rzn}) fail("differs from the original generator without pause");

	if (mp && mn) fail("68K p and n in the same cycle");
	if (mp) begin
		if (m_last == 1) fail("68K p twice without n");
		if (m68k_gate) fail("68K p while gated");
		m_last = 1; m_p = m_p + 1;
		m_resumed = 0;
	end
	if (mn) begin
		if (m_last == 2) fail("68K n twice without p");
		if (m_resumed) fail("68K first pulse after resume is n");
		if (m68k_gate) fail("68K n while gated");
		m_last = 2; m_n = m_n + 1;
	end
	if (zp) begin
		if (z_last == 1) fail("Z80 p twice without n");
		if (z80_gate) fail("Z80 p while gated");
		z_last = 1; z_p = z_p + 1;
		z_resumed = 0;
	end
	if (zn) begin
		if (z_last == 2) fail("Z80 n twice without p");
		if (z_resumed) fail("Z80 first pulse after resume is n");
		if (z80_gate) fail("Z80 n while gated");
		z_last = 2; z_n = z_n + 1;
	end
	if (PAUSED) frozen_cycles = frozen_cycles + 1;
	if (pause_hold == 0 && (m68k_gate || z80_gate)) fail("a CPU is gated without the bus hold");
end

task run(input integer n);
	repeat(n) begin
		@(posedge MCLK);
		as_n <= ($urandom % 4) != 0;    // 68K bus cycles most of the time
		bus_idle <= ($urandom % 3) != 0;
	end
endtask

task wait_paused(input integer limit);
	integer k;
	begin
		k = 0;
		while (!PAUSED && k < limit) begin run(1); k = k + 1; end
		if (!PAUSED) fail("did not reach PAUSED");
	end
endtask

initial begin
	if (!$value$plusargs("seed=%d", seed)) seed = 1;
	i = $urandom(seed);

	repeat (5) @(posedge MCLK);
	RESET_N <= 1;

	// 1. no pause: identical to the original generator
	run(3000);
	if (m_p < 400 || z_p < 190) fail("enables not running");

	// 2. a normal pause
	compare = 0;
	PAUSE_EN <= 1;
	wait_paused(200);
	begin : hold_check
		integer mp0, zp0;
		mp0 = m_p; zp0 = z_p;
		run(2000);
		if (m_p != mp0 || z_p != zp0) fail("enables ran while PAUSED");
		if (!PAUSED) fail("PAUSED dropped while PAUSE_EN held");
	end
	PAUSE_EN <= 0;
	run(40);
	if (PAUSED || m68k_gate || z80_gate || pause_hold) fail("not fully resumed after 40 cycles");
	pauses = pauses + 1;

	// 3. random pauses: long, short, and cancelled before they took effect
	for (i = 0; i < 400; i = i + 1) begin
		PAUSE_EN <= 1;
		run($urandom % ((i % 3 == 0) ? 3 : 60));
		PAUSE_EN <= 0;
		run(1 + $urandom % 40);
		pauses = pauses + 1;
	end

	// 4. reset while paused clears everything
	PAUSE_EN <= 1;
	wait_paused(200);
	RESET_N <= 0;
	run(5);
	if (m68k_gate || z80_gate) fail("reset did not clear the gates");
	PAUSE_EN <= 0;
	RESET_N <= 1;
	run(200);
	if (m68k_gate || z80_gate || pause_hold) fail("state left after reset");

	$display("%0s: tb_gen_clken seed=%0d  pauses=%0d frozen_cycles=%0d  68K p/n=%0d/%0d  Z80 p/n=%0d/%0d",
	         errors ? "FAILED" : "PASS", seed, pauses, frozen_cycles, m_p, m_n, z_p, z_n);
	$finish;
end

endmodule
