// Testbench for rtl/GEN/gen_vram_dash.sv (dashboard VRAM access, control step 4).
`timescale 1ns/1ps

// dpram port A as used by gen.sv: registered q, write on the clock edge
module vram_bank(input clk, input [13:0] a, input [7:0] d, input we, output reg [7:0] q);
	reg [7:0] mem[0:16383];
	integer i;
	initial for (i = 0; i < 16384; i = i + 1) mem[i] = 0;
	always @(posedge clk) begin
		if (we) mem[a] <= d;
		q <= mem[a];
	end
endmodule

module tb_gen_vram_dash;

reg clk = 0;
always #9.3 clk = ~clk;

// VDP side
reg  [15:1] vram_a = 0;
reg  [15:0] vram_d = 0;
reg         vram_we_u = 0, vram_we_l = 0, vram_req = 0;
wire        vram_ack;
// dashboard side
reg         dash_req = 0, dash_we = 0;
reg   [1:0] dash_be = 0;
reg  [15:1] dash_a = 0;
reg  [15:0] dash_d = 0;
wire [15:0] dash_q;
wire        dash_ack;

wire [13:0] ram_addr;
wire [15:0] ram_d;
wire        wl1, wu1, wl2, wu2;
wire  [7:0] ql1, qu1, ql2, qu2;

gen_vram_dash dut(.clk(clk), .vram_a(vram_a), .vram_d(vram_d), .vram_we_u(vram_we_u), .vram_we_l(vram_we_l),
	.vram_req(vram_req), .vram_ack(vram_ack), .ram_addr(ram_addr), .ram_d(ram_d),
	.wren_l1(wl1), .wren_u1(wu1), .wren_l2(wl2), .wren_u2(wu2),
	.ram_q1({qu1, ql1}), .ram_q2({qu2, ql2}),
	.dash_req(dash_req), .dash_we(dash_we), .dash_be(dash_be), .dash_a(dash_a), .dash_d(dash_d),
	.dash_q(dash_q), .dash_ack(dash_ack));

vram_bank l1(clk, ram_addr, ram_d[7:0],  wl1, ql1);
vram_bank u1(clk, ram_addr, ram_d[15:8], wu1, qu1);
vram_bank l2(clk, ram_addr, ram_d[7:0],  wl2, ql2);
vram_bank u2(clk, ram_addr, ram_d[15:8], wu2, qu2);

// the original gen.sv logic, for the "no dashboard traffic" equivalence phase
reg  ref_ack = 0;
always @(posedge clk) ref_ack <= vram_req;
wire ref_cyc = ref_ack ^ vram_req;
wire [13:0] ref_addr = vram_a[15:2];
wire ref_wl1 = vram_we_l & ref_cyc & ~vram_a[1], ref_wu1 = vram_we_u & ref_cyc & ~vram_a[1];
wire ref_wl2 = vram_we_l & ref_cyc &  vram_a[1], ref_wu2 = vram_we_u & ref_cyc &  vram_a[1];

integer errors = 0, seed, i, vdp_ops = 0, dash_ops = 0, dash_stall = 0;
reg compare = 1;
reg [7:0] shadow[0:65535];   // canonical byte address -> value

task fail(input [8*72-1:0] what);
	begin
		errors = errors + 1;
		if (errors < 20) $display("FAIL @%0t: %0s", $time, what);
	end
endtask

always @(posedge clk) if (compare) begin
	if (vram_ack !== ref_ack || ram_addr !== ref_addr || {wl1, wu1, wl2, wu2} !== {ref_wl1, ref_wu1, ref_wl2, ref_wu2})
		fail("differs from the original logic without dashboard traffic");
end

function [7:0] sb(input [15:0] a); sb = shadow[a]; endfunction

// VDP model: toggle req, wait until ack == req, then sample q (the original timing)
task vdp_access(input we, input [15:0] byte_a, input [1:0] be, input [15:0] data, output [15:0] q);
	begin
		@(posedge clk);
		vram_a <= byte_a[15:1];
		vram_d <= data;
		vram_we_u <= we & be[1];
		vram_we_l <= we & be[0];
		vram_req <= ~vram_req;
		@(posedge clk);
		while (vram_ack !== vram_req) @(posedge clk);
		q = byte_a[1] ? {qu2, ql2} : {qu1, ql1};
		vram_we_u <= 0; vram_we_l <= 0;
	end
endtask

task dash_access(input we, input [15:0] byte_a, input [1:0] be, input [15:0] data, output [15:0] q);
	integer k;
	begin
		@(posedge clk);
		dash_a <= byte_a[15:1]; dash_d <= data; dash_we <= we; dash_be <= be; dash_req <= 1;
		k = 0;
		@(posedge clk);
		while (!dash_ack) begin @(posedge clk); k = k + 1; end
		if (k > dash_stall) dash_stall = k;
		q = dash_q;
		dash_req <= 0;
	end
endtask

// VDP works on $0000-$7FFF, the dashboard on $8000-$FFFF
task automatic vdp_thread(input integer n);
	integer j;
	reg [15:0] a, d, q;
	reg [1:0] be;
	begin
		for (j = 0; j < n; j = j + 1) begin
			a = ($urandom % 16384) * 2;
			if ($urandom % 2) begin
				d = $urandom; be = 1 + $urandom % 3;
				vdp_access(1, a, be, d, q);
				if (be[1]) shadow[a] = d[15:8];
				if (be[0]) shadow[a + 1] = d[7:0];
			end
			else begin
				vdp_access(0, a, 2'b11, 0, q);
				if (q !== {sb(a), sb(a + 1)}) fail("VDP read wrong data");
			end
			vdp_ops = vdp_ops + 1;
			repeat ($urandom % 3) @(posedge clk);
		end
	end
endtask

task automatic dash_thread(input integer n);
	integer j;
	reg [15:0] a, d, q;
	reg [1:0] be;
	begin
		for (j = 0; j < n; j = j + 1) begin
			a = 16'h8000 + ($urandom % 16384) * 2;
			if ($urandom % 2) begin
				d = $urandom; be = 1 + $urandom % 3;
				dash_access(1, a, be, d, q);
				if (be[1]) shadow[a] = d[15:8];
				if (be[0]) shadow[a + 1] = d[7:0];
			end
			else begin
				dash_access(0, a, 2'b11, 0, q);
				if (q !== {sb(a), sb(a + 1)}) fail("dashboard read wrong data");
			end
			dash_ops = dash_ops + 1;
			repeat ($urandom % 4) @(posedge clk);
		end
	end
endtask

initial begin
	if (!$value$plusargs("seed=%d", seed)) seed = 1;
	i = $urandom(seed);
	for (i = 0; i < 65536; i = i + 1) shadow[i] = 0;
	repeat (5) @(posedge clk);

	// 1. VDP alone: identical to the original port-A logic
	vdp_thread(2000);
	compare = 0;

	// 2. both at once, interleaving at random
	fork
		vdp_thread(6000);
		dash_thread(6000);
	join

	// 3. the air-freshener sized upload: 528 words by the dashboard, then read back
	for (i = 0; i < 528; i = i + 1) begin : up
		reg [15:0] q;
		dash_access(1, 16'h26E0 + 2 * i, 2'b11, 16'hA000 + i, q);
		shadow[16'h26E0 + 2 * i] = 8'hA0 + (i >> 8); shadow[16'h26E0 + 2 * i + 1] = i[7:0];
	end
	for (i = 0; i < 528; i = i + 1) begin : rb
		reg [15:0] q;
		dash_access(0, 16'h26E0 + 2 * i, 2'b11, 0, q);
		if (q !== 16'hA000 + i) fail("upload readback");
	end

	// 4. final contents of every byte equal the shadow
	repeat (5) @(posedge clk);
	for (i = 0; i < 65536; i = i + 1) begin : fin
		reg [7:0] v;
		v = i[1] ? (i[0] ? l2.mem[i >> 2] : u2.mem[i >> 2]) : (i[0] ? l1.mem[i >> 2] : u1.mem[i >> 2]);
		if (v !== shadow[i]) begin fail("final VRAM contents differ from shadow"); i = 65536; end
	end

	$display("%0s: tb_gen_vram_dash seed=%0d vdp_ops=%0d dash_ops=%0d max_dash_wait=%0d cycles",
	         errors ? "FAILED" : "PASS", seed, vdp_ops, dash_ops, dash_stall);
	$finish;
end

endmodule
