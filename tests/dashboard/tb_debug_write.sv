// Testbench for dashboard writes and coherent reads (control milestone step 2).
// A behavioural "68K" executes through work RAM, freezes the way gen.sv does,
// keeps rewriting a 32-bit game variable while it runs, and checks that it never
// observes a partially written patch.
// Run: make -C tests/dashboard sim
`timescale 1ns/1ps

module tb_debug_write;

reg clk = 0;
always #9.3 clk = ~clk;
reg reset = 1;

// ---------------------------------------------------------------- HPS bus
reg  [15:0] hps_din = 0;
reg         hps_strobe = 0, hps_enable = 0;
wire [35:0] EXT_BUS;
assign EXT_BUS[31:16] = hps_din;
assign EXT_BUS[33] = hps_strobe;
assign EXT_BUS[34] = hps_enable;
assign EXT_BUS[35] = 1'b0;
wire [48:0] cd_out;
wire        ext_enable, ext_strobe, dash_claim;
wire [15:0] ext_din, dash_dout;

hps_ext hps_ext(.clk_sys(clk), .EXT_BUS(EXT_BUS), .cd_in(49'h0), .cd_out(cd_out),
                .cdda_ready(1'b1), .cd_data_ready(1'b1), .ext_enable(ext_enable),
                .ext_strobe(ext_strobe), .ext_din(ext_din), .dash_dout(dash_dout), .dash_claim(dash_claim));

// ---------------------------------------------------------------- DUT
wire        pause_req;
reg         frozen = 0;
reg  [23:1] prog_addr = 23'h7F8000;     // $FF0000
wire [11:0] joy;
wire        mem_req, mem_we, mem_ack, mem_err;
wire  [1:0] mem_be;
wire  [7:0] mem_space;
wire [15:1] mem_addr;
wire [15:0] mem_wdata, mem_rdata;

dashboard_debug #(.BUILD_ID(32'h01260925), .LEASE_BITS(16), .FEAT_FREEZE(1), .FEAT_WORKRAM_WRITE(1),
                  .FEAT_READ_COHERENT(1), .STEP_CYCLES(64), .MAX_STEPS(40), .FREEZE_TIMEOUT(20000)) dut
(
	.clk(clk), .reset(reset),
	.io_enable(ext_enable), .io_strobe(ext_strobe), .io_din(ext_din), .io_dout(dash_dout), .io_claim(dash_claim),
	.vblank(1'b0), .joy_inject(joy),
	.mem_req(mem_req), .mem_we(mem_we), .mem_be(mem_be), .mem_space(mem_space), .mem_addr(mem_addr),
	.mem_wdata(mem_wdata), .mem_rdata(mem_rdata), .mem_ack(mem_ack), .mem_err(mem_err), .mem_block(1'b0),
	.pause_req(pause_req), .frozen(frozen), .prog_addr(prog_addr)
);

wire [24:1] sdr_addr;
wire [15:0] sdr_din;
wire        sdr_rd, sdr_wrl, sdr_wrh;
reg         sdr_busy = 0;
reg  [15:0] sdr_dout = 0;
wire        port_busy;

dashboard_sdram_port port(.clk(clk), .reset(reset), .req(mem_req), .we(mem_we), .be(mem_be), .space(mem_space),
	.addr(mem_addr), .wdata(mem_wdata), .rdata(mem_rdata), .ack(mem_ack), .err(mem_err),
	.port_free(1'b1), .abort(1'b0), .busy(port_busy), .owns(),
	.sdr_addr(sdr_addr), .sdr_din(sdr_din), .sdr_rd(sdr_rd), .sdr_wrl(sdr_wrl), .sdr_wrh(sdr_wrh),
	.sdr_busy(sdr_busy), .sdr_dout(sdr_dout));

// ---------------------------------------------------------------- work RAM
// 64 KiB, big-endian words; written by the SDRAM port model and by the CPU model.
reg [15:0] wram[0:32767];
integer    errors = 0, dash_writes = 0;

task fail(input [8*72-1:0] what);
	begin
		errors = errors + 1;
		if (errors < 20) $display("FAIL @%0t: %0s", $time, what);
	end
endtask

// SDRAM port 2 model (edge accept, delayed busy, shared dout clobbered)
reg        old_rd = 0, old_wr = 0, pending = 0, pend_we = 0;
reg  [1:0] pend_be;
reg [24:1] pend_addr;
reg [15:0] pend_din;
integer    wait_cnt = 0, busy_cnt = 0;
reg [15:0] patch_lo, patch_hi;          // current write target (byte offsets) for the window check

always @(posedge clk) begin
	old_rd <= old_rd & sdr_rd;
	old_wr <= old_wr & (sdr_wrl | sdr_wrh);
	if (!pending && !sdr_busy && ((~old_rd && sdr_rd) || (~old_wr && (sdr_wrl | sdr_wrh)))) begin
		pending <= 1; pend_we <= sdr_wrl | sdr_wrh; pend_be <= {sdr_wrh, sdr_wrl};
		pend_addr <= sdr_addr; pend_din <= sdr_din;
		wait_cnt <= $urandom % 5; old_rd <= sdr_rd; old_wr <= sdr_wrl | sdr_wrh;
	end
	else if (pending && !sdr_busy) begin
		if (wait_cnt) wait_cnt <= wait_cnt - 1;
		else begin sdr_busy <= 1; busy_cnt <= 5; end
	end
	else if (sdr_busy) begin
		if (busy_cnt > 1) busy_cnt <= busy_cnt - 1;
		else begin
			sdr_busy <= 0;
			pending <= 0;
			if (pend_addr[24:16] != 9'b010000000) fail("access outside work RAM");
			if (pend_we) begin
				dash_writes = dash_writes + 1;
				if (!frozen) fail("dashboard write while the CPU runs");
				if (pend_be[1]) wram[pend_addr[15:1]][15:8] = pend_din[15:8];
				if (pend_be[0]) wram[pend_addr[15:1]][7:0]  = pend_din[7:0];
			end
			else sdr_dout <= wram[pend_addr[15:1]];
		end
	end
	else if ($urandom % 3 == 0) sdr_dout <= $urandom;
end

// ---------------------------------------------------------------- 68K model
// gen.sv-like freeze: follows pause_req after 3..20 cycles (bus idle + phase slot).
integer frz_wait = 0;
always @(posedge clk) begin
	if (pause_req != frozen) begin
		if (frz_wait == 0) frz_wait <= 3 + $urandom % 18;
		else if (frz_wait == 1) begin frozen <= pause_req; frz_wait <= 0; end
		else frz_wait <= frz_wait - 1;
	end
	else frz_wait <= 0;
end

// While running: the PC walks a code loop [code_lo, code_hi) (or sits in a tight
// loop at stuck_pc), the 32-bit variable at $FF6FDC is rewritten with a counter
// in both words, and the CPU "executes" the 8 bytes at the patch site whenever
// its PC passes them, checking they are entirely old or entirely new.
reg  [15:0] pc = 16'h7000;
reg  [15:0] code_lo = 16'h7000, code_hi = 16'h7100;
reg         stuck = 0;
reg  [15:0] stuck_pc = 16'h7040;
reg  [15:0] counter = 0;
reg  [63:0] patch_old, patch_new;
reg  [15:0] patch_at = 16'h7040;
reg         check_patch = 0;
integer     step_div = 0;

function [63:0] read8(input [15:0] a);
	read8 = {wram[a[15:1]], wram[a[15:1] + 1], wram[a[15:1] + 2], wram[a[15:1] + 3]};
endfunction

always @(posedge clk) if (!frozen && !reset) begin
	// variable update: both halves carry the same counter (a torn read shows a mismatch)
	counter <= counter + 1;
	wram[16'h6FDC >> 1] <= counter;
	wram[(16'h6FDC >> 1) + 1] <= counter;

	step_div = step_div + 1;
	if (step_div == 4) begin
		step_div = 0;
		pc = stuck ? stuck_pc : ((pc + 2 >= code_hi) ? code_lo : pc + 2);
		prog_addr <= {8'hFF, pc[15:1]};
		if (check_patch && pc == patch_at) begin
			if (read8(patch_at) !== patch_old && read8(patch_at) !== patch_new)
				fail("CPU executed a partially written patch");
		end
	end
end

// window invariant: every dashboard work-RAM write lands while the 68K's last
// fetch is at least PATCH_WINDOW (16) bytes away from the whole write range
always @(posedge clk) if (sdr_wrl | sdr_wrh) begin
	if (&prog_addr[23:21] && ({prog_addr[15:1], 1'b0} + 16 >= patch_lo) &&
	    ({prog_addr[15:1], 1'b0} < patch_hi + 16))
		fail("write issued while the 68K executes near the target");
end

// ---------------------------------------------------------------- HPS packets
reg [15:0] tx[0:63];
reg [15:0] rx[0:63];

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

reg [7:0]  gen = 8'd3;
integer    txn = 1;

task cmd(input [7:0] sc, input integer nargs, input [15:0] a2, a3, a4, a5, a6);
	begin
		tx[0] = 16'h0070; tx[1] = {sc, gen};
		tx[2] = a2; tx[3] = a3; tx[4] = a4; tx[5] = a5; tx[6] = a6;
		spi_packet(2 + nargs);
	end
endtask

task check(input [15:0] got, input [15:0] exp, input [8*48-1:0] what);
	if (got !== exp) begin
		errors = errors + 1;
		$display("FAIL: %0s: got %h expected %h", what, got, exp);
	end
endtask

task wait_done;
	integer guard;
	begin
		guard = 0;
		do begin cmd(8'h02, 5, 0, 0, 0, 0, 0); guard = guard + 1; end
		while (!rx[1][1] && guard < 20000);
		if (!rx[1][1]) begin errors = errors + 1; $display("FAIL: op never completed"); end
	end
endtask

// write len bytes of data64 (big-endian, left-aligned) at work-RAM offset a
task dash_write(input [15:0] a, input integer len, input [63:0] data, output [15:0] result);
	integer i;
	begin
		txn = txn + 1;
		patch_lo = a; patch_hi = a + len;
		cmd(8'h03, 5, txn, 16'h0201, 0, a, len);
		check(rx[6], 0, "BEGIN write");
		tx[0] = 16'h0070; tx[1] = {8'h04, gen}; tx[2] = txn; tx[3] = 0; tx[4] = (len + 1) / 2;
		for (i = 0; i < (len + 1) / 2; i = i + 1) tx[5 + i] = data[63 - 16 * i -: 16];
		spi_packet(5 + (len + 1) / 2);
		check(rx[4], 0, "UPLOAD");
		cmd(8'h05, 1, txn, 0, 0, 0, 0);
		check(rx[2], 0, "COMMIT write");
		wait_done;
		cmd(8'h02, 5, 0, 0, 0, 0, 0);
		result = rx[3];
		cmd(8'h07, 1, txn, 0, 0, 0, 0);
	end
endtask

task dash_read4(input [15:0] a, output [31:0] v);
	begin
		txn = txn + 1;
		cmd(8'h03, 5, txn, 16'h0101, 0, a, 4);
		check(rx[6], 0, "BEGIN read");
		cmd(8'h05, 1, txn, 0, 0, 0, 0);
		wait_done;
		tx[0] = 16'h0070; tx[1] = {8'h06, gen}; tx[2] = txn; tx[3] = 0; tx[4] = 2; tx[5] = 0; tx[6] = 0;
		spi_packet(7);
		check(rx[4], 0, "FETCH");
		v = {rx[5], rx[6]};
		cmd(8'h07, 1, txn, 0, 0, 0, 0);
	end
endtask

integer i, seed, torn = 0, busy_results = 0;
reg [15:0] res;
reg [31:0] v;
reg [63:0] want;

initial begin
	if (!$value$plusargs("seed=%d", seed)) seed = 1;
	i = $urandom(seed);
	for (i = 0; i < 32768; i = i + 1) wram[i] = $urandom;

	repeat (10) @(posedge clk);
	reset <= 0;
	repeat (10) @(posedge clk);
	tx[0] = 16'h0070; tx[1] = {8'h0F, 8'h00}; tx[2] = gen; spi_packet(3);

	// PROBE: write + coherent-read + freeze features advertised
	for (i = 2; i < 11; i = i + 1) tx[i] = 0;
	tx[0] = 16'h0070; tx[1] = {8'h01, gen}; spi_packet(11);
	check(rx[5], 16'h01A7, "features WORKRAM_WRITE|FREEZE|READ_COHERENT");

	// 1. patches while the CPU loops through the patch site
	check_patch = 1;
	for (i = 0; i < 60; i = i + 1) begin
		patch_old = read8(patch_at);
		want = {$urandom, $urandom};
		patch_new = want;
		dash_write(patch_at, 8, want, res);
		check(res, 0, "patch result");
		if (read8(patch_at) !== want) begin errors = errors + 1; $display("FAIL: patch %0d not in RAM", i); end
		repeat ($urandom % 400) @(posedge clk);
	end

	// 2. odd address, odd length, 1 and 2 byte data writes elsewhere
	check_patch = 0;
	dash_write(16'h6FF6, 2, 64'h0000_0000_0000_0000, res); check(res, 0, "2-byte write");
	check(wram[16'h6FF6 >> 1], 16'h0000, "2-byte data");
	dash_write(16'h7105, 3, 64'hA1B2C3_0000000000, res); check(res, 0, "odd write");
	check({wram[16'h7104 >> 1][7:0], wram[16'h7106 >> 1]}, 24'hA1B2C3, "odd write data");

	// 3. coherent 32-bit reads of a variable the CPU rewrites continuously
	for (i = 0; i < 300; i = i + 1) begin
		dash_read4(16'h6FDC, v);
		if (v[31:16] !== v[15:0]) torn = torn + 1;
	end
	if (torn) begin errors = errors + 1; $display("FAIL: %0d torn 32-bit reads", torn); end

	// 4. CPU stuck in a loop on the patch site: BUSY, nothing written
	stuck = 1; stuck_pc = 16'h7040;
	repeat (50) @(posedge clk);
	patch_old = read8(patch_at);
	dash_write(patch_at, 8, 64'hDEADBEEF_DEADBEEF, res);
	check(res, 2, "stuck CPU -> BUSY");
	if (read8(patch_at) !== patch_old) begin errors = errors + 1; $display("FAIL: BUSY write changed RAM"); end
	stuck = 0;

	// 5. write during a user pause: works, and the machine stays paused
	cmd(8'h0B, 1, 0, 0, 0, 0, 0); check(rx[2], 0, "PAUSE");
	repeat (100) @(posedge clk);
	if (!frozen) begin errors = errors + 1; $display("FAIL: not frozen after PAUSE"); end
	want = 64'h1122334455667788;
	dash_write(16'h7A08, 2, want, res); check(res, 0, "write while paused");
	check(wram[16'h7A08 >> 1], 16'h1122, "paused write data");
	repeat (100) @(posedge clk);
	if (!frozen) begin errors = errors + 1; $display("FAIL: pause lost after a write"); end
	cmd(8'h0C, 1, 0, 0, 0, 0, 0); check(rx[2], 0, "RESUME");
	repeat (100) @(posedge clk);
	if (frozen) begin errors = errors + 1; $display("FAIL: still frozen after RESUME"); end

	$display("%0s: tb_debug_write seed=%0d  dashboard SDRAM writes=%0d",
	         errors ? "FAILED" : "PASS", seed, dash_writes);
	$finish;
end

initial begin #400_000_000; $display("FAIL: timeout"); $finish; end

endmodule
