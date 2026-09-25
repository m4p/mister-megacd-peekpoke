// Protocol testbench for hps_ext + dashboard_debug + dashboard_sdram_port.
// Run: make -C tests/dashboard sim
`timescale 1ns/1ps

module tb_debug_transport;

reg clk = 0;
always #9.3 clk = ~clk;   // ~53.7 MHz

reg reset = 1;

// ---------------------------------------------------------------- HPS bus
reg  [15:0] hps_din = 0;
reg         hps_strobe = 0;
reg         hps_enable = 0;
wire [35:0] EXT_BUS;
assign EXT_BUS[31:16] = hps_din;
assign EXT_BUS[33]    = hps_strobe;
assign EXT_BUS[34]    = hps_enable;
assign EXT_BUS[35]    = 1'b0;

wire [48:0] cd_out;
wire        ext_enable, ext_strobe;
wire [15:0] ext_din, dash_dout;
wire        dash_claim;

hps_ext hps_ext
(
	.clk_sys(clk),
	.EXT_BUS(EXT_BUS),
	.cd_in(49'h0),
	.cd_out(cd_out),
	.cdda_ready(1'b1),
	.cd_data_ready(1'b1),
	.ext_enable(ext_enable),
	.ext_strobe(ext_strobe),
	.ext_din(ext_din),
	.dash_dout(dash_dout),
	.dash_claim(dash_claim)
);

// ---------------------------------------------------------------- DUT
reg         vblank = 0;
reg         rom_download = 0;
wire [11:0] joy;
wire        mem_req, mem_we, mem_ack, mem_err;
wire  [1:0] mem_be;
wire  [7:0] mem_space;
wire [15:1] mem_addr;
wire [15:0] mem_wdata, mem_rdata;

dashboard_debug #(.BUILD_ID(32'h01260925), .LEASE_BITS(12)) dut
(
	.clk(clk), .reset(reset),
	.io_enable(ext_enable), .io_strobe(ext_strobe), .io_din(ext_din),
	.io_dout(dash_dout), .io_claim(dash_claim),
	.vblank(vblank),
	.joy_inject(joy),
	.mem_req(mem_req), .mem_we(mem_we), .mem_be(mem_be), .mem_space(mem_space),
	.mem_addr(mem_addr), .mem_wdata(mem_wdata), .mem_rdata(mem_rdata),
	.mem_ack(mem_ack), .mem_err(mem_err), .mem_block(rom_download),
	.pause_req(), .frozen(1'b0)
);

wire [24:1] sdr_addr;
wire [15:0] sdr_din;
wire        sdr_rd_raw, sdr_wrl, sdr_wrh;
wire        sdr_rd = sdr_rd_raw & ~rom_download;   // as gated in MegaCD.sv
reg         sdr_busy = 0;
reg  [15:0] sdr_dout = 0;
wire        port_busy;

dashboard_sdram_port port
(
	.clk(clk), .reset(reset),
	.req(mem_req), .we(mem_we), .be(mem_be), .space(mem_space), .addr(mem_addr),
	.wdata(mem_wdata), .rdata(mem_rdata), .ack(mem_ack), .err(mem_err),
	.port_free(~rom_download), .abort(rom_download), .busy(port_busy), .owns(),
	.sdr_addr(sdr_addr), .sdr_din(sdr_din),
	.sdr_rd(sdr_rd_raw), .sdr_wrl(sdr_wrl), .sdr_wrh(sdr_wrh),
	.sdr_busy(sdr_busy), .sdr_dout(sdr_dout)
);

// ------------------------------------------------ SDRAM port-2 model
// Mirrors rtl/sdram.sv: requests are rising-edge detected, busy2 rises on
// acceptance (after arbitrary port-1 contention) and falls in the same
// cycle dout is loaded. dout is shared and gets clobbered afterwards.
reg [15:0] wram[0:32767];
integer    reads = 0, writes = 0;
reg        old_rd = 0, old_wr = 0;
integer    wait_cnt = 0, busy_cnt = 0;
reg        pending = 0, pend_we = 0;
reg [24:1] pend_addr;

always @(posedge clk) begin
	old_rd <= old_rd & sdr_rd;
	old_wr <= old_wr & (sdr_wrl | sdr_wrh);
	if(!pending && !sdr_busy && ((~old_rd && sdr_rd) || (~old_wr && (sdr_wrl | sdr_wrh)))) begin
		pending   <= 1;
		pend_we   <= sdr_wrl | sdr_wrh;
		pend_addr <= sdr_addr;
		wait_cnt  <= $urandom % 7;          // port 1 (CPU) priority
		old_rd    <= sdr_rd;
		old_wr    <= sdr_wrl | sdr_wrh;
	end
	else if(pending && !sdr_busy) begin
		if(wait_cnt) wait_cnt <= wait_cnt - 1;
		else begin sdr_busy <= 1; busy_cnt <= 5; end
	end
	else if(sdr_busy) begin
		if(busy_cnt > 1) busy_cnt <= busy_cnt - 1;
		else begin
			sdr_busy <= 0;
			pending  <= 0;
			if(pend_addr[24:16] != 9'b010000000) begin
				$display("FAIL: access outside work RAM: %h", pend_addr);
				$finish;
			end
			if(pend_we) writes <= writes + 1;
			else begin
				reads    <= reads + 1;
				sdr_dout <= wram[pend_addr[15:1]];
			end
		end
	end
	else if($urandom % 3 == 0) sdr_dout <= $urandom;   // shared dout clobbered
end

// ------------------------------------------------ HPS packet driver
reg [15:0] tx[0:63];
reg [15:0] rx[0:63];
integer    errors = 0;

task spi_packet(input integer n);
	integer i;
	begin
		@(posedge clk); hps_enable <= 1;
		repeat(3) @(posedge clk);
		for(i = 0; i < n; i = i + 1) begin
			hps_din <= tx[i];
			@(posedge clk); hps_strobe <= 1;
			@(posedge clk); hps_strobe <= 0;
			repeat(2 + ($urandom % 3)) @(posedge clk);
			rx[i] = EXT_BUS[15:0];
		end
		hps_enable <= 0;
		repeat(3) @(posedge clk);
	end
endtask

reg [7:0] gen = 0;

task check(input [15:0] got, input [15:0] exp, input [8*48-1:0] what);
	if(got !== exp) begin
		$display("FAIL: %0s: got %h expected %h", what, got, exp);
		errors = errors + 1;
	end
endtask

task cmd(input [7:0] sc, input integer nargs, input [15:0] a2, a3, a4, a5, a6);
	begin
		tx[0] = 16'h0070; tx[1] = {sc, gen};
		tx[2] = a2; tx[3] = a3; tx[4] = a4; tx[5] = a5; tx[6] = a6;
		spi_packet(2 + nargs);
	end
endtask

task status_wait_done;
	integer guard;
	begin
		guard = 0;
		do begin
			cmd(8'h02, 5, 0, 0, 0, 0, 0);
			guard = guard + 1;
		end while(!rx[1][1] && guard < 5000);
		if(!rx[1][1]) begin $display("FAIL: op never completed"); errors = errors + 1; end
	end
endtask

reg [7:0] got_bytes[0:2047];
integer   txn = 1;

task begin_op(input [7:0] op, input [7:0] sp, input [15:0] addr, input [15:0] len, output [15:0] code);
	begin
		txn = txn + 1;
		cmd(8'h03, 5, txn, {op, sp}, 0, addr, len);
		code = rx[6];
	end
endtask

task fetch_all(input integer len);
	integer w, words, off, cnt, i;
	begin
		words = (len + 1) / 2;
		off = 0;
		while(off < words) begin
			cnt = (words - off > 32) ? 32 : words - off;
			tx[0] = 16'h0070; tx[1] = {8'h06, gen}; tx[2] = txn; tx[3] = off; tx[4] = cnt;
			for(i = 0; i < cnt; i = i + 1) tx[5 + i] = 0;
			spi_packet(5 + cnt);
			check(rx[4], 0, "FETCH accept");
			for(i = 0; i < cnt; i = i + 1) begin
				got_bytes[(off + i) * 2]     = rx[5 + i][15:8];
				got_bytes[(off + i) * 2 + 1] = rx[5 + i][7:0];
			end
			off = off + cnt;
		end
	end
endtask

task read_check(input [15:0] addr, input integer len);
	reg [15:0] code;
	integer i;
	reg [7:0] exp;
	begin
		begin_op(8'h01, 8'h01, addr, len, code);
		check(code, 0, "BEGIN read");
		cmd(8'h05, 1, txn, 0, 0, 0, 0);
		check(rx[2], 0, "COMMIT read");
		status_wait_done;
		cmd(8'h02, 5, 0, 0, 0, 0, 0);
		check(rx[3], 0, "read result");
		check(rx[4], len, "read length");
		fetch_all(len);
		for(i = 0; i < len; i = i + 1) begin
			exp = ((addr + i) & 1) ? wram[(addr + i) >> 1][7:0] : wram[(addr + i) >> 1][15:8];
			if(got_bytes[i] !== exp) begin
				$display("FAIL: read %h+%0d byte %0d got %h exp %h", addr, len, i, got_bytes[i], exp);
				errors = errors + 1;
			end
		end
		cmd(8'h07, 1, txn, 0, 0, 0, 0);
		check(rx[2], 0, "ACK");
	end
endtask

task frame;
	begin
		vblank <= 1; repeat(4) @(posedge clk);
		vblank <= 0; repeat(4) @(posedge clk);
	end
endtask

integer i, r0, seed;
reg [15:0] code;

initial begin
	if(!$value$plusargs("seed=%d", seed)) seed = 1;
	r0 = $urandom(seed);
	$display("seed=%0d", seed);
	for(i = 0; i < 32768; i = i + 1) wram[i] = $urandom;
	wram[16'h6FEA >> 1] = 16'h3A00;

	repeat(10) @(posedge clk);
	reset <= 0;
	repeat(10) @(posedge clk);

	// --- stock commands unaffected / unknown commands unclaimed
	tx[0] = 16'h0034; tx[1] = 0; tx[2] = 0;
	hps_enable <= 1; @(posedge clk);
	hps_din <= 16'h0034; @(posedge clk); hps_strobe <= 1; @(posedge clk); hps_strobe <= 0;
	repeat(3) @(posedge clk);
	check(EXT_BUS[32], 1, "CD_GET claimed by hps_ext");
	check(dash_claim, 0, "CD_GET not claimed by dashboard");
	hps_enable <= 0; repeat(3) @(posedge clk);

	tx[0] = 16'h0071; spi_packet(2);
	check(EXT_BUS[32], 0, "unknown cmd unclaimed (after)");
	hps_enable <= 1; @(posedge clk);
	hps_din <= 16'h0071; @(posedge clk); hps_strobe <= 1; @(posedge clk); hps_strobe <= 0;
	repeat(3) @(posedge clk);
	check(EXT_BUS[32], 0, "unknown cmd 0x71 unclaimed");
	hps_enable <= 0; repeat(3) @(posedge clk);

	// --- PROBE
	for(i = 2; i < 11; i = i + 1) tx[i] = 0;
	tx[0] = 16'h0070; tx[1] = {8'h01, 8'h00};
	spi_packet(11);
	check(rx[0], 16'hDB26, "magic ack");
	check(rx[2], 16'h4442, "probe DB");
	check(rx[3], 16'h4D43, "probe MC");
	check(rx[4], 1, "proto version");
	check(rx[5], 16'h0083, "features");
	check(rx[6], 2048, "staging bytes");
	check(rx[7], 32, "max xfer words");
	check(rx[8], 16'h0925, "build lo");
	check(rx[9], 16'h0126, "build hi");

	// --- session enforcement
	gen = 8'h05;
	cmd(8'h02, 5, 0, 0, 0, 0, 0);
	check(rx[2], 1, "STATUS before SESSION -> ERR_SESSION");
	cmd(8'h09, 2, 16'h0002, 0, 0, 0, 0);
	check(joy, 0, "rejected INPUT has no effect");
	gen = 8'h00;
	tx[0] = 16'h0070; tx[1] = {8'h0F, 8'h00}; tx[2] = 16'h0005; spi_packet(3);
	check(rx[2], 5, "SESSION echo");
	gen = 8'h05;
	cmd(8'h02, 5, 0, 0, 0, 0, 0);
	check(rx[1][15:8], 5, "flags gen");
	check(rx[1][6], 1, "owner lease alive");

	// --- reads: sizes the dashboard uses, even/odd starts, boundaries
	read_check(16'h6FEA, 2);
	check({got_bytes[0], got_bytes[1]}, 16'h3A00, "speed word");
	read_check(16'h6FEA, 1);
	read_check(16'h6FEB, 1);
	read_check(16'h70E4, 4);
	read_check(16'h70EA, 10);
	read_check(16'h7B6C, 56);
	read_check(16'h7ABC, 64);
	read_check(16'h7AA9, 7);
	read_check(16'h0000, 2);
	read_check(16'hFFFE, 2);
	read_check(16'hFFFF, 1);
	read_check(16'h1233, 2048);

	// --- range / unsupported
	begin_op(8'h01, 8'h01, 16'hFFFF, 2, code); check(code, 4, "read past end");
	begin_op(8'h01, 8'h01, 16'h1000, 0, code); check(code, 4, "zero length");
	begin_op(8'h01, 8'h01, 16'h1000, 2049, code); check(code, 4, "too long");
	txn = txn + 1; cmd(8'h03, 5, txn, 16'h0101, 1, 0, 2); check(rx[6], 4, "addr hi");
	begin_op(8'h02, 8'h01, 16'h6FF6, 2, code); check(code, 5, "workram write unsupported");
	begin_op(8'h01, 8'h02, 16'h2940, 32, code); check(code, 5, "vram read unsupported");
	begin_op(8'h01, 8'h03, 16'h0000, 2, code); check(code, 5, "unknown space");
	cmd(8'h03, 5, 0, 16'h0101, 0, 0, 2); check(rx[6], 3, "txn id 0 rejected");

	// --- loopback 1056 bytes with a truncated upload in the middle
	begin_op(8'h7F, 8'h00, 0, 1056, code); check(code, 0, "BEGIN loopback");
	// truncated: header announces 32 words, packet carries 10
	tx[0] = 16'h0070; tx[1] = {8'h04, gen}; tx[2] = txn; tx[3] = 0; tx[4] = 32;
	for(i = 0; i < 10; i = i + 1) tx[5 + i] = 16'hA000 + i;
	spi_packet(15);
	check(rx[4], 0, "UPLOAD header accepted");
	cmd(8'h05, 1, txn, 0, 0, 0, 0); check(rx[2], 6, "COMMIT incomplete");
	tx[0] = 16'h0070; tx[1] = {8'h04, gen}; tx[2] = txn; tx[3] = 32; tx[4] = 1; tx[5] = 0;
	spi_packet(6); check(rx[4], 4, "UPLOAD wrong offset");
	for(r0 = 10; r0 < 528; r0 = r0 + 32) begin
		tx[0] = 16'h0070; tx[1] = {8'h04, gen}; tx[2] = txn; tx[3] = r0;
		tx[4] = (528 - r0 > 32) ? 32 : 528 - r0;
		for(i = 0; i < tx[4]; i = i + 1) tx[5 + i] = 16'hA000 + r0 + i;
		spi_packet(5 + tx[4]);
		check(rx[4], 0, "UPLOAD chunk");
	end
	tx[0] = 16'h0070; tx[1] = {8'h04, gen}; tx[2] = txn; tx[3] = 528; tx[4] = 1; tx[5] = 0;
	spi_packet(6); check(rx[4], 4, "UPLOAD past length");
	r0 = reads;
	cmd(8'h05, 1, txn, 0, 0, 0, 0); check(rx[2], 0, "COMMIT loopback");
	cmd(8'h05, 1, txn, 0, 0, 0, 0); check(rx[2], 8, "duplicate COMMIT");
	status_wait_done;
	begin_op(8'h01, 8'h01, 0, 2, code); check(code, 2, "BEGIN while result held");
	txn = txn - 1;
	fetch_all(1056);
	for(i = 0; i < 528; i = i + 1)
		check({got_bytes[2 * i], got_bytes[2 * i + 1]}, 16'hA000 + i, "loopback data");
	tx[0] = 16'h0070; tx[1] = {8'h06, gen}; tx[2] = txn; tx[3] = 520; tx[4] = 9;
	spi_packet(14); check(rx[4], 4, "FETCH past result");
	tx[2] = txn + 7; tx[3] = 0; tx[4] = 1; spi_packet(6); check(rx[4], 3, "FETCH wrong txn");
	cmd(8'h08, 1, txn, 0, 0, 0, 0); check(rx[2], 0, "CANCEL releases result");
	cmd(8'h07, 1, txn, 0, 0, 0, 0); check(rx[2], 7, "ACK after release");
	check(reads, r0, "loopback touched no memory");

	// --- input: frame-boundary application, same-frame tap, release no-op
	cmd(8'h09, 2, 16'h0002, 0, 0, 0, 0); check(rx[3], 16'h0002, "press left target");
	check(joy, 0, "not applied before frame boundary");
	frame; check(joy, 12'h002, "left applied at vblank");
	frame; check(joy, 12'h002, "hold persists");
	cmd(8'h09, 2, 16'h0080, 0, 0, 0, 0);
	cmd(8'h09, 2, 0, 16'h0080, 0, 0, 0);
	frame; check(joy, 12'h082, "same-frame START tap held one frame");
	frame; check(joy, 12'h002, "START tap released next frame");
	cmd(8'h09, 2, 0, 16'h0083, 0, 0, 0); check(rx[3], 0, "release all");
	cmd(8'h09, 2, 0, 16'h0083, 0, 0, 0); check(rx[3], 0, "release idempotent");
	frame; check(joy, 0, "released");

	// --- pause unsupported in this build
	cmd(8'h0B, 1, 0, 0, 0, 0, 0); check(rx[2], 5, "PAUSE unsupported");
	cmd(8'h0C, 1, 0, 0, 0, 0, 0); check(rx[2], 5, "RESUME unsupported");

	// --- SESSION 0 (bridge detached) releases input
	cmd(8'h09, 2, 16'h0001, 0, 0, 0, 0); frame; check(joy, 12'h001, "right held");
	tx[0] = 16'h0070; tx[1] = {8'h0F, 8'h00}; tx[2] = 0; spi_packet(3);
	check(joy, 0, "SESSION 0 releases input");
	gen = 0;
	cmd(8'h02, 5, 0, 0, 0, 0, 0); check(rx[1][6], 0, "no owner after SESSION 0");

	// --- lease expiry releases input
	tx[0] = 16'h0070; tx[1] = {8'h0F, 8'h00}; tx[2] = 16'h0006; spi_packet(3); gen = 6;
	cmd(8'h09, 2, 16'h0004, 0, 0, 0, 0); frame; check(joy, 12'h004, "down held");
	repeat(5000) @(posedge clk);
	check(joy, 0, "lease expiry releases input");
	cmd(8'h02, 5, 0, 0, 0, 0, 0); check(rx[1][6], 0, "FLAGS shows expired lease");
	cmd(8'h02, 5, 0, 0, 0, 0, 0); check(rx[1][6], 1, "lease renewed by packet");

	// --- ROM download aborts a read cleanly
	begin_op(8'h01, 8'h01, 16'h0000, 2048, code); check(code, 0, "BEGIN long read");
	cmd(8'h05, 1, txn, 0, 0, 0, 0);
	repeat(200) @(posedge clk);
	rom_download <= 1;
	repeat(200) @(posedge clk);
	status_wait_done;
	cmd(8'h02, 5, 0, 0, 0, 0, 0); check(rx[3], 9, "read aborted by ROM download");
	check(rx[4], 0, "aborted result has no data");
	cmd(8'h07, 1, txn, 0, 0, 0, 0);
	rom_download <= 0;
	read_check(16'h6FEA, 2);

	// --- SESSION while an op runs: result discarded, no stale DONE
	begin_op(8'h01, 8'h01, 16'h0000, 512, code);
	cmd(8'h05, 1, txn, 0, 0, 0, 0);
	tx[0] = 16'h0070; tx[1] = {8'h0F, 8'h00}; tx[2] = 16'h0007; spi_packet(3); gen = 7;
	repeat(8000) @(posedge clk);
	cmd(8'h02, 5, 0, 0, 0, 0, 0); check(rx[1][2:0], 0, "discarded op leaves IDLE");
	read_check(16'h6FEA, 2);

	check(writes, 0, "no SDRAM writes in this build");

	if(errors == 0) $display("PASS: tb_debug_transport (%0d SDRAM reads)", reads);
	else $display("FAILED: %0d errors", errors);
	$finish;
end

initial begin
	#200_000_000;
	$display("FAIL: timeout");
	$finish;
end

endmodule
